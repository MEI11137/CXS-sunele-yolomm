"""真实训练子进程：由 ``backend.services.training`` 以参数列表启动。

为什么单独一个进程：

* 训练会长时间占用 CPU/GPU，还可能因为数据/依赖问题直接崩掉；放进 Web 进程会把
  整个服务拖垮，也没法可靠地“确认它真的结束了”。
* 参数通过 ``params.json`` 传入（不拼 shell），日志直接写标准输出（父进程重定向到
  ``training/<run_id>/train.log``），结果写 ``worker.json`` 供父进程按**真实产物**定状态。

分派与输入：

* 训练用哪个架构由**父进程决定并写进 params.json**（``family``）：父模型优先，
  其次数据集真实模态（RGB+X → YOLOMM，纯 RGB → YOLO）。这里只按指示执行，
  不自己猜。
* 数据用父进程生成的**运行时** ``data.yaml``（绝对 path 指向本数据集目录，
  train/val 相对），不直接用数据集自带的 yaml——那份可能带 ``path: raw`` 之类
  会被 vendor 相对全局目录解析的值。
* ``device`` 原样透传（cpu / cuda / cuda:N）：GPU 在部署机上真实执行；
  本机开发环境不会因为传了 cuda 就假装训练成功。

边界：

* 只用项目内 ``backend/vendor/ultralytics``（与推理同一份实现），缺 vendor / 缺依赖
  直接失败，不静默回退 pip 版，不自动安装/升级 torch。
* 首次训练用 vendor 自带的模型结构 yaml 从零开始；增量训练必须由用户指定项目内已有的
  ``.pt``。
* **AMP 默认关闭**：vendor 的 AMP 校验会在线下载 5.35MB 的 ``yolo11n.pt`` 基准权重
  （真实训练验收时发现它落在 run_dir 里，和“不下载任何预训练权重”的旧说明不符）。
  正常离线训练不应依赖这个隐式联网下载，因此默认显式传 ``amp=False``；
  如需 AMP 提速，设置环境变量 ``HX17_TRAINING_AMP=1``，届时会真实下载并做校验
  （校验不通过 vendor 会自动关闭 AMP，不会假装通过）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_METRIC_MAP = {
    "metrics/precision(B)": "precision",
    "metrics/recall(B)": "recall",
    "metrics/mAP50(B)": "map50",
    "metrics/mAP50-95(B)": "map50_95",
}
# 从零训练用的架构（vendor 自带的结构 yaml，本身不下载权重；但若显式 HX17_TRAINING_AMP=1，
# vendor 的 AMP 校验会另下载 5.35MB yolo11n.pt 基准权重，见模块 docstring）：
# yolomm 用非对称深度融合（RGB 深 / X 浅），vendor 里可用的默认 MM 检测配置。
_SCRATCH_ARCH = {"yolo": "yolov8n.yaml", "yolomm": "yolo11n-mm-asymmetric.yaml"}


def _write(run_dir: Path, payload: dict) -> None:
    (run_dir / "worker.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _project_root(run_dir: Path) -> Path:
    """``<project>/training/<run_id>`` → ``<project>``。"""
    return run_dir.parents[1]


def _numbers(raw: dict) -> dict[str, float]:
    values: dict[str, float] = {}
    for source, target in _METRIC_MAP.items():
        value = raw.get(source)
        if isinstance(value, (int, float)):
            values[target] = round(float(value), 6)
    return values


def _classes(data_yaml: Path) -> list[str]:
    try:
        import yaml  # noqa: PLC0415 - 与 datasets 服务同一份解析
    except ImportError:
        return []
    try:
        parsed = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    names = parsed.get("names")
    if isinstance(names, dict):
        return [str(names[key]) for key in sorted(names, key=lambda item: int(item))]
    if isinstance(names, list):
        return [str(item) for item in names]
    return []


def _model_class(family: str):
    from ultralytics import YOLO, YOLOMM  # noqa: PLC0415 - 必须在 vendor 激活后导入

    if family == "yolomm":
        if getattr(YOLOMM, "__name__", "") == "YOLOMM":
            return YOLOMM
        if YOLOMM is None:
            return None
        return YOLOMM
    if family == "yolo":
        return YOLO
    return None


def train(run_dir: Path, spec: dict) -> dict:
    root = _project_root(run_dir)
    family = str(spec.get("family") or "yolo").strip().lower()
    if family not in _SCRATCH_ARCH:
        return {
            "status": "failed",
            "code": "TRAINING_FAMILY_UNSUPPORTED",
            "message": f"不支持的训练架构：{family}（可训练：yolo / yolomm）",
        }
    # 运行时配置由父进程生成并验证过；这里再核一次存在性，避免启动后被删。
    runtime_yaml = run_dir / str(spec.get("runtime_data_yaml") or "data.yaml")
    if not runtime_yaml.is_file():
        runtime_yaml = (root / str(spec.get("runtime_data_yaml") or "data.yaml")).resolve()
    if not runtime_yaml.is_file():
        return {"status": "failed", "code": "DATA_YAML_MISSING", "message": "找不到运行时训练配置 data.yaml"}
    params = dict(spec.get("params") or {})
    base_rel = str(spec.get("base_weight_rel") or "")
    base_weight = (root / base_rel).resolve() if base_rel else None
    if base_weight is not None and not base_weight.is_file():
        return {"status": "failed", "code": "BASE_WEIGHT_MISSING", "message": "父模型权重文件不存在"}

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.vision import runtime as vision_runtime  # noqa: PLC0415

    try:
        vision_runtime.activate_vendor()
    except Exception as exc:  # noqa: BLE001 - 依赖/供应问题要如实回报
        return {
            "status": "failed",
            "code": str(getattr(exc, "code", "VISION_UNAVAILABLE")),
            "message": str(exc)[:500],
        }
    model_cls = _model_class(family)  # activate_vendor 之后导入：走 vendor 版
    if model_cls is None:
        return {
            "status": "failed",
            "code": "TRAINING_FAMILY_UNSUPPORTED",
            "message": "vendor 版 ultralytics 没有可用的 YOLOMM 实现",
        }

    model = model_cls(str(base_weight) if base_weight else _SCRATCH_ARCH[family])
    run_root = run_dir / "runs"
    # 默认关闭 AMP：vendor 的 check_amp() 会在线下载 yolo11n.pt 做基准校验，
    # 离线/受限网络环境下这是隐式依赖，不属于“正常训练”应有的行为。
    amp_requested = str(os.environ.get("HX17_TRAINING_AMP", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    model.train(
        data=str(runtime_yaml),
        epochs=int(params.get("epochs", 10)),
        imgsz=int(params.get("imgsz", 640)),
        batch=int(params.get("batch", 4)),
        lr0=float(params.get("lr0", 0.01)),
        seed=int(params.get("seed", 0)),
        patience=int(params.get("patience", 20)),
        workers=int(params.get("workers", 0)),
        device=str(params.get("device", "cpu")),
        project=str(run_root),
        name="train",
        exist_ok=True,
        plots=False,
        val=True,
        verbose=True,
        amp=amp_requested,
    )

    trainer = getattr(model, "trainer", None)
    best = getattr(trainer, "best", None) if trainer is not None else None
    if not best or not Path(best).is_file():
        return {
            "status": "failed",
            "code": "TRAINING_NO_WEIGHT",
            "message": "训练结束但没有 best.pt（可能是样本为空或全部被跳过）",
        }
    target = run_dir / "best.pt"
    shutil.copyfile(Path(best), target)

    metrics_raw = getattr(trainer, "metrics", None)
    values = _numbers(metrics_raw if isinstance(metrics_raw, dict) else {})
    names = _classes(runtime_yaml)
    val_images = None
    try:
        loader = getattr(trainer, "test_loader", None)
        dataset = getattr(loader, "dataset", None)
        val_images = int(len(dataset)) if dataset is not None else None
    except Exception:  # noqa: BLE001 - 只是展示用，取不到就不写
        val_images = None
    epochs_run = None
    try:
        epochs_run = int(getattr(trainer, "epoch", 0)) + 1
    except Exception:  # noqa: BLE001
        epochs_run = None
    device_note = "cpu" if str(params.get("device", "cpu")) == "cpu" else str(params.get("device"))
    return {
        "status": "succeeded",
        "metrics": values,
        "conditions": dict(spec.get("conditions") or {}),
        "classes": names,
        "split": "val",
        "source": "ultralytics trainer.metrics（同一次训练末尾的验证集评估）",
        "images": val_images,
        "epochs_run": epochs_run,
        "amp": amp_requested,
        "weight_rel": target.resolve().relative_to(root).as_posix(),
        "notes": (
            f"训练子进程产出（{family}，device={device_note}）；指标来自训练结束时的验证集评估，"
            "未做独立测试集复评。类别表以服务端解析权重得到的 names 为准。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="海巡智检 v1.7 训练子进程")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    payload: dict[str, Any]
    try:
        spec = json.loads((run_dir / "params.json").read_text(encoding="utf-8"))
        payload = train(run_dir, spec)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落成可读结果
        payload = {
            "status": "failed",
            "code": "TRAINING_ERROR",
            "message": f"{type(exc).__name__}: {exc}"[:500],
        }
    _write(run_dir, payload)
    return 0 if payload.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
