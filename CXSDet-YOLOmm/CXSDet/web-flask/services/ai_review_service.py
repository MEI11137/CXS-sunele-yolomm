"""Structured VLM review of an existing YOLO detection record."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


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
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return AIReviewResult("needs_human_review", "AI输出无法解析，需要人工复核")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return AIReviewResult("needs_human_review", "AI输出无法解析，需要人工复核")

    status = str(payload.get("status") or "needs_human_review")
    if status not in ALLOWED_STATUSES:
        status = "needs_human_review"
    summary = str(payload.get("summary") or "AI未提供明确说明，需要人工复核")
    false_positives = [
        int(value)
        for value in payload.get("possible_false_positives", [])
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

    try:
        raw = client.review_detection_structured(image_bytes, detections)
        result = _parse_review_payload(raw)
    except Exception as exc:
        result = AIReviewResult(
            "needs_human_review",
            f"AI复核调用失败，需要人工复核：{type(exc).__name__}",
        )

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
    return result


def check_vl_health(client) -> dict[str, Any]:
    result = client.health_check()
    return {
        "ok": bool(result.get("ok")),
        "model": result.get("model"),
        "available": bool(result.get("available")),
    }
