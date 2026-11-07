"""测试专用：造一份**真实可校验**的最小 YOLO 权重。

背景：``{'names': {...}}`` 这种只有类别表的字典不是可运行权重。WB17-05 起
``backend.vision.runtime.inspect_weight`` 要求 checkpoint 里真的有模型对象
（``model`` / ``ema``）且是 vendor 支持的架构，所以测试夹具不能再拿 names 字典
冒充权重。

这里用一个极小的自定义结构（3 层 backbone + Detect 头，两个类别）即时构建
``DetectionModel`` 并序列化，约 250KB，构建一次缓存；不联网、不依赖本机已有大权重、
不跑训练。构建出来的是**真的模型对象**，走的是生产同一条安全加载与架构校验。
"""

from __future__ import annotations

import copy
import io
from pathlib import Path

# 与训练/检测用例里的数据集类别保持一致（索引 → 名称）。
NAMES: dict[int, str] = {0: "单一热斑", 1: "大面积热斑"}

_TINY_CFG: dict = {
    "nc": len(NAMES),
    "names": dict(NAMES),
    "backbone": [
        [-1, 1, "Conv", [8, 3, 2]],
        [-1, 1, "Conv", [16, 3, 2]],
        [-1, 1, "SPPF", [16, 5]],
    ],
    "head": [[[-1], 1, "Detect", ["nc"]]],
}

# 双模态（YOLOMM）最小结构：与真实训练产物同一套表达——两条分支分别标 RGB / X，
# 由 Concat 融合，YAML 里留下 2 处 5 字段路由注解，metadata 写明 X 模态。
# 单模态权重**没有**这些，所以它不能冒充双模态；反过来也不能因为类名是
# DetectionModel 就把真实双模态权重当成单模态（真实权重反序列化后同样是这个类）。
_MM_TINY_CFG: dict = {
    "nc": len(NAMES),
    "names": dict(NAMES),
    "backbone": [
        [-1, 1, "Conv", [8, 3, 2], "RGB"],
        [-1, 1, "Conv", [16, 3, 2]],
        [-1, 1, "Conv", [8, 3, 2], "X"],
        [-1, 1, "Conv", [16, 3, 2]],
        [[1, 3], 1, "Concat", [1]],
        [-1, 1, "Conv", [32, 3, 2]],
    ],
    "head": [[[-1], 1, "Detect", ["nc"]]],
}

_MM_METADATA = {"x_modality_name": "ir", "primary_modality_name": "rgb"}

_TEMPLATE: bytes | None = None
_MM_TEMPLATE: bytes | None = None


def real_weight_bytes() -> bytes:
    """返回一份合法 checkpoint 的字节（构建一次后缓存）。"""
    global _TEMPLATE
    if _TEMPLATE is not None:
        return _TEMPLATE
    from backend.vision import runtime as vision_runtime  # noqa: PLC0415 - 延迟导入，避免测试收集期加载 torch

    # vendor 必须先于任何 ultralytics 导入被放到 sys.path 首位，否则可能拿到 pip 版。
    vision_runtime.activate_vendor()
    import torch  # noqa: PLC0415
    from ultralytics.nn.tasks import DetectionModel  # noqa: PLC0415

    model = DetectionModel(copy.deepcopy(_TINY_CFG), ch=3, nc=len(NAMES), verbose=False)
    model.names = dict(NAMES)
    buffer = io.BytesIO()
    torch.save({"model": model, "names": model.names}, buffer)
    _TEMPLATE = buffer.getvalue()
    return _TEMPLATE


def write_real_weight(target: Path) -> Path:
    """把合法权重写到 ``target``（父目录不存在就建）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(real_weight_bytes())
    return target


def mm_weight_bytes() -> bytes:
    """双模态（YOLOMM）合法 checkpoint：有真实路由注解与 X 模态 metadata。"""
    global _MM_TEMPLATE
    if _MM_TEMPLATE is not None:
        return _MM_TEMPLATE
    from backend.vision import runtime as vision_runtime  # noqa: PLC0415

    vision_runtime.activate_vendor()
    import torch  # noqa: PLC0415
    from ultralytics.nn.tasks import DetectionModel  # noqa: PLC0415

    model = DetectionModel(copy.deepcopy(_MM_TINY_CFG), ch=3, nc=len(NAMES), verbose=False)
    model.names = dict(NAMES)
    model.metadata = dict(_MM_METADATA)
    buffer = io.BytesIO()
    torch.save({"model": model, "names": model.names}, buffer)
    _MM_TEMPLATE = buffer.getvalue()
    return _MM_TEMPLATE


def write_mm_weight(target: Path) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(mm_weight_bytes())
    return target
