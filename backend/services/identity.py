"""样本身份：用内容而不是文件名判断“是不是同一张图”。

三层指纹，按可信度从高到低：

* ``sha256``：原始字节。完全相同才算同一份文件；
* ``pixel_sha256``：解码后的像素（RGB 展平 + 尺寸）。重新压缩/改扩展名仍能对上，
  这是“同图”的主要依据；
* ``sample_key``：数据集导入时的来源键（划分/相对路径/文件名），只用于追溯，
  不单独作为“同一张图”的证据。

同名、同大小、同修改时间都不参与判定——它们太容易碰巧相同。
匹配到多条即“歧义”，交给用户选择，绝不自动覆盖。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from backend.errors import ApiError
from backend.services import storage

PIXEL_VERSION = "rgb-v1"


def pixel_digest(data: bytes) -> tuple[str, int, int]:
    """解码后的像素指纹。解不出来就返回空，让上层按“无法确认”处理。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            converted = image.convert("RGB")
            width, height = converted.size
            payload = converted.tobytes()
    except Exception:
        return "", 0, 0
    digest = hashlib.sha256(
        PIXEL_VERSION.encode("ascii") + b"|" + str(width).encode() + b"x" + str(height).encode()
        + b"|" + payload
    ).hexdigest()
    return digest, width, height


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def visual_hash(data: bytes) -> str:
    """16x16 灰度均值哈希：只用于**列候选**，不能单独当作同一张图的证据。"""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            small = opened.convert("L").resize((16, 16))
            pixels = list(small.getdata())
    except Exception:
        return ""
    if len(pixels) != 256:
        return ""
    average = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= average else 0)
    return f"{bits:064x}"


def visual_distance(left: str, right: str) -> int:
    if not left or not right:
        return 999
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def source_key(value: str | None) -> str:
    """把来源路径归一成 ``<split>/<stem>``；只追溯，不作为内容相同的证据。"""
    text = str(value or "").replace("\\", "/").strip().lstrip("./")
    if not text:
        return ""
    parts = [part for part in PurePosixPath(text).parts if part not in {".", ".."}]
    stem = PurePosixPath(parts[-1]).stem if parts else ""
    split = next((part for part in parts if part in {"train", "val", "test"}), "")
    return f"{split}/{stem}" if split and stem else "/".join(parts)


def identity_of_bytes(data: bytes) -> dict:
    sha = file_digest(data)
    pixel, width, height = pixel_digest(data)
    return {
        "sha256": sha,
        "pixel_sha256": pixel,
        "visual_hash": visual_hash(data),
        "width": width,
        "height": height,
    }


def identity_of_asset(conn, user_id: int, project_id: str, asset_id: str) -> dict:
    from backend.services import assets as asset_service

    row, data = asset_service.read_bytes(conn, user_id, project_id, asset_id)
    identity = identity_of_bytes(data)
    identity.update(
        {
            "asset_id": str(asset_id),
            "filename": row["filename"],
            "source_path": row["source_path"],
        }
    )
    return identity


def match_record(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    dataset,
    samples: list[dict],
) -> dict:
    """把一条记录的原图与样本清单做综合身份匹配。

    判定只看**内容**：

    * 原字节 / 解码像素完全一致：唯一命中才算 ``unique``（可自动使用）；
    * 来源 sample_key 相同但字节不同：只是 ``transformed`` 候选，**不是**同图证据；
    * 感知哈希相近：只是 ``visual_similar`` 候选，同样不自动采用。

    只有弱候选时返回 ``candidate``（需要用户明确选目标或显式按新图处理），
    不是 ``unique``——早期版本把 ``transformed`` 也算强命中，于是“同名不同内容”
    会被自动替换，这正好违反本函数自己的说明。
    """
    record = conn.execute(
        "SELECT * FROM detections WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(record_id), str(project_id), int(user_id)),
    ).fetchone()
    if record is None:
        return {"status": "missing", "matches": [], "hints": [], "candidates": [],
                "note": "记录不存在或不属于当前项目"}
    identity = identity_of_asset(conn, user_id, project_id, str(record["rgb_asset_id"]))
    root = storage.project_root(conn, user_id, project_id)
    candidates: dict[str, dict] = {}

    def add(sample: dict, reason: str) -> None:
        key = str(sample["id"])
        current = candidates.get(key)
        entry = {
            "sample_id": key,
            "split": str(sample["split"]),
            "stem": str(sample["stem"]),
            "rgb_rel": str(sample.get("rgb_rel") or ""),
            "matched_by": reason,
            "identity_state": str(sample.get("identity_state") or ""),
        }
        if current is None:
            candidates[key] = entry
            return
        if current["matched_by"] != reason and reason != "visual_similar":
            current["matched_by"] = f"{current['matched_by']},{reason}"

    for sample in samples:
        # 文件已经和记录不一致的样本：登记的指纹只能指出“大概就是它”，
        # 不能当同图证据——磁盘上现在不是那张图了，坐标也就不可信。
        stale = str(sample.get("identity_state") or "") == IDENTITY_DRIFT
        exact_sha = bool(identity["sha256"]) and str(sample.get("sha256") or "") == identity["sha256"]
        exact_pixel = bool(identity["pixel_sha256"]) and str(sample.get("pixel_sha256") or "") == identity["pixel_sha256"]
        if exact_sha or exact_pixel:
            if stale:
                add(sample, "stale_identity")
            else:
                add(sample, "sha256" if exact_sha else "pixel")
            continue
        key = source_key(str(sample.get("origin_sample_key") or ""))
        if key and key == source_key(identity.get("source_path")):
            add(sample, "transformed")
            continue
        similar = 999
        cached_visual = str(sample.get("visual_hash") or "")
        if cached_visual:
            similar = visual_distance(identity["visual_hash"], cached_visual)
        else:
            try:
                path = storage.resolve_rel(root, str(sample["rgb_rel"]), must_exist=True)
                similar = visual_distance(identity["visual_hash"], visual_hash(path.read_bytes()))
            except Exception:
                similar = 999
        if similar <= 12:
            add(sample, "visual_similar")

    matches = list(candidates.values())
    strong = [item for item in matches if is_strong(item["matched_by"])]
    weak = [item for item in matches if not is_strong(item["matched_by"])]
    # 只有“来源同名但内容不同”和“撞上已改文件”才算需要用户裁决的候选；感知相近只是提示。
    # 实测两张纯色图（内容完全不同）的 16x16 均值哈希完全一致，若拿它改状态，
    # 任何新图都会变成“需要选择”，回原修订就永远做不成。
    flagged = [
        item for item in weak
        if "transformed" in str(item["matched_by"]) or "stale_identity" in str(item["matched_by"])
    ]
    stale_hits = [item for item in weak if "stale_identity" in str(item["matched_by"])]
    if len(strong) > 1:
        status = "ambiguous"
    elif len(strong) == 1:
        status = "unique"
    elif flagged:
        status = "candidate"
    else:
        status = "new"
    note = ""
    if status == "ambiguous":
        note = "这张图在数据集里有多条内容相同的样本（可能是历史重复）；请明确指定要覆盖哪一条，系统不会猜。"
    elif status == "candidate" and stale_hits:
        note = (
            "命中的样本文件内容已经被改过，登记的指纹不能再证明它还是那张图；"
            "请先重新核验这个样本，或显式按“新图追加”。"
        )
    elif status == "candidate":
        note = (
            "只找到了来源同名或视觉相近的候选，字节/像素并不一致，不能当作同一张图自动替换；"
            "请确认目标样本，或显式按“新图追加”。"
        )
    return {
        "status": status,
        "identity": identity,
        "matches": strong,
        "hints": weak,
        # 已改文件的样本不放进可选目标：选了也只会被判 SAMPLE_CHANGED，
        # 放在 hints 里提示它存在就够了。
        "candidates": [item for item in weak if "stale_identity" not in str(item["matched_by"])],
        "note": note,
    }


def is_strong(matched_by: str) -> bool:
    """强弱唯一判据：字节或像素一致才算同图。来源同名、视觉相近都只是候选。"""
    for part in str(matched_by or "").split(","):
        if part in {"sha256", "pixel", "user_selected"}:
            return True
    return False


def sample_identity(sample: dict) -> tuple[str, str]:
    """返回 ``(主键, 次键)``：有像素指纹优先用它，否则退回字节指纹。"""
    pixel = str(sample.get("pixel_sha256") or "")
    sha = str(sample.get("sha256") or "")
    return (pixel or sha, sha)


# --------------------------------------------------------------------------- 老样本身份读取

# 这些状态来自**真实文件**，不是库里的旧值；调用方据此决定“能不能自动用”。
IDENTITY_CACHED = "cached"
IDENTITY_DERIVED = "derived"
IDENTITY_DRIFT = "drift"
IDENTITY_MISSING = "missing"
IDENTITY_UNREADABLE = "unreadable"
PROBLEM_STATES = (IDENTITY_DRIFT, IDENTITY_MISSING, IDENTITY_UNREADABLE)
PROBLEM_CODES = {
    IDENTITY_DRIFT: "SAMPLE_CHANGED",
    IDENTITY_MISSING: "SAMPLE_MISSING",
    IDENTITY_UNREADABLE: "SAMPLE_UNREADABLE",
}


def inspect_sample(root: Path, sample: dict) -> dict:
    """读**授权目录里的真实文件**，判断这个样本当前的身份，绝不写库。

    文件戳（大小 + mtime）只是**提示**，不能当“内容没变”的证据：改写文件后用
    ``os.utime`` 把 mtime 改回去，戳看着一模一样，内容已经换了。所以这里每次都读字节
    算 SHA-256；**字节与记录一致时直接沿用像素指纹，不再解码**（省掉解码开销）。
    mtime/size 只用来决定“要不要顺手刷新戳”。

    三种结论：

    * ``cached``：内容与记录一致（字节一致；只有像素指纹的行则像素一致），可以信；
    * ``derived``：库里没有指纹（03 导入的老行就是这样），现在按真实文件补出来；
    * ``drift`` / ``missing`` / ``unreadable``：文件被改过、不存在或读不出来。
      这三种**不能**当成“新图”，也不能拿旧指纹顶上，必须让用户重新核验。
    """
    rel = str(sample.get("rgb_rel") or "")
    cached_sha = str(sample.get("sha256") or "")
    cached_pixel = str(sample.get("pixel_sha256") or "")
    result: dict[str, Any] = {
        "sample_id": str(sample.get("id") or ""),
        "split": str(sample.get("split") or ""),
        "stem": str(sample.get("stem") or ""),
        "cached_sha256": cached_sha,
        "cached_pixel_sha256": cached_pixel,
        "size": 0,
        "mtime_ns": 0,
        "actual_sha256": "",
        "identity": None,
        "state": IDENTITY_MISSING,
        "reason": "",
        "stat_refresh": False,
        "backfill": False,
    }
    if not rel:
        result["reason"] = "样本没有 RGB 相对路径，无法核验内容"
        return result
    try:
        path = storage.resolve_rel(root, rel, must_exist=True)
        stat = path.stat()
    except (ApiError, OSError):
        result["reason"] = "样本图片不在项目工作区里（被移动或删除）"
        return result

    result["size"] = int(stat.st_size)
    result["mtime_ns"] = int(stat.st_mtime_ns)
    # 只是提示：戳相同不代表内容没变（mtime 可以被改回去），下面仍然读字节核对。
    stamp_same = (
        int(sample.get("identity_size") or 0) == result["size"]
        and int(sample.get("identity_mtime_ns") or 0) == result["mtime_ns"]
    )

    try:
        data = path.read_bytes()
    except OSError:
        result["state"] = IDENTITY_UNREADABLE
        result["reason"] = "样本图片读不出来（权限或文件损坏）"
        return result

    actual_sha = file_digest(data)
    result["actual_sha256"] = actual_sha

    if cached_sha:
        if actual_sha == cached_sha:
            # 字节逐位相同 → 内容必然没变；像素指纹直接沿用，不必再解码。
            result["state"] = IDENTITY_CACHED
            result["identity"] = {"sha256": cached_sha, "pixel_sha256": cached_pixel}
            result["stat_refresh"] = not stamp_same
            if not cached_pixel:
                # 老的“只有字节指纹”行：顺手补一次像素指纹，之后能按同图比对。
                detail = identity_of_bytes(data)
                if detail["pixel_sha256"]:
                    result["identity"] = detail
                    result["backfill"] = True
            return result
        # 字节变了：内容确实不同。解码只是为了让结论准确（像素是否也变了）。
        result["state"] = IDENTITY_DRIFT
        detail = identity_of_bytes(data)
        if detail["pixel_sha256"]:
            result["identity"] = detail
            if cached_pixel and cached_pixel == detail["pixel_sha256"]:
                result["reason"] = "文件字节与记录的指纹不一致，但像素未变（被重新编码过），仍需重新核验"
            else:
                result["reason"] = "文件内容与记录的指纹不一致（图片被改过）"
        else:
            result["identity"] = {"sha256": actual_sha, "pixel_sha256": ""}
            result["reason"] = "文件内容与记录的指纹不一致，且当前文件已无法解码"
        return result

    if cached_pixel:
        # 只有像素指纹的行：必须解码，按像素判断是不是同一张图。
        detail = identity_of_bytes(data)
        if not detail["pixel_sha256"]:
            result["state"] = IDENTITY_UNREADABLE
            result["reason"] = "样本图片无法解码，不能作为身份依据"
            return result
        result["identity"] = detail
        if detail["pixel_sha256"] == cached_pixel:
            result["state"] = IDENTITY_CACHED
            result["stat_refresh"] = not stamp_same
            result["backfill"] = True  # 顺带把字节指纹补齐，下次可走快路径
        else:
            result["state"] = IDENTITY_DRIFT
            result["reason"] = "文件像素与记录的指纹不一致（图片被改过）"
        return result

    # 两条指纹都没有：按真实文件补出来。
    detail = identity_of_bytes(data)
    if not detail["sha256"] or not detail["pixel_sha256"]:
        result["state"] = IDENTITY_UNREADABLE
        result["reason"] = "样本图片无法解码，不能作为身份依据"
        return result
    result["identity"] = detail
    result["state"] = IDENTITY_DERIVED
    return result


def prepare_samples(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    """回流匹配 / 查重 / 重新划分共用的**同一条**身份读取路径。

    老样本（``sha256`` 与 ``pixel_sha256`` 都为 NULL）在这里按真实文件补齐并落库，
    来源键按导入布局 ``<split>/<stem>`` 推出；文件被改或不存在的样本不改库、只报问题，
    让上层明确拒绝，而不是把它们当“新图”。
    """
    dataset = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if dataset is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在或不属于当前项目")
    root = storage.project_root(conn, user_id, project_id)
    rows = conn.execute(
        "SELECT * FROM dataset_samples WHERE dataset_id = ? ORDER BY split, stem",
        (str(dataset_id),),
    ).fetchall()

    samples: list[dict] = []
    problems: list[dict] = []
    derived = 0
    refreshed = 0
    for row in rows:
        sample = dict(row)
        if not sample.get("origin_sample_key"):
            # 来源键是路径事实，不需要读内容；老行用它恢复“同一来源”的追溯能力。
            sample["origin_sample_key"] = f"{sample['split']}/{sample['stem']}"
        info = inspect_sample(root, sample)
        sample["identity_state"] = info["state"]
        identity = info["identity"] or {}
        # 只有“记录可信”的行才把真实文件指纹带进内存：drift 行必须保留**记录时**的
        # 指纹，不能拿当前（已改）文件的内容冒充样本身份。
        if info["state"] in (IDENTITY_CACHED, IDENTITY_DERIVED) and identity.get("sha256"):
            sample["sha256"] = identity["sha256"]
            sample["pixel_sha256"] = identity.get("pixel_sha256") or sample.get("pixel_sha256")
            sample["width"] = int(sample.get("width") or identity.get("width") or 0)
            sample["height"] = int(sample.get("height") or identity.get("height") or 0)
            sample["visual_hash"] = identity.get("visual_hash") or ""
        if info["state"] == IDENTITY_DERIVED:
            derived += 1
            conn.execute(
                "UPDATE dataset_samples SET sha256 = ?, pixel_sha256 = ?, origin_sample_key = ?,"
                " identity_size = ?, identity_mtime_ns = ?,"
                " width = CASE WHEN width IS NULL OR width = 0 THEN ? ELSE width END,"
                " height = CASE WHEN height IS NULL OR height = 0 THEN ? ELSE height END"
                " WHERE id = ?",
                (
                    identity.get("sha256"),
                    identity.get("pixel_sha256"),
                    sample["origin_sample_key"],
                    info["size"],
                    info["mtime_ns"],
                    int(identity.get("width") or 0),
                    int(identity.get("height") or 0),
                    str(sample["id"]),
                ),
            )
        elif info["state"] == IDENTITY_CACHED and (info.get("stat_refresh") or info.get("backfill")):
            refreshed += 1
            # 只补空的列，绝不覆盖已有的指纹；戳一并更新，纯提示用途。
            conn.execute(
                "UPDATE dataset_samples SET"
                " sha256 = CASE WHEN sha256 IS NULL OR sha256 = '' THEN ? ELSE sha256 END,"
                " pixel_sha256 = CASE WHEN pixel_sha256 IS NULL OR pixel_sha256 = '' THEN ?"
                " ELSE pixel_sha256 END,"
                " identity_size = ?, identity_mtime_ns = ?"
                " WHERE id = ?",
                (
                    identity.get("sha256") or "",
                    identity.get("pixel_sha256") or "",
                    info["size"],
                    info["mtime_ns"],
                    str(sample["id"]),
                ),
            )
        elif info["state"] in PROBLEM_STATES:
            problems.append(
                {
                    "sample_id": str(sample["id"]),
                    "split": str(sample["split"]),
                    "stem": str(sample["stem"]),
                    "rgb_rel": str(sample.get("rgb_rel") or ""),
                    "code": PROBLEM_CODES[info["state"]],
                    "state": info["state"],
                    "message": info["reason"],
                }
            )
        samples.append(sample)
    if derived or refreshed:
        conn.commit()
    return {
        "dataset_id": str(dataset_id),
        "samples": samples,
        "problems": problems,
        "derived": derived,
        "refreshed": refreshed,
        "total": len(samples),
    }


def problem_conflicts(problems: list[dict], *, limit: int = 5) -> list[dict]:
    """把文件问题翻成回流冲突：宁可停，也不拿旧指纹猜。"""
    if not problems:
        return []
    shown = problems[:limit]
    codes = sorted({item["code"] for item in problems})
    label = "、".join(
        f"{item['split']}/{item['stem']}（{item['message']}）" for item in shown
    )
    more = f" 等 {len(problems)} 个样本" if len(problems) > len(shown) else ""
    return [
        {
            "record_id": "",
            "code": "DATASET_IDENTITY_UNVERIFIED" if len(codes) > 1 else codes[0],
            "message": (
                f"父数据集里有样本的真实文件与记录不一致：{label}{more}。"
                "先用真实文件重新核验（或恢复原文件）再回流；系统不会把未核实的数据当新图追加，也不会用旧指纹覆盖当前文件。"
            ),
            "details": {"problems": shown, "codes": codes, "count": len(problems)},
        }
    ]


def dataset_report(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    """只读检查：重复组、跨划分泄漏、标注冲突。不删任何东西。

    身份依据走 ``prepare_samples``，与回流匹配、重新划分完全一致——
    否则“查重报告说没问题、回流却把两张图当一张”这种矛盾就会再次出现。
    """
    prepared = prepare_samples(conn, user_id, project_id, dataset_id)
    rows = prepared["samples"]
    groups: dict[str, list[dict]] = {}
    missing = 0
    for row in rows:
        key, _ = sample_identity(row)
        if not key:
            missing += 1
            continue
        groups.setdefault(key, []).append(row)
    duplicates = []
    leakage = []
    conflicts = []
    for key, items in groups.items():
        if len(items) > 1:
            duplicates.append(
                {
                    "identity": key,
                    "samples": [
                        {"id": item["id"], "split": item["split"], "stem": item["stem"],
                         "rgb_rel": item["rgb_rel"]}
                        for item in items
                    ],
                }
            )
        splits = {str(item["split"]) for item in items}
        if len(splits) > 1:
            leakage.append({"identity": key, "splits": sorted(splits),
                            "stems": [item["stem"] for item in items]})
        labels = {str(item["boxes"] or "[]") for item in items}
        if len(labels) > 1:
            conflicts.append(
                {
                    "identity": key,
                    "stems": [item["stem"] for item in items],
                    "variants": len(labels),
                }
            )
    return {
        "dataset_id": str(dataset_id),
        "sample_count": len(rows),
        "duplicate_groups": duplicates,
        "cross_split_leakage": leakage,
        "annotation_conflicts": conflicts,
        "identity_missing": missing,
        "identity_derived": prepared["derived"],
        "identity_problems": prepared["problems"],
        "identity_states": {
            state: sum(1 for row in rows if str(row.get("identity_state")) == state)
            for state in (IDENTITY_CACHED, IDENTITY_DERIVED, *PROBLEM_STATES)
        },
        "note": "只读检查：不会自动删除或合并任何样本。",
    }


def match_records(conn, user_id: int, project_id: str, dataset_id: str, record_ids: list[str]) -> list[dict]:
    """只读身份检查：与回流使用同一套匹配规则，避免 UI 与执行判断不一致。"""
    prepared = prepare_samples(conn, user_id, project_id, dataset_id)
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?", (str(dataset_id),)).fetchone()
    samples = prepared["samples"]
    results: list[dict] = []
    for record_id in record_ids:
        result = match_record(conn, user_id, project_id, str(record_id), dataset, samples)
        result["record_id"] = str(record_id)
        results.append(result)
    return results


def resolve_dataset_sample(dataset_row: dict) -> Path:
    return Path(str(dataset_row["rgb_rel"]))


def loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


__all__ = [
    "IDENTITY_CACHED",
    "IDENTITY_DERIVED",
    "IDENTITY_DRIFT",
    "IDENTITY_MISSING",
    "IDENTITY_UNREADABLE",
    "dataset_report",
    "file_digest",
    "identity_of_asset",
    "identity_of_bytes",
    "inspect_sample",
    "is_strong",
    "loads",
    "match_record",
    "match_records",
    "pixel_digest",
    "prepare_samples",
    "problem_conflicts",
    "sample_identity",
    "source_key",
    "visual_distance",
    "visual_hash",
]
