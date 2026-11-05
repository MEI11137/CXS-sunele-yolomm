"""YOLO / YOLOMM 推理适配。

来源与许可：``backend/vendor/ultralytics`` 是原项目（CXSDet-YOLOmm）自带的
ultralytics 8.3.163 定制分支，含 ``YOLOMM`` 双模态实现；按 AGPL-3.0 原样复用，
未在本工程内修改其源码。见 ``THIRD_PARTY_NOTICES.md``。

必须遵守的边界：

* **禁止静默回退**到 pip 安装的 ultralytics：vendor 不在 sys.path 或加载到的不是
  vendor 版本时直接报错。否则同一个权重会在两套实现上跑出不同结果，且没人知道。
* **缺什么说什么**：缺权重、缺依赖、缺 X 模态都给出稳定错误码，绝不零填充、
  绝不返回伪造 bbox、绝不自动下载模型。
* 缓存与 matplotlib/YOLO 配置都在**项目运行目录**（``var/runtime``）里，
  不往用户主目录或源码目录写文件。
* 假 detector 只能由测试注入（模块级工厂），产品没有“假成功”开关。
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor"
VENDOR_PACKAGE = VENDOR_ROOT / "ultralytics"

_lock = threading.RLock()
_detectors: dict[tuple[str, str, float], Any] = {}
# 只能由测试替换；产品代码里没有任何入口能设置它。
_DETECTOR_FACTORY: Callable[..., Any] | None = None
_vendor_ready = False
_runtime_root: Path | None = None


class VisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DetectionResult:
    boxes: list[dict] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    image_width: int = 0
    image_height: int = 0
    runtime: str = ""


def configure(settings) -> None:
    """把 YOLO/Matplotlib 的**进程级**运行配置固定到服务端私有目录。

    这里区分两类东西，不要混：

    * **服务私有运行数据**：ultralytics 的 `settings.json`、matplotlib 字体缓存。
      它们是本进程的配置与缓存，不是业务产物，放 ``<data_dir>/runtime``
      （已在私有根列表里，业务接口和文件工具都读不到/写不进）。
    * **业务可交付产物**：edge 派生图、结果图、数据集副本。它们必须落在
      **当前项目绑定目录**内（``.cache/edge`` / ``artifacts/<record>/`` / ``datasets/<id>``），
      由 :mod:`backend.services.storage` 负责，与这里无关。

    用直接赋值而不是 ``os.environ.setdefault``：``setdefault`` 会沿用开发机上
    已经存在的 ``YOLO_CONFIG_DIR``/``MPLCONFIGDIR``，导致产物散落到源码目录或用户主目录，
    换台机器就找不到。这里每次都显式覆盖，并且不允许自动安装依赖。
    """
    global _vendor_ready, _runtime_root
    root = Path(settings.data_dir) / "runtime"
    with _lock:
        if _vendor_ready and _runtime_root == root:
            return
        (root / "ultralytics").mkdir(parents=True, exist_ok=True)
        (root / "matplotlib").mkdir(parents=True, exist_ok=True)
        os.environ["YOLO_CONFIG_DIR"] = str(root / "ultralytics")
        os.environ["MPLCONFIGDIR"] = str(root / "matplotlib")
        os.environ["YOLO_AUTOINSTALL"] = "false"
        _runtime_root = root
        _vendor_ready = True


def activate_vendor() -> str:
    """把 vendor 放到 sys.path 最前面，并确认 ``ultralytics`` 真的来自 vendor。

    这里故意不做“找不到就用 pip 版”的降级：两套实现的 YOLOMM 行为不同，
    静默替换会让“同一个权重”在不同机器上给出不同结果。
    """
    if not VENDOR_PACKAGE.is_dir():
        raise VisionError(
            "VISION_VENDOR_MISSING",
            "项目内缺少 vendor/ultralytics（定制 YOLOMM 组件），无法做视觉推理",
        )
    text = str(VENDOR_ROOT)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)
    try:
        import ultralytics  # noqa: PLC0415 - 必须在 sys.path 调整之后导入
    except Exception as exc:  # pragma: no cover - 依赖缺失时才走到
        raise VisionError("VISION_DEPENDENCY_MISSING", f"视觉依赖不可用：{type(exc).__name__}") from exc
    loaded = Path(str(getattr(ultralytics, "__file__", ""))).resolve()
    if VENDOR_PACKAGE.resolve() not in loaded.parents:
        raise VisionError(
            "VISION_VENDOR_SHADOWED",
            "加载到的 ultralytics 不是本项目 vendor 版本，已拒绝使用（不允许静默回退）",
        )
    # vendor 的权重加载走 torch.load(weights_only=False)；在进程内挂上白名单闸门，
    # 让“推理时加载权重”和“导入时检查权重”用同一套反序列化许可。
    from backend.vision import safe_weight  # noqa: PLC0415 - 与 runtime 同包，延迟导入避免循环

    safe_weight.install_guard()
    return str(loaded)


def clear_cache() -> None:
    with _lock:
        _detectors.clear()


def _detector(weight_path: Path, model_mode: str):
    """按 (路径, 模式, mtime) 缓存 detector；进程内只加载一次。"""
    key = (str(weight_path.resolve()), str(model_mode), weight_path.stat().st_mtime)
    with _lock:
        cached = _detectors.get(key)
        if cached is not None:
            return cached
    if _DETECTOR_FACTORY is not None:  # 测试注入
        detector = _DETECTOR_FACTORY(str(weight_path), str(model_mode))
        with _lock:
            _detectors[key] = detector
        return detector

    activate_vendor()
    from backend.vision import safe_weight  # noqa: PLC0415

    if not weight_path.is_file():
        raise VisionError("VISION_WEIGHT_MISSING", "模型权重文件不存在")
    import ultralytics  # noqa: PLC0415

    try:
        if str(model_mode) == "multimodal":
            if getattr(ultralytics, "YOLOMM", None) is None:
                raise VisionError("VISION_MODEL_UNSUPPORTED", "vendor 版 ultralytics 不含 YOLOMM")
            detector = ultralytics.YOLOMM(str(weight_path))
        else:
            detector = ultralytics.YOLO(str(weight_path))
    except VisionError:
        raise
    except safe_weight.WeightRejected as exc:
        raise VisionError(
            "VISION_WEIGHT_UNTRUSTED" if exc.code == "WEIGHT_UNTRUSTED" else "VISION_WEIGHT_INVALID",
            exc.message,
        ) from exc
    except Exception as exc:
        raise VisionError(
            "VISION_LOAD_FAILED", f"权重无法加载（{type(exc).__name__}: {exc}）"
        ) from exc
    with _lock:
        _detectors[key] = detector
    return detector


def inspect_weight(weight_path: Path) -> dict:
    """读取权重的类别表与规模；读不出来就明确失败（导入时用）。

    反序列化走 :mod:`backend.vision.safe_weight`：先静态解释 pickle 引用并逐个判定，
    再带白名单闸门加载。**不会**为了“能读出来”而回退到 ``weights_only=False`` 的裸加载。
    """
    if not weight_path.is_file():
        raise VisionError("VISION_WEIGHT_MISSING", "权重文件不存在")
    if weight_path.stat().st_size == 0:
        raise VisionError("VISION_WEIGHT_EMPTY", "权重文件是空的")
    activate_vendor()
    from backend.vision import safe_weight  # noqa: PLC0415

    try:
        import torch  # noqa: PLC0415,F401 - 只用于确认依赖存在
    except Exception as exc:  # pragma: no cover
        raise VisionError("VISION_DEPENDENCY_MISSING", "缺少 torch，无法校验权重") from exc
    try:
        checkpoint = safe_weight.load(str(weight_path), map_location="cpu")
    except safe_weight.WeightRejected as exc:
        code = (
            "VISION_WEIGHT_UNTRUSTED"
            if exc.code == "WEIGHT_UNTRUSTED"
            else "VISION_WEIGHT_INVALID"
        )
        raise VisionError(code, exc.message) from exc
    if not isinstance(checkpoint, dict):
        raise VisionError("VISION_WEIGHT_INVALID", "权重文件结构不是 PyTorch checkpoint")
    model = checkpoint.get("model")
    names = getattr(model, "names", None) or checkpoint.get("names") or {}
    if isinstance(names, list):
        names = {index: str(name) for index, name in enumerate(names)}
    if not isinstance(names, dict) or not names:
        raise VisionError(
            "VISION_CLASSES_UNKNOWN",
            "权重里没有类别表，无法确定它输出的是什么类别；请在导入时显式提供类别映射",
        )
    clean = {int(index): str(name) for index, name in names.items()}
    return {
        "classes": [{"index": index, "name": name} for index, name in sorted(clean.items())],
        "class_count": len(clean),
        "kind": type(model).__name__ if model is not None else "unknown",
        "multimodal": type(model).__name__.lower().startswith("mm")
        or bool(getattr(model, "multimodal", False)),
    }


@dataclass
class ResolvedInputs:
    rgb_path: str
    x_path: str | None
    x_origin: str


def resolve_inputs(
    *,
    model_mode: str,
    secondary_modality: str | None,
    rgb_path: Path,
    x_path: Path | None,
    edge_factory: Callable[[Path], Path] | None = None,
) -> ResolvedInputs:
    """决定这次推理到底喂哪两张图；缺 X 不猜、不造假。"""
    if not rgb_path.is_file():
        raise VisionError("VISION_RGB_MISSING", "RGB 图片不存在")
    if str(model_mode) != "multimodal":
        return ResolvedInputs(str(rgb_path), None, "none")

    secondary = str(secondary_modality or "").strip().lower()
    if not secondary:
        raise VisionError("VISION_MODALITY_UNSET", "多模态模型没有声明第二模态")
    if x_path is not None:
        if not x_path.is_file():
            raise VisionError("VISION_X_MISSING", f"{secondary} 模态图片不存在")
        return ResolvedInputs(str(rgb_path), str(x_path), "uploaded")
    if secondary == "edge" and edge_factory is not None:
        return ResolvedInputs(str(rgb_path), str(edge_factory(rgb_path)), "derived")
    raise VisionError(
        "VISION_X_REQUIRED",
        f"这个模型需要 {secondary} 模态图片；系统不会用零图或 RGB 冒充",
    )


def read_image(path: Path):
    """Windows 下 OpenCV 读不了部分中文路径：先读字节再 decode。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    try:
        buffer = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def predict(inputs: ResolvedInputs, *, weight_path: Path, model_mode: str, confidence: float) -> DetectionResult:
    detector = _detector(Path(weight_path), str(model_mode))
    try:
        if str(model_mode) == "multimodal":
            results = detector.predict(
                rgb_source=inputs.rgb_path,
                x_source=inputs.x_path,
                conf=float(confidence),
                save=False,
                verbose=False,
            )
        else:
            results = detector.predict(
                source=inputs.rgb_path,
                conf=float(confidence),
                save=False,
                verbose=False,
            )
    except VisionError:
        raise
    except Exception as exc:
        raise VisionError(
            "VISION_INFERENCE_FAILED", f"推理失败（{type(exc).__name__}: {exc}）"
        ) from exc
    return _normalize(results, inputs, model_mode)


def _normalize(results: Any, inputs: ResolvedInputs, model_mode: str) -> DetectionResult:
    """兼容 Tensor boxes 与 YOLOMM 的 Nx6 ndarray 两种结果形态。"""
    listing = list(results or [])
    output = DetectionResult(runtime=f"vendor-yolomm-{model_mode}")
    image = read_image(Path(inputs.rgb_path))
    if image is not None:
        output.image_height, output.image_width = int(image.shape[0]), int(image.shape[1])
    for result in listing:
        names = getattr(result, "names", None) or {}
        for index, name in dict(names).items():
            output.names[int(index)] = str(name)
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        rows = _rows(boxes)
        for coords, score, class_id in rows:
            if len(coords) != 4:
                continue
            output.boxes.append(
                {
                    "class_id": int(class_id),
                    "class_name": output.names.get(int(class_id), str(int(class_id))),
                    "confidence": round(float(score), 6),
                    "bbox": [round(float(value), 3) for value in coords],
                }
            )
    output.boxes.sort(key=lambda item: item["confidence"], reverse=True)
    if not output.image_width or not output.image_height:
        # 读图失败时不编造尺寸：交给上层按“无尺寸”处理。
        output.image_width = 0
        output.image_height = 0
    return output


def _rows(boxes: Any) -> list[tuple[list[float], float, int]]:
    # 形态一：ultralytics 的 Boxes 对象（torch Tensor）
    if hasattr(boxes, "xyxy") and hasattr(boxes, "conf"):
        xyxy = boxes.xyxy.detach().cpu().tolist()
        conf = boxes.conf.detach().cpu().tolist()
        cls = boxes.cls.detach().cpu().tolist()
        return [(list(map(float, row)), float(score), int(label)) for row, score, label in zip(xyxy, conf, cls)]
    # 形态二：YOLOMM 的 MultiModalResults —— Nx6 ndarray：x1,y1,x2,y2,conf,cls
    rows: list[tuple[list[float], float, int]] = []
    raw = boxes.tolist() if hasattr(boxes, "tolist") else list(boxes)
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        rows.append(([float(value) for value in row[:4]], float(row[4]), int(row[5])))
    return rows
