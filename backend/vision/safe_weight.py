"""用 PyTorch **原生**受限加载器读权重，不自己实现 pickle 解释器。

背景：``torch.load(..., weights_only=False)`` 会执行 pickle 里的任意可调用对象。
从 2.6 起 ``torch.load`` 默认 ``weights_only=True``，只允许张量与一批内置的
重建函数；自定义类（YOLO 的 ``DetectionModel``、各 ``nn.Module``）需要在
``torch.serialization.safe_globals([...])`` 上下文里**显式**许可。

因此这里的流程是：

1. ``torch.serialization.get_unsafe_globals_in_checkpoint(path)`` 列出该权重
   声明的所有非内置 globals（这一步只读元数据，不执行）；
2. 逐个判定是否属于「已知安全类型」；有任何一个不在白名单里就直接拒绝，
   **不加载**；
3. 把通过判定的类型放进 ``safe_globals`` 上下文，用原生
   ``torch.load(..., weights_only=True)`` 加载。

白名单是**代码里写死的符号表**，不是“权重声明什么就信什么”，也不是
“import 后看是不是某个基类的子类”。表外符号连模块导入都不会发生；要支持新架构，
必须先人工审核符号并补测试。函数对象与 ``torch.storage._load_from_bytes`` 这类
“间接再加载一次”的入口一律拒绝。

vendor 版 ultralytics 内部仍然调用 ``torch.load(weights_only=False)``；``install_guard()``
在进程内把这条路径重定向到本模块（不改 vendor 源码）。``safe_globals`` 是**进程级**
状态，所以加载用一把模块级锁串行化，避免多线程互相增删许可项。
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

# **审核过的符号表**：只有出现在这里的全名才允许 import 与交给 safe_globals。
#
# 这张表是“导入前”的唯一判定依据：不可信 checkpoint 不能通过声明某个符号来诱导
# 我们 import 任意模块。需要支持新架构时，先人工审核该符号（来源、类型、是否
# 只做数据/结构、是否可能在构造时触达 IO），再把它加进这里并补一个测试。
#
# 前 17 项来自真实 best.pt 的实际引用（见 WB17-03 原生受限加载试验 JSON）；
# 其余是按本工程已支持架构人工补充的常见结构类，均为 nn.Module 子类或纯数据类型。
ALLOWED_SYMBOLS: dict[str, str] = {
    # —— torch 结构/张量容器 ——
    "torch.nn.modules.activation.SiLU": "激活层",
    "torch.nn.modules.batchnorm.BatchNorm2d": "归一化层",
    "torch.nn.modules.container.Sequential": "顺序容器",
    "torch.nn.modules.container.ModuleList": "模块列表",
    "torch.nn.modules.conv.Conv2d": "卷积层",
    "torch.nn.modules.linear.Linear": "全连接层",
    "torch.nn.modules.upsampling.Upsample": "上采样",
    "torch.Size": "张量形状（纯数据）",
    # —— ultralytics 结构 ——
    "ultralytics.nn.tasks.DetectionModel": "检测模型根对象",
    "ultralytics.nn.modules.conv.Conv": "Conv 组合块",
    "ultralytics.nn.modules.conv.Concat": "特征拼接",
    "ultralytics.nn.modules.conv.DWConv": "深度可分离卷积",
    "ultralytics.nn.modules.block.C3k2": "C3k2 块",
    "ultralytics.nn.modules.block.C3k": "C3k 块",
    "ultralytics.nn.modules.block.Bottleneck": "Bottleneck 块",
    "ultralytics.nn.modules.block.DFL": "分布焦点损失层",
    "ultralytics.nn.modules.block.C2f": "C2f 块（v8/v11 常用）",
    "ultralytics.nn.modules.block.SPPF": "SPPF 池化块（v8/v11 常用）",
    "ultralytics.nn.modules.head.Detect": "检测头",
    "ultralytics.nn.mm.router.MultiModalRouter": "双模态路由参数（纯数据）",
    # —— 纯数据类型 ——
    "builtins.set": "集合",
    "collections.OrderedDict": "有序字典",
    "pathlib.WindowsPath": "Windows 路径对象（纯数据）",
    "pathlib.PosixPath": "POSIX 路径对象（纯数据）",
}

# 即使出现在 checkpoint 里也一律拒绝：间接加载/执行类入口。
FORBIDDEN = frozenset(
    {
        "torch.storage._load_from_bytes",
        "torch.load",
        "torch.save",
        "builtins.eval",
        "builtins.exec",
        "builtins.__import__",
        "os.system",
        "subprocess.Popen",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
    }
)

_load_lock = threading.Lock()


class WeightRejected(RuntimeError):
    """权重被拒绝加载（结构不可读、或声明了未许可的反序列化类型）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_approved(ref: str) -> Any:
    """只解析**表内**符号；表外引用连 import 都不会发生。"""
    if ref in FORBIDDEN or ref not in ALLOWED_SYMBOLS:
        raise WeightRejected(
            "WEIGHT_UNTRUSTED",
            f"权重引用了未审核的符号：{ref}。只允许 backend/vision/safe_weight.py "
            "ALLOWED_SYMBOLS 中人工审核过的类型；若要支持新架构，请先审核该符号并补测试。",
        )
    module, _, name = ref.rpartition(".")
    return getattr(importlib.import_module(module), name)


def allowed_globals(path: Path) -> tuple[list[Any], list[str]]:
    """返回 ``(可许可的类型对象, 被拒绝的引用)``；只读 checkpoint 元数据。"""
    import torch

    try:
        refs = torch.serialization.get_unsafe_globals_in_checkpoint(str(path))
    except Exception as exc:
        raise WeightRejected(
            "WEIGHT_UNVERIFIABLE",
            f"无法读取权重的元数据（{type(exc).__name__}），不是可用的 PyTorch 权重",
        ) from exc
    allowed: list[Any] = []
    denied: list[str] = []
    for ref in refs:
        # 先判表、再 import：未知符号不会触发任何模块导入。
        if ref in FORBIDDEN or ref not in ALLOWED_SYMBOLS:
            denied.append(ref)
            continue
        try:
            allowed.append(_resolve_approved(ref))
        except WeightRejected:
            denied.append(ref)
        except Exception:
            denied.append(ref)
    return allowed, denied


def load(path: str | Path, *, map_location: Any = "cpu") -> Any:
    """原生受限加载：``weights_only=True`` + 显式 ``safe_globals``。

    没有“先试安全、失败就退回任意 pickle”的分支——退回等于把闸门交给文件。
    """
    import torch

    source = Path(path)
    allowed, denied = allowed_globals(source)
    if denied:
        preview = "、".join(denied[:5])
        more = f" 等 {len(denied)} 项" if len(denied) > 5 else ""
        raise WeightRejected(
            "WEIGHT_UNTRUSTED",
            f"权重声明了未审核的反序列化类型（{preview}{more}）；已拒绝加载。"
            "需要支持新架构时，请审核这些符号并加入 backend/vision/safe_weight.py 的 ALLOWED_SYMBOLS。",
        )
    with _load_lock:
        try:
            with torch.serialization.safe_globals(allowed):
                return torch.load(source, map_location=map_location, weights_only=True)
        except WeightRejected:
            raise
        except Exception as exc:
            raise WeightRejected(
                "WEIGHT_UNREADABLE", f"权重无法加载（{type(exc).__name__}: {exc}）"
            ) from exc


def install_guard() -> None:
    """把 vendor 的 ``torch.load(weights_only=False)`` 重定向到原生受限加载。

    范围：只接管**显式**要求 ``weights_only=False`` 的调用（即会执行 pickle 的那条路）。
    ``weights_only=True`` 或未指定（走 PyTorch 默认）的调用原样放行，不影响无关加载。
    显式传入 ``pickle_module`` 的调用也放行（那是调用方自带的反序列化器，
    本工程没有任何地方这样用）。
    """
    import torch

    current = torch.load
    if getattr(current, "__hx17_guarded__", False):
        return
    original = getattr(current, "__wrapped__", current)

    def guarded(source, *args, **kwargs):
        if kwargs.get("weights_only") is False and "pickle_module" not in kwargs:
            map_location = kwargs.get("map_location", "cpu")
            if args:
                map_location = args[0]
            return load(source, map_location=map_location)
        return original(source, *args, **kwargs)

    guarded.__hx17_guarded__ = True  # type: ignore[attr-defined]
    guarded.__wrapped__ = original  # type: ignore[attr-defined]
    torch.load = guarded


__all__ = [
    "ALLOWED_SYMBOLS",
    "FORBIDDEN",
    "WeightRejected",
    "allowed_globals",
    "install_guard",
    "load",
]
