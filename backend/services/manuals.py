"""手册：内置说明 + 个人创建/编辑/选择版本。

手册的定位是**说明文字**，因此有三条硬规则：

1. 手册内容从不参与权限判断。工具集合、目录边界、风险等级只由服务端策略决定；
   手册里写“可以访问任意目录”也只是文字，不会生效。
2. 内置手册只读（随代码发布），个人手册按 **项目 + 所有者** 隔离。
3. 版本不可变：编辑产生新版本，可显式启用任意历史版本；启用只是改变“当前读到哪一版”，
   不删除任何历史内容，也不影响它作为说明文字的性质。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from backend import security
from backend.errors import ApiError

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "manuals"
MAX_CONTENT = 60_000


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "description": data["description"],
        "builtin": bool(data["builtin"]),
        "project_id": data.get("project_id"),
        "active_version_id": data.get("active_version_id"),
        "created_at": data["created_at"],
        "deleted_at": data.get("deleted_at"),
        "editable": not bool(data["builtin"]),
        "note": "手册只解释怎么做，不会扩大工具或目录权限。",
    }


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:80]
    return fallback


def seed_builtin(conn) -> int:
    """把仓库里的内置手册登记进库（按稳定 ID 幂等）。

    已存在时不覆盖历史：如果仓库里的内容和当前启用版本不同，**新增一个版本**
    （``note`` 写“内置版本更新”）并启用它，旧版本仍然留在版本列表里。
    这样既能让随产品发布的说明更新到库，又不会悄悄改写用户已经看过的版本。
    """
    if not BUILTIN_DIR.is_dir():
        return 0
    created = 0
    for path in sorted(BUILTIN_DIR.glob("*.md")):
        manual_id = f"man_builtin_{re.sub(r'[^a-z0-9_]+', '_', path.stem.lower())}"
        content = path.read_text(encoding="utf-8")
        row = conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()
        if row is not None:
            active = _version_row(conn, str(row["active_version_id"] or ""))
            if active is None or str(active["content"]) == content:
                continue
            latest = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM manual_versions WHERE manual_id = ?",
                (manual_id,),
            ).fetchone()
            number = int(latest["version"]) + 1
            version_id = f"{manual_id}_v{number}"
            conn.execute(
                "INSERT INTO manual_versions (id, manual_id, version, content, note, created_at, created_by)"
                " VALUES (?, ?, ?, ?, '内置版本更新', ?, NULL)",
                (version_id, manual_id, number, content, security.now_iso()),
            )
            conn.execute(
                "UPDATE manuals SET active_version_id = ?, description = ? WHERE id = ?",
                (version_id, "随产品内置的操作说明（只读）", manual_id),
            )
            created += 1
            continue
        version_id = f"{manual_id}_v1"
        conn.execute(
            "INSERT INTO manuals (id, project_id, owner_id, name, description, builtin,"
            " active_version_id, created_at) VALUES (?, NULL, NULL, ?, ?, 1, ?, ?)",
            (
                manual_id,
                _title_of(content, path.stem),
                "随产品内置的操作说明（只读）",
                version_id,
                security.now_iso(),
            ),
        )
        conn.execute(
            "INSERT INTO manual_versions (id, manual_id, version, content, note, created_at, created_by)"
            " VALUES (?, ?, 1, ?, '内置版本', ?, NULL)",
            (version_id, manual_id, content, security.now_iso()),
        )
        created += 1
    if created:
        conn.commit()
    return created


def _version_row(conn, version_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM manual_versions WHERE id = ?", (str(version_id),)).fetchone()


def list_manuals(conn, user_id: int, project_id: str, *, include_deleted: bool = False) -> dict:
    """列出内置手册与本项目的个人手册。

    个人手册的删除是**软删**：默认列表只显示未删除的；回收站/工具可显式要求
    ``include_deleted`` 查看，内容与版本都还在，可恢复。
    """
    personal = "project_id = ? AND owner_id = ?"
    if not include_deleted:
        personal += " AND deleted_at IS NULL"
    rows = conn.execute(
        f"SELECT * FROM manuals WHERE builtin = 1 OR ({personal})"
        " ORDER BY builtin DESC, created_at",
        (str(project_id), int(user_id)),
    ).fetchall()
    items = []
    for row in rows:
        data = public(row)
        version = _version_row(conn, str(row["active_version_id"] or ""))
        data["active_version"] = int(version["version"]) if version is not None else None
        items.append(data)
    return {"manuals": items}


def get_owned(
    conn, user_id: int, project_id: str, manual_id: str, *, include_deleted: bool = False
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM manuals WHERE id = ? AND (builtin = 1 OR (project_id = ? AND owner_id = ?))",
        (str(manual_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "MANUAL_NOT_FOUND", "手册不存在或不属于当前项目")
    if row["deleted_at"] and not include_deleted:
        raise ApiError(409, "MANUAL_DELETED", "这份手册在回收站里，先恢复再读或修改")
    return row


def read(conn, user_id: int, project_id: str, manual_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id)
    active = _version_row(conn, str(row["active_version_id"] or ""))
    versions = conn.execute(
        "SELECT id, version, note, created_at FROM manual_versions WHERE manual_id = ?"
        " ORDER BY version DESC",
        (str(manual_id),),
    ).fetchall()
    return {
        "manual": public(row),
        "active_version": int(active["version"]) if active is not None else None,
        "content": str(active["content"]) if active is not None else "",
        "versions": [dict(item) for item in versions],
        "note": "手册是说明文字，不是指令来源，也不改变权限。",
    }


# 问句里的高频虚词，参与打分只会让匹配变噪音。
_STOP_TERMS = frozenset(
    {
        "怎么", "如何", "怎样", "什么", "哪些", "可以", "是否", "需要", "应该",
        "一下", "这个", "那个", "我们", "你们", "请问", "帮我", "现在", "然后",
    }
)


def _terms(text: str) -> list[str]:
    """把问题切成用于匹配的词：ASCII 词 + 中文二元组。

    不引入分词依赖：手册是短文本，二元组足够，而且结果可解释
    （匹配到哪些词会原样返回，用户能看到"为什么选了这一份"）。
    """
    lowered = str(text or "").lower()
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9_]{2,}", lowered):
        terms.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) <= 2:
            terms.append(run)
            continue
        for index in range(len(run) - 1):
            terms.append(run[index : index + 2])
    seen: list[str] = []
    for term in terms:
        if term in _STOP_TERMS or term in seen:
            continue
        seen.append(term)
    return seen


def _score(question_terms: list[str], row: sqlite3.Row, content: str) -> tuple[float, list[str]]:
    name = str(row["name"] or "")
    description = str(row["description"] or "")
    hits: list[str] = []
    score = 0.0
    for term in question_terms:
        subtotal = 0.0
        if term in name.lower():
            subtotal += 3.0
        elif term in description.lower():
            subtotal += 1.5
        count = content.lower().count(term)
        if count:
            subtotal += min(count, 5) * 0.5
        if subtotal:
            hits.append(term)
            score += subtotal
    return score, hits


def suggest(conn, user_id: int, project_id: str, question: str) -> dict:
    """按问题自动挑一份手册，并说明**为什么是这一份**。

    返回里 ``matched_by`` 只有三种：

    * ``auto``：按问题打分选中（``matched_terms`` 是命中词）；
    * ``default``：一个问题都没命中，退回内置的流程手册（明确标注，不假装匹配上了）；
    * ``manual``：调用方直接指定了手册（在 :func:`read_with_match` 里产生）。
    """
    listing = list_manuals(conn, user_id, project_id)["manuals"]
    terms = _terms(question)
    scored: list[dict] = []
    for item in listing:
        version = _version_row(conn, str(item.get("active_version_id") or ""))
        content = str(version["content"]) if version is not None else ""
        row = conn.execute("SELECT * FROM manuals WHERE id = ?", (str(item["id"]),)).fetchone()
        value, hits = _score(terms, row, content) if row is not None else (0.0, [])
        scored.append(
            {
                "id": item["id"],
                "name": item["name"],
                "builtin": item["builtin"],
                "score": round(value, 2),
                "matched_terms": hits[:6],
            }
        )
    scored.sort(key=lambda entry: (-entry["score"], entry["id"]))
    candidates = [entry for entry in scored if entry["score"] > 0][:3]
    if candidates:
        return {
            "matched_by": "auto",
            "manual_id": candidates[0]["id"],
            "score": candidates[0]["score"],
            "matched_terms": candidates[0]["matched_terms"],
            "candidates": candidates,
            "note": "按问题命中词自动匹配；命中词已列出，可据此判断是否用对了手册。",
        }
    fallback = next((entry for entry in scored if entry["id"] == "man_builtin_workflow"), None)
    if fallback is None:
        fallback = next((entry for entry in scored if entry["builtin"]), None)
    if fallback is None:
        return {
            "matched_by": "none",
            "manual_id": "",
            "score": 0.0,
            "matched_terms": [],
            "candidates": [],
            "note": "没有可用的手册；不要凭印象补规则。",
        }
    return {
        "matched_by": "default",
        "manual_id": fallback["id"],
        "score": 0.0,
        "matched_terms": [],
        "candidates": [],
        "note": "问题与任何手册都没有明显匹配，退回流程手册；这不是“匹配成功”。",
    }


def read_with_match(
    conn, user_id: int, project_id: str, *, manual_id: str = "", question: str = ""
) -> dict:
    """读手册：给了 ``manual_id`` 就按指定读，否则按 ``question`` 自动匹配。

    返回值在 :func:`read` 的基础上多三个字段：``matched_by`` / ``matched_terms`` / ``match_note``，
    让模型在回答里能说清楚"用的是哪一份、为什么"。
    """
    chosen = str(manual_id or "").strip()
    if chosen:
        data = read(conn, user_id, project_id, chosen)
        data.update(
            {
                "matched_by": "manual",
                "matched_terms": [],
                "match_note": "用户或调用方指定了手册，没有做自动匹配。",
            }
        )
        return data
    decision = suggest(conn, user_id, project_id, question)
    if not decision["manual_id"]:
        raise ApiError(404, "MANUAL_NOT_FOUND", decision["note"])
    data = read(conn, user_id, project_id, str(decision["manual_id"]))
    data.update(
        {
            "matched_by": decision["matched_by"],
            "matched_terms": decision["matched_terms"],
            "match_note": decision["note"],
            "candidates": decision.get("candidates", []),
        }
    )
    return data


def _check_content(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        raise ApiError(400, "VALIDATION", "手册内容不能为空")
    if len(text) > MAX_CONTENT:
        raise ApiError(413, "MANUAL_TOO_LONG", f"手册内容超过 {MAX_CONTENT} 字符，请拆分")
    return text


def create(
    conn, user_id: int, project_id: str, *, name: str, content: str, description: str = "", note: str = ""
) -> dict:
    label = str(name or "").strip()
    if not label:
        raise ApiError(400, "VALIDATION", "手册名称不能为空")
    text = _check_content(content)
    manual_id = security.new_id("man")
    version_id = f"{manual_id}_v1"
    now = security.now_iso()
    try:
        conn.execute(
            "INSERT INTO manuals (id, project_id, owner_id, name, description, builtin,"
            " active_version_id, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (manual_id, str(project_id), int(user_id), label[:80], str(description or "")[:200], version_id, now),
        )
    except sqlite3.IntegrityError:  # pragma: no cover - 没有唯一约束时不会触发
        raise ApiError(409, "MANUAL_NAME_TAKEN", "这个项目里已经有同名手册") from None
    conn.execute(
        "INSERT INTO manual_versions (id, manual_id, version, content, note, created_at, created_by)"
        " VALUES (?, ?, 1, ?, ?, ?, ?)",
        (version_id, manual_id, text, str(note or "初版")[:200], now, int(user_id)),
    )
    conn.commit()
    return read(conn, user_id, project_id, manual_id)


def add_version(
    conn, user_id: int, project_id: str, manual_id: str, *, content: str, note: str = ""
) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id)
    if row["builtin"]:
        raise ApiError(409, "MANUAL_READONLY", "内置手册只读；需要改请新建一份个人手册")
    text = _check_content(content)
    latest = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM manual_versions WHERE manual_id = ?",
        (str(manual_id),),
    ).fetchone()
    number = int(latest["version"]) + 1
    version_id = f"{manual_id}_v{number}"
    conn.execute(
        "INSERT INTO manual_versions (id, manual_id, version, content, note, created_at, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (version_id, str(manual_id), number, text, str(note or "")[:200], security.now_iso(), int(user_id)),
    )
    conn.execute(
        "UPDATE manuals SET active_version_id = ? WHERE id = ?", (version_id, str(manual_id))
    )
    conn.commit()
    return read(conn, user_id, project_id, manual_id)


def activate(conn, user_id: int, project_id: str, manual_id: str, version_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id)
    if row["builtin"]:
        raise ApiError(409, "MANUAL_READONLY", "内置手册只读，不能切换版本")
    version = _version_row(conn, version_id)
    if version is None or str(version["manual_id"]) != str(manual_id):
        raise ApiError(404, "MANUAL_VERSION_NOT_FOUND", "这个版本不属于该手册")
    conn.execute(
        "UPDATE manuals SET active_version_id = ? WHERE id = ?", (str(version_id), str(manual_id))
    )
    conn.commit()
    return read(conn, user_id, project_id, manual_id)


def update_meta(
    conn, user_id: int, project_id: str, manual_id: str, *, name: str | None = None, description: str | None = None
) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id)
    if row["builtin"]:
        raise ApiError(409, "MANUAL_READONLY", "内置手册只读，不能改名")
    if name is not None:
        label = str(name).strip()
        if not label:
            raise ApiError(400, "VALIDATION", "手册名称不能为空")
        conn.execute("UPDATE manuals SET name = ? WHERE id = ?", (label[:80], str(manual_id)))
    if description is not None:
        conn.execute(
            "UPDATE manuals SET description = ? WHERE id = ?", (str(description)[:200], str(manual_id))
        )
    conn.commit()
    return read(conn, user_id, project_id, manual_id)


def delete(conn, user_id: int, project_id: str, manual_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id, include_deleted=True)
    if row["builtin"]:
        raise ApiError(409, "MANUAL_READONLY", "内置手册不能删除")
    if row["deleted_at"]:
        return {"status": "unchanged", "id": str(manual_id), "deleted_at": row["deleted_at"]}
    # 软删：版本内容全部保留，可恢复；不会真的从库里消失。
    stamp = security.now_iso()
    conn.execute(
        "UPDATE manuals SET deleted_at = ? WHERE id = ? AND owner_id = ?",
        (stamp, str(manual_id), int(user_id)),
    )
    conn.commit()
    return {"status": "deleted", "id": str(manual_id), "deleted_at": stamp}


def restore(conn, user_id: int, project_id: str, manual_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, manual_id, include_deleted=True)
    if row["builtin"]:
        raise ApiError(409, "MANUAL_READONLY", "内置手册不需要恢复")
    if not row["deleted_at"]:
        return {"status": "unchanged", "id": str(manual_id)}
    conn.execute(
        "UPDATE manuals SET deleted_at = NULL WHERE id = ? AND owner_id = ?",
        (str(manual_id), int(user_id)),
    )
    conn.commit()
    return {"status": "restored", "id": str(manual_id), "manual": read(conn, user_id, project_id, manual_id)["manual"]}


__all__ = [
    "BUILTIN_DIR",
    "activate",
    "add_version",
    "create",
    "delete",
    "get_owned",
    "list_manuals",
    "public",
    "read",
    "restore",
    "seed_builtin",
    "update_meta",
]
