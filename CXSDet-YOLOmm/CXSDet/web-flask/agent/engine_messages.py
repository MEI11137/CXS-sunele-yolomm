"""区分海巡引擎的公开回答与私有上下文压缩消息。

引擎把上下文压缩写成一条 ``role=assistant`` 的私有消息，并带有结构化元数据：
``info.summary is True``，或 ``info.agent`` / ``info.mode`` 为 ``compaction``。
它不是给用户的回答，也不是本轮的最终答复。这里只按结构化字段判断，绝不按正文
关键词猜测，避免误删正常的助手回答。
"""
from __future__ import annotations

from typing import Any, Dict

_INTERNAL_AGENTS = {"compaction"}
_INTERNAL_MODES = {"compaction"}


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def is_internal_info(info: Dict[str, Any] | None) -> bool:
    """按引擎的结构化元数据判断这条消息是否是内部压缩。"""
    if not isinstance(info, dict):
        return False
    if info.get("summary") is True:
        return True
    return _text(info.get("agent")) in _INTERNAL_AGENTS or _text(info.get("mode")) in _INTERNAL_MODES


def is_internal_row(row: Dict[str, Any] | None) -> bool:
    return is_internal_info((row or {}).get("info"))


def is_aborted_info(info: Dict[str, Any] | None) -> bool:
    """压缩或用户中止会产生 MessageAbortedError，不能当成模型失败或完成。"""
    if not isinstance(info, dict):
        return False
    error = info.get("error")
    if not error:
        return False
    if isinstance(error, dict):
        values = [error.get("name"), error.get("type"), error.get("message")]
    else:
        values = [error]
    for value in values:
        text = _text(value)
        if "messageabortederror" in text or text == "aborted":
            return True
    return False


def public_text(row: Dict[str, Any] | None) -> str:
    """只取用户可见正文：text 类型、非 synthetic；reasoning 与工具过程不算回答。

    即使调用方漏过滤，结构化内部压缩也不能因为 parts 里有 text 就被当成回答。
    """
    if is_internal_row(row):
        return ""
    parts = (row or {}).get("parts") or []
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and not part.get("synthetic")
    ]
    return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()