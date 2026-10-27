"""Derived dataset candidates: preview, build, register (C-08).

A transform never edits the source. It produces a *candidate* directory plus a
manifest that records the source, the rules, every conflict and the per-sample
mapping. Registration then feeds that directory through the same archive
contract used by C-03, so a candidate cannot become a dataset by another route.

Class remapping is explicit: the caller supplies ``class_map`` and/or
``keep_classes``; nothing is inferred from names. When two source classes would
collapse onto one target name, or a class disappears without being asked to,
the plan reports a conflict instead of guessing.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from services.authorized_directories import scan_dataset_tree

CANDIDATE_STATUS = {"prepared": "prepared", "built": "built", "registered": "registered"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_root(adapter) -> Path:
    from config import TEMP_DIR

    root = Path(adapter.candidate_root or Path(TEMP_DIR) / "dataset_candidates")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _table(adapter) -> None:
    with adapter.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dataset_candidates (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT,
                dataset_mode TEXT NOT NULL DEFAULT 'single',
                primary_modality TEXT NOT NULL DEFAULT 'rgb',
                secondary_modality TEXT,
                class_names_json TEXT NOT NULL DEFAULT '[]',
                split_counts_json TEXT NOT NULL DEFAULT '{}',
                sample_count INTEGER NOT NULL DEFAULT 0,
                manifest_path TEXT,
                status TEXT NOT NULL DEFAULT 'prepared',
                dataset_id TEXT,
                created_at TEXT NOT NULL
            )"""
        )


def get_candidate(adapter, candidate_id: str) -> dict:
    _table(adapter)
    with adapter.connect() as conn:
        row = conn.execute(
            "SELECT * FROM dataset_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
    if row is None:
        raise FileNotFoundError("候选数据集不存在")
    return dict(row)


def _candidate_name_taken(adapter, name: str) -> str | None:
    with adapter.connect() as conn:
        row = conn.execute("SELECT id FROM datasets WHERE TRIM(name)=?", (name.strip(),)).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM dataset_candidates WHERE TRIM(name)=? AND status<>'failed'",
            (name.strip(),),
        ).fetchone()
    return row["id"] if row else None


def _dataset_scan(adapter, dataset_resource) -> tuple[dict, dict]:
    """Download the registered archive, extract it privately and scan it."""
    from services.dataset_contract import safe_extract_dataset

    if adapter.storage_client is None:
        raise ValueError("A protected storage adapter is required")
    row = adapter._dataset_row(dataset_resource.id)
    if not row.get("zip_bucket") or not row.get("zip_object_key"):
        raise ValueError("数据集没有可读取的数据包")
    workspace = Path(tempfile.mkdtemp(prefix="transform-src-", dir=adapter.work_dir or None))
    archive = workspace / "source.zip"
    payload = adapter.storage_client.get(row["zip_bucket"], row["zip_object_key"])
    archive.write_bytes(payload)
    extract_dir = workspace / "source"
    root = safe_extract_dataset(str(archive), str(extract_dir))
    scan = scan_dataset_tree(root)
    scan['_workspace'] = str(workspace)
    source = {
        "type": "dataset",
        "dataset_id": row["id"],
        "name": row["name"],
        "dataset_mode": row.get("dataset_mode") or "single",
        "primary_modality": row.get("primary_modality") or "rgb",
        "secondary_modality": row.get("secondary_modality"),
        "archive_sha256": __import__("hashlib").sha256(payload).hexdigest(),
    }
    return scan, source


def _feedback_samples(adapter, dataset_id: str) -> list[dict]:
    """Human-confirmed feedback rows only, as extra sources for an explicit merge."""
    with adapter.connect() as conn:
        rows = conn.execute(
            """SELECT fs.id AS sample_id, fs.annotation_hash, dr.id AS record_id, dr.corrected_annotations,
                      dr.original_image_bucket, dr.original_image_object_key,
                      dr.secondary_image_bucket, dr.secondary_image_object_key
               FROM feedback_samples fs
               JOIN detection_records dr ON dr.id = fs.record_id
               WHERE fs.dataset_id=? AND dr.human_review_status='confirmed'
               ORDER BY fs.id""",
            (dataset_id,),
        ).fetchall()
    from services.feedback_dataset_service import _canonical_annotations
    current = []
    for row in rows:
        item = dict(row)
        canonical, _ = _canonical_annotations(item['corrected_annotations'])
        if hashlib.sha256(canonical.encode('utf-8')).hexdigest() == item['annotation_hash']:
            current.append(item)
    return current


def _merge_feedback(adapter, scan, source):
    """Materialize confirmed feedback into a private source copy before planning.

    Feedback has no implicit split: an explicit split_policy is required, or
    the planner blocks on unassigned samples. The original archive is untouched.
    """
    from services.feedback_dataset_service import _canonical_annotations, _yolo_label
    from services.review_service import _validate_annotations
    root = Path(scan['root'])
    rows = _feedback_samples(adapter, source['dataset_id'])
    if not rows:
        raise ValueError('没有与当前人工标注版本一致的反馈样本，请先人工确认并加入反馈池')
    for row in rows:
        _, annotations = _canonical_annotations(row['corrected_annotations'])
        _validate_annotations(annotations, scan['class_names'])
        key = 'feedback_' + row['sample_id']
        assets = [('images', row['original_image_bucket'], row['original_image_object_key'])]
        if source.get('secondary_modality'):
            assets.append(('images_' + source['secondary_modality'], row['secondary_image_bucket'], row['secondary_image_object_key']))
        for directory, bucket, object_key in assets:
            if not bucket or not object_key:
                raise ValueError('反馈样本缺少所需配套图片，不能合并')
            destination = root / directory / 'unassigned' / (key + (Path(object_key).suffix.lower() or '.jpg'))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(adapter.storage_client.get(bucket, object_key))
        label = root / 'labels' / 'unassigned' / (key + '.txt')
        label.parent.mkdir(parents=True, exist_ok=True)
        label.write_text(_yolo_label(annotations), encoding='utf-8')
    merged = scan_dataset_tree(root)
    merged['_workspace'] = scan['_workspace']
    source['feedback_samples'] = [{'sample_id': r['sample_id'], 'record_id': r['record_id'], 'annotation_hash': r['annotation_hash']} for r in rows]
    return merged


def _input_fingerprint(scan):
    items = []
    for sample in sorted(scan['samples'], key=lambda s: s['key']):
        files = [(name, hashlib.sha256(Path(path).read_bytes()).hexdigest()) for name, path in sorted(sample['modalities'].items())]
        label = hashlib.sha256(Path(sample['label']).read_bytes()).hexdigest() if sample.get('label') else None
        items.append([sample['key'], files, label])
    return hashlib.sha256(json.dumps([scan['class_names'], items], sort_keys=True).encode()).hexdigest()


def _plan(scan: dict, arguments: dict, source: dict, provided_classes: list[str] | None) -> dict:
    source_classes = list(scan["class_names"]) if not scan["class_names_derived"] else []
    if provided_classes:
        source_classes = list(provided_classes)
    conflicts: list[dict] = []
    warnings: list[dict] = []
    if scan.get('sample_count', len(scan['samples'])) != len(scan['samples']):
        conflicts.append({'code': 'SCAN_TRUNCATED', 'message': '源样本超过扫描上限，不能把部分扫描结果当成完整候选'})
    for issue in scan.get('issues', []):
        if issue.get('severity') == 'blocking' and not (provided_classes and issue.get('code') == 'CLASS_NAMES_MISSING'):
            conflicts.append(issue)
    for sample in scan['samples']:
        if not sample.get('label') or 'rgb' not in sample['modalities']:
            conflicts.append({'code': 'SAMPLE_INCOMPLETE', 'message': '样本缺少 RGB 或标签', 'key': sample['key']})
        if source.get('secondary_modality') and source['secondary_modality'] not in sample['modalities']:
            conflicts.append({'code': 'PAIR_MISSING', 'message': '样本缺少配套模态', 'key': sample['key']})
    if not source_classes:
        conflicts.append({"code": "CLASS_NAMES_REQUIRED",
                          "message": "源数据没有类别列表，需显式提供 class_names"})
        return {
            "effect": "write", "source": source, "target": {},
            "class_names": [], "counts": {}, "conflicts": conflicts,
            "warnings": warnings, "blocked": True, "conflicts_only": True,
        }

    class_map = dict(arguments.get("class_map") or {})
    unknown = [name for name in class_map if name not in source_classes]
    if unknown:
        conflicts.append({"code": "CLASS_MAP_UNKNOWN",
                          "message": f"映射里的源类别不存在: {', '.join(unknown)}"})
    keep = list(arguments.get("keep_classes") or [])
    unknown_keep = [name for name in keep if name not in source_classes]
    if unknown_keep:
        conflicts.append({"code": "CLASS_FILTER_UNKNOWN",
                          "message": f"保留列表里的类别不存在: {', '.join(unknown_keep)}"})

    kept = [name for name in source_classes if not keep or name in keep]
    target_names = [class_map.get(name, name) for name in kept]
    if len(set(target_names)) != len(target_names):
        duplicates = sorted({name for name in target_names if target_names.count(name) > 1})
        conflicts.append({"code": "CLASS_MAP_DUPLICATE",
                          "message": f"多个源类别映射到同一名称: {', '.join(duplicates)}"})
    if not target_names:
        conflicts.append({"code": "CLASS_FILTER_EMPTY", "message": "筛选后没有剩余类别"})
    index_map = {name: target_names.index(class_map.get(name, name)) for name in kept
                 if class_map.get(name, name) in target_names}

    split_policy = arguments.get("split_policy") or None
    if split_policy:
        unknown_splits = set(split_policy) - {"train", "val", "test"}
        total = sum(float(value) for value in split_policy.values())
        if unknown_splits:
            conflicts.append({"code": "SPLIT_POLICY_INVALID",
                              "message": f"未知划分: {', '.join(sorted(unknown_splits))}"})
        if abs(total - 1.0) > 0.001:
            conflicts.append({"code": "SPLIT_POLICY_SUM",
                              "message": f"划分比例之和为 {total:.3f}，应为 1"})
        if float(split_policy.get("train", 0)) <= 0 or float(split_policy.get("val", 0)) <= 0:
            conflicts.append({"code": "SPLIT_POLICY_EMPTY",
                              "message": "train 与 val 都必须大于 0"})

    included: list[dict] = []
    seen_content: dict[str, str] = {}
    skipped_duplicates = []
    for sample in scan["samples"]:
        if not sample["class_ids"]:
            # A background image with an empty label is legitimate.
            if sample["label"] is None:
                warnings.append({"code": "SAMPLE_WITHOUT_LABEL", "key": sample["key"]})
                continue
        kept_ids = [index_map[source_classes[class_id]]
                    for class_id in sample["class_ids"]
                    if 0 <= class_id < len(source_classes)
                    and source_classes[class_id] in index_map]
        dropped = len([value for value in sample["class_ids"]
                       if not (0 <= value < len(source_classes))
                       or source_classes[value] not in index_map])
        if dropped:
            warnings.append({"code": "ANNOTATIONS_DROPPED", "key": sample["key"],
                             "count": dropped})
        if keep and not kept_ids and sample["class_ids"]:
            continue
        digest = sample.get("sha256")
        if digest is None and sample["modalities"].get("rgb"):
            import hashlib

            try:
                digest = hashlib.sha256(
                    Path(sample["modalities"]["rgb"]).read_bytes()
                ).hexdigest()
            except OSError:
                digest = None
        if digest and digest in seen_content:
            previous = next(s for s in included if s['key'] == seen_content[digest])
            if previous.get('label') and sample.get('label') and Path(previous['label']).read_bytes() != Path(sample['label']).read_bytes():
                conflicts.append({'code': 'ANNOTATION_CONFLICT', 'message': '相同图片的标签不同，需人工决定，不能静默去重', 'key': sample['key']})
            skipped_duplicates.append({"key": sample["key"],
                                       "duplicate_of": seen_content[digest]})
            continue
        if digest:
            seen_content[digest] = sample["key"]
        included.append(sample)

    if not included:
        conflicts.append({"code": "NO_SAMPLES", "message": "变换后没有可写入的样本"})

    # Split leakage: identical content in two different splits must stop the
    # transform, including copies the duplicate rule would drop afterwards.
    split_of_key: dict[str, str] = {}
    ordered_all = sorted(scan["samples"], key=lambda item: item["key"])
    if split_policy:
        train_ratio = float(split_policy.get("train", 0))
        val_ratio = float(split_policy.get("val", 0))
        train_end = int(round(len(ordered_all) * train_ratio))
        val_end = train_end + int(round(len(ordered_all) * val_ratio))
        for position, sample in enumerate(ordered_all):
            split_of_key[sample["key"]] = (
                "train" if position < train_end
                else "val" if position < val_end
                else "test"
            )
    else:
        for sample in scan["samples"]:
            split_of_key[sample["key"]] = sample["split"]

    leakage = []
    for entry in scan["duplicates"]:
        splits = {split_of_key[item["key"]] for item in entry["samples"]
                  if item["key"] in split_of_key}
        if len(splits) > 1:
            leakage.append({"sha256": entry["sha256"], "splits": sorted(splits)})
    if leakage:
        conflicts.append({
            "code": "SPLIT_LEAKAGE",
            "message": f"有 {len(leakage)} 组相同图片出现在不同划分，需先处理",
        })
    if skipped_duplicates:
        warnings.append({"code": "DUPLICATES_SKIPPED", "count": len(skipped_duplicates),
                         "samples": skipped_duplicates[:20]})

    counts: dict[str, int] = {}
    for sample in included:
        split = split_of_key[sample["key"]]
        counts[split] = counts.get(split, 0) + 1

    if not split_policy:
        unassigned = [sample["key"] for sample in included
                      if split_of_key.get(sample["key"]) == "unassigned"]
        if unassigned:
            conflicts.append({
                "code": "SPLIT_UNKNOWN",
                "message": (f"{len(unassigned)} 个样本不在 train/val/test 目录下，"
                            "需提供 split_policy 或先整理目录"),
            })

    multimodal = len({modality for sample in included
                      for modality in sample["modalities"]}) > 1
    secondary = source.get("secondary_modality")
    if multimodal and not secondary:
        modalities = sorted({modality for sample in included
                             for modality in sample["modalities"]} - {"rgb"})
        secondary = modalities[0] if modalities else None
    return {
        "effect": "write",
        "source": source,
        "_source_classes": source_classes,
        "_splits": split_of_key,
        "_included": [sample["key"] for sample in included],
        "target": {
            "name": arguments["name"].strip(),
            "description": arguments.get("description", ""),
            "dataset_mode": "multimodal" if multimodal else "single",
            "primary_modality": "rgb",
            "secondary_modality": secondary if multimodal else None,
        },
        "class_names": target_names,
        "class_map": {name: class_map.get(name, name) for name in kept},
        "counts": counts,
        "sample_count": len(included),
        "warnings": warnings,
        "conflicts": conflicts,
        "leakage": {"cross_split": leakage, "count": len(leakage)},
        "blocked": bool(conflicts),
        "ignored_arguments": [name for name in ("split_policy",) if not split_policy],
    }


def plan_from_dataset(adapter, dataset_resource, arguments: dict) -> dict:
    _table(adapter)
    scan, source = _dataset_scan(adapter, dataset_resource)
    try:
        source['project_id'] = dataset_resource.project_id
        if arguments.get('include_feedback'):
            scan = _merge_feedback(adapter, scan, source)
        plan = _plan(scan, arguments, source, arguments.get("class_names"))
        plan['input_fingerprint'] = _input_fingerprint(scan)
    finally:
        shutil.rmtree(scan['_workspace'], ignore_errors=True)
    conflict = _candidate_name_taken(adapter, arguments["name"])
    if conflict:
        plan["conflicts"].append({"code": "NAME_CONFLICT",
                                  "message": "数据集或候选名称已存在"})
        plan["blocked"] = True
    plan["source"]["feedback_available"] = len(_feedback_samples(adapter, dataset_resource.id))
    if arguments.get("include_feedback"):
        plan["source"]["merge_feedback"] = True
        plan["warnings"].append({
            "code": "FEEDBACK_MERGE",
            "message": "将并入人工确认的反馈样本；原反馈流程不变",
        })
    return plan


def plan_from_directory(adapter, resource, root, arguments: dict) -> dict:
    _table(adapter)
    scan = scan_dataset_tree(root)
    source = {"type": "directory", "directory_id": resource.id, "root": str(root)}
    plan = _plan(scan, arguments, source, arguments.get("class_names"))
    plan['input_fingerprint'] = _input_fingerprint(scan)
    conflict = _candidate_name_taken(adapter, arguments["name"])
    if conflict:
        plan["conflicts"].append({"code": "NAME_CONFLICT",
                                  "message": "数据集或候选名称已存在"})
        plan["blocked"] = True
    blocking = [issue for issue in scan["issues"] if issue["severity"] == "blocking"]
    if blocking:
        plan["conflicts"].extend({
            "code": issue["code"], "message": issue["message"],
            "path": issue.get("path"),
        } for issue in blocking[:20])
        plan["blocked"] = True
    return plan


# --------------------------------------------------------------------- build


def _materialize(scan: dict, plan: dict, output: Path, source: dict,
                 rules: dict, adapter) -> dict:
    """Write the candidate tree, its data.yaml and the manifest."""
    target = plan["target"]
    included = set(plan.get("_included") or [])
    split_of = {key: split for key, split in (plan.get("_splits") or {}).items()
                if split != "unassigned" and key in included}
    source_classes = plan.get("_source_classes") or list(scan["class_names"])
    index_map = plan["class_map"]
    secondary = target["secondary_modality"]
    output.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for sample in scan["samples"]:
        split = split_of.get(sample["key"])
        if split is None:
            continue
        kept_lines, _boxes = _rewrite_label(
            sample, source_classes, index_map, plan["class_names"],
        )
        if sample["class_ids"] and not kept_lines:
            continue
        rgb = sample["modalities"].get("rgb") or next(iter(sample["modalities"].values()), None)
        if rgb is None:
            continue
        suffix = Path(rgb).suffix.lower() or ".jpg"
        name_key = _relative_key(sample["key"])
        relative_image = Path("images") / split / f"{name_key}{suffix}"
        destination = output / relative_image
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rgb, destination)

        relative_secondary = None
        if secondary:
            for modality, path in sample["modalities"].items():
                if modality == "rgb":
                    continue
                relative_secondary = (Path(f"images_{secondary}") / split /
                                      f"{name_key}{Path(path).suffix.lower()}")
                secondary_destination = output / relative_secondary
                secondary_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, secondary_destination)
                break

        relative_label = Path("labels") / split / f"{name_key}.txt"
        label_path = output / relative_label
        label_path.parent.mkdir(parents=True, exist_ok=True)
        label_path.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""),
                              encoding="utf-8")
        entries.append({
            "key": sample["key"], "split": split,
            "image": relative_image.as_posix(),
            "secondary": relative_secondary.as_posix() if relative_secondary else None,
            "label": relative_label.as_posix(),
            "annotation_count": len(kept_lines),
        })

    names = plan["class_names"]
    import yaml

    payload = {
        "path": ".",
        "train": "./images/train",
        "val": "./images/val",
        "names": names,
    }
    if any(entry["split"] == "test" for entry in entries):
        payload["test"] = "./images/test"
    if secondary:
        payload["modalities"] = {"rgb": "./images", str(secondary): f"./images_{secondary}"}
        payload["modality_used"] = ["rgb", secondary]
    (output / "data.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["split"]] = counts.get(entry["split"], 0) + 1
    manifest = {
        "name": target["name"],
        "description": target.get("description", ""),
        "dataset_mode": target["dataset_mode"],
        "primary_modality": target["primary_modality"],
        "secondary_modality": target["secondary_modality"],
        "class_names": names,
        "class_map": index_map,
        "rules": rules,
        "source": source,
        "split_counts": counts,
        "sample_count": len(entries),
        "warnings": plan["warnings"],
        "blocking_reasons": plan["conflicts"],
        "entries": entries,
        "created_at": _now(),
        "generator": "workbuddy/business-tools/C-08",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def _relative_key(key: str) -> str:
    """Drop the leading split directory: "train/sub/a" -> "sub/a"."""
    parts = Path(key).parts
    if parts and parts[0].lower() in {"train", "val", "valid", "validation", "test"}:
        return Path(*parts[1:]).as_posix() if len(parts) > 1 else parts[0]
    return Path(key).as_posix()


def _rewrite_label(sample: dict, source_classes: list[str], index_map: dict,
                   target_names: list[str]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    boxes: list[str] = []
    if not sample["label"]:
        return lines, boxes
    try:
        content = Path(sample["label"]).read_text(encoding="utf-8")
    except OSError:
        return lines, boxes
    for row in content.splitlines():
        fields = row.split()
        if len(fields) != 5:
            continue
        try:
            class_id = int(float(fields[0]))
        except ValueError:
            continue
        if not 0 <= class_id < len(source_classes):
            continue
        name = source_classes[class_id]
        if name not in index_map:
            continue
        lines.append(f"{target_names.index(index_map[name])} " + " ".join(fields[1:]))
    return lines, boxes


def _execute(adapter, scan: dict, source: dict, arguments: dict, plan: dict,
             source_kind: str, source_id: str | None, extra_samples: list[dict] | None) -> dict:
    candidate_id = str(uuid.uuid4())
    output = _candidate_root(adapter) / candidate_id / "dataset"
    rules = {
        "class_map": plan.get("class_map") or {},
        "keep_classes": arguments.get("keep_classes") or [],
        "split_policy": arguments.get("split_policy") or None,
        "include_feedback": bool(arguments.get("include_feedback")),
    }
    manifest = _materialize(scan, plan, output, source, rules, adapter)
    if extra_samples:
        manifest["feedback_samples"] = extra_samples
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    _table(adapter)
    with adapter.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_candidates "
            "(id, project_id, name, output_dir, source_type, source_id, dataset_mode, "
            "primary_modality, secondary_modality, class_names_json, split_counts_json, "
            "sample_count, manifest_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'built', ?)",
            (
                candidate_id, plan["source"].get("project_id") or "",
                plan["target"]["name"], str(output), source_kind, source_id,
                plan["target"]["dataset_mode"], plan["target"]["primary_modality"],
                plan["target"]["secondary_modality"],
                json.dumps(plan["class_names"], ensure_ascii=False),
                json.dumps(manifest["split_counts"], ensure_ascii=False),
                manifest["sample_count"], str(output / "manifest.json"), _now(),
            ),
        )
    return {
        "candidate_id": candidate_id,
        "output_dir": str(output),
        "class_names": plan["class_names"],
        "split_counts": manifest["split_counts"],
        "sample_count": manifest["sample_count"],
        "warnings": plan["warnings"],
        "manifest_path": str(output / "manifest.json"),
    }


def build_candidate_from_dataset(adapter, dataset_resource, arguments: dict,
                                 preview: dict) -> dict:
    scan, source = _dataset_scan(adapter, dataset_resource)
    try:
        source["project_id"] = dataset_resource.project_id
        if arguments.get('include_feedback'):
            scan = _merge_feedback(adapter, scan, source)
        plan = _plan(scan, arguments, source, arguments.get("class_names"))
        if plan.get('blocked') or _input_fingerprint(scan) != preview.get('input_fingerprint'):
            from services.tool_common import ToolRefused
            raise ToolRefused('TRANSFORM_INPUT_CHANGED', '源文件、反馈或标注已变化，或存在未解决冲突，请重新预览并确认')
        return _execute(adapter, scan, source, arguments, plan, "dataset", dataset_resource.id, source.get('feedback_samples'))
    finally:
        shutil.rmtree(scan['_workspace'], ignore_errors=True)


def build_candidate_from_directory(adapter, resource, root, arguments: dict,
                                  preview: dict) -> dict:
    scan = scan_dataset_tree(root)
    source = {"type": "directory", "directory_id": resource.id,
              "root": str(root), "project_id": resource.project_id}
    plan = _plan(scan, arguments, source, arguments.get("class_names"))
    if plan.get('blocked') or _input_fingerprint(scan) != preview.get('input_fingerprint'):
        from services.tool_common import ToolRefused
        raise ToolRefused('TRANSFORM_INPUT_CHANGED', '授权目录内容已变化或存在冲突，请重新预览并确认')
    return _execute(adapter, scan, source, arguments, plan, "directory", resource.id, None)


def read_manifest(candidate: dict) -> dict:
    path = Path(candidate["manifest_path"] or Path(candidate["output_dir"]) / "manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


def register_candidate(adapter, principal: int, candidate_id: str) -> dict:
    """Zip the candidate tree and register it through the archive contract."""
    from services.dataset_import_service import import_dataset_archive

    candidate = get_candidate(adapter, candidate_id)
    if candidate["status"] == "registered" and candidate.get("dataset_id"):
        row = adapter._read_dataset(candidate["dataset_id"])
        return {
            "dataset_id": row["id"], "name": row["name"],
            "class_names": [], "split_counts": {}, "total_samples": 0,
        }
    manifest = read_manifest(candidate)
    output = Path(candidate["output_dir"])
    workspace = Path(tempfile.mkdtemp(prefix="candidate-", dir=adapter.work_dir or None))
    archive = workspace / "candidate.zip"
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(output.rglob("*")):
                if path.is_file() and path.name != "manifest.json":
                    bundle.write(path, path.relative_to(output).as_posix())
        record = import_dataset_archive(
            zip_path=str(archive),
            name=manifest["name"],
            description=manifest.get("description") or "",
            dataset_mode=manifest["dataset_mode"],
            secondary_modality=manifest.get("secondary_modality"),
            class_names=manifest["class_names"],
            database=adapter.database(),
            storage_client=adapter.storage_client,
            project_id=candidate["project_id"],
            owner_id=principal,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    with adapter.connect() as conn:
        conn.execute(
            "UPDATE dataset_candidates SET status='registered', dataset_id=? WHERE id=?",
            (record["dataset_id"], candidate_id),
        )
    return record
