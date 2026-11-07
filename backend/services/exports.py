"""导出：识别记录 CSV 与数据集 ZIP。

两条规则是硬的：

1. **范围必须明确**：CSV 只支持"当前一页"或"明确选中的记录"，
   不接受"把整个项目导一份"这种含糊范围（那种请求会 400 ``EXPORT_SCOPE_REQUIRED``）。
2. **返回真实文件**：导出会真的写进项目工作区 ``exports/``，回读校验行数与字节后
   才把相对路径、大小、sha256 交出去；不是"页面打开了"就算完成。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

from backend import security
from backend.errors import ApiError
from backend.services import detection as detection_service
from backend.services import storage

EXPORTS = "exports"
MAX_PAGE_ROWS = 200
MAX_SELECTED_ROWS = 200

COLUMNS = (
    "记录ID",
    "创建时间",
    "状态",
    "模型",
    "缺陷数",
    "类别统计",
    "最高置信度",
    "RGB资产",
    "红外/派生资产",
    "x_origin",
    "人工结论",
    "人工结论版本",
    "已入反馈池",
)


def _folder(conn, user_id: int, project_id: str) -> tuple[Path, Path]:
    root = storage.project_root(conn, user_id, project_id)
    return root, storage.project_dir(root, EXPORTS)


def _rows_for_ids(conn, user_id: int, project_id: str, ids: list[str]) -> list[sqlite3.Row]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in ids:
        key = str(item or "").strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    if not unique:
        raise ApiError(400, "VALIDATION", "选中的记录为空")
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        f"SELECT * FROM detections WHERE project_id = ? AND owner_id = ? AND id IN ({placeholders})",
        (str(project_id), int(user_id), *unique),
    ).fetchall()
    found = {str(row["id"]): row for row in rows}
    missing = [item for item in unique if item not in found]
    if missing:
        raise ApiError(
            404,
            "RECORD_NOT_FOUND",
            f"有 {len(missing)} 条记录不存在或不属于当前项目，未导出任何内容",
            details={"missing": missing[:10]},
        )
    return [found[item] for item in unique]


def _review_map(conn, user_id: int, project_id: str, record_ids: list[str]) -> dict[str, dict]:
    if not record_ids:
        return {}
    placeholders = ",".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT detection_id, version, outcome, submitted_at FROM review_versions"
        f" WHERE project_id = ? AND owner_id = ? AND status IN ('confirmed', 'rejected')"
        f" AND detection_id IN ({placeholders}) ORDER BY version",
        (str(project_id), int(user_id), *record_ids),
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:  # 版本号递增，后面覆盖前面即为最近一次已提交结论
        latest[str(row["detection_id"])] = {
            "outcome": row["outcome"],
            "version": int(row["version"]),
            "submitted_at": row["submitted_at"],
        }
    return latest


def _feedback_map(conn, user_id: int, project_id: str, record_ids: list[str]) -> dict[str, str]:
    if not record_ids:
        return {}
    placeholders = ",".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT detection_id, status FROM record_feedback WHERE project_id = ? AND owner_id = ?"
        f" AND detection_id IN ({placeholders})",
        (str(project_id), int(user_id), *record_ids),
    ).fetchall()
    return {str(row["detection_id"]): str(row["status"]) for row in rows}


def _write_csv(target: Path, rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    # utf-8-sig：Excel 直接打开不乱码；换行按 CSV 标准用 \r\n。
    buffer.write("\ufeff")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(row)
    data = buffer.getvalue().encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.hx17-{security.new_token(4)}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return data


def records_csv(
    conn,
    user_id: int,
    project_id: str,
    *,
    scope: str,
    ids: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict:
    """把**一页**或**一批选中记录**导出成 CSV，并回读确认行数。"""
    kind = str(scope or "").strip()
    if kind not in {"page", "selected"}:
        raise ApiError(
            400,
            "EXPORT_SCOPE_REQUIRED",
            "导出范围必须明确：scope=page（当前一页）或 scope=selected（选中记录），"
            "不支持含糊的整项目导出",
        )
    if kind == "selected":
        chosen = list(ids or [])
        if len(chosen) > MAX_SELECTED_ROWS:
            raise ApiError(413, "EXPORT_TOO_LARGE", f"一次最多导出 {MAX_SELECTED_ROWS} 条选中记录")
        records = [detection_service.public(row) for row in _rows_for_ids(conn, user_id, project_id, chosen)]
    else:
        cap = max(1, min(int(limit or 20), MAX_PAGE_ROWS))
        found = detection_service.list_for_project(
            conn,
            int(user_id),
            project_id,
            status=(filters or {}).get("status") or None,
            model_id=(filters or {}).get("model_id") or None,
            session_id=(filters or {}).get("session_id") or None,
            has_hits=(filters or {}).get("has_hits"),
            date_from=(filters or {}).get("date_from") or None,
            date_to=(filters or {}).get("date_to") or None,
            limit=cap,
            deleted=(filters or {}).get("deleted") or "exclude",
            q=(filters or {}).get("q") or None,
        )
        records = found["records"]
    if not records:
        raise ApiError(404, "EXPORT_EMPTY", "这个范围内没有记录，未生成文件")

    record_ids = [str(item["id"]) for item in records]
    reviews = _review_map(conn, user_id, project_id, record_ids)
    feedback = _feedback_map(conn, user_id, project_id, record_ids)
    rows: list[list[Any]] = []
    for item in records:
        review = reviews.get(str(item["id"])) or {}
        rows.append(
            [
                item["id"],
                item["created_at"],
                item["status"],
                item["model_id"],
                item["hit_count"],
                json.dumps(item.get("class_summary") or {}, ensure_ascii=False),
                f"{float(item.get('confidence') or 0):.4f}",
                item["rgb_asset_id"],
                item.get("x_asset_id") or "",
                item.get("x_origin") or "",
                review.get("outcome") or "",
                review.get("version") or "",
                feedback.get(str(item["id"])) or "",
            ]
        )

    root, folder = _folder(conn, user_id, project_id)
    stamp = security.now_iso().replace("-", "").replace(":", "").replace("T", "-").split(".")[0]
    name = storage.safe_name(f"records-{kind}-{stamp}-{security.new_token(3)}.csv", fallback="records.csv")
    target = folder / name
    data = _write_csv(target, rows)

    # 回读：行数、字节、编码都要对得上，否则不返回成功。
    read_back = target.read_bytes()
    if read_back != data:
        raise ApiError(409, "EXPORT_VERIFY_FAILED", "导出文件回读后与写入内容不一致，请重试")
    parsed = list(csv.reader(io.StringIO(read_back.decode("utf-8-sig"))))
    if len(parsed) != len(rows) + 1:
        raise ApiError(
            409,
            "EXPORT_VERIFY_FAILED",
            f"导出文件回读行数不对（期望 {len(rows) + 1} 行，实际 {len(parsed)} 行）",
        )
    return {
        "relative": storage.relative(root, target),
        "filename": name,
        "rows": len(rows),
        "bytes": len(read_back),
        "scope": kind,
        "sha256": security.fingerprint(read_back.decode("utf-8", "replace")),
        "columns": list(COLUMNS),
        "verification": {"verified": True, "rows": len(rows), "bytes": len(read_back)},
        "note": (
            "CSV 只包含本次明确范围（一页或选中记录）；人工结论取最近一次已提交版本，"
            "草稿不算。"
        ),
    }


def dataset_zip(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    """数据集导出包：没有就现生成一个，返回真实路径与大小。"""
    from backend.services import datasets as dataset_service
    from backend.services import reflow as reflow_service

    row = dataset_service.get_owned(conn, user_id, project_id, dataset_id)
    root = storage.project_root(conn, user_id, project_id)
    rel = str(row["export_rel"] or "")
    path: Path | None = None
    if rel:
        try:
            candidate = storage.resolve_rel(root, rel, must_exist=True)
            if candidate.is_file():
                path = candidate
        except ApiError:
            path = None
    if path is None:
        # 数据集根是**目录**，不能用 must_exist=True（那是文件判定）
        base = storage.resolve_rel(root, str(row["root_rel"]))
        if not base.is_dir():
            raise ApiError(404, "FILE_MISSING", "数据集目录已不存在，无法导出")
        rel = reflow_service._export_zip(root, base, str(dataset_id))
        conn.execute("UPDATE datasets SET export_rel = ? WHERE id = ?", (rel, str(dataset_id)))
        conn.commit()
        path = storage.resolve_rel(root, rel, must_exist=True)
    size = path.stat().st_size
    if size <= 0:
        raise ApiError(409, "EXPORT_VERIFY_FAILED", "导出包是空文件，未返回成功")
    return {
        "relative": storage.relative(root, path),
        "filename": path.name,
        "bytes": int(size),
        "dataset_id": str(dataset_id),
        "verification": {"verified": True, "bytes": int(size)},
        "note": "导出包是项目工作区里的真实 ZIP；下载入口在数据集页。",
    }


_COMPARE_KEYS = ("map50", "map50_95", "precision", "recall")


def models_compare(conn, user_id: int, project_id: str, left_id: str, right_id: str) -> dict:
    """两个模型的指标与条件对照；条件不同**不**给出"更好"的结论。"""
    from backend.services import models as model_service

    left = model_service.public(model_service.get_owned(conn, user_id, project_id, left_id))
    right = model_service.public(model_service.get_owned(conn, user_id, project_id, right_id))
    left_metrics = left.get("metrics") or {}
    right_metrics = right.get("metrics") or {}
    diffs: dict[str, dict] = {}
    for key in _COMPARE_KEYS:
        a = left_metrics.get(key)
        b = right_metrics.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            diffs[key] = {"left": float(a), "right": float(b), "delta": round(float(b) - float(a), 6)}
    condition_fields = ("dataset_id", "training_run_id", "source")
    condition_diff = [
        {"field": field, "left": left.get(field), "right": right.get(field)}
        for field in condition_fields
        if left.get(field) != right.get(field)
    ]
    comparable = bool(diffs) and not condition_diff
    return {
        "left": {"id": left["id"], "name": left["name"], "status": left["status"], "source": left.get("source")},
        "right": {"id": right["id"], "name": right["name"], "status": right["status"], "source": right.get("source")},
        "metrics": diffs,
        "conditions": condition_diff,
        "comparable": comparable,
        "note": (
            "指标来自各自训练时的验证集记录。"
            + ("" if comparable else "两者的数据集/训练运行/来源不同，只能并列展示差异，不能说“谁更好”。")
        ),
    }


__all__ = [
    "COLUMNS",
    "EXPORTS",
    "dataset_zip",
    "models_compare",
    "records_csv",
]
