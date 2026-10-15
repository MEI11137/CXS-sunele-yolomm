"""Structured VLM review of an existing YOLO detection record."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any


LOGGER = logging.getLogger(__name__)
PARSE_FAILURE_SUMMARY = "AI返回格式异常，需要人工复核"
UPSTREAM_FAILURE_SUMMARY = "AI服务调用失败，需要人工复核"


class AIReviewError(RuntimeError):
    """Base class for safe, caller-visible AI review failures."""

    public_message = "AI辅助复核失败"


class AIReviewParseError(AIReviewError):
    """Raised when model output cannot become a review object."""

    public_message = PARSE_FAILURE_SUMMARY


class AIReviewUpstreamError(AIReviewError):
    """Raised when the model client or provider call fails."""

    public_message = UPSTREAM_FAILURE_SUMMARY


ALLOWED_STATUSES = {
    "consistent",
    "possible_false_positive",
    "possible_missed_detection",
    "needs_human_review",
}


@dataclass(frozen=True)
class AIReviewResult:
    status: str
    summary: str
    possible_false_positives: list[int] = field(default_factory=list)
    possible_missed_detections: list[dict[str, Any]] = field(default_factory=list)
    attention_regions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_review_payload(raw: Any) -> AIReviewResult:
    if isinstance(raw, dict):
        payload = raw
    else:
        text = raw.strip() if isinstance(raw, str) else ""
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                raise AIReviewParseError("AI响应中没有JSON对象")
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise AIReviewParseError("AI响应不是有效JSON对象") from exc

    if not isinstance(payload, dict):
        raise AIReviewParseError("AI响应顶层必须是JSON对象")

    status = str(payload.get("status") or "needs_human_review")
    if status not in ALLOWED_STATUSES:
        status = "needs_human_review"
    summary = str(payload.get("summary") or "AI未提供明确说明，需要人工复核")
    false_positive_values = payload.get("possible_false_positives", [])
    if not isinstance(false_positive_values, list):
        raise AIReviewParseError("possible_false_positives必须是JSON数组")
    false_positives = [
        int(value)
        for value in false_positive_values
        if isinstance(value, (int, float))
    ]
    missed = payload.get("possible_missed_detections", [])
    regions = payload.get("attention_regions", [])
    return AIReviewResult(
        status=status,
        summary=summary,
        possible_false_positives=false_positives,
        possible_missed_detections=missed if isinstance(missed, list) else [],
        attention_regions=regions if isinstance(regions, list) else [],
    )


def _persist_review(database, record_id: str, result: AIReviewResult) -> None:
    database.execute(
        """
        UPDATE detection_records
        SET ai_review_status = ?, ai_review_result = ?
        WHERE id = ?
        """,
        (
            result.status,
            json.dumps(result.to_dict(), ensure_ascii=False),
            record_id,
        ),
    )


def _configured_model(client) -> str:
    config = getattr(client, "config", None)
    if isinstance(config, dict) and config.get("model"):
        return str(config["model"])
    return str(getattr(client, "model_name", "unknown"))


def _response_metadata(raw: Any) -> tuple[str, int, str]:
    raw_type = type(raw).__name__
    if isinstance(raw, str):
        encoded = raw.encode("utf-8", errors="replace")
        length = len(raw)
    elif isinstance(raw, bytes):
        encoded = raw
        length = len(raw)
    else:
        encoded = repr(type(raw)).encode("utf-8")
        length = 0
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return raw_type, length, digest


def _public_exception_field(exc: Exception, field_name: str) -> str:
    if field_name != "status_code":
        return "unknown"
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    return str(value) if type(value) is int and 100 <= value <= 599 else "unknown"


def review_detection(record_id: str, client, database, storage_client) -> AIReviewResult:
    record = database.query(
        """
        SELECT id, original_image_bucket, original_image_object_key, detection_result
        FROM detection_records WHERE id = ?
        """,
        (record_id,),
        fetchone=True,
    )
    if not record:
        raise ValueError("识别记录不存在")
    image_bytes = storage_client.get(
        record["original_image_bucket"], record["original_image_object_key"]
    )
    try:
        detections = json.loads(record.get("detection_result") or "[]")
    except json.JSONDecodeError:
        detections = []

    model = _configured_model(client)
    for attempt in (1, 2):
        try:
            raw = client.review_detection_structured(image_bytes, detections)
        except Exception as exc:
            LOGGER.warning(
                "ai_review_upstream_failure record_id=%s model=%s exception_type=%s "
                "status_code=%s error_code=%s",
                record_id,
                model,
                type(exc).__name__,
                _public_exception_field(exc, "status_code"),
                _public_exception_field(exc, "code"),
            )
            fallback = AIReviewResult("needs_human_review", UPSTREAM_FAILURE_SUMMARY)
            _persist_review(database, record_id, fallback)
            raise AIReviewUpstreamError(UPSTREAM_FAILURE_SUMMARY) from exc

        try:
            result = _parse_review_payload(raw)
        except AIReviewParseError as exc:
            raw_type, raw_length, response_hash = _response_metadata(raw)
            LOGGER.warning(
                "ai_review_parse_failure record_id=%s model=%s attempt=%s "
                "raw_type=%s raw_length=%s response_hash=%s",
                record_id,
                model,
                attempt,
                raw_type,
                raw_length,
                response_hash,
            )
            if attempt == 1:
                continue
            fallback = AIReviewResult("needs_human_review", PARSE_FAILURE_SUMMARY)
            _persist_review(database, record_id, fallback)
            raise AIReviewParseError(PARSE_FAILURE_SUMMARY) from exc

        _persist_review(database, record_id, result)
        return result

    raise AssertionError("AI review retry loop exhausted unexpectedly")


def check_vl_health(client) -> dict[str, Any]:
    result = client.health_check()
    return {
        "ok": bool(result.get("ok")),
        "model": result.get("model"),
        "available": bool(result.get("available")),
    }
