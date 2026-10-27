"""上下文组装（B-04）。

固定基础规则 + 项目要求 + 匹配手册 + 交接笔记 + 近期对话 + 已引用资源。
按字符预算裁剪：先丢最早的对话，再截断笔记，始终保留基础规则与最后一条用户消息。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent import manuals as manual_store
from agent import memory as memory_store
from agent import sessions as session_store
from agent.config import settings
from agent.prompts import BASE_RULES, context_notice
from agent.store import dumps

_TOOL_RESULT_PREFIX = "【工具结果】"


def project_block(project: Dict[str, Any]) -> str:
    requirements = project.get("requirements") or {}
    lines = [
        "【当前项目】",
        f"- 名称：{project.get('name')}",
        f"- 说明：{project.get('description') or '（未填写）'}",
    ]
    if requirements:
        lines.append("- 项目要求（用户显式配置，不能扩大权限或跳过确认）：")
        for key, value in requirements.items():
            lines.append(f"  · {key}：{value}")
    else:
        lines.append("- 项目要求：暂无")
    return "\n".join(lines)


def resources_block(resources: List[Dict[str, Any]]) -> str:
    if not resources:
        return "【已引用资源】（无）"
    lines = ["【已引用资源】（名称仅代表引用，不代表已读取内容）"]
    for resource in resources[:20]:
        lines.append(
            f"- {resource.get('kind')}｜{resource.get('display_name')}"
            f"｜项目 {resource.get('project_id')}"
            f"｜版本 {resource.get('revision')}"
            f"｜id={resource.get('id')}"
        )
    if len(resources) > 20:
        lines.append(f"- …另有 {len(resources) - 20} 项未列出")
    return "\n".join(lines)


def tool_result_text(tool_name: str, result: Dict[str, Any]) -> str:
    """把持久化的工具结果转成模型可读文本（不使用 role=tool，避免协议限制）。"""
    return (
        f"{_TOOL_RESULT_PREFIX}{tool_name}\n"
        f"{dumps(result)}\n"
        "（上述为程序记录的真实结果；若状态不是 succeeded，不要描述成已完成。）"
    )


def build_system_prompt(
    project: Dict[str, Any],
    manual: Optional[Dict[str, Any]],
    note: Optional[Dict[str, Any]],
    resources: Optional[List[Dict[str, Any]]] = None,
) -> str:
    parts = [
        BASE_RULES,
        project_block(project),
        manual_store.manual_prompt(manual),
        memory_store.note_prompt(note),
        resources_block(resources or []),
        context_notice(),
    ]
    return "\n\n".join(part for part in parts if part)


def build_messages(
    session: Dict[str, Any],
    task: Optional[Dict[str, Any]],
    project: Dict[str, Any],
    *,
    manual: Optional[Dict[str, Any]] = None,
    note: Optional[Dict[str, Any]] = None,
    resources: Optional[List[Dict[str, Any]]] = None,
    extra_user_message: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """组装一次模型调用所需的 messages。"""
    cfg = settings()
    system_prompt = build_system_prompt(project, manual, note, resources)

    history: List[Dict[str, str]] = []
    recent = session_store.list_messages(session["id"], limit=cfg.recent_message_limit)
    for message in recent:
        role = message["role"]
        if role == "user":
            history.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            history.append({"role": "assistant", "content": message["content"] or "（无文字）"})
        elif role == "tool":
            history.append(
                {
                    "role": "user",
                    "content": tool_result_text(
                        message.get("tool_name") or "tool",
                        {"content": message["content"]},
                    ),
                }
            )
        # system_event 不进入模型对话

    if extra_user_message:
        history.append({"role": "user", "content": extra_user_message})

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(_trim(history, cfg.context_max_chars - len(system_prompt)))
    return messages


def _trim(history: List[Dict[str, str]], budget: int) -> List[Dict[str, str]]:
    """从最早的对话开始丢，保证最后一条用户消息一定保留。"""
    if budget <= 0:
        return history[-1:] if history else []
    total = sum(len(item.get("content") or "") for item in history)
    trimmed = list(history)
    while total > budget and len(trimmed) > 1:
        removed = trimmed.pop(0)
        total -= len(removed.get("content") or "")
    if trimmed and total > budget:
        last = trimmed[-1]
        keep = max(500, budget)
        trimmed[-1] = {
            **last,
            "content": str(last.get("content"))[-keep:],
        }
    return trimmed


def retrieval_hint(task: Optional[Dict[str, Any]]) -> str:
    """提示模型"还缺什么"，避免一次索要全部材料。"""
    if not task:
        return "先了解当前项目概况，再决定需要读取哪些具体内容。"
    waiting = task.get("waiting_for") or {}
    if waiting.get("kind"):
        return f"当前任务正在等待：{waiting.get('reason')}，不要重复提交同一动作。"
    pending = [s for s in task.get("steps", []) if s.get("status") not in ("done", "skipped")]
    if pending:
        return f"下一步待执行：{pending[0].get('title')}"
    return "按用户目标继续推进。"
