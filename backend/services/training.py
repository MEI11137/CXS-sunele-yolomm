"""训练运行：单机锁、受控子进程、指标与父子对比（WB17-05）。

三条边界先说清楚：

1. **训练是服务端受控子进程，不是模型自己起命令。**
   命令是参数列表（不拼 shell），Windows 下隐藏窗口，工作目录固定在项目内的
   ``training/<run_id>``，日志与权重都落在那里；不自动安装/升级 torch，
   正常训练默认不联网：基础权重必须已经在项目里，训练从项目内权重或 vendor 结构 yaml 开始。
   只有显式设置 HX17_TRAINING_AMP=1 时，vendor 的 check_amp() 首次会在线下载 5.35MB 的
   yolo11n.pt 基准权重做 CUDA AMP 校验（落在本次 run_dir；下载/校验失败则自动关闭 AMP，
   不会假装通过）。
2. **先抢锁，再建“训练中”的记录。**
   没有锁就不会出现一条看似在跑的训练；启动失败要把锁放回去并标失败，
   否则用户会看到“训练中”却永远等不到结果。
3. **取消要能证明两件事**：PID 是我们启动的那个进程（``create_time`` 对得上），
   并且它**真的结束了**。终止失败时保留锁、状态不变，返回失败/未知，
   绝不假装取消成功。
4. **准备阶段也能取消，但不能含糊**：``launch_state`` 把“准备中（preparing，可取消）”
   与“已认领启动（launching，进程即将或已经起来）”分开。前者取消立即生效且**一个进程
   都不起**；后者只让调用方稍后重试（``CANCEL_STARTING``），绝不提前宣称取消成功、
   也不放锁——否则下一秒就可能被一个真实进程打脸。

重启只核对真实进程与产物，绝不重复启动：模型侧可能有费用或覆盖风险，
“自动重跑”不是本阶段允许的默认行为。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from backend import db, security
from backend.errors import ApiError
from backend.services import datasets as dataset_service
from backend.services import jobs as job_service
from backend.services import models as model_service
from backend.services import storage

TRAINING = "training"
LOCK_ID = "machine"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
TERMINAL = {SUCCEEDED, FAILED, CANCELLED, INTERRUPTED}

# 参数范围：越界直接拒绝，不做“猜一个合理值”。
PARAM_RULES: dict[str, tuple[int | float, int | float]] = {
    "epochs": (1, 500),
    "imgsz": (64, 2048),
    "batch": (1, 64),
    "lr0": (0.00001, 0.5),
    "seed": (0, 2**31 - 1),
    "workers": (0, 8),
    "patience": (0, 300),
}
DEFAULT_PARAMS: dict[str, Any] = {
    "epochs": 10,
    "imgsz": 640,
    "batch": 4,
    "lr0": 0.01,
    "seed": 0,
    "workers": 0,
    "patience": 20,
    "device": "cpu",
}

# 启动交接的两段状态（存在 training_runs.launch_state，不是 status）：
# * PREPARING：准备/校验中，任何时刻都可以取消——取消方原子地把行改成终态；
# * LAUNCHING：已认领“启动权”，进程即将或已经起来。此时取消**不能**提前宣称
#   成功，也不能放锁；只能让调用方稍后重试（那时 PID 已落库，走真正的终止路径）。
PREPARING = "preparing"
LAUNCHING = "launching"

CANCEL_WAIT_SECONDS = 8.0
KILL_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.2

_log = logging.getLogger(__name__)
_worker_command: list[str] | None = None
_watchers: dict[str, threading.Thread] = {}
_watchers_lock = threading.Lock()
# 子进程句柄：必须有人 wait()，否则解释器退出时会把“还在跑”的警告甩给用户。
_processes: dict[str, subprocess.Popen] = {}


# --------------------------------------------------------------------------- 进程身份


# 进程状态三态：alive（确认是我们的进程且在运行）/ gone（确认已退出或 PID 被复用）/
# unknown（**查不出来**：查询失败、缺依赖、身份证据缺失）。unknown 绝不当作“已退出”。
ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"


def _psutil():
    try:
        import psutil  # noqa: PLC0415 - 可选依赖，延迟导入
    except ImportError:  # pragma: no cover - 基础依赖没装时也要给出明确报错
        return None
    return psutil


def process_started_at(pid: int | None) -> float | None:
    """进程创建时间，作为“这个 PID 还是当初那个进程”的身份证据。

    返回 ``None`` 只说明**拿不到身份证据**（进程可能已退出，也可能是查询失败）：
    调用方必须结合存在性检查区分 gone / unknown，不能把 None 当“已退出”。
    """
    if not pid:
        return None
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except psutil.NoSuchProcess:
        return None
    except Exception:
        return None


def _probe(pid: int | None, started_at: float | None) -> tuple[str, str]:
    """判定子进程状态：``(状态, 证据)``。

    * 先查**存在性**（不是身份）：不存在 → ``gone``，这是确定结论；
    * 再取身份（``process_started_at``，可被测试注入为查询失败）：取不到 → ``unknown``；
    * 身份对不上启动时刻 → PID 被复用，**我们的**进程已结束 → ``gone``；
    * 进程在、但没有记录启动时刻（历史数据）→ ``unknown``：不敢终止也不敢宣布结束。
    """
    if not pid:
        return GONE, "NO_PID"
    psutil = _psutil()
    if psutil is None:
        return UNKNOWN, "NO_PSUTIL"
    try:
        exists = psutil.pid_exists(int(pid))
    except Exception:
        return UNKNOWN, "PID_CHECK_FAILED"
    if not exists:
        return GONE, "PROCESS_NOT_FOUND"
    actual = process_started_at(pid)
    if actual is None:
        return UNKNOWN, "IDENTITY_UNAVAILABLE"
    if started_at is None:
        return UNKNOWN, "NO_PID_STARTED_AT"
    if abs(actual - float(started_at)) > 1.0:
        return GONE, "PID_REUSED"
    return ALIVE, "IDENTIFIED"


def _alive(pid: int | None, started_at: float | None) -> bool:
    """保守判定：只有**确认 gone** 才算不在；unknown 一律当作还在（宁可不放锁）。"""
    return _probe(pid, started_at)[0] != GONE


def _terminate_tree(pid: int, started_at: float | None) -> tuple[bool, str]:
    """终止进程及其子进程；返回 ``(是否仍存活, 原因)``。

    只有身份**确认**（有启动时刻且对得上）才动手；身份证据缺失时直接返回
    ``(True, IDENTITY_UNAVAILABLE)``——不能对不认识的进程下手。

    判定只认**明确证据**：``NoSuchProcess`` 才算退出。``AccessDenied``、进程对象
    读不出来、子进程枚举失败、等待失败，全都说明“不知道”，一律按**仍存活**返回
    （与 v1.6 ``_process_alive`` 的保守原则一致：AccessDenied 视为存活）。
    宁可让调用方报“终止未确认、训练槽继续占用”，也不能把活着的 worker 说成结束。

    **根进程退出不等于整棵树结束**：训练 worker 通常会再拉起子进程（数据加载等）。
    已经枚举到的 children 在根进程消失后仍要等/kill 并核实；只有“根没了且当时
    一个子进程都没枚举到”才算确实没有已知进程。
    """
    psutil = _psutil()
    if psutil is None:
        return True, "NO_PSUTIL"
    state, reason = _probe(pid, started_at)
    if state == UNKNOWN:
        return True, reason
    if state == GONE:
        return False, f"ALREADY_GONE({reason})"
    try:
        root = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        # 探测刚说还活着，取对象时就没了：它可能刚好退出，也可能把子进程留给了别人。
        # 这时我们连枚举的机会都没有，不能据此宣布“整棵树结束”。
        return True, "PROCESS_VANISHED"
    except Exception:  # AccessDenied 等：查不到不等于已退出
        return True, "PROCESS_UNREADABLE"
    try:
        victims = root.children(recursive=True)
    except psutil.NoSuchProcess:
        # 根进程在枚举过程中消失：子进程可能已被重新挂载，我们不知道是谁 → 不知道就不算结束。
        return True, "CHILDREN_UNKNOWN"
    except Exception:
        # 枚举不了子进程：不知道还有没有人在跑，不能按空列表宣称“整棵树已结束”。
        # 一次信号都不发，交给调用方报“未确认”。
        return True, "CHILDREN_UNKNOWN"
    for proc in victims:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 - 单个子进程失败留给下面的 wait/kill 复核
            continue
    root_gone = False
    try:
        root.terminate()
    except psutil.NoSuchProcess:
        # **根进程退出 ≠ 整棵树结束**：已经枚举到的 children 还要逐个核实。
        root_gone = True
    except Exception:  # noqa: BLE001 - 根进程没接受 terminate，再试 kill；都不行就是不知道
        try:
            root.kill()
        except psutil.NoSuchProcess:
            root_gone = True
        except Exception:
            return True, "TERMINATE_DENIED"
    targets = list(victims) if root_gone else [root, *victims]
    if not targets:
        # 根已经退出，且当时没枚举到任何子进程：这才是“确实没有已知进程”。
        return False, "ALREADY_GONE"
    try:
        _, alive = psutil.wait_procs(targets, timeout=CANCEL_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - 等待本身失败：没有“已退出”的证据
        return True, "WAIT_FAILED"
    if not alive:
        return False, "TERMINATED"
    for proc in alive:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            continue
    try:
        _, still = psutil.wait_procs(alive, timeout=KILL_WAIT_SECONDS)
    except Exception:  # noqa: BLE001
        return True, "KILL_WAIT_FAILED"
    if still:
        # 根进程没了但子进程还活着（或杀不掉）：照样报“未结束”，让调用方保留训练槽。
        return True, "KILL_TIMEOUT"
    return False, "KILLED"


# --------------------------------------------------------------------------- 参数与计划


# 设备探测钩子：生产默认查真实 torch.cuda；测试注入用来验证 GPU 分支的
# 参数传递与配置落盘，不在本机启动真实 GPU 训练。
_device_probe: Callable[[str], dict] | None = None


def set_device_probe(probe: Callable[[str], dict] | None) -> None:
    """**仅供测试注入**：替换设备可用性探测。"""
    global _device_probe
    _device_probe = probe


def _default_device_probe(device: str) -> dict:
    if device == "cpu":
        return {"ok": True, "detail": "cpu"}
    try:
        import torch  # noqa: PLC0415 - 与推理同一份依赖，懒导入

        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else 0
        # 指定了序号的设备（cuda:N）必须**真实存在**：CUDA 可用 ≠ 任何序号都可用。
        # 单卡机上 cuda:15 只是"能跑 CUDA"，不代表第 15 号卡存在——按索引越界拒绝，
        # 不等启动后才失败（root 反例：默认探针对 cuda:15 返回 ok=True）。
        match = re.fullmatch(r"cuda[:_](\d+)", str(device).strip().lower())
        if match:
            index = int(match.group(1))
            return {
                "ok": available and index < count,
                "detail": f"cuda_available={available}, device_count={count}, requested=cuda:{index}",
                "count": count,
            }
        return {
            "ok": available,
            "detail": f"cuda_available={available}, device_count={count}",
            "count": count,
        }
    except Exception as exc:  # noqa: BLE001 - 缺 torch 也要给出明确报错
        return {"ok": False, "detail": f"torch import 失败（{type(exc).__name__}）"}


def _check_device(device: str) -> None:
    """GPU 是产品能力，不是“本机能跑才行”：环境不支持时明确报错，而不是硬编码拒绝。

    指定序号（``cuda:N``）的设备还必须是**真实存在**的那一块（探测会比对
    ``N < device_count``），不存在就在计划阶段拒绝，不等启动后失败。
    """
    if device == "cpu":
        return
    probe = _device_probe or _default_device_probe
    outcome = probe(device)
    if not outcome.get("ok"):
        raise ApiError(
            409,
            "TRAINING_DEVICE_UNAVAILABLE",
            "这台机器当前用不了这个训练设备（" + str(outcome.get("detail") or "探测失败") + "）。"
            "GPU 训练需要部署机有真实可用的 CUDA 设备；指定的序号不存在或本机探测不支持时，"
            "不会假装能跑",
            details={"device": device, "detail": outcome.get("detail")},
        )


def device_capabilities() -> dict:
    """训练设备的**真实能力**：CPU 恒可用；GPU 看当前部署机探测结果。

    用与 ``_check_device`` 相同的探针（测试可注入），UI 据此展示可选项与
    不可用原因——GPU 是产品能力，不因为某台开发机没有 CUDA 就从界面消失。
    """
    probe = _device_probe or _default_device_probe
    cuda = probe("cuda")
    try:
        count = max(0, int(cuda.get("count") or 0))
    except (TypeError, ValueError):
        count = 0
    return {
        "cpu": True,
        "cuda_available": bool(cuda.get("ok")),
        "cuda_count": count if cuda.get("ok") else 0,
        "detail": str(cuda.get("detail") or ""),
    }


def _normalize_device(raw: Any) -> str:
    """``cpu / gpu / cuda / gpu:N / cuda:N`` → 规范设备名（worker 原样传给训练引擎）。"""
    text = str(raw or "").strip().lower()
    if not text:
        return str(DEFAULT_PARAMS["device"])
    if text == "cpu":
        return "cpu"
    if text in {"gpu", "cuda"}:
        return "cuda"
    match = re.fullmatch(r"(?:gpu|cuda)[:_](\d+)", text)
    if match:
        index = int(match.group(1))
        if index > 15:
            raise ApiError(400, "BAD_PARAM", f"设备序号太大：{text}")
        return f"cuda:{index}"
    raise ApiError(400, "BAD_PARAM", f"device 只能是 cpu / gpu / cuda 或 cuda:N，收到：{text}")


def validate_params(params: dict | None) -> dict:
    """把参数收敛成受控取值；越界/未知项直接报错，不静默忽略。"""
    raw = dict(params or {})
    unknown = sorted(set(raw) - set(PARAM_RULES) - {"device", "single_ir"})
    if unknown:
        raise ApiError(400, "BAD_PARAM", f"不支持的训练参数：{', '.join(unknown)}")
    clean: dict[str, Any] = {}
    for key, (low, high) in PARAM_RULES.items():
        value = raw.get(key, DEFAULT_PARAMS[key])
        if isinstance(low, int):
            if isinstance(value, bool) or not isinstance(value, int):
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    raise ApiError(400, "BAD_PARAM", f"{key} 必须是整数") from None
            if not low <= value <= high:
                raise ApiError(400, "BAD_PARAM", f"{key} 必须在 {low}-{high} 之间")
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ApiError(400, "BAD_PARAM", f"{key} 必须是数字") from None
            if not low <= value <= high:
                raise ApiError(400, "BAD_PARAM", f"{key} 必须在 {low}-{high} 之间")
        if key == "imgsz" and value % 32 != 0:
            raise ApiError(400, "BAD_PARAM", "imgsz 必须是 32 的倍数")
        clean[key] = value
    clean["device"] = _normalize_device(raw.get("device"))
    _check_device(clean["device"])
    single_ir = raw.get("single_ir", False)
    if isinstance(single_ir, str):
        single_ir = single_ir.strip().lower() in {"1", "true", "yes", "on"}
    clean["single_ir"] = bool(single_ir)
    return clean


def _dataset_for_training(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    dataset = dataset_service.get_owned(conn, user_id, project_id, dataset_id)
    root = storage.project_root(conn, user_id, project_id)
    data_yaml = storage.resolve_rel(root, str(dataset["root_rel"] or ""), must_exist=False)
    data_file = data_yaml / "data.yaml"
    if not data_file.is_file():
        raise ApiError(409, "DATASET_NOT_TRAINABLE", "这个数据集没有 data.yaml，无法训练")
    if int(dataset["sample_count"] or 0) <= 0:
        raise ApiError(409, "DATASET_EMPTY", "数据集里没有样本，无法训练")
    return {"dataset": dataset, "data_file": data_file, "root": root}


# --------------------------------------------------------------------------- 模型分派与运行时配置


FAMILIES = ("yolo", "yolomm")


def _dataset_reference(dataset) -> str:
    """数据集的标注参考模态：统一委托 datasets.reference_modality（显式列优先）。"""
    from backend.services import datasets as dataset_service

    return dataset_service.reference_modality(dataset)


def _dataset_secondary(dataset) -> str | None:
    """数据集声明的第二模态（不含参考模态本身）。"""
    data = dict(dataset)
    secondary = str(data.get("secondary_modality") or "").strip().lower() or None
    if secondary:
        return secondary
    try:
        loaded = json.loads(data.get("modalities") or "[]")
    except (ValueError, TypeError):
        loaded = []
    modes = [str(item).lower() for item in loaded] if isinstance(loaded, list) else []
    reference = _dataset_reference(dataset)
    extra = [item for item in modes if item != reference]
    return extra[0] if extra else None


def _family_for(
    conn,
    user_id: int,
    project_id: str,
    dataset,
    parent_model_id: str | None,
    *,
    single_ir: bool = False,
) -> tuple[str, str | None]:
    """决定这次训练用 YOLO 还是 YOLOMM，并核对参考模态与父模型兼容。

    * 数据集以 IR 为参考、RGB 为辅助图时，训练器只有两条合法路径：
      默认拒绝（明确提示），或用户显式选择 ``single_ir`` 派生单 IR 训练
      （images_rgb/ 与 labels_ir/ 作为辅助资料保留、不参与训练）；
    * 父模型的参考模态/第二模态必须与数据集一致，不能静默换标注坐标系；
    * 无父模型时按数据集真实模态分派（RGB 主 → YOLOMM，单 IR → YOLO）。
    """
    reference = _dataset_reference(dataset)
    secondary = _dataset_secondary(dataset)
    if secondary and reference != "rgb":
        if not (single_ir and reference == "ir"):
            raise ApiError(
                409,
                "DATASET_REFERENCE_IR_UNSUPPORTED",
                "这个数据集以 IR 为标注参考、RGB 是辅助图；当前训练器只支持 RGB 主模态的双模态训练。"
                "请选择“仅 IR 单模态”训练（images_rgb/ 与 labels_ir/ 会作为辅助资料保留、不参与训练），"
                "或另建 RGB 参考的双模态数据集后再训练。",
                details={"reference_modality": reference, "secondary_modality": secondary},
            )
    if single_ir:
        if reference != "ir":
            raise ApiError(
                409,
                "SINGLE_IR_REQUIRES_IR_DATASET",
                "“仅 IR 单模态”只适用于 IR 参考模态的数据集",
            )
        if parent_model_id:
            parent = model_service.get_owned(conn, int(user_id), str(project_id), parent_model_id)
            parent_family = str(parent["family"] or "yolo").lower()
            if parent_family != "yolo":
                raise ApiError(
                    409,
                    "PARENT_MODALITY_MISMATCH",
                    "父模型是双模态（YOLOMM）权重，不能用来做单 IR 派生训练；请改用“从零开始”或单模态父模型。",
                )
            parent_reference = _parent_reference(conn, user_id, project_id, parent)
            if parent_reference != "ir":
                raise ApiError(
                    409,
                    "PARENT_MODALITY_MISMATCH",
                    "父模型的训练数据不是 IR 参考模态，不能静默用于单 IR 训练；请改用“从零开始”。",
                )
        return "yolo", None
    if parent_model_id:
        parent = model_service.get_owned(conn, int(user_id), str(project_id), parent_model_id)
        family = str(parent["family"] or "yolo").lower()
        if family not in FAMILIES:
            raise ApiError(409, "PARENT_FAMILY_UNKNOWN", f"父模型的架构 {family} 不支持训练分派")
        parent_reference = _parent_reference(conn, user_id, project_id, parent)
        if parent_reference != reference:
            raise ApiError(
                409,
                "PARENT_MODALITY_MISMATCH",
                f"父模型的参考模态是 {parent_reference}，数据集是 {reference}；继续训练会把标注坐标系悄悄换掉。"
                "请改用与数据集参考模态一致的父模型，或从零开始训练。",
            )
        if family == "yolomm":
            if not secondary:
                raise ApiError(
                    409,
                    "PARENT_DATASET_MODALITY_MISMATCH",
                    "父模型是双模态（YOLOMM），但所选数据集没有第二模态；请选双模态数据集或单模态父模型。",
                )
            parent_secondary = str(parent["secondary_modality"] or "").strip().lower() or None
            if parent_secondary and parent_secondary != secondary:
                raise ApiError(
                    409,
                    "PARENT_SECONDARY_MISMATCH",
                    f"父模型的第二模态是 {parent_secondary}，数据集的是 {secondary}，不能静默替换。",
                )
            return family, secondary
        if secondary:
            raise ApiError(
                409,
                "PARENT_DATASET_MODALITY_MISMATCH",
                "父模型是单模态，但数据集带第二模态；继续训练会静默丢掉 X 标注。"
                "请改选双模态父模型、改用单模态数据集，或从零开始训练。",
            )
        return family, None
    if secondary:
        return "yolomm", secondary
    return "yolo", None


def _parent_reference(conn, user_id: int, project_id: str, parent) -> str:
    """父模型训练数据的参考模态；查不到时保守按 rgb 处理。"""
    parent_dataset_id = str(parent["dataset_id"] or "")
    if not parent_dataset_id:
        return "rgb"
    try:
        parent_dataset = dataset_service.get_owned(conn, int(user_id), str(project_id), parent_dataset_id)
    except ApiError:
        return "rgb"
    return _dataset_reference(parent_dataset)


def _source_yaml(data_file: Path) -> dict:
    try:
        parsed = yaml.safe_load(data_file.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise ApiError(409, "DATASET_NOT_TRAINABLE", f"data.yaml 无法解析（{type(exc).__name__}）") from None
    if not isinstance(parsed, dict):
        raise ApiError(409, "DATASET_NOT_TRAINABLE", "data.yaml 必须是键值结构")
    return parsed


def _modality_folders(source: dict) -> dict[str, str]:
    """模态 → 真实目录名（来自数据集自己的 data.yaml，不猜）。"""
    declared = source.get("modalities")
    if isinstance(declared, dict) and declared:
        return {str(key): str(value) for key, value in declared.items()}
    return {"rgb": "images"}


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def _has_images(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    for item in folder.iterdir():
        if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES:
            return True
    return False


def generate_runtime_config(
    run_dir: Path,
    data_file: Path,
    *,
    family: str,
    secondary: str | None,
    primary: str | None = None,
    single_ir: bool = False,
) -> Path:
    """在训练工作区生成**运行时** data.yaml：绝对 path 指向本数据集真实目录。

    背景：数据集自带的 data.yaml 可能写 ``path: raw`` 这类相对值，vendor 的
    ``data/utils.py`` 会把它相对**全局 DATASETS_DIR** 解析（而不是 yaml 所在目录），
    换一台机器就找错地方。生成的运行配置：

    * ``path`` 写**绝对路径**（按源 yaml 的 path 拼出真实数据根目录），train/val
      保持相对值——vendor 校验只允许 ``path`` 绝对、split 相对，正好符合；
    * 原数据集的 data.yaml **一个字节都不改**；
    * 多模态时带上 ``modalities`` / ``modality_used``（vendor 训练器按这两项配图）。
    """
    source = _source_yaml(data_file)
    base = data_file.parent
    folders = _modality_folders(source)
    # 主模态目录：显式传入的 primary 优先（按数据集 reference_modality），
    # 否则旧规则“有 rgb 就是 rgb”。single_ir 只在 IR 参考数据集上派生单 IR。
    declared = str(primary or "").strip().lower()
    if declared in {"rgb", "ir", "edge", "depth"}:
        primary = declared
    else:
        primary = "rgb" if "rgb" in folders else next(iter(folders), "rgb")
    if single_ir:
        primary = "ir"
        family = "yolo"
        secondary = None
    rgb_folder = folders.get(primary, "images")
    names = source.get("names")
    if not names:
        raise ApiError(409, "DATASET_NOT_TRAINABLE", "data.yaml 没有 names，无法确定类别")

    # 源 yaml 的 path 决定真实数据根：相对值（含 "."）相对 yaml 目录；绝对值必须在
    # 项目数据集目录内（导入时已过 guard，这里再验一次，不允许指到项目外）。
    source_path = str(source.get("path") or "").strip() or "."
    candidate = Path(source_path)
    if candidate.is_absolute():
        real_root = candidate.resolve()
        try:
            real_root.relative_to(base.resolve())
        except ValueError:
            raise ApiError(
                409, "DATA_CONFIG_INVALID", "data.yaml 的 path 指到了数据集目录之外，拒绝训练"
            ) from None
    else:
        real_root = (base / candidate).resolve()

    payload: dict[str, Any] = {
        "path": str(real_root),
        "nc": len(names) if isinstance(names, (list, dict)) else int(source.get("nc") or 0),
        "names": names,
    }
    splits: dict[str, str] = {}
    for split in ("train", "val", "test", "minival"):
        value = source.get(split)
        if isinstance(value, str) and value.strip():
            splits[split] = value.strip()
        elif isinstance(value, list) and value:
            splits[split] = str(value[0]).strip()
    if "train" not in splits or "val" not in splits:
        raise ApiError(409, "DATA_CONFIG_INVALID", "data.yaml 必须同时有 train 和 val 划分")
    payload.update(splits)
    payload["modalities"] = {primary: rgb_folder} if single_ir else folders
    if family == "yolomm":
        if not secondary:
            raise ApiError(409, "DATA_CONFIG_INVALID", "YOLOMM 训练需要声明第二模态")
        payload["modality_used"] = [primary, secondary]
        if "Xch" in source:
            payload["Xch"] = source["Xch"]
    else:
        payload["modality_used"] = [primary]

    # 样本核验：每个用到的划分，RGB 目录必须有真实图片；YOLOMM 还要 X 目录有真实配图。
    for split, rel in splits.items():
        parts = [item for item in str(rel).replace("\\", "/").split("/") if item]
        rgb_dir = real_root.joinpath(*parts)
        if not _has_images(rgb_dir):
            raise ApiError(
                409, "DATA_CONFIG_INVALID", f"{split} 划分（{rel}）里找不到真实图片，训练配置无效"
            )
        if family == "yolomm":
            if parts[0] != rgb_folder:
                raise ApiError(
                    409,
                    "DATA_CONFIG_INVALID",
                    f"{split} 划分路径（{rel}）不以 RGB 模态目录 {rgb_folder} 开头，"
                    "无法按目录约定配对 X 模态",
                )
            x_folder = folders.get(secondary, f"images_{secondary}")
            x_dir = real_root.joinpath(x_folder, *parts[1:])
            if not _has_images(x_dir):
                raise ApiError(
                    409,
                    "X_MODALITY_MISSING",
                    f"{split} 划分缺少 {secondary} 模态目录（{x_folder}/{'/'.join(parts[1:])}）"
                    "的真实配图，YOLOMM 训练需要 RGB 与 X 一一成对",
                )
    target = run_dir / "data.yaml"
    storage.write_bytes(
        run_dir.parents[1],
        target,
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )
    return target


# vendor 数据解析器校验钩子：默认真实调用 vendor 的 check_det_dataset（在服务进程里
# 先证明“这份运行配置 vendor 能解析、能找到 train/val”），测试可注入轻量实现避免
# 每个用例都加载 torch。真实路径不降低：见 test_business_wb17_05_training 专项用例。
_config_validator: Callable[[Path], dict] | None = None


def set_config_validator(validator: Callable[[Path], dict] | None) -> None:
    """**仅供测试注入**：替换 vendor 数据解析器校验。"""
    global _config_validator
    _config_validator = validator


def _verify_with_vendor(yaml_path: Path) -> dict:
    if _config_validator is not None:
        return _config_validator(yaml_path)
    from backend.vision import runtime as vision_runtime  # noqa: PLC0415

    vision_runtime.activate_vendor()
    from ultralytics.data.utils import check_det_dataset  # noqa: PLC0415

    try:
        data = check_det_dataset(str(yaml_path), autodownload=False)
    except Exception as exc:  # noqa: BLE001 - vendor 会抛 SyntaxError/FileNotFoundError 等
        raise ApiError(
            409, "DATA_CONFIG_INVALID", f"vendor 数据解析器拒绝了这份训练配置：{exc}"[:500]
        ) from None
    return {"resolved": {"train": str(data.get("train") or ""), "val": str(data.get("val") or "")}}


def plan(
    conn,
    user_id: int,
    project_id: str,
    *,
    dataset_id: str,
    params: dict | None = None,
    parent_model_id: str | None = None,
    name: str | None = None,
) -> dict:
    """只做校验与展示，不建记录、不抢锁、不启动任何进程。"""
    clean = validate_params(params)
    found = _dataset_for_training(conn, user_id, project_id, dataset_id)
    dataset = found["dataset"]
    root = found["root"]
    single_ir = bool(clean.get("single_ir"))
    family, secondary = _family_for(
        conn, user_id, project_id, dataset, parent_model_id, single_ir=single_ir
    )
    parent = None
    warnings: list[str] = []
    base_weight_rel = ""
    if parent_model_id:
        row = model_service.get_owned(conn, user_id, project_id, parent_model_id)
        if row["deleted_at"]:
            raise ApiError(409, "PARENT_DELETED", "父模型在回收站里，先恢复再用它做增量训练")
        weight = storage.resolve_rel(root, str(row["weight_rel"]), must_exist=True)
        parent = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "dataset_id": row["dataset_id"],
            "status": str(row["status"]),
            "family": str(row["family"]),
            "metrics": _loads(row["metrics"], {}),
        }
        base_weight_rel = storage.relative(root, weight)
        if str(row["family"] or "yolo").lower() != family:
            warnings.append(
                f"按父模型分派为 {family} 训练（父模型是 {row['family']}）；数据集模态与父模型不一致时以父模型为准"
            )
        if str(row["dataset_id"] or "") != str(dataset_id):
            warnings.append("父模型是用别的数据集训练的：指标差异不能直接说明“变好”，要看条件")
        if not parent["metrics"]:
            warnings.append("父模型没有记录指标（导入权重通常没有验证集结果），无法对比")
    else:
        warnings.append("这是首次训练：没有父模型，权重从零开始（随机初始化）")
    if single_ir:
        warnings.append(
            "“仅 IR 单模态”训练：images_rgb/ 与 labels_ir/ 作为辅助资料保留，不参与本次训练；"
            "指标不能与双模态训练直接比较"
        )
    if family == "yolomm":
        warnings.append(
            f"YOLOMM 双模态训练（RGB + {secondary}）：启动时会核验每个划分的 X 模态配图目录"
        )
    if clean["device"] != "cpu":
        warnings.append(
            f"device={clean['device']}：本次只是计划预览，还没有启动训练进程；"
            "确认启动后会在当前服务器上用所选设备（GPU）真实运行"
        )
    condition = {
        "dataset_id": str(dataset_id),
        "dataset_samples": int(dataset["sample_count"] or 0),
        "split_for_metrics": "val",
        **{key: clean[key] for key in ("epochs", "imgsz", "batch", "lr0", "seed", "device")},
    }
    return {
        "dataset": {
            "id": str(dataset["id"]),
            "name": str(dataset["name"]),
            "samples": int(dataset["sample_count"] or 0),
            "classes": _loads(dataset["classes"], []),
        },
        "family": family,
        "secondary_modality": secondary,
        "single_ir": single_ir,
        "primary_modality": _dataset_reference(dataset),
        "parent": parent,
        "base_weight_rel": base_weight_rel,
        "params": clean,
        "conditions": condition,
        "name": str(name or "").strip() or f"训练-{str(dataset['name'])[:40]}",
        "warnings": warnings,
        "ready": True,
    }


# --------------------------------------------------------------------------- 锁


def _lock_holder(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM training_locks WHERE id = ?", (LOCK_ID,)).fetchone()


def _acquire_lock(conn, *, project_id: str, owner_id: int, run_id: str) -> None:
    now = security.now_iso()
    try:
        conn.execute(
            "INSERT INTO training_locks (id, project_id, owner_id, run_id, pid, acquired_at, heartbeat_at)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (LOCK_ID, str(project_id), int(owner_id), str(run_id), now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        holder = _lock_holder(conn)
        raise ApiError(
            409,
            "TRAINING_BUSY",
            "这台机器上已经有一次训练在占用训练槽；等它结束或先取消，再开始新的训练",
            details={
                "run_id": str(holder["run_id"]) if holder else "",
                "project_id": str(holder["project_id"]) if holder else "",
                "acquired_at": str(holder["acquired_at"]) if holder else "",
            },
        ) from None


def _release_lock(conn, run_id: str) -> bool:
    cur = conn.execute("DELETE FROM training_locks WHERE id = ? AND run_id = ?", (LOCK_ID, str(run_id)))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- 启动交接


def _claim_launch(conn, run_id: str) -> bool:
    """原子认领启动权：只有把 ``preparing`` 推进到 ``launching`` 的一方能 spawn。

    这一步和 :func:`_claim_cancel_while_queued` 抢的是**同一行**，靠 SQLite 的
    条件 UPDATE 决出胜负：rowcount=1 的一方赢。普通 SELECT 之后再 spawn 挡不住
    “查询与启动之间”的取消——那正是之前的竞态。
    """
    cur = conn.execute(
        "UPDATE training_runs SET launch_state = ?"
        " WHERE id = ? AND status = 'queued' AND launch_state = ?",
        (LAUNCHING, str(run_id), PREPARING),
    )
    conn.commit()
    return cur.rowcount > 0


def _claim_cancel_while_queued(conn, run_id: str) -> bool:
    """准备阶段的取消：原子地把 ``queued + preparing`` 直接改成 cancelled。

    只认“还没认领启动权”的行；已经 ``launching`` 的行留给调用方报“启动交接中”，
    绝不在这里提前宣布取消成功或释放训练槽。
    """
    cur = conn.execute(
        "UPDATE training_runs SET status = ?, error_code = ?, error_message = ?,"
        " finished_at = ?, launch_state = NULL"
        " WHERE id = ? AND status = 'queued' AND pid IS NULL"
        " AND (launch_state IS NULL OR launch_state = ?)",
        (CANCELLED, "TRAINING_CANCELLED", "开始前被取消", security.now_iso(), str(run_id), PREPARING),
    )
    conn.commit()
    return cur.rowcount > 0


def _fail_start(conn, run_id: str, code: str, message: str) -> None:
    """启动链路上任何一步失败：标 failed + 释放**本 run 的**锁（幂等，可重复调用）。

    已经是终态的行（例如准备期间被取消）不再覆盖结论，也不重复发事件。
    """
    fresh = conn.execute("SELECT status FROM training_runs WHERE id = ?", (str(run_id),)).fetchone()
    if fresh is not None and str(fresh["status"]) in TERMINAL:
        return
    conn.execute(
        "UPDATE training_runs SET status = ?, error_code = ?, error_message = ?,"
        " finished_at = ?, launch_state = NULL"
        " WHERE id = ? AND status NOT IN ('succeeded','failed','cancelled','interrupted')",
        (FAILED, code, message[:500], security.now_iso(), str(run_id)),
    )
    conn.commit()
    _release_lock(conn, run_id)
    _sync_job(conn, run_id)


def _mark_launch_unknown(conn, run_id: str) -> None:
    """启动交接没走完（PID 从未落库）：只写诊断，**不改状态、不放锁**。

    这是在“可能有一个真实 worker 在跑、却没有任何证据说明它结束了”时唯一诚实的做法：
    训练槽继续占用、页面能看到原因、不自动重跑。等人工确认进程后再处理。
    """
    message = (
        "服务重启时发现这次训练已认领启动，但 PID 从未写入记录，无法确认 worker 是否还在运行；"
        "训练槽保持占用，请人工核对（必要时结束该进程）后再处理。系统不会自动重跑"
    )
    conn.execute(
        "UPDATE training_runs SET error_code = ?, error_message = ? WHERE id = ?"
        " AND status NOT IN ('succeeded','failed','cancelled','interrupted')",
        ("TRAINING_PROCESS_UNKNOWN", message, str(run_id)),
    )
    conn.commit()


def _set_pid(conn, run_id: str, pid: int, started_at: float | None) -> None:
    cur = conn.execute(
        "UPDATE training_locks SET pid = ?, heartbeat_at = ? WHERE id = ? AND run_id = ?",
        (int(pid), security.now_iso(), LOCK_ID, str(run_id)),
    )
    if cur.rowcount == 0:
        # 锁不在这次 run 手里：进程起来了却没有槽位可归属，不能假装登记成功。
        conn.rollback()
        raise RuntimeError("训练槽已不属于这次启动（锁被释放），拒绝登记 PID")
    conn.execute(
        "UPDATE training_runs SET pid = ?, pid_started_at = ?, launch_state = NULL WHERE id = ?",
        (int(pid), started_at, str(run_id)),
    )
    conn.commit()


def _persist_pid(conn, run_id: str, pid: int, started_at: float | None) -> None:
    """登记 PID；第一次失败用**新连接**重试一次（SQLite 的写冲突多是瞬时的）。"""
    try:
        _set_pid(conn, run_id, pid, started_at)
        return
    except Exception as exc:  # noqa: BLE001 - 目的就是重试，原始异常只用来记日志
        _log.warning("训练 PID 写库失败，改用新连接重试 run=%s pid=%s err=%s", run_id, pid, exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    settings = job_service.settings()
    if settings is None:  # pragma: no cover - 工厂阶段一定注入了配置
        raise RuntimeError("没有可用的数据库配置，无法登记训练 PID")
    fresh = db.connect(settings.db_path)
    try:
        _set_pid(fresh, run_id, pid, started_at)
    finally:
        fresh.close()


def _abort_after_spawn(
    conn, run_id: str, process: subprocess.Popen, pid: int, started_at: float | None, error: Exception
) -> None:
    """PID 没能落库：真实进程已经在跑，绝不能丢。

    两条出路，都必须有证据：

    * 终止成功并**核实**已结束 → 标失败、放锁；
    * 终止不了/无法确认 → **保留训练槽**，把 PID 与原因写进可诊断状态（写不进去就记日志）。

    凡是“不知道”都不写成“已结束”。
    """
    _processes.setdefault(str(run_id), process)
    # 注意返回值语义：``_terminate_tree`` 返回的是**是否仍存活**。
    still_alive, reason = _terminate_tree(int(pid), started_at)
    if still_alive:
        message = (
            f"训练进程（PID {pid}）已经启动，但把 PID 写进数据库失败；"
            f"也无法确认它已结束（{reason}）。训练槽保持占用，请按这个 PID 人工核对后再处理"
        )
        _log.error("PID 未落库且未确认结束 run=%s pid=%s reason=%s", run_id, pid, reason)
        try:
            conn.execute(
                "UPDATE training_runs SET error_code = ?, error_message = ? WHERE id = ?",
                ("TRAINING_PID_UNRECORDED", message[:500], str(run_id)),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - 数据库本身不可写时只剩日志
            _log.exception("记录 PID 未落库状态失败 run=%s", run_id)
        raise ApiError(500, "TRAINING_PID_UNRECORDED", message)
    message = f"训练进程启动后 PID 写库失败（{type(error).__name__}），已终止并确认结束（{reason}）"
    _log.error("PID 未落库，已终止 run=%s pid=%s reason=%s", run_id, pid, reason)
    try:
        _fail_start(conn, run_id, "TRAINING_PID_UNRECORDED", message)
    except Exception:  # noqa: BLE001
        _log.exception("标记训练启动失败时数据库不可写 run=%s", run_id)
        try:
            _release_lock(conn, run_id)
        except Exception:  # noqa: BLE001
            pass
    _reap(str(run_id))
    raise ApiError(500, "TRAINING_PID_UNRECORDED", message)


# --------------------------------------------------------------------------- 读写


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "dataset_id": data["dataset_id"],
        "parent_model_id": data.get("parent_model_id"),
        "base_weight_rel": data.get("base_weight_rel") or "",
        "name": data["name"],
        "status": data["status"],
        "family": data.get("family") or "yolo",
        "secondary_modality": data.get("secondary_modality"),
        "params": _loads(data.get("params"), {}),
        "metrics": _loads(data.get("metrics"), {}),
        "conditions": _loads(data.get("conditions"), {}),
        "run_dir_rel": data.get("run_dir_rel") or "",
        "log_rel": data.get("log_rel") or "",
        "pid": data.get("pid"),
        "output_model_id": data.get("output_model_id"),
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message")}
            if data.get("error_code")
            else None
        ),
        "job_id": data.get("job_id"),
        "created_at": data["created_at"],
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def get_owned(conn, user_id: int, project_id: str, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM training_runs WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(run_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "TRAINING_NOT_FOUND", "训练记录不存在或不属于当前项目")
    return row


def list_for_project(conn, user_id: int, project_id: str, *, limit: int = 30) -> dict:
    rows = conn.execute(
        "SELECT * FROM training_runs WHERE project_id = ? AND owner_id = ?"
        " ORDER BY created_at DESC LIMIT ?",
        (str(project_id), int(user_id), max(1, min(int(limit), 100))),
    ).fetchall()
    lock = _lock_holder(conn)
    return {
        "runs": [public(row) for row in rows],
        "lock": {
            "held": lock is not None,
            "run_id": str(lock["run_id"]) if lock else "",
            "project_id": str(lock["project_id"]) if lock else "",
        },
    }


# --------------------------------------------------------------------------- 启动


def set_worker_command(command: list[str] | None) -> None:
    """**仅供测试注入**假 worker；生产默认（None）走 ``backend.training_worker``。"""
    global _worker_command
    _worker_command = list(command) if command else None


def worker_command() -> list[str]:
    if _worker_command:
        return list(_worker_command)
    return [sys.executable, "-m", "backend.training_worker"]


def _worker_env(run_dir: Path) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    # 不自动安装、不联网下载：缺依赖就让 worker 明确失败。
    env["YOLO_AUTOINSTALL"] = "false"
    env["YOLO_CONFIG_DIR"] = str(run_dir / ".ultralytics")
    env["MPLCONFIGDIR"] = str(run_dir / ".mpl")
    env["HX17_TRAINING_RUN"] = run_dir.name
    return env


def start(
    conn,
    user_id: int,
    project_id: str,
    *,
    dataset_id: str,
    request_key: str,
    name: str | None = None,
    params: dict | None = None,
    parent_model_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[dict, bool]:
    """抢锁 → 建记录 → 生成并校验运行配置 → 起子进程。

    抢到锁之后的**每一步**（建行、建 job、写 params、生成运行时配置、启动）都在
    同一个收尾保护里：任何一步失败都会把本 run 标成 failed 并释放**本 run 的**锁
    （按 run_id 限定删除，绝不碰别的 run 的锁），不会留下“训练中”的假记录。
    """
    key = str(request_key or "").strip()
    if not key:
        raise ApiError(400, "REQUEST_KEY_REQUIRED", "启动训练必须带 request_key（幂等键）")
    existing = conn.execute(
        "SELECT * FROM training_runs WHERE project_id = ? AND request_key = ?",
        (str(project_id), key),
    ).fetchone()
    if existing is not None:
        return public(existing), False

    prepared = plan(
        conn,
        user_id,
        project_id,
        dataset_id=dataset_id,
        params=params,
        parent_model_id=parent_model_id,
        name=name,
    )
    root = storage.project_root(conn, user_id, project_id)
    run_id = security.new_id("trn")
    # 先抢锁：没有锁就没有“训练中”的记录。
    _acquire_lock(conn, project_id=project_id, owner_id=int(user_id), run_id=run_id)

    try:
        run_dir = storage.project_dir(root, TRAINING, run_id)
    except ApiError:
        _release_lock(conn, run_id)
        raise

    log_path = run_dir / "train.log"
    params_file = run_dir / "params.json"
    now = security.now_iso()
    try:
        conn.execute(
            """
            INSERT INTO training_runs
                (id, project_id, owner_id, job_id, dataset_id, parent_model_id, base_weight_rel,
                 name, status, params, conditions, run_dir_rel, log_rel, request_key,
                 session_id, turn_id, family, secondary_modality, created_at, launch_state)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(project_id),
                int(user_id),
                str(dataset_id),
                parent_model_id,
                prepared["base_weight_rel"],
                prepared["name"],
                json.dumps(prepared["params"], ensure_ascii=False),
                json.dumps(prepared["conditions"], ensure_ascii=False),
                storage.relative(root, run_dir),
                storage.relative(root, log_path),
                key,
                session_id,
                turn_id,
                prepared["family"],
                prepared["secondary_modality"],
                now,
                PREPARING,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发下同一 request_key 被别的请求抢先生成：放锁，返回那一条。
        _release_lock(conn, run_id)
        existing = conn.execute(
            "SELECT * FROM training_runs WHERE project_id = ? AND request_key = ?",
            (str(project_id), key),
        ).fetchone()
        if existing is not None:
            return public(existing), False
        raise

    job: sqlite3.Row | None = None
    process = None
    try:
        job, _ = job_service.create(
            conn,
            project_id=project_id,
            owner_id=int(user_id),
            kind="training",
            request_key=f"training:{run_id}",
            payload={"run_id": run_id, "dataset_id": str(dataset_id)},
            session_id=session_id,
            turn_id=turn_id,
        )
        conn.execute("UPDATE training_runs SET job_id = ? WHERE id = ?", (str(job["id"]), run_id))
        conn.commit()

        # 运行时配置：在**训练工作区**生成指向真实 RGB/X/划分的 data.yaml（绝对 path），
        # 并用 vendor 的数据解析器真实验证；原数据集配置不动。
        data_file = Path(_dataset_for_training(conn, user_id, project_id, dataset_id)["data_file"])
        runtime_yaml = generate_runtime_config(
            run_dir,
            data_file,
            family=str(prepared["family"]),
            secondary=prepared["secondary_modality"],
            primary=prepared.get("primary_modality"),
            single_ir=bool(prepared.get("single_ir")),
        )
        _verify_with_vendor(runtime_yaml)

        storage.write_bytes(
            root,
            params_file,
            json.dumps(
                {
                    "run_id": run_id,
                    "name": prepared["name"],
                    "family": prepared["family"],
                    "secondary_modality": prepared["secondary_modality"],
                    "primary_modality": prepared.get("primary_modality"),
                    "single_ir": bool(prepared.get("single_ir")),
                    "runtime_data_yaml": storage.relative(root, runtime_yaml),
                    "params": prepared["params"],
                    "conditions": prepared["conditions"],
                    "base_weight_rel": prepared["base_weight_rel"],
                    "parent_model_id": parent_model_id,
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )

        # 启动交接：准备期间随时可能被取消（另一条连接、网页或 Agent）。
        # 只有把 preparing **原子**推进到 launching 的一方有权 spawn；认领失败说明
        # 取消已经赢了，这里必须一个进程都不起——不是 spawn 前“再看一眼状态”。
        if not _claim_launch(conn, run_id):
            fresh = conn.execute("SELECT * FROM training_runs WHERE id = ?", (run_id,)).fetchone()
            if fresh is not None and str(fresh["status"]) in TERMINAL:
                raise ApiError(
                    409,
                    "TRAINING_CANCELLED",
                    "训练在启动准备阶段被取消，没有启动任何进程",
                    details={"status": str(fresh["status"])},
                )
            raise ApiError(
                409, "TRAINING_STATE_CHANGED", "训练记录在启动过程中被其他操作改变，没有启动进程"
            )
        try:
            process, pid, started_at = _spawn(root, run_dir, log_path)
        except Exception as exc:  # noqa: BLE001 - Popen 失败单独归为 spawn 失败
            raise ApiError(
                500, "TRAINING_SPAWN_FAILED", f"训练进程没能启动（{type(exc).__name__}）"
            ) from None
        _processes[run_id] = process
    except ApiError as exc:
        _fail_start(conn, run_id, exc.code or "TRAINING_START_FAILED", exc.msg)
        raise
    except Exception as exc:  # noqa: BLE001 - 启动链路任何失败都必须落库并放锁
        _fail_start(conn, run_id, "TRAINING_START_FAILED", f"训练没能启动（{type(exc).__name__}）")
        raise ApiError(500, "TRAINING_START_FAILED", f"训练没能启动（{type(exc).__name__}）") from None

    try:
        _persist_pid(conn, run_id, pid, started_at)
    except Exception as exc:  # noqa: BLE001 - 进程已在跑：要么确认终止，要么保留可诊断状态
        _abort_after_spawn(conn, run_id, process, pid, started_at, exc)
    if not _mark_running(conn, run_id, security.now_iso()):
        # 进程刚起来就被取消并确认结束：不覆盖取消结论，也不发“已开始”事件。
        return public(get_owned(conn, user_id, project_id, run_id)), True
    job_service.emit(conn, job_service.get(conn, str(job["id"])), "job.started", {"kind": "training"})
    _watch_async(run_id)
    return public(get_owned(conn, user_id, project_id, run_id)), True


def _spawn(root: Path, run_dir: Path, log_path: Path) -> tuple[subprocess.Popen, int, float | None]:
    command = worker_command() + ["--run-dir", str(run_dir)]
    options: dict[str, Any] = {
        "cwd": str(run_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": open(log_path, "ab"),
        "stderr": subprocess.STDOUT,
        "env": _worker_env(run_dir),
    }
    if os.name == "nt":  # Windows：不弹黑窗口
        options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(command, **options)
    finally:
        handle = options["stdout"]
        if hasattr(handle, "close") and not handle.closed:
            # 父进程不持有日志句柄：子进程已继承，父进程关掉避免占用与缓冲错乱。
            handle.close()
    # 立刻挂一个等待线程：Popen 句柄总有主，退出时不会把“still running”甩给用户。
    threading.Thread(
        target=process.wait, daemon=True, name=f"hx17-train-wait-{process.pid}"
    ).start()
    return process, int(process.pid), process_started_at(process.pid)


# --------------------------------------------------------------------------- 监视与终态


def _watch_async(run_id: str) -> None:
    with _watchers_lock:
        current = _watchers.get(str(run_id))
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(target=_watch, args=(str(run_id),), daemon=True, name=f"hx17-train-{run_id}")
        _watchers[str(run_id)] = thread
    thread.start()


def _reap(run_id: str) -> None:
    """回收子进程句柄：确认退出后把僵尸状态收掉，不留“still running”告警。"""
    process = _processes.pop(str(run_id), None)
    if process is None:
        return
    try:
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - 收尾失败不影响状态判定
        pass


def _watch(run_id: str) -> None:
    settings = job_service.settings()
    if settings is None:  # pragma: no cover - 工厂阶段一定注入了配置
        return
    conn = db.connect(settings.db_path)
    try:
        warned = 0.0
        while True:
            run = conn.execute("SELECT * FROM training_runs WHERE id = ?", (str(run_id),)).fetchone()
            if run is None or str(run["status"]) in TERMINAL:
                return
            pid = run["pid"]
            if not pid:
                _finalize(conn, run)
                return
            # 优先用本进程持有的句柄：poll() 是“确实退出了”的硬证据，不依赖 psutil。
            process = _processes.get(str(run_id))
            if process is not None and process.poll() is not None:
                _finalize(conn, run)
                return
            state, reason = _probe(int(pid), run["pid_started_at"])
            if state == GONE:
                _finalize(conn, run)
                return
            if state == UNKNOWN:
                # 查询失败/身份缺失：不知道进程死活，绝不收尾、不放锁；
                # 每隔几秒记录一次可观测的告警，然后继续受控重查。
                now = time.monotonic()
                if now - warned > 5.0:
                    warned = now
                    _log.warning("训练进程状态未知 run=%s pid=%s reason=%s（保持占用，继续重查）",
                                 run_id, pid, reason)
                time.sleep(_POLL_SECONDS)
                continue
            time.sleep(_POLL_SECONDS)
    except Exception:  # pragma: no cover - 线程异常不能静默
        _log.exception("训练监视线程异常 run=%s", run_id)
    finally:
        conn.close()


def _read_worker_result(run_dir: Path) -> dict | None:
    path = run_dir / "worker.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _finalize(conn, run: sqlite3.Row) -> None:
    """进程已确认退出：按**真实产物**定终态，绝不因为“看起来跑完了”就算成功。

    权重必须真读一遍（与导入同一套安全加载）：文本文件改名成 best.pt 骗不过这里；
    类别表取权重里的真实 ``names``，按 {index, name} 契约登记——worker 报的文字
    类别不作为登记依据。
    """
    run_id = str(run["id"])
    fresh = conn.execute("SELECT * FROM training_runs WHERE id = ?", (run_id,)).fetchone()
    if fresh is None or str(fresh["status"]) in TERMINAL:
        _reap(run_id)
        return
    _reap(run_id)
    root = _root_for(conn, fresh)
    if root is None:
        # 进程确实结束了，但项目根目录已不可用（撤权/删除）：不假成功、不越权读文件。
        # 槽位跟着进程走（进程已确认退出），状态如实记“结果未知，按失败”。
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code="TRAINING_ROOT_UNAVAILABLE",
            message="训练进程已结束，但项目绑定目录当前不可用，读不到产物；"
            "结果未知，按失败记录（不会自动重跑）",
            result=None,
        )
        _release_lock(conn, run_id)
        return
    try:
        run_dir = storage.resolve_rel(root, str(fresh["run_dir_rel"]))
    except ApiError:
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code="TRAINING_ROOT_UNAVAILABLE",
            message="训练进程已结束，但训练目录当前不可访问；结果未知，按失败记录",
            result=None,
        )
        _release_lock(conn, run_id)
        return
    result = _read_worker_result(run_dir)
    if result is None:
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code="TRAINING_NO_RESULT",
            message="训练进程结束了，但没有写出结果文件；结果未知，按失败记录（不会自动重跑）",
            result=None,
        )
        _release_lock(conn, run_id)
        return
    state = str(result.get("status") or "").strip().lower()
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    conditions = result.get("conditions") if isinstance(result.get("conditions"), dict) else {}
    if state != "succeeded":
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code=str(result.get("code") or "TRAINING_FAILED"),
            message=str(result.get("message") or "训练失败")[:500],
            result=None,
        )
        _release_lock(conn, run_id)
        return

    weight_rel = str(result.get("weight_rel") or "")
    if not weight_rel:
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code="TRAINING_NO_WEIGHT",
            message="训练报告成功，但没有找到权重文件；按失败记录",
            result=None,
        )
        _release_lock(conn, run_id)
        return

    payload = {
        "values": metrics,
        "conditions": conditions or _loads(fresh["conditions"], {}),
        "split": str(result.get("split") or "val"),
        "source": str(result.get("source") or "training/results.csv"),
        "dataset_id": str(fresh["dataset_id"]),
        "run_id": run_id,
        "images": result.get("images"),
        "epochs_run": result.get("epochs_run"),
    }
    try:
        model = model_service.create_trained(
            conn,
            int(fresh["owner_id"]),
            str(fresh["project_id"]),
            name=str(fresh["name"]),
            dataset_id=str(fresh["dataset_id"]),
            weight_rel=weight_rel,
            training_run_id=run_id,
            parent_model_id=fresh["parent_model_id"],
            metrics=payload,
            family=str(fresh["family"] or "yolo"),
            secondary_modality=(
                str(fresh["secondary_modality"]) if fresh["secondary_modality"] else None
            ),
            notes=str(result.get("notes") or ""),
        )
    except ApiError as exc:
        # 保留业务层给出的具体失败码（如 TRAINING_WEIGHT_INVALID / TRAINED_CLASS_MISMATCH），
        # 只在异常不带业务码时才落到笼统的 TRAINING_REGISTER_FAILED。
        code = str(getattr(exc, "code", "") or "").strip() or "TRAINING_REGISTER_FAILED"
        _mark_terminal(
            conn,
            run_id,
            status=FAILED,
            code=code,
            message=f"训练产出无法登记成模型：{exc.msg}",
            result=None,
        )
        _release_lock(conn, run_id)
        return
    _mark_terminal(conn, run_id, status=SUCCEEDED, result=payload, output_model_id=str(model["id"]))
    _release_lock(conn, run_id)


def _root_for(conn, run: sqlite3.Row) -> Path | None:
    try:
        return storage.project_root(conn, int(run["owner_id"]), str(run["project_id"]))
    except ApiError:
        return None


def _mark_running(conn, run_id: str, started_at: str) -> bool:
    """``queued → running``：条件更新，绝不覆盖别人已经写下的终态。

    极端窗口里进程刚起来就被另一条连接取消并确认结束——这时 ``status`` 已经是
    cancelled，启动链路不能再把它改回 running（那会留下一条“在跑”但没有进程的记录）。
    """
    cur = conn.execute(
        "UPDATE training_runs SET status = ?, started_at = ?, launch_state = NULL"
        " WHERE id = ? AND status = ?",
        (RUNNING, started_at, str(run_id), QUEUED),
    )
    conn.commit()
    _sync_job(conn, run_id)
    return cur.rowcount > 0


def _transition(conn, run_id: str, status: str, **fields: Any) -> None:
    sets = ", ".join(f"{key} = ?" for key in fields)
    sql = "UPDATE training_runs SET status = ?" + (", " + sets if sets else "") + " WHERE id = ?"
    conn.execute(sql, (status, *fields.values(), str(run_id)))
    conn.commit()
    _sync_job(conn, run_id)


def _mark_terminal(
    conn,
    run_id: str,
    *,
    status: str,
    result: dict | None,
    code: str = "",
    message: str = "",
    output_model_id: str | None = None,
) -> None:
    now = security.now_iso()
    conn.execute(
        "UPDATE training_runs SET status = ?, metrics = ?, error_code = ?, error_message = ?,"
        " finished_at = ?, output_model_id = COALESCE(?, output_model_id), launch_state = NULL"
        " WHERE id = ?",
        (
            status,
            json.dumps(result or {}, ensure_ascii=False),
            code or None,
            message or None,
            now,
            output_model_id,
            str(run_id),
        ),
    )
    conn.commit()
    _sync_job(conn, run_id)


def _sync_job(conn, run_id: str) -> None:
    """把训练域状态映射到既有 job（事件流/轮次查询都靠它，不另建一套）。"""
    run = conn.execute("SELECT * FROM training_runs WHERE id = ?", (str(run_id),)).fetchone()
    if run is None or not run["job_id"]:
        return
    try:
        job = job_service.get(conn, str(run["job_id"]))
    except KeyError:
        return
    status = str(run["status"])
    mapped = {
        QUEUED: "queued",
        RUNNING: "running",
        SUCCEEDED: "done",
        FAILED: "failed",
        CANCELLED: "failed",
        INTERRUPTED: "interrupted",
    }[status]
    fields: dict[str, Any] = {"status": mapped}
    if status == RUNNING:
        fields["started_at"] = run["started_at"] or security.now_iso()
    if status in TERMINAL:
        fields["finished_at"] = run["finished_at"] or security.now_iso()
        if run["error_code"]:
            fields["error_code"] = run["error_code"]
            fields["error_message"] = run["error_message"]
        payload = _loads(run["metrics"], {})
        fields["result"] = json.dumps(
            {
                "run_id": str(run["id"]),
                "status": status,
                "metrics": payload,
                "output_model_id": run["output_model_id"],
            },
            ensure_ascii=False,
        )
    job_service.update(conn, str(job["id"]), **fields)
    if status in TERMINAL:
        job_service.emit(
            conn,
            job_service.get(conn, str(job["id"])),
            f"job.{mapped}",
            {"run_id": str(run["id"]), "status": status, "error": run["error_code"]},
        )
        # 通知用真实字段拼，不调模型：卡片直接显示“训练成功/失败 + 指标 + 产出模型”。
        spoken = summary_text(public(run))
        if spoken:
            job_service.emit(
                conn,
                job_service.get(conn, str(job["id"])),
                "job.summary",
                {"summary": spoken, "status": mapped},
            )


# --------------------------------------------------------------------------- 取消与日志


def cancel(conn, user_id: int, project_id: str, run_id: str) -> dict:
    run = get_owned(conn, user_id, project_id, run_id)
    status = str(run["status"])
    if status in TERMINAL:
        raise ApiError(
            409,
            "TRAINING_NOT_RUNNING",
            f"这次训练已经是 {status}，不能再取消",
            details={"status": status},
        )
    pid = run["pid"]
    if not pid:
        # 准备阶段：与 start 的“启动认领”抢**同一行**，条件 UPDATE 谁先写谁赢。
        if _claim_cancel_while_queued(conn, run_id):
            _release_lock(conn, run_id)
            _sync_job(conn, run_id)
            return {**public(get_owned(conn, user_id, project_id, run_id)), "terminated": True}
        # 没抢到：可能是别人先取消/收尾了，也可能已经进入启动交接。
        fresh = get_owned(conn, user_id, project_id, run_id)
        if str(fresh["status"]) in TERMINAL:
            raise ApiError(
                409,
                "TRAINING_NOT_RUNNING",
                f"这次训练已经是 {fresh['status']}，不能再取消",
                details={"status": str(fresh["status"])},
            )
        pid = fresh["pid"]
        if not pid:
            # launching：启动权已被认领，进程即将或已经起来，但 PID 还没落库。
            # 此刻宣布取消成功或放锁，下一秒就可能被一个真实进程打脸，所以只报“交接中”。
            raise ApiError(
                409,
                "CANCEL_STARTING",
                "训练正在启动交接中（已认领启动、PID 还没登记）：此刻取消无法保证进程不启动，"
                "训练槽保持占用，请稍后重试；若服务刚重启过，说明这次启动的 PID 从未落库，"
                "需要人工核对进程后再处理",
                details={"run_id": str(run_id), "launch_state": str(fresh["launch_state"] or "")},
            )
        started = fresh["pid_started_at"]
    else:
        started = run["pid_started_at"]

    ppid = int(pid)
    if _psutil() is None:
        raise ApiError(
            409,
            "CANCEL_UNVERIFIABLE",
            "本机缺少进程校验依赖（psutil），无法确认 PID 身份，不敢贸然终止；请人工确认后处理",
        )
    state, reason = _probe(ppid, started)
    if state == UNKNOWN:
        # 查询失败 / 身份证据缺失：进程可能还活着。不终止、不宣布结束、不放锁。
        raise ApiError(
            409,
            "PROCESS_STATE_UNKNOWN",
            "训练进程的当前状态无法确认（" + reason + "）：不敢终止也不敢宣布结束，"
            "训练槽保持占用、状态不变；请稍后重试，或人工确认后处理",
            details={"pid": ppid, "reason": reason},
        )
    if state == GONE:
        # 确认已退出（含 PID 被复用）：交给真实产物定终态，不硬写成 cancelled。
        _finalize(conn, run)
        fresh = get_owned(conn, user_id, project_id, run_id)
        return {
            **public(fresh),
            "terminated": True,
            "note": "进程已确认结束（" + reason + "），按真实结果定状态（没有结果时记为失败）",
        }

    still_alive, reason = _terminate_tree(ppid, started)
    if still_alive:
        raise ApiError(
            409,
            "CANCEL_FAILED",
            "终止失败：进程仍在运行，训练槽保持占用、状态不变；请稍后重试或人工处理",
            details={"pid": ppid, "reason": reason},
        )
    _mark_terminal(
        conn,
        run_id,
        status=CANCELLED,
        result=None,
        code="TRAINING_CANCELLED",
        message="用户取消；进程已确认结束",
    )
    _release_lock(conn, run_id)
    return {**public(get_owned(conn, user_id, project_id, run_id)), "terminated": True, "reason": reason}


def log_tail(conn, user_id: int, project_id: str, run_id: str, *, offset: int = 0, limit: int = 16000) -> dict:
    run = get_owned(conn, user_id, project_id, run_id)
    root = _root_for(conn, run)
    if root is None:
        raise ApiError(409, "ROOT_MISSING", "项目绑定目录已不可用，读不到日志")
    rel = str(run["log_rel"] or "")
    if not rel:
        return {"text": "", "next": int(offset), "size": 0, "eof": True}
    path = storage.resolve_rel(root, rel, must_exist=False)
    if not path.is_file():
        return {"text": "", "next": int(offset), "size": 0, "eof": True}
    size = path.stat().st_size
    start = max(0, min(int(offset), size))
    with open(path, "rb") as handle:
        handle.seek(start)
        chunk = handle.read(max(1024, min(int(limit), 64000)))
    return {
        "text": chunk.decode("utf-8", "replace"),
        "next": start + len(chunk),
        "size": size,
        "eof": start + len(chunk) >= size,
    }


# --------------------------------------------------------------------------- 对比


METRIC_KEYS = ("map50", "map50_95", "precision", "recall")
COMPARE_KEYS = ("dataset_id", "epochs", "imgsz", "batch", "lr0", "seed", "device")


def compare(conn, user_id: int, project_id: str, run_id: str) -> dict:
    """父子指标对比：只报真实数字与条件差异，不宣布“一定更好”。"""
    run = get_owned(conn, user_id, project_id, run_id)
    child = _loads(run["metrics"], {})
    child_values = child.get("values") if isinstance(child.get("values"), dict) else {}
    parent_model = None
    parent_values: dict = {}
    parent_conditions: dict = {}
    if run["parent_model_id"]:
        try:
            row = model_service.get_owned(conn, user_id, project_id, str(run["parent_model_id"]))
        except ApiError:
            row = None
        if row is not None:
            stored = _loads(row["metrics"], {})
            parent_model = {"id": str(row["id"]), "name": str(row["name"])}
            parent_values = stored.get("values") if isinstance(stored.get("values"), dict) else {}
            parent_conditions = stored.get("conditions") if isinstance(stored.get("conditions"), dict) else {}
    deltas: dict[str, float] = {}
    for key in METRIC_KEYS:
        left, right = parent_values.get(key), child_values.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            deltas[key] = round(float(right) - float(left), 4)
    child_conditions = child.get("conditions") or _loads(run["conditions"], {})
    compared = [key for key in COMPARE_KEYS if key in parent_conditions or key in child_conditions]
    differences = [
        {"key": key, "parent": parent_conditions.get(key), "child": child_conditions.get(key)}
        for key in compared
        if parent_conditions.get(key) != child_conditions.get(key)
    ]
    comparable = bool(parent_values) and bool(child_values) and not differences
    note = ""
    if not parent_model:
        note = "这是首次训练，没有父模型可对比。"
    elif not parent_values:
        note = "父模型没有记录验证集指标（导入权重一般没有），无法对比。"
    elif not child_values:
        note = "这次训练没有产出可用的验证集指标，无法对比。"
    elif differences:
        note = "两次训练的样本或参数不同，指标差异不能直接说明模型变好；要比较请在相同条件下再训一次。"
    else:
        note = "两次训练条件一致，指标可直接比较；是否上线仍由人工判断。"
    return {
        "run": public(run),
        "parent": {"model": parent_model, "values": parent_values, "conditions": parent_conditions},
        "child": {
            "values": child_values,
            "conditions": child_conditions,
            "split": child.get("split"),
            "source": child.get("source"),
        },
        "delta": deltas,
        "comparable": comparable,
        "differences": differences,
        "note": note,
    }


# --------------------------------------------------------------------------- 重启核对


def recover(conn) -> dict:
    """服务启动时核对真实进程与产物：不重复启动，不把残留锁留成“永远忙”。

    唯一的例外是**启动交接未完成**（``launch_state='launching'`` 且 ``pid`` 为空）：
    那说明进程可能已经起来而 PID 从未落库，我们没有“它已退出”的任何证据。
    这种情况按 ``TRAINING_PROCESS_UNKNOWN`` 保守阻塞——不放锁、不标 interrupted、不重跑，
    宁可让人工来确认，也不把未知 worker 说成结束。
    """
    stats = {
        "locks_released": 0,
        "runs_finalized": 0,
        "runs_interrupted": 0,
        "runs_reattached": 0,
        "runs_blocked": 0,
    }
    lock = _lock_holder(conn)
    if lock is not None:
        run = conn.execute(
            "SELECT * FROM training_runs WHERE id = ?", (str(lock["run_id"]),)
        ).fetchone()
        if run is None:
            conn.execute("DELETE FROM training_locks WHERE id = ?", (LOCK_ID,))
            conn.commit()
            stats["locks_released"] += 1
        elif str(run["status"]) in TERMINAL:
            conn.execute("DELETE FROM training_locks WHERE id = ? AND run_id = ?", (LOCK_ID, str(run["id"])))
            conn.commit()
            stats["locks_released"] += 1

    rows = conn.execute(
        "SELECT * FROM training_runs WHERE status IN ('queued', 'running') ORDER BY created_at"
    ).fetchall()
    for run in rows:
        run_id = str(run["id"])
        pid = run["pid"]
        if pid:
            state, reason = _probe(int(pid), run["pid_started_at"])
            if state == ALIVE:
                # 进程还活着：只重新挂监视，绝不重启进程。
                _watch_async(run_id)
                stats["runs_reattached"] += 1
                continue
            if state == UNKNOWN:
                # 状态查不出来（查询失败/身份证据缺失/缺 psutil）：不能宣称结束，
                # 也不能释放槽位；挂上监视继续受控重查，等它给出确定结论。
                _log.warning("重启核对：训练进程状态未知 run=%s pid=%s reason=%s（保持占用）",
                             run_id, pid, reason)
                _watch_async(run_id)
                stats["runs_reattached"] += 1
                continue
            # state == GONE（含 PID 被复用）：我们的进程确定结束了，按真实产物收尾。
        elif str(run["launch_state"] or "") == LAUNCHING:
            # 启动权已认领、PID 从未落库：可能真有一个 worker 在跑，不能当作“没有启动”。
            _log.warning("重启核对：启动交接未完成且 PID 未知 run=%s（保持占用）", run_id)
            _mark_launch_unknown(conn, run_id)
            stats["runs_blocked"] += 1
            continue
        root = _root_for(conn, run)
        result = None
        if root is not None and str(run["run_dir_rel"] or ""):
            try:
                result = _read_worker_result(storage.resolve_rel(root, str(run["run_dir_rel"])))
            except ApiError:
                result = None
        if result is not None:
            _finalize(conn, run)
            stats["runs_finalized"] += 1
        else:
            _mark_terminal(
                conn,
                run_id,
                status=INTERRUPTED,
                result=None,
                code="SERVICE_RESTART",
                message="服务重启时这次训练还没结束，结果未知；需要时请重新启动训练（不会自动重跑）",
            )
            _release_lock(conn, run_id)
            stats["runs_interrupted"] += 1
    return stats


# --------------------------------------------------------------------------- 摘要


def summary_text(run: dict) -> str:
    """用真实字段拼一句完成摘要（通知用，不调模型）。"""
    status = str(run.get("status") or "")
    metrics = ((run.get("metrics") or {}).get("values") or {}) if isinstance(run.get("metrics"), dict) else {}
    part = f"训练 {status}：{run.get('name') or run.get('id')}"
    if metrics.get("map50") is not None:
        part += f"，验证集 mAP50={metrics['map50']}"
    if run.get("output_model_id"):
        part += f"，产出模型 {run['output_model_id']}"
    error = run.get("error") or {}
    if error.get("code"):
        part += f"（{error['code']}）"
    return part + "。"


__all__ = [
    "ALIVE",
    "CANCELLED",
    "FAILED",
    "GONE",
    "INTERRUPTED",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "TERMINAL",
    "TRAINING",
    "UNKNOWN",
    "cancel",
    "compare",
    "generate_runtime_config",
    "get_owned",
    "list_for_project",
    "log_tail",
    "plan",
    "public",
    "recover",
    "set_config_validator",
    "set_device_probe",
    "set_worker_command",
    "start",
    "summary_text",
    "validate_params",
    "worker_command",
]
