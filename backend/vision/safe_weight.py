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

白名单是**代码里写死的策略**，不是“权重声明什么就信什么”：

* ``torch.nn.Module`` / ``torch.Tensor`` 的子类（模型结构）；
* 少量纯数据类型（``builtins.set``、``pathlib`` 的路径类、``OrderedDict``）；
* ``ultralytics.nn.mm.router.MultiModalRouter`` 这种只保存配置的数据对象。

函数对象一律拒绝——包括 ``pathlib.Path.write_text``、``os.system``，以及
``torch.storage._load_from_bytes`` 这类“间接再加载一次”的入口。

vendor 版 ultralytics 内部仍然调用 ``torch.load(weights_only=False)``；``install_guard()``
在进程内把这条路径重定向到本模块（不改 vendor 源码）。``safe_globals`` 是**进程级**
状态，所以加载用一把模块级锁串行化，避免多线程互相增删许可项。
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

# 明确许可的纯数据类型（不是 nn.Module，也不会在构造时碰 IO）。
ALLOWED_DATA_TYPES = frozenset(
    {
        "builtins.set",
        "builtins.frozenset",
        "collections.OrderedDict",
        "collections.defaultdict",
        "pathlib.WindowsPath",
        "pathlib.PosixPath",
        "pathlib.PureWindowsPath",
        "pathlib.PurePosixPath",
        "ultralytics.nn.mm.router.MultiModalRouter",
    }
)

# 即使出现在 checkpoint 里也一律拒绝：间接加载/执行类入口。
FORBIDDEN = frozenset(
    {
        "torch.storage._load_from_bytes",
        "torch.load",
        "torch.save",
        "builtins.eval",
        "builtins.exec",
        "builtins.__import__",
    }
)

_load_lock = threading.Lock()


class WeightRejected(RuntimeError):
    """权重被拒绝加载（结构不可读、或声明了未许可的反序列化类型）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _import_symbol(ref: str) -> Any:
    module, _, name = ref.rpartition(".")
    return getattr(importlib.import_module(module), name)


def _is_allowed(ref: str, target: Any) -> bool:
    import torch

    if ref in FORBIDDEN:
        return False
    if not isinstance(target, type):
        # 只许可类型；函数/内建对象一律不放行。
        return False
    if issubclass(target, (torch.nn.Module, torch.Tensor)):
        return True
    return ref in ALLOWED_DATA_TYPES


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
        if ref in FORBIDDEN:
            denied.append(ref)
            continue
        try:
            target = _import_symbol(ref)
        except Exception:
            denied.append(ref)
            continue
        if _is_allowed(ref, target):
            allowed.append(target)
        else:
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
            f"权重声明了未许可的反序列化类型（{preview}{more}）；"
            "只接受标准 YOLO 权重结构，已拒绝加载以避免执行文件里的代码",
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
    "ALLOWED_DATA_TYPES",
    "FORBIDDEN",
    "WeightRejected",
    "allowed_globals",
    "install_guard",
    "load",
]
