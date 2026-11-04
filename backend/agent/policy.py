"""权限模式与风险分级。

三种模式由**用户本人在界面上选择**并落库，模型无法修改（工具里没有任何“设置模式”的能力）：

* ``request``：只读工具直接执行；任何写入都要生成确认卡，用户点确认才落盘。
* ``assist``：常规写入自动执行；删除、覆盖、跨目录移动等高风险动作仍需确认。
* ``full``：已授权范围内的内置业务与受控文件动作直接执行并审计，不出计划卡，
  也不逐个文件确认。

``full`` **不扩大**任何边界：目录授权、账号、他人数据、人工结论、外部 MCP 授权都不在
工具清单里，因而无法借 ``full`` 绕过。
"""

from __future__ import annotations

from dataclasses import dataclass

MODES: tuple[str, ...] = ("request", "assist", "full")
DEFAULT_MODE = "request"

MODE_LABELS: dict[str, str] = {
    "request": "请求批准",
    "assist": "帮我批准",
    "full": "完全访问",
}

MODE_HINTS: dict[str, str] = {
    "request": "读取类工具直接执行；任何写入、移动、删除都会先给你一张确认卡，点确认才执行。",
    "assist": "常规写入自动执行；删除、覆盖和跨目录移动仍需你确认。",
    "full": "只在你已授权的目录内读写并记录审计；不会扩大目录授权，也不会替你下人工结论。",
}

RISK_READ = "read"
RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"

DECISION_AUTO = "auto"
DECISION_CONFIRM = "confirm"
DECISION_DENY = "deny"


@dataclass(frozen=True)
class Policy:
    mode: str
    decision: str
    reason: str

    @property
    def requires_confirmation(self) -> bool:
        return self.decision == DECISION_CONFIRM

    @property
    def allowed(self) -> bool:
        return self.decision != DECISION_DENY


def normalize_mode(raw: object, fallback: str = DEFAULT_MODE) -> str:
    text = str(raw or "").strip().lower()
    return text if text in MODES else fallback


def decide(mode: str, risk: str) -> Policy:
    """把（模式，风险）映射成执行策略。未知风险一律按最高风险处理。"""
    current = normalize_mode(mode)
    level = risk if risk in {RISK_READ, RISK_WRITE, RISK_DESTRUCTIVE} else RISK_DESTRUCTIVE

    if level == RISK_READ:
        return Policy(current, DECISION_AUTO, "只读工具")
    if current == "full":
        return Policy(current, DECISION_AUTO, "完全访问：授权范围内直接执行并审计")
    if current == "assist":
        if level == RISK_WRITE:
            return Policy(current, DECISION_AUTO, "常规写入：自动执行")
        return Policy(current, DECISION_CONFIRM, "高风险动作：需要确认")
    return Policy(current, DECISION_CONFIRM, "请求确认模式：写入需要确认")
