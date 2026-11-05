"""本项目自带的视觉运行时（vendor 版 ultralytics）。

只做三件事：装载本项目 vendor 里的定制 ultralytics、按模态跑一次推理、
把结果规范化成可落库的字典。不训练、不下载、不自动换模型。
"""

from backend.vision.runtime import (
    DetectionResult,
    VisionError,
    clear_cache,
    inspect_weight,
    predict,
    resolve_inputs,
    activate_vendor,
)

__all__ = [
    "DetectionResult",
    "VisionError",
    "activate_vendor",
    "clear_cache",
    "inspect_weight",
    "predict",
    "resolve_inputs",
]
