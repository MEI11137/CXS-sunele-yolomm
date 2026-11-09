"""AI 核查：用**当前用户的当前连接**看图复核，结论与人工状态分开存放。

硬约束：

* 只使用本人连接，且必须是**当前模型**的图像能力证据（``identity.resolve().vision``）；
  换个模型、换份 Key，旧证据立刻失效，这里会直接拒绝而不是“先用着看”；
* 真的把 RGB 和配图一起发过去（有 IR 就发 IR），不会只发路径文字，也不会把 IR 丢掉；
* 返回必须是严格 JSON；解析失败就是失败，**绝不给一个假的 consistent**；
* 单次调用 180 秒上限，走异步作业：接口立刻返回 job_id，逐条状态可查，
  不自动重做已经付过费/发过请求的条目。
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from typing import Any

import requests

from backend import security
from backend.agent import identity
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import detection as detection_service
from backend.services import jobs as job_service

VERDICTS = {
    "consistent",
    "possible_false_positive",
    "possible_missed_detection",
    "needs_human_review",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 180.0

SYSTEM_PROMPT = (
    "你是光伏组件巡检的复核助手。用户会给出检测时真正使用的图片及每张图的模态说明："
    "可见光 RGB、红外 IR、或由 RGB 派生出来的边缘图；也可能还有其他配套模态。\n"
    "请依据图片真实内容判断检测结果是否可信。\n"
    "注意：如果某张图被标注为「派生边缘图」，它**不是**红外热成像，不能据此判断温度或热斑，"
    "只能作为轮廓/结构的参考；缺真实红外时不要假装看过红外。\n"
    "只输出一个 JSON 对象，不要任何解释文字、不要 Markdown 代码块，字段为：\n"
    '{"verdict": "consistent|possible_false_positive|possible_missed_detection|needs_human_review",'
    ' "confidence": 0.0-1.0,'
    ' "summary": "不超过 200 字的结论",'
    ' "findings": [{"kind": "short_code", "note": "不超过 80 字"}]}\n'
    "如果图片看不清、被裁切或无法判断，用 needs_human_review，不要猜。"
)

# 资产角色 → 给模型看的模态名。``origin`` 只说明来源，不能替代模态。
MODALITY_LABELS = {
    "rgb": "可见光 RGB",
    "ir": "红外 IR 热成像",
    "edge": "边缘图（由 RGB 派生，不是真实红外）",
    "x": "配套模态图（未标注具体模态）",
    "image": "图片（未标注模态）",
}
ORIGIN_LABELS = {"upload": "用户上传", "local": "项目内已有文件", "derived": "由父资产派生"}


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "detection_id": data["detection_id"],
        "status": data["status"],
        "verdict": data["verdict"],
        "confidence": data["confidence"],
        "summary": data["summary"],
        "findings": _loads(data["findings"], {}),
        "model": data["model"],
        "provider": data["provider"],
        "created_at": data["created_at"],
        "finished_at": data["finished_at"],
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message")}
            if data.get("error_code")
            else None
        ),
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def latest_for_records(conn, user_id: int, project_id: str, record_ids: list[str]) -> dict[str, dict]:
    if not record_ids:
        return {}
    placeholders = ", ".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT * FROM ai_reviews WHERE project_id = ? AND owner_id = ? "
        f"AND detection_id IN ({placeholders}) ORDER BY created_at ASC",
        (str(project_id), int(user_id), *[str(item) for item in record_ids]),
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        latest[str(row["detection_id"])] = public(row)
    return latest


def _modality_label(row: sqlite3.Row | dict) -> str:
    """资产角色才是模态；``x_origin`` 只是来源，不能拿来当模态名。"""
    data = dict(row)
    role = str(data.get("role") or "image").lower()
    label = MODALITY_LABELS.get(role, f"图片（角色 {role}）")
    origin = str(data.get("origin") or "")
    origin_text = ORIGIN_LABELS.get(origin, origin or "来源未知")
    if origin == "derived":
        parent = json.loads(data.get("meta") or "{}").get("derived_from") if data.get("meta") else None
        suffix = f"，派生自 {parent}" if parent else "，由 RGB 派生"
        return f"{label}；来源：{origin_text}{suffix}"
    return f"{label}；来源：{origin_text}"


def _review_images(conn, user_id: int, project_id: str, record: sqlite3.Row) -> list[dict]:
    """真实读取两张图并各自产出「模态说明 + data URI」，IR 不会被丢掉。"""
    parts: list[dict] = []
    rgb_row, rgb_bytes = asset_service.read_bytes(conn, user_id, project_id, record["rgb_asset_id"])
    caption = _modality_label(rgb_row)
    primary = detection_service.primary_modality_of_record(conn, user_id, project_id, record)
    if primary == "ir":
        # 单 IR 数据集：原图本身就是红外图。资产角色可能还是历史上的 rgb，
        # 但送给模型看的模态说明必须按数据集真实主模态写，不能骗模型这是可见光。
        overridden = dict(rgb_row)
        overridden["role"] = "ir"
        caption = (
            _modality_label(overridden)
            + "（数据集主模态为 IR：这张原图本身就是红外图，没有 RGB 替身）"
        )
    parts.extend(_image_parts(rgb_row, rgb_bytes, caption))
    if record["x_asset_id"]:
        x_row, x_bytes = asset_service.read_bytes(conn, user_id, project_id, record["x_asset_id"])
        parts.extend(_image_parts(x_row, x_bytes, _modality_label(x_row)))
    return parts


def _image_parts(row: sqlite3.Row, data: bytes, caption: str) -> list[dict]:
    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(
            413,
            "REVIEW_IMAGE_TOO_LARGE",
            f"{caption} 超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB，无法安全地随请求发送",
        )
    mime = str(row["mime"] or "image/jpeg")
    encoded = base64.b64encode(data).decode("ascii")
    return [
        {
            "type": "text",
            "text": f"{caption}：{row['filename']}（{row['width']}x{row['height']}，sha256={row['sha256'][:16]}…）",
        },
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
    ]


def _payload(record: sqlite3.Row, images: list[dict]) -> dict:
    content: list[dict] = list(images)
    boxes = _loads(record["boxes"], [])
    content.append(
        {
            "type": "text",
            "text": (
                "检测模型输出如下（像素坐标 xyxy）：\n"
                + json.dumps(
                    [
                        {
                            "class_name": box.get("class_name"),
                            "confidence": box.get("confidence"),
                            "bbox": box.get("bbox"),
                        }
                        for box in boxes
                    ],
                    ensure_ascii=False,
                )
                + "\n请按要求只输出 JSON。"
            ),
        }
    )
    return {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]}


def start_batch(
    conn,
    user_id: int,
    project_id: str,
    *,
    record_ids: list[str],
    request_key: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    model: str | None = None,
) -> tuple[dict, bool]:
    """创建一个 AI 核查作业；前置条件不满足时**同步**报错（不建作业）。

    ``model`` 是本轮真正下发给引擎的模型（Agent 场景必传）：核查必须用它，
    否则会出现聊天用 B、核查却按偏好用 A 的分裂，而视觉证据只对其中一个成立。

    去重：``[同一 ID, 同一 ID]`` 只会发一次请求——重复 ID 不是两张图，不能重复计费。
    """
    ids: list[str] = []
    for item in record_ids or []:
        text = str(item).strip()
        if text and text not in ids:
            ids.append(text)
    if not ids:
        raise ApiError(400, "NO_RECORDS", "需要至少一条识别记录")
    if len(ids) > 20:
        raise ApiError(400, "TOO_MANY_RECORDS", "一次最多核查 20 条")
    selection = identity.resolve(conn, int(user_id), model=model)
    if selection is None:
        raise ApiError(409, "CONNECTION_MISSING", "还没有保存个人模型连接，先到设置页配置")
    if not selection.vision:
        raise ApiError(
            409,
            "VISION_UNVERIFIED",
            f"当前模型（{selection.model}）没有通过图像能力验证（{selection.vision_reason}），"
            "不能做看图核查；请先在设置页检查连接",
        )
    for record_id in ids:
        detection_service.get_owned(conn, user_id, project_id, record_id)

    conflict = active_conflict(conn, user_id, project_id, ids, selection)
    if conflict:
        raise ApiError(
            409,
            "REVIEW_IN_PROGRESS",
            "同一批记录正在被核查中，请等它结束再提交；重复提交会重复计费。",
            details=conflict,
        )

    job, created = job_service.create(
        conn,
        project_id=project_id,
        owner_id=int(user_id),
        kind="ai_review",
        request_key=request_key,
        payload={
            "record_ids": ids,
            "model": selection.model,
            "provider": selection.provider,
            "connection_fingerprint": selection.fingerprint,
        },
        session_id=session_id,
        turn_id=turn_id,
    )
    if created:
        job_service.submit(str(job["id"]), run_batch)
    return job_service.public(job), created


def active_conflict(conn, user_id: int, project_id: str, ids: list[str], selection) -> dict | None:
    """在途核查的**同步预检**；真正的原子性由唯一索引保证（见 :func:`_claim`）。"""
    if not ids:
        return None
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT detection_id, job_id, model FROM ai_reviews "
        f"WHERE owner_id = ? AND project_id = ? AND status = 'running' "
        f"AND model = ? AND connection_fingerprint = ? AND detection_id IN ({placeholders})",
        (int(user_id), str(project_id), selection.model, selection.fingerprint, *ids),
    ).fetchall()
    if not rows:
        return None
    return {
        "job_id": str(rows[0]["job_id"] or ""),
        "record_ids": [str(row["detection_id"]) for row in rows],
        "model": selection.model,
        "note": "同一记录 + 同一模型 + 同一连接指纹的在途核查只会有一条；等它结束再决定要不要重新核查。",
    }


def interrupted(conn) -> int:
    """启动时把遗留的 running 核查标成 interrupted：结果未知，不自动重发。"""
    rows = conn.execute("SELECT id FROM ai_reviews WHERE status = 'running'").fetchall()
    if not rows:
        return 0
    conn.execute(
        "UPDATE ai_reviews SET status = 'interrupted', error_code = ?, error_message = ?, finished_at = ? "
        "WHERE status = 'running'",
        (
            "SERVICE_RESTART",
            "服务重启时这次核查还没有结果，未自动重发（可能已经产生费用）。",
            security.now_iso(),
        ),
    )
    conn.commit()
    return len(rows)


def run_batch(conn, job) -> dict:
    payload = _loads(job["payload"], {})
    ids: list[str] = []
    for item in payload.get("record_ids") or []:
        text = str(item).strip()
        if text and text not in ids:
            ids.append(text)
    # 作业创建时的模型/连接快照：不再按“当前偏好”重新解析，避免中途换模型或换连接导致混搭。
    model = str(payload.get("model") or "") or None
    snapshot = {
        "connection_fingerprint": str(payload.get("connection_fingerprint") or ""),
        "provider": str(payload.get("provider") or ""),
    }
    owner_id = int(job["owner_id"])
    project_id = str(job["project_id"])
    items: list[dict] = []
    done = 0
    failed = 0
    job_service.update(
        conn,
        str(job["id"]),
        progress=json.dumps({"done": 0, "failed": 0, "total": len(ids)}, ensure_ascii=False),
    )
    for index, record_id in enumerate(ids, 1):
        try:
            review = _review_one(
                conn,
                owner_id,
                project_id,
                record_id,
                job_id=str(job["id"]),
                model=model,
                snapshot=snapshot,
            )
            items.append({"record_id": record_id, "status": review["status"], "review": review})
            if review["status"] == "done":
                done += 1
            else:
                failed += 1
        except ApiError as exc:
            # 单条失败不拖累其它条目（已经成功的记录保留）。
            failed += 1
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": {"code": exc.code, "message": exc.msg},
                }
            )
        # 先落累计进度再发事件：终态必须包含最后一条，不能停在 N-1。
        job_service.update(
            conn,
            str(job["id"]),
            progress=json.dumps({"done": done, "failed": failed, "total": len(ids)}, ensure_ascii=False),
        )
        job_service.emit(
            conn,
            job,
            "business.review",
            {
                "record_id": record_id,
                "index": index,
                "total": len(ids),
                "item": items[-1],
            },
        )
    status = "done" if failed == 0 else ("failed" if done == 0 else "partial")
    return {
        "status": status,
        "items": items,
        "done": done,
        "failed": failed,
        "total": len(ids),
        "note": "AI 结论只作参考，人工复核状态需单独提交。",
    }


def _claim(conn, *, review_id: str, project_id: str, user_id: int, record_id: str,
           job_id: str, selection) -> None:
    """原子认领：同一 (记录, 模型, 连接指纹) 只允许一条在途核查。

    唯一索引 ``uq_reviews_active`` 是权威判定；这里不靠“先查再插”，也不靠调用方
    每次都生成新 request_key——那样两次点击就会发两次请求、收两次费。
    """
    try:
        conn.execute(
            """
            INSERT INTO ai_reviews
                (id, project_id, owner_id, detection_id, job_id, provider, model,
                 connection_fingerprint, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                review_id,
                str(project_id),
                int(user_id),
                str(record_id),
                job_id,
                selection.provider,
                selection.model,
                selection.fingerprint,
                security.now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            "SELECT job_id FROM ai_reviews WHERE detection_id = ? AND model = ? "
            "AND connection_fingerprint = ? AND status = 'running' LIMIT 1",
            (str(record_id), selection.model, selection.fingerprint),
        ).fetchone()
        raise ApiError(
            409,
            "REVIEW_IN_PROGRESS",
            "这条记录已经在核查中，本次没有重复发送请求。",
            details={"job_id": str(row["job_id"] or "") if row else "", "record_id": str(record_id)},
        ) from None


def _review_one(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    *,
    job_id: str,
    model: str | None = None,
    snapshot: dict | None = None,
) -> dict:
    record = detection_service.get_owned(conn, user_id, project_id, record_id)
    if record["status"] != "done":
        raise ApiError(409, "RECORD_NOT_DONE", "这条记录还没有成功的检测结果，不能核查")
    selection = identity.resolve(conn, user_id, model=model)
    if selection is None or not selection.vision:
        detail = f"（本作业记录的是模型 {model}）" if model else ""
        raise ApiError(409, "VISION_UNVERIFIED", "当前模型没有可用的图像能力证据" + detail)

    # 作业创建后用户换了连接：这里明确停止，绝不把旧模型配到新地址/新 Key 上。
    expected = str((snapshot or {}).get("connection_fingerprint") or "")
    if expected and selection.fingerprint != expected:
        raise ApiError(
            409,
            "CONNECTION_CHANGED",
            "作业创建后个人连接已被修改，本次核查没有发送请求；请用当前连接重新提交。",
        )

    review_id = security.new_id("rev")
    _claim(
        conn,
        review_id=review_id,
        project_id=project_id,
        user_id=user_id,
        record_id=record_id,
        job_id=job_id,
        selection=selection,
    )

    try:
        try:
            images = _review_images(conn, user_id, project_id, record)
            body = _payload(record, images)
            text = _call(conn, user_id, selection, body)
            parsed = _parse(text)
        except ApiError as exc:
            _finish(conn, review_id, status="failed", error=(exc.code, exc.msg))
        except requests.RequestException as exc:
            _finish(
                conn,
                review_id,
                status="failed",
                error=("REVIEW_NETWORK", f"调用模型服务失败（{type(exc).__name__}）"),
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常出口都必须把核查行收尾
            logging.getLogger(__name__).exception("核查失败 review=%s", review_id)
            _finish(
                conn,
                review_id,
                status="failed",
                error=(
                    "REVIEW_FAILED",
                    f"核查过程中出现未预期的错误（{type(exc).__name__}），本条按失败处理",
                ),
            )
        else:
            _finish(conn, review_id, status="done", parsed=parsed)
    finally:
        # 兜底：任何出口都不能把一条 running 永远留在库里；异常离开时记“结果未知”，
        # 而不是编造一个 consistent。
        _settle_leftover(conn, review_id)
    return public(_row(conn, review_id))


def _settle_leftover(conn, review_id: str) -> None:
    _finish(
        conn,
        review_id,
        status="interrupted",
        error=("REVIEW_UNSETTLED", "核查没有走完就结束了，结果未知；未自动重发。"),
    )


def _call(conn, user_id: int, selection, body: dict) -> str:
    from backend.services import connections as connection_service

    credentials = connection_service.server_credentials(
        conn, int(user_id), key=_connection_key()
    )
    # 取凭据与发送之间再核对一次连接指纹：用户可能刚好在这两步之间换了地址/Key。
    # 不允许“旧模型 + 新地址/新 Key”混搭发出去。
    if credentials.get("fingerprint") and credentials["fingerprint"] != selection.fingerprint:
        raise ApiError(
            409,
            "CONNECTION_CHANGED",
            "个人连接在核查过程中被修改，本次没有发送请求；请用当前连接重新提交。",
        )
    url = credentials["base_url"].rstrip("/") + "/chat/completions"
    payload = {**body, "model": selection.model, "temperature": 0, "stream": False}
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {credentials['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(5.0, TIMEOUT_SECONDS),
    )
    if response.status_code >= 400:
        raise ApiError(
            502,
            "REVIEW_PROVIDER_ERROR",
            f"模型服务返回 HTTP {response.status_code}",
        )
    try:
        data = response.json()
    except ValueError:
        raise ApiError(502, "REVIEW_BAD_RESPONSE", "模型服务返回的不是 JSON") from None
    if not isinstance(data, dict):
        raise ApiError(502, "REVIEW_BAD_RESPONSE", "模型服务的响应不是对象")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ApiError(502, "REVIEW_EMPTY", "模型服务没有返回任何候选回答")
    first = choices[0]
    if not isinstance(first, dict):
        raise ApiError(502, "REVIEW_BAD_RESPONSE", "模型服务返回的候选回答格式不对")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ApiError(502, "REVIEW_BAD_RESPONSE", "模型服务返回的 message 不是对象")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(item.get("text") or "" for item in content if isinstance(item, dict))
    text = str(content or "").strip()
    if not text:
        raise ApiError(502, "REVIEW_EMPTY", "模型服务返回了空内容")
    return text


def _parse(text: str) -> dict:
    """严格解析：字段与取值都必须合规，否则按失败处理（不降级成 consistent）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        raise ApiError(502, "REVIEW_BAD_JSON", "模型返回的不是合法 JSON，本次核查按失败处理") from None
    if not isinstance(data, dict):
        raise ApiError(502, "REVIEW_BAD_JSON", "模型返回的 JSON 不是对象")
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        raise ApiError(502, "REVIEW_BAD_VERDICT", f"模型返回了不认识的结论：{verdict[:40]}")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ApiError(502, "REVIEW_BAD_CONFIDENCE", "模型返回的置信度不是数字") from None
    if not 0.0 <= confidence <= 1.0:
        raise ApiError(502, "REVIEW_BAD_CONFIDENCE", "模型返回的置信度不在 0-1 之间")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise ApiError(502, "REVIEW_EMPTY_SUMMARY", "模型返回的结论说明为空")
    findings = data.get("findings") or []
    if isinstance(findings, dict):
        findings = [findings]
    if not isinstance(findings, list):
        raise ApiError(502, "REVIEW_BAD_FINDINGS", "findings 必须是数组")
    clean_findings = [
        {"kind": str(item.get("kind") or "")[:40], "note": str(item.get("note") or "")[:200]}
        for item in findings[:10]
        if isinstance(item, dict)
    ]
    return {
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "summary": summary[:400],
        "findings": {"items": clean_findings},
    }


def _row(conn, review_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM ai_reviews WHERE id = ?", (str(review_id),)).fetchone()


def _finish(conn, review_id: str, *, status: str, parsed: dict | None = None, error: tuple | None = None) -> None:
    if parsed is not None:
        conn.execute(
            """
            UPDATE ai_reviews SET status = ?, verdict = ?, confidence = ?, summary = ?, findings = ?,
                   finished_at = ? WHERE id = ? AND status = 'running'
            """,
            (
                status,
                parsed["verdict"],
                parsed["confidence"],
                parsed["summary"],
                json.dumps(parsed["findings"], ensure_ascii=False),
                security.now_iso(),
                str(review_id),
            ),
        )
    else:
        code, message = error or ("REVIEW_FAILED", "核查失败")
        conn.execute(
            "UPDATE ai_reviews SET status = ?, error_code = ?, error_message = ?, finished_at = ? "
            "WHERE id = ? AND status = 'running'",
            (status, code, message[:500], security.now_iso(), str(review_id)),
        )
    conn.commit()


def _connection_key() -> bytes:
    """后台线程里没有 Flask 应用上下文，密钥路径从工厂注入的配置快照取。"""
    from pathlib import Path

    settings = job_service.settings()
    if settings is None:  # pragma: no cover
        raise ApiError(500, "CONFIG_MISSING", "服务配置未初始化")
    return security.load_or_create_key(Path(settings.secret_dir) / "connection.key")


__all__ = [
    "VERDICTS",
    "active_conflict",
    "interrupted",
    "latest_for_records",
    "public",
    "run_batch",
    "start_batch",
]
