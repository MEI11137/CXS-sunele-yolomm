"""身份、连接指纹与**唯一**模型选择。

引擎、网关、运行器三方都用同一套算法判断“这条轮次属于谁、连着哪份配置”：

* ``actor`` 一律从数据库反查，绝不接受模型传入的 user/session；
* ``fingerprint`` 覆盖 provider / base_url / model / key 指纹，任一变化即视为换连接，
  旧引擎与旧轮次随之失效（不静默沿用旧配置）；
* 模型选择只有一个出口 ``resolve()``：偏好里选的模型必须仍在个人连接可用范围内，
  否则回落到连接里保存的模型。**不允许**出现“界面说用 A、后台其实用 B”的两套选择；
* 视觉能力是**针对具体模型**的结论：只有检查连接时对**当前模型**做过图像挑战，
  才允许给引擎声明 ``image`` 输入。换模型后旧证据立即失效（保守地按无视觉处理）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from backend.agent import store
from backend.agent.policy import normalize_mode


def connection_fingerprint(row: sqlite3.Row | dict | None) -> str:
    if row is None:
        return ""
    data = dict(row)
    raw = "|".join(
        [
            str(data.get("provider") or ""),
            str(data.get("base_url") or ""),
            str(data.get("model") or ""),
            str(data.get("key_fingerprint") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def connection_for(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM connections WHERE user_id = ?", (int(user_id),)
    ).fetchone()


def effective_mode(conn: sqlite3.Connection, user_id: int) -> str:
    """权限模式只认本人保存的偏好，默认最保守的 request。"""
    return normalize_mode(store.prefs(conn, user_id).get("mode"), "request")


def available_models(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """可选的模型 ID：个人连接里的模型 + 该 provider 的预设模型（去重保序）。"""
    from backend.services import connections as connection_service

    row = connection_for(conn, user_id)
    if row is None:
        return []
    models = [str(row["model"])]
    for preset in connection_service.PROVIDERS:
        if preset["id"] == row["provider"]:
            for name in preset.get("models") or []:
                if name not in models:
                    models.append(name)
    return models


@dataclass(frozen=True)
class Selection:
    """一次对话真正会用的那套配置。"""

    connection: sqlite3.Row
    provider: str
    model: str
    models: tuple[str, ...]
    vision: bool
    #: ``verified`` / ``unsupported`` / ``stale`` / ``unchecked``，只用于说明原因。
    vision_reason: str

    @property
    def fingerprint(self) -> str:
        return connection_fingerprint(self.connection)


def _capabilities(row: sqlite3.Row) -> dict:
    try:
        data = json.loads(row["capabilities"] or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def evidence_scope(row: sqlite3.Row) -> tuple[str, str, str, str]:
    """那次“检查连接”覆盖的是哪套配置（base_url / provider / model / key 指纹）。"""
    return (
        str(row["checked_base_url"] or ""),
        str(row["checked_provider"] or ""),
        str(row["checked_model"] or ""),
        str(row["checked_fingerprint"] or ""),
    )


def resolve(conn: sqlite3.Connection, user_id: int) -> Selection | None:
    """唯一的模型/能力解析入口。

    优先级：偏好里选的模型（且仍在可用列表里）→ 连接里保存的模型。
    解析出的模型若与“检查连接”覆盖的模型不一致，视觉结论一律作废：
    给 B 声明 A 验证过的图像能力，会让引擎把图片发给一个不支持图像的模型。
    """
    row = connection_for(conn, user_id)
    if row is None:
        return None
    models = available_models(conn, user_id)
    preferred = store.prefs(conn, user_id).get("model")
    model = str(preferred) if preferred and str(preferred) in models else str(row["model"])

    capabilities = _capabilities(row)
    checked_at = row["checked_at"]
    base_url, provider, checked_model, checked_fingerprint = evidence_scope(row)
    fresh = bool(
        checked_at
        and base_url == str(row["base_url"] or "")
        and provider == str(row["provider"] or "")
        and checked_fingerprint == str(row["key_fingerprint"] or "")
        and checked_model == model
    )
    if not fresh:
        reason = "unchecked" if not checked_at else "stale"
        vision = False
    elif capabilities.get("vision"):
        reason = "verified"
        vision = True
    else:
        reason = "unsupported"
        vision = False
    return Selection(
        connection=row,
        provider=str(row["provider"]),
        model=model,
        models=tuple(models),
        vision=vision,
        vision_reason=reason,
    )


def effective_model(
    conn: sqlite3.Connection, user_id: int, connection: sqlite3.Row | None = None
) -> str:
    selection = resolve(conn, user_id)
    if selection is not None:
        return selection.model
    return str(connection["model"]) if connection is not None else ""


def verified_vision(conn: sqlite3.Connection, user_id: int, model: str | None = None) -> bool:
    """只有对**当前模型**做过图像挑战并通过，才允许给引擎声明图像输入。

    未验证不等于不可用，但在没有正向证据前不能声明——本机 1.19.4 对未知模型
    默认 ``image=false``，填错会让真实视觉模型被当成纯文本模型。

    传 ``model`` 时还要求它与解析结果一致：轮次里记的模型和偏好里的最新选择
    可能在极端时序下不同，这时宁可否掉视觉能力，也不给另一个模型声明图像输入。
    """
    selection = resolve(conn, user_id)
    if selection is None or not selection.vision:
        return False
    return model is None or str(model) == selection.model
