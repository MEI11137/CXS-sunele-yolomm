"""Implementations of every tool described in :mod:`services.business_tool_specs`.

Each handler has two responsibilities only:

``prepare(adapter, principal, arguments)``
    Resolve the real business resource through the workspace policy service,
    read the business facts that decide whether the call may run, and return
    ``(snapshot, preview)``. The preview is what the user confirms: it names the
    target, the arguments the archive or dataset already fixes, and every
    blocking reason. Preparing never writes business data.

``execute(adapter, principal, arguments, preview, resource)``
    Re-check the facts the preview promised (versions, modality pairing, file
    digests), then call the existing business service. It returns
    ``(outputs, business_refs)``. A refusal raises :class:`ToolRefused`; an
    unexpected failure after a write leaves the operation ``unknown``.

Handlers must not invent data: no default class list, no guessed pairing, no
"published" flag taken from the model's own claim.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from config import TEMP_DIR
from services.tool_common import ResourceSnapshot, ToolConflict, ToolRefused

HANDLERS: dict[str, "BaseHandler"] = {}


def handler(tool_name: str):
    def register(cls):
        HANDLERS[tool_name] = cls()
        return cls

    return register


class BaseHandler:
    def prepare(self, adapter, principal: int, arguments: dict) -> tuple[ResourceSnapshot, dict]:
        raise NotImplementedError

    def execute(self, adapter, principal: int, arguments: dict, preview: dict,
                resource: ResourceSnapshot) -> tuple[dict, list[dict]]:
        raise NotImplementedError


# --------------------------------------------------------------------- helpers


def _work_dir(adapter, prefix: str = "tool") -> Path:
    base = Path(adapter.work_dir or TEMP_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=base))


def _asset(adapter, principal: int, asset_id: str, kinds: set[str]) -> dict:
    """Resolve one upload through the protected gateway and check its kind."""
    gateway = adapter.file_assets
    if gateway is None:
        raise ValueError("A protected file asset gateway is required")
    asset = gateway.resolve(principal, str(asset_id).strip())
    if not isinstance(asset, dict):
        raise ValueError("File asset resolver returned an invalid asset")
    required = ("id", "bucket", "object_key", "project_id", "kind", "status")
    if any(key not in asset for key in required) or asset["id"] != str(asset_id).strip():
        raise ValueError("File asset resolver returned an invalid asset")
    if asset["kind"] not in kinds:
        raise ToolRefused(
            "FILE_KIND_MISMATCH",
            f"上传文件类型为 {asset['kind']}，该工具需要 {', '.join(sorted(kinds))}",
        )
    if not asset["project_id"]:
        raise ToolRefused("PROJECT_REQUIRED", "独立上传的文件不能用于项目业务资源")
    if asset["status"] not in {"uploaded", "registered"}:
        raise ToolRefused("FILE_NOT_READY", f"上传状态为 {asset['status']}，不能使用")
    return asset


def _class_names(structure_info) -> list[str]:
    """Read the dataset's own class order; never fall back to a fixed list.

    Legacy rows without ``names`` return an empty list so the tool refuses to
    import a weight or add feedback against a guessed class mapping.
    """
    if not structure_info:
        return []
    info = structure_info
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except (TypeError, ValueError):
            return []
    names = (info or {}).get("names")
    if isinstance(names, dict):
        try:
            indexed = {int(key): value for key, value in names.items()}
        except (TypeError, ValueError):
            return []
        names = [indexed[index] for index in sorted(indexed)]
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if str(name).strip()]


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _records(adapter, principal: int, record_ids: list[str]) -> list[ResourceSnapshot]:
    """Resolve each unique record once: a paid review must not repeat an id."""
    unique = list(dict.fromkeys(str(value).strip() for value in record_ids))
    return [
        adapter.resolve(principal, "record", record_id,
                        ("detection:read", "detection:review"))
        for record_id in unique
    ]


# -------------------------------------------------------------------- datasets


@handler("dataset.get_state")
class DatasetState(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        spec = _spec("dataset.get_state")
        resource = adapter._resource(principal, arguments["dataset_id"], spec)
        adapter._read_dataset(resource.id)
        return resource, {"effect": "read"}

    def execute(self, adapter, principal, arguments, preview, resource):
        state = adapter._read_dataset(resource.id)
        return state, [_dataset_ref(resource, state)]


def _dataset_ref(resource: ResourceSnapshot, state: dict) -> dict:
    return {"type": "dataset", "id": resource.id, "version": resource.version,
            "project_id": resource.project_id}


def _spec(name: str):
    from services.business_tool_specs import TOOL_SPECS

    return TOOL_SPECS[name]


@handler("dataset.import")
class DatasetImport(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        asset = _asset(adapter, principal, arguments["asset_id"], {"dataset_archive"})
        spec = _spec("dataset.import")
        resource = adapter._project_resource(principal, asset["project_id"], spec)

        from services.dataset_import_service import inspect_dataset_archive

        path = adapter.file_assets.materialize(asset["bucket"], asset["object_key"])
        try:
            report = inspect_dataset_archive(
                path,
                arguments["dataset_mode"],
                secondary_modality=arguments.get("secondary_modality"),
                class_names=arguments.get("class_names"),
            )
        finally:
            Path(path).unlink(missing_ok=True)

        inspection = report.to_dict()
        blockers = [issue for issue in inspection["issues"]
                    if issue["severity"] == "blocking"]
        ignored = []
        if not report.generated_data_yaml:
            if "class_names" in arguments:
                ignored.append("class_names")
            if "secondary_modality" in arguments:
                ignored.append("secondary_modality")
        elif arguments["dataset_mode"] != "multimodal" and "class_names" in arguments:
            ignored.append("class_names")

        conflict = _dataset_name_taken(adapter, arguments["name"])
        if conflict:
            blockers.append({
                "code": "NAME_CONFLICT",
                "severity": "blocking",
                "message": "数据集名称已存在",
                "path": None,
                "line": None,
            })
        preview = {
            "effect": "write",
            "asset": {
                "id": asset["id"],
                "bucket": asset["bucket"],
                "object_key": asset["object_key"],
                "sha256": asset.get("sha256"),
                "size": asset.get("size"),
                "project_id": asset["project_id"],
                "original_name": asset.get("original_name"),
            },
            "inspection": inspection,
            "blocked": bool(blockers) or not inspection["ready_to_register"],
            "blocked_reasons": blockers,
            "ignored_arguments": ignored,
            "proposed_registration": {
                "name": arguments["name"],
                "description": arguments.get("description", ""),
                "project_id": asset["project_id"],
                "dataset_mode": inspection["dataset_mode"],
                "secondary_modality": inspection["secondary_modality"],
                "class_names": inspection["class_names"],
                "class_count": inspection["class_count"],
                "split_counts": inspection["split_counts"],
                "total_samples": inspection["total_samples"],
                "source_file_id": asset["id"],
                "existing_dataset_id": conflict,
            },
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        asset = _asset(adapter, principal, arguments["asset_id"], {"dataset_archive"})
        expected = preview.get("asset") or {}
        if (asset.get("sha256") != expected.get("sha256")
                or asset["object_key"] != expected.get("object_key")):
            raise ToolConflict("Uploaded archive changed since preparation; prepare again")
        return adapter._project_resource(principal, asset["project_id"], _spec("dataset.import"))

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_import_service import import_dataset_archive

        asset = _asset(adapter, principal, arguments["asset_id"], {"dataset_archive"})
        expected = preview.get("asset") or {}
        if (asset.get("sha256") != expected.get("sha256")
                or asset["object_key"] != expected.get("object_key")
                or asset["project_id"] != resource.id):
            raise ToolConflict("Uploaded archive changed since preparation; prepare again")

        if asset.get("business_id"):
            existing = adapter._read_dataset(asset["business_id"])
            outputs = {
                "dataset_id": existing["id"],
                "name": existing["name"],
                "dataset_mode": existing.get("dataset_mode"),
                "primary_modality": existing.get("primary_modality"),
                "secondary_modality": existing.get("secondary_modality"),
                "class_names": [],
                "split_counts": {},
                "total_samples": 0,
                "status": existing.get("status"),
                "source_file_id": asset["id"],
                "reused": True,
            }
            return outputs, [{"type": "dataset", "id": existing["id"], "version": "1",
                              "project_id": resource.project_id,
                              "source_file_id": asset["id"]}]

        path = adapter.file_assets.materialize(asset["bucket"], asset["object_key"])
        try:
            def bind(conn, dataset_id: str) -> None:
                if hasattr(adapter.file_assets, "bind_with"):
                    adapter.file_assets.bind_with(conn, asset["id"], "dataset", dataset_id)

            record = import_dataset_archive(
                zip_path=path,
                name=arguments["name"],
                description=arguments.get("description", ""),
                dataset_mode=arguments["dataset_mode"],
                secondary_modality=arguments.get("secondary_modality"),
                class_names=arguments.get("class_names"),
                database=adapter.database(),
                storage_client=adapter.storage_client,
                project_id=resource.project_id,
                owner_id=principal,
                on_registered=bind if hasattr(adapter.file_assets, "bind_with") else None,
            )
        finally:
            Path(path).unlink(missing_ok=True)

        if not hasattr(adapter.file_assets, "bind_with") and hasattr(adapter.file_assets, "bind"):
            adapter.file_assets.bind(asset["id"], "dataset", record["dataset_id"])

        outputs = {
            "dataset_id": record["dataset_id"],
            "name": record["name"],
            "dataset_mode": record["dataset_mode"],
            "primary_modality": record["primary_modality"],
            "secondary_modality": record["secondary_modality"],
            "class_names": record["class_names"],
            "split_counts": record["split_counts"],
            "total_samples": record["total_samples"],
            "status": record["status"],
            "source_file_id": asset["id"],
            "reused": False,
        }
        return outputs, [{"type": "dataset", "id": record["dataset_id"], "version": "1",
                          "project_id": resource.project_id,
                          "source_file_id": asset["id"]}]


def _dataset_name_taken(adapter, name: str) -> str | None:
    with adapter.connect() as conn:
        row = conn.execute("SELECT id FROM datasets WHERE name=?", (name,)).fetchone()
    return row["id"] if row else None


@handler("dataset.transform")
class DatasetTransform(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.dataset_transform_service import plan_from_dataset

        spec = _spec("dataset.transform")
        resource = adapter._resource(principal, arguments["dataset_id"], spec)
        plan = plan_from_dataset(adapter, resource, arguments)
        return resource, plan

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_transform_service import build_candidate_from_dataset

        if preview.get("blocked"):
            raise ToolRefused("TRANSFORM_BLOCKED", "变换存在未解决冲突，需先处理预览中的冲突清单",
                              preview.get("conflicts") or [])
        candidate = build_candidate_from_dataset(adapter, resource, arguments, preview)
        return candidate, [{"type": "dataset_candidate", "id": candidate["candidate_id"],
                            "version": "1", "project_id": resource.project_id,
                            "source_dataset_id": resource.id}]


@handler("dataset.register_candidate")
class DatasetRegisterCandidate(BaseHandler):
    def _candidate(self, adapter, principal, candidate_id: str):
        from services.dataset_transform_service import get_candidate

        candidate = get_candidate(adapter, candidate_id)
        resource = adapter._project_resource(principal, candidate["project_id"],
                                             _spec("dataset.register_candidate"))
        return candidate, resource

    def prepare(self, adapter, principal, arguments):
        candidate, resource = self._candidate(adapter, principal, arguments["candidate_id"])
        from services.dataset_transform_service import read_manifest

        manifest = read_manifest(candidate)
        blockers = list(manifest.get("blocking_reasons") or [])
        if _dataset_name_taken(adapter, manifest["name"]):
            blockers.append({"code": "NAME_CONFLICT", "message": "数据集名称已存在"})
        preview = {
            "effect": "write",
            "candidate_id": candidate["id"],
            "name": manifest["name"],
            "dataset_mode": manifest["dataset_mode"],
            "primary_modality": manifest["primary_modality"],
            "secondary_modality": manifest["secondary_modality"],
            "class_names": manifest["class_names"],
            "split_counts": manifest.get("split_counts") or {},
            "sample_count": manifest.get("sample_count") or 0,
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
            "source": manifest.get("source"),
            "output_dir": candidate["output_dir"],
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        _, resource = self._candidate(adapter, principal, arguments["candidate_id"])
        return resource

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_transform_service import register_candidate

        if preview.get("blocked"):
            raise ToolRefused("CANDIDATE_BLOCKED", "候选数据集存在未解决的问题",
                              preview.get("blocked_reasons") or [])
        record = register_candidate(adapter, principal, arguments["candidate_id"])
        outputs = {
            "dataset_id": record["dataset_id"],
            "name": record["name"],
            "class_names": record["class_names"],
            "split_counts": record["split_counts"],
            "total_samples": record["total_samples"],
        }
        return outputs, [{"type": "dataset", "id": record["dataset_id"], "version": "1",
                          "project_id": resource.project_id,
                          "source_dataset_id": preview.get("source", {}).get("dataset_id")}]


# ---------------------------------------------------------------------- models


@handler("model.import_weight")
class ModelImportWeight(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        asset = _asset(adapter, principal, arguments["asset_id"], {"model_weight"})
        spec = _spec("model.import_weight")
        resource = adapter._project_resource(principal, asset["project_id"], spec)
        dataset = adapter.resolve(principal, "dataset", arguments["dataset_id"],
                                  ("dataset:read",))
        row = adapter._dataset_row(dataset.id)

        class_names = _class_names(row.get("structure_info"))
        blockers = []
        if int(row.get("status") or 0) != 1:
            blockers.append({"code": "DATASET_NOT_VERIFIED",
                             "message": "数据集尚未验证通过，不能用于导入模型"})
        if not class_names:
            blockers.append({
                "code": "CLASS_MAPPING_UNKNOWN",
                "message": "数据集没有类别映射，先补充类别信息再导入权重",
            })
        if _model_name_taken(adapter, arguments["name"]):
            blockers.append({"code": "NAME_CONFLICT", "message": "模型名称已存在"})

        preview = {
            "effect": "write",
            "asset": {
                "id": asset["id"], "bucket": asset["bucket"],
                "object_key": asset["object_key"], "sha256": asset.get("sha256"),
                "size": asset.get("size"), "project_id": asset["project_id"],
                "original_name": asset.get("original_name"),
            },
            "weight_contract": {
                "source_dataset_id": row["id"],
                "source_dataset_name": row["name"],
                "model_mode": row.get("dataset_mode") or "single",
                "primary_modality": row.get("primary_modality") or "rgb",
                "secondary_modality": row.get("secondary_modality"),
                "class_names": class_names,
                "class_count": len(class_names),
            },
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
            "loader_check": "权重会被实际加载一次以确认与所选模态兼容",
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        asset = _asset(adapter, principal, arguments["asset_id"], {"model_weight"})
        expected = preview.get("asset") or {}
        if (asset.get("sha256") != expected.get("sha256")
                or asset["object_key"] != expected.get("object_key")):
            raise ToolConflict("Weight file changed since preparation; prepare again")
        return adapter._project_resource(principal, asset["project_id"],
                                         _spec("model.import_weight"))

    def execute(self, adapter, principal, arguments, preview, resource):
        from werkzeug.datastructures import FileStorage

        from services.model_import_service import import_model_weight

        if preview.get("blocked"):
            raise ToolRefused("WEIGHT_IMPORT_BLOCKED",
                              "权重导入缺少必要条件，请先补充",
                              preview.get("blocked_reasons") or [])
        asset = _asset(adapter, principal, arguments["asset_id"], {"model_weight"})
        expected = preview.get("asset") or {}
        if (asset.get("sha256") != expected.get("sha256")
                or asset["object_key"] != expected.get("object_key")):
            raise ToolConflict("Weight file changed since preparation; prepare again")
        if adapter.storage_client is None:
            raise ValueError("A protected storage adapter is required")

        workspace = _work_dir(adapter, "model-import")
        filename = asset.get("original_name") or "weight.pt"
        local = adapter.file_assets.materialize(asset["bucket"], asset["object_key"])
        try:
            size = Path(local).stat().st_size
            with open(local, "rb") as stream:
                upload = FileStorage(stream=stream, filename=filename,
                                     content_type="application/octet-stream")
                kwargs = {}
                if callable(adapter.detector_loader):
                    kwargs["detector_loader"] = adapter.detector_loader
                record = import_model_weight(
                    name=arguments["name"],
                    description=arguments.get("description", ""),
                    dataset_id=arguments["dataset_id"],
                    upload=upload,
                    database=adapter.database(),
                    storage_client=adapter.storage_client,
                    **kwargs,
                )
        finally:
            Path(local).unlink(missing_ok=True)
            for stale in workspace.glob("*"):
                stale.unlink(missing_ok=True)
            workspace.rmdir()

        if hasattr(adapter.file_assets, "bind"):
            adapter.file_assets.bind(asset["id"], "model", record.id)
        outputs = {
            "model_id": record.id,
            "name": record.name,
            "dataset_id": record.dataset_id,
            "model_mode": record.model_mode,
            "primary_modality": record.primary_modality,
            "secondary_modality": record.secondary_modality,
            "class_names": (preview.get("weight_contract") or {}).get("class_names") or [],
            "publish_status": record.publish_status,
            "source_file_id": asset["id"],
            "size": size,
            "reused": False,
        }
        return outputs, [{"type": "model", "id": record.id, "version": "1",
                          "project_id": resource.project_id,
                          "source_file_id": asset["id"]}]


def _model_name_taken(adapter, name: str) -> str | None:
    with adapter.connect() as conn:
        row = conn.execute("SELECT id FROM models WHERE name=?", (name.strip(),)).fetchone()
    return row["id"] if row else None


@handler("model.get_state")
class ModelState(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["model_id"], _spec("model.get_state"))
        adapter._model_row(resource.id)
        return resource, {"effect": "read"}

    def execute(self, adapter, principal, arguments, preview, resource):
        row = adapter._model_row(resource.id)
        outputs = adapter.model_output(row)
        return outputs, [{"type": "model", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


@handler("model.publish")
class ModelPublish(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        spec = _spec("model.publish")
        resource = adapter._resource(principal, arguments["model_id"], spec)
        return resource, _preview_model_publish(adapter, principal, resource.id)

    def execute(self, adapter, principal, arguments, preview, resource):
        outputs = adapter._publish_model(
            resource.id, principal, preview["archived_model_ids"],
            scope_model_ids=preview.get("scope_model_ids"),
        )
        return outputs, [{"type": "model", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


def _preview_model_publish(adapter, principal: int, model_id: str) -> dict:
    if not callable(adapter.storage_exists):
        raise ValueError("Model storage check is not configured")
    model = adapter._read_model(model_id)
    if model["status"] != 1 or not model["model_bucket"] or not model["model_object_key"]:
        raise ToolRefused("MODEL_NOT_READY", "模型未就绪，不能发布")
    if adapter.storage_exists(model["model_bucket"], model["model_object_key"]) is not True:
        raise ToolRefused("MODEL_WEIGHT_MISSING", "文件服务中找不到模型权重")
    # 归档范围只认真实 owner/project 内的模型，并且逐个重新授权
    scope = adapter._authorised_model_scope(principal, model_id)
    with adapter.connect() as conn:
        dataset = conn.execute(
            "SELECT dataset_mode, secondary_modality FROM datasets WHERE id=?",
            (model["dataset_id"],),
        ).fetchone()
        if (dataset is None or (dataset["dataset_mode"] or "single") !=
                (model["model_mode"] or "single") or
                (dataset["secondary_modality"] or None) != (model["secondary_modality"] or None)):
            raise ToolRefused("MODEL_DATASET_MISMATCH", "模型数据集缺失或模态不匹配")
        placeholders = ", ".join("?" for _ in scope)
        archived = conn.execute(
            f"SELECT id FROM models WHERE status=3 AND id<>? AND model_mode=? "
            f"AND primary_modality=? AND secondary_modality IS ? "
            f"AND id IN ({placeholders}) "
            f"ORDER BY create_time DESC",
            (model_id, model["model_mode"], model["primary_modality"],
             model["secondary_modality"], *scope),
        ).fetchall()
    return {
        "effect": "write",
        "model_id": model_id,
        "previous_model_id": archived[0]["id"] if archived else None,
        "archived_model_ids": [row["id"] for row in archived],
        # 预览里带上本次核准的范围：确认后仍按这份范围复核，范围本身不可扩大
        "scope_model_ids": scope,
        "impact": "本次发布将把上面列出的同模态已发布模型置为归档，不可自动回滚",
    }


@handler("model.unpublish")
class ModelUnpublish(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["model_id"], _spec("model.unpublish"))
        model = adapter._model_row(resource.id)
        if model["status"] != 3:
            raise ToolRefused("MODEL_NOT_PUBLISHED", "只能取消发布已发布的模型")
        scope = adapter._authorised_model_scope(principal, resource.id)
        placeholders = ", ".join("?" for _ in scope)
        with adapter.connect() as conn:
            archived = conn.execute(
                f"SELECT id FROM models WHERE publish_status='archived' AND id<>? "
                f"AND model_mode=? AND primary_modality=? AND secondary_modality IS ? "
                f"AND id IN ({placeholders})",
                (resource.id, model["model_mode"], model["primary_modality"],
                 model["secondary_modality"], *scope),
            ).fetchall()
        preview = {
            "effect": "write",
            "model_id": resource.id,
            "model_name": model.get("name") or model["id"],
            "archived_model_ids": [row["id"] for row in archived],
            "scope_model_ids": scope,
            "impact": "取消发布后该模型不再参与检测；已归档的旧模型不会自动恢复发布",
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        # 执行前重新核一次归档范围授权：默认共享模型（owner 为系统）不可被普通用户撤销
        adapter._authorised_model_scope(principal, resource.id)
        with adapter.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM models WHERE id=?", (resource.id,)
            ).fetchone()
            if current is None or current["status"] != 3:
                raise ToolRefused("MODEL_NOT_PUBLISHED", "模型已不是发布状态，无需重复取消")
            conn.execute(
                "UPDATE models SET status=1, publish_status='archived' WHERE id=?",
                (resource.id,),
            )
            conn.commit()
        outputs = {
            "model_id": resource.id,
            "publish_status": "archived",
            "archived_model_ids": preview.get("archived_model_ids") or [],
        }
        return outputs, [{"type": "model", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


@handler("model.compare")
class ModelCompare(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.model_registry_service import compare_models

        resource = adapter._resource(principal, arguments["parent_model_id"],
                                     _spec("model.compare"))
        child = adapter.resolve(principal, "model", arguments["child_model_id"],
                                ("model:read",))
        result = compare_models(resource.id, child.id, adapter.database())
        known = [key for key, value in result.metrics.items() if value["status"] == "known"]
        preview = {
            "effect": "read",
            "parent_model_id": resource.id,
            "child_model_id": child.id,
            "known_metrics": known,
            "conditions": _compare_conditions(adapter, resource.id, child.id),
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.model_registry_service import compare_models

        result = compare_models(resource.id, arguments["child_model_id"], adapter.database())
        outputs = result.to_dict()
        outputs["conditions"] = _compare_conditions(
            adapter, resource.id, arguments["child_model_id"],
        )
        child = adapter._model_row(arguments["child_model_id"])
        return outputs, [
            {"type": "model", "id": resource.id, "version": resource.version,
             "project_id": resource.project_id},
            {"type": "model", "id": child["id"], "version": str(child.get("version") or 1),
             "project_id": resource.project_id},
        ]


def _compare_conditions(adapter, parent_id: str, child_id: str) -> dict:
    parent = adapter._model_row(parent_id)
    child = adapter._model_row(child_id)
    same = {
        "dataset": parent["dataset_id"] == child["dataset_id"],
        "epochs": parent.get("epochs") == child.get("epochs"),
        "batch_size": parent.get("batch_size") == child.get("batch_size"),
    }
    comparable = all(same.values())
    return {
        **same,
        "comparable": comparable,
        "note": (
            "评估条件一致，指标可直接比较"
            if comparable
            else "两次训练的数据集或参数不同，指标差异不能直接归因于方法改进"
        ),
    }


# ------------------------------------------------------------------- detection


@handler("detection.run")
class DetectionRun(BaseHandler):
    def _output_resource(self, adapter, principal, arguments, asset):
        """解析本轮**显式选择**的输出项目；没选就用原图所在项目。

        模型归属不参与这里的判断：跨项目只读引用模型是允许的，检测结果只落在
        用户能写的那个项目里。
        """
        explicit = str(arguments.get("output_project_id") or "").strip()
        project_id = explicit or asset["project_id"]
        resource = adapter._project_resource(principal, project_id, _spec("detection.run"))
        return resource, "explicit" if explicit else "asset_project"

    def prepare(self, adapter, principal, arguments):
        asset = _asset(adapter, principal, arguments["asset_id"], {"image"})
        resource, output_origin = self._output_resource(adapter, principal, arguments, asset)
        # 模型只要通过授权解析即可使用：默认只读模型、以及本人显式引用的
        # 跨项目模型都属于合法用法，因此不再按 project_id 硬阻断。
        model_resource = adapter.resolve(principal, "model", arguments["model_id"],
                                         ("model:read",))
        model = adapter._model_row(model_resource.id)
        blockers = []
        cross_project_model = model_resource.project_id != resource.id
        if model["status"] != 3:
            blockers.append({"code": "MODEL_NOT_PUBLISHED",
                             "message": "只能使用已发布的模型进行检测"})

        multimodal = (model.get("model_mode") or "single") == "multimodal"
        ignored = []
        secondary = None
        if multimodal:
            if not arguments.get("secondary_asset_id"):
                blockers.append({
                    "code": "MODALITY_PAIR_REQUIRED",
                    "message": "该模型需要 RGB 与第二模态配对输入，缺少第二模态图片",
                })
            else:
                secondary = _asset(adapter, principal, arguments["secondary_asset_id"],
                                   {"image"})
                if (secondary["project_id"] != asset["project_id"]
                        and secondary["owner_id"] != asset["owner_id"]):
                    # 同一用户显式跨项目可以，别人的图片仍必须拒绝
                    blockers.append({"code": "SECONDARY_PROJECT_MISMATCH",
                                     "message": "第二模态图片不属于同一项目且不是本人资源"})
        elif arguments.get("secondary_asset_id"):
            ignored.append("secondary_asset_id")

        preview = {
            "effect": "write",
            "asset": _asset_brief(asset),
            "secondary_asset": _asset_brief(secondary) if secondary else None,
            "model": {
                "id": model["id"], "name": model.get("name") or model["id"],
                "version": model.get("version") or 1,
                "model_mode": model.get("model_mode") or "single",
                "primary_modality": model.get("primary_modality"),
                "secondary_modality": model.get("secondary_modality"),
            },
            "confidence": float(arguments.get("confidence") or 0.25),
            "ignored_arguments": ignored,
            "model_source": "cross_project_readonly" if cross_project_model else "same_project",
            "model_project_id": model_resource.project_id,
            "output_project_id": resource.id,
            "output_project_source": output_origin,
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
            "impact": "将上传原图（及第二模态图）、结果图并在输出项目生成一条检测记录",
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        asset = _asset(adapter, principal, arguments["asset_id"], {"image"})
        resource, _ = self._output_resource(adapter, principal, arguments, asset)
        return resource

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.detection_service import execute_detection

        if preview.get("blocked"):
            raise ToolRefused("DETECTION_BLOCKED", "检测缺少必要条件",
                              preview.get("blocked_reasons") or [])
        asset = _asset(adapter, principal, arguments["asset_id"], {"image"})
        original_project = (preview.get("asset") or {}).get("project_id")
        if original_project and asset["project_id"] != original_project:
            raise ToolConflict("Source image moved to another project; prepare again")
        if resource.id != preview.get("output_project_id"):
            raise ToolConflict("Output project changed; prepare again")
        model = adapter._model_row(arguments["model_id"])
        secondary_path = None
        workspace = _work_dir(adapter, "detection")
        rgb_path = adapter.file_assets.materialize(asset["bucket"], asset["object_key"])
        try:
            if arguments.get("secondary_asset_id"):
                secondary = _asset(adapter, principal, arguments["secondary_asset_id"],
                                   {"image"})
                secondary_path = adapter.file_assets.materialize(
                    secondary["bucket"], secondary["object_key"],
                )
            kwargs = {}
            if callable(adapter.detector_loader):
                kwargs["detector_loader"] = adapter.detector_loader
            if callable(adapter.detector_predictor):
                kwargs["predictor"] = adapter.detector_predictor
            result = execute_detection(
                user_id=principal,
                model_meta=model,
                rgb_path=rgb_path,
                x_path=secondary_path,
                confidence=float(arguments.get("confidence") or 0.25),
                work_dir=str(workspace),
                storage_client=adapter.storage_client,
                database=adapter.database(),
                **kwargs,
            )
        finally:
            Path(rgb_path).unlink(missing_ok=True)
            if secondary_path:
                Path(secondary_path).unlink(missing_ok=True)
            for stale in workspace.glob("*"):
                if stale.is_file():
                    stale.unlink(missing_ok=True)

        outputs = {
            "record_id": result.record_id,
            "model_id": result.model_id,
            "model_name": result.model_name,
            "detection_count": result.detection_count,
            "detections": result.detections,
            "original_image_url": result.original_image_url,
            "result_image_url": result.result_image_url,
            "secondary_image_url": result.secondary_image_url,
            "secondary_image_origin": result.secondary_image_origin,
            # 记录归输出项目；模型归属不受影响
            "output_project_id": resource.id,
        }
        return outputs, [{"type": "record", "id": result.record_id, "version": "1",
                          "project_id": resource.id,
                          "model_id": result.model_id}]


def _asset_brief(asset: dict) -> dict:
    return {
        "id": asset["id"], "project_id": asset["project_id"],
        "original_name": asset.get("original_name"), "size": asset.get("size"),
        "sha256": asset.get("sha256"),
    }


@handler("detection.get_state")
class DetectionState(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["record_id"],
                                     _spec("detection.get_state"))
        adapter._read_record(resource.id)
        return resource, {"effect": "read"}

    def execute(self, adapter, principal, arguments, preview, resource):
        row = adapter._read_record(resource.id)
        outputs = {
            "id": row["id"],
            "model_id": row.get("model_id"),
            "detection_result": _json_list(row.get("detection_result")),
            "ai_review": _json_obj(row.get("ai_review_result")),
            "human_review_status": row.get("human_review_status"),
            "review_version": int(row.get("review_version") or 0),
            "corrected_annotations": _json_list(row.get("corrected_annotations")),
        }
        return outputs, [{"type": "record", "id": resource.id,
                          "version": str(outputs["review_version"]),
                          "project_id": resource.project_id}]


def _json_obj(value) -> dict | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


@handler("review.ai")
class ReviewAI(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        if not (callable(adapter.review_client_factory)
                or callable(adapter.model_client_factory)):
            raise ValueError("review.ai requires the current user's personal model client")
        record_ids = [str(value).strip() for value in arguments["record_ids"]]
        snapshots = _records(adapter, principal, record_ids)
        records = []
        for snapshot in snapshots:
            row = adapter._read_record(snapshot.id)
            records.append({
                "record_id": snapshot.id,
                "version": str(row.get("review_version") or 0),
                "human_review_status": row.get("human_review_status"),
                "ai_review_status": row.get("ai_review_status"),
            })
        preview = {
            "effect": "write",
            "billable": True,
            "records": records,
            "requested": len(records),
            "impact": "逐条调用当前用户配置的大模型，失败项不会自动重试以免重复计费",
        }
        return snapshots[0], preview

    def recheck(self, adapter, principal, arguments, preview):
        record_ids = [str(value).strip() for value in arguments["record_ids"]]
        snapshots = _records(adapter, principal, record_ids)
        expected = {item["record_id"]: item["version"] for item in preview.get("records") or []}
        for snapshot in snapshots:
            row = adapter._read_record(snapshot.id)
            if expected.get(snapshot.id) != str(row.get("review_version") or 0):
                raise ToolConflict("A record was reviewed by someone else; prepare again")
        return snapshots[0]

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.batch_review_service import run_batch_ai_reviews
        from services.ai_review_service import review_detection

        record_ids = [str(value).strip() for value in arguments["record_ids"]]
        snapshots = _records(adapter, principal, record_ids)
        expected = {item["record_id"]: item["version"] for item in preview.get("records") or []}
        for snapshot in snapshots:
            row = adapter._read_record(snapshot.id)
            if expected.get(snapshot.id) != str(row.get("review_version") or 0):
                raise ToolConflict("A record was reviewed by someone else; prepare again")

        # One paid call per unique record; duplicates were already collapsed.
        record_ids = [snapshot.id for snapshot in snapshots]
        # Authorization already happened per record through the policy service,
        # so the batch runner must not re-apply the page's owner-only rule.
        from services.personal_review_client import PersonalReviewClient

        def client_factory():
            # 零参数闭包：优先用装配层注入的本人复核客户端（含视觉能力校验），
            # 退路是用本人网关现包一层——两者都只持有当前用户那份凭据。
            if callable(adapter.review_client_factory):
                return adapter.review_client_factory()
            client = adapter.model_client_factory()
            # 注入的也可能是已经实现复核协议的客户端：不要二次包装
            if hasattr(client, "review_detection_structured"):
                return client
            return PersonalReviewClient(client)

        summary = run_batch_ai_reviews(
            record_ids=record_ids,
            user_id=principal,
            is_admin_user=True,
            database=adapter.database(),
            storage_client=adapter.storage_client,
            review_runner=review_detection,
            client_factory=client_factory,
        )
        failed = int(summary.get("failed", 0))
        succeeded = int(summary.get("succeeded", 0))
        outputs = {
            "requested": summary.get("requested", len(record_ids)),
            "succeeded": succeeded,
            "failed": failed,
            # 外层 Operation 可能整体 succeeded，但逐项失败必须能被看见
            "status": (
                "failed" if failed and not succeeded
                else "partial_failed" if failed
                else "succeeded"
            ),
            "items": summary.get("items") or [],
        }
        return outputs, [{"type": "record", "id": snapshot.id,
                          "version": expected.get(snapshot.id, "0"),
                          "project_id": resource.project_id} for snapshot in snapshots]


# --------------------------------------------------------------------- review


@handler("review.submit_human")
class ReviewSubmitHuman(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["record_id"],
                                     _spec("review.submit_human"))
        row = adapter._read_record(resource.id)
        current = int(row.get("review_version") or 0)
        blockers = []
        if current != int(arguments["expected_version"]):
            blockers.append({
                "code": "RECORD_VERSION_CONFLICT",
                "message": f"记录当前版本为 {current}，与提交版本不一致",
            })
        preview = {
            "effect": "write",
            "record_id": resource.id,
            "current_version": current,
            "new_version": current + 1,
            "decision": arguments["decision"],
            "annotation_count": len(arguments.get("annotations") or []),
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
            "impact": "写入人工确认状态与标注，只有该状态可回流训练",
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.review_service import (
            ReviewConflictError,
            ReviewValidationError,
            submit_human_review,
        )

        if preview.get("blocked"):
            raise ToolRefused("RECORD_VERSION_CONFLICT", "记录版本已变化，请重新读取后再提交",
                              preview.get("blocked_reasons") or [])
        try:
            submit_human_review(
                record_id=resource.id,
                reviewer_id=principal,
                expected_version=arguments["expected_version"],
                annotations=arguments.get("annotations") or [],
                decision=arguments["decision"],
                database=adapter.database(),
            )
        except ReviewConflictError as exc:
            raise ToolRefused("RECORD_VERSION_CONFLICT", str(exc)) from exc
        except ReviewValidationError as exc:
            raise ToolRefused("ANNOTATION_INVALID", str(exc)) from exc

        row = adapter._read_record(resource.id)
        outputs = {
            "record_id": resource.id,
            "human_review_status": row.get("human_review_status"),
            "review_version": int(row.get("review_version") or 0),
            "annotation_count": len(_json_list(row.get("corrected_annotations"))),
        }
        return outputs, [{"type": "record", "id": resource.id,
                          "version": str(outputs["review_version"]),
                          "project_id": resource.project_id}]


@handler("feedback.add_confirmed")
class FeedbackAddConfirmed(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["record_id"],
                                     _spec("feedback.add_confirmed"))
        dataset = adapter.resolve(principal, "dataset", arguments["dataset_id"],
                                  ("dataset:read", "dataset:update"))
        record = adapter._read_record(resource.id)
        dataset_row = adapter._dataset_row(dataset.id)
        blockers = []
        if record.get("human_review_status") != "confirmed":
            blockers.append({
                "code": "NOT_HUMAN_CONFIRMED",
                "message": "只有人工确认的记录才能加入反馈数据集",
            })
        preview = {
            "effect": "write",
            "record_id": resource.id,
            "dataset": {"id": dataset_row["id"], "name": dataset_row["name"],
                        "class_names": _class_names(dataset_row.get("structure_info"))},
            "human_review_status": record.get("human_review_status"),
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.feedback_dataset_service import (
            FeedbackValidationError,
            add_confirmed_record,
        )

        if preview.get("blocked"):
            raise ToolRefused("NOT_HUMAN_CONFIRMED", "记录尚未人工确认",
                              preview.get("blocked_reasons") or [])
        try:
            sample = add_confirmed_record(
                arguments["record_id"], arguments["dataset_id"], adapter.database(),
            )
        except FeedbackValidationError as exc:
            raise ToolRefused("FEEDBACK_REFUSED", str(exc)) from exc
        outputs = {
            "sample_id": sample.sample_id,
            "record_id": sample.record_id,
            "dataset_id": sample.dataset_id,
            "created": sample.created,
        }
        return outputs, [
            {"type": "record", "id": resource.id, "version": "1",
             "project_id": resource.project_id},
            {"type": "dataset", "id": sample.dataset_id, "version": "1",
             "project_id": resource.project_id},
        ]


# --------------------------------------------------------------------- drafts


@handler("draft.create_from_feedback")
class DraftCreateFromFeedback(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["dataset_id"],
                                     _spec("draft.create_from_feedback"))
        with adapter.connect() as conn:
            confirmed = conn.execute(
                "SELECT COUNT(*) FROM feedback_samples fs "
                "JOIN detection_records dr ON dr.id = fs.record_id "
                "WHERE fs.dataset_id=? AND dr.human_review_status='confirmed'",
                (resource.id,),
            ).fetchone()[0]
        dataset = adapter._dataset_row(resource.id)
        blockers = []
        if not confirmed:
            blockers.append({"code": "NO_CONFIRMED_SAMPLES",
                             "message": "反馈数据集中还没有已确认样本"})
        preview = {
            "effect": "write",
            "dataset": {"id": dataset["id"], "name": dataset["name"],
                        "dataset_mode": dataset.get("dataset_mode")},
            "confirmed_samples": confirmed,
            "proposed_name": f"{dataset['name']} 反馈草稿",
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
            "impact": "只把人工确认的反馈样本写入新草稿，原训练集不会被自动混入",
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.feedback_dataset_service import create_feedback_dataset_draft

        if preview.get("blocked"):
            raise ToolRefused("NO_CONFIRMED_SAMPLES", "没有可用的已确认反馈样本",
                              preview.get("blocked_reasons") or [])
        workspace = _work_dir(adapter, "feedback-draft")
        draft = create_feedback_dataset_draft(
            dataset_id=resource.id,
            work_dir=str(workspace),
            creator_id=principal,
            database=adapter.database(),
            storage_client=adapter.storage_client,
        )
        outputs = {
            "draft_id": draft.id,
            "name": draft.name,
            "sample_count": preview.get("confirmed_samples") or 0,
            "workflow_status": draft.workflow_status,
        }
        return outputs, [{"type": "draft", "id": draft.id, "version": "1",
                          "project_id": resource.project_id,
                          "source_dataset_id": resource.id}]


@handler("draft.assign_splits")
class DraftAssignSplits(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["draft_id"],
                                     _spec("draft.assign_splits"))
        draft = adapter._read_draft(resource.id)
        blockers = []
        if draft.get("workflow_status") != "draft":
            blockers.append({"code": "DRAFT_NOT_EDITABLE",
                             "message": "草稿尚未生成完成或已不可修改"})
        preview = {
            "effect": "write",
            "draft": {"id": draft["id"], "name": draft["name"],
                      "workflow_status": draft.get("workflow_status")},
            "split": arguments["split"],
            "sample_count": len(arguments["sample_ids"]),
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_draft_service import DraftValidationError, assign_draft_samples

        if preview.get("blocked"):
            raise ToolRefused("DRAFT_NOT_EDITABLE", "草稿当前不可划分",
                              preview.get("blocked_reasons") or [])
        try:
            result = assign_draft_samples(
                resource.id, arguments["sample_ids"], arguments["split"],
                adapter.database(),
            )
        except DraftValidationError as exc:
            raise ToolRefused("DRAFT_SPLIT_REFUSED", str(exc)) from exc
        outputs = {"draft_id": resource.id, "updated": result.get("updated", 0)}
        return outputs, [{"type": "draft", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


@handler("draft.finalize")
class DraftFinalize(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.dataset_draft_service import DraftValidationError, validate_draft_for_finalization

        resource = adapter._resource(principal, arguments["draft_id"],
                                     _spec("draft.finalize"))
        try:
            validation = validate_draft_for_finalization(resource.id, adapter.database())
        except DraftValidationError as exc:
            draft = adapter._read_draft(resource.id)
            return resource, {
                "effect": "write", "draft": {"id": draft["id"], "name": draft["name"]},
                "blocked": True,
                "blocked_reasons": [{"code": "FINALIZE_CONDITIONS_UNMET",
                                     "message": str(exc)}],
            }
        draft = validation["draft"]
        return resource, {
            "effect": "write",
            "draft": {"id": draft["id"], "name": draft["name"],
                      "workflow_status": draft.get("workflow_status")},
            "class_names": validation["class_names"],
            "counts": validation["counts"],
            "sample_count": validation["sample_count"],
            "blocked": False,
            "blocked_reasons": [],
            "impact": "将生成数据包并登记为可训练数据集版本，草稿保留",
        }

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_draft_service import DraftValidationError
        from services.dataset_finalization_service import finalize_draft

        if preview.get("blocked"):
            raise ToolRefused("FINALIZE_CONDITIONS_UNMET", "定稿条件不满足",
                              preview.get("blocked_reasons") or [])
        workspace = _work_dir(adapter, "finalize")
        try:
            result = finalize_draft(
                resource.id, str(workspace), adapter.database(), adapter.storage_client,
            )
        except DraftValidationError as exc:
            raise ToolRefused("FINALIZE_CONDITIONS_UNMET", str(exc)) from exc
        payload = result.to_dict()
        outputs = {
            "dataset_id": payload["dataset_id"],
            "name": payload["name"],
            "split_counts": preview.get("counts") or {},
            "sample_count": payload.get("sample_count") or preview.get("sample_count"),
        }
        return outputs, [{"type": "dataset", "id": outputs["dataset_id"], "version": "1",
                          "project_id": resource.project_id,
                          "source_draft_id": resource.id}]


@handler("draft.get_state")
class DraftState(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["draft_id"], _spec("draft.get_state"))
        adapter._read_draft(resource.id)
        return resource, {"effect": "read"}

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_draft_service import DraftValidationError, validate_draft_for_finalization

        draft = adapter._read_draft(resource.id)
        with adapter.connect() as conn:
            rows = conn.execute(
                "SELECT split, COUNT(*) AS total FROM dataset_draft_samples "
                "WHERE draft_id=? GROUP BY split", (resource.id,),
            ).fetchall()
        counts = {row["split"]: row["total"] for row in rows}
        finalizable, reasons = True, []
        if draft.get("workflow_status") == "draft":
            try:
                validate_draft_for_finalization(resource.id, adapter.database())
            except DraftValidationError as exc:
                finalizable, reasons = False, [str(exc)]
        else:
            finalizable = False
            reasons = [f"当前状态为 {draft.get('workflow_status')}"]
        outputs = {
            "id": draft["id"],
            "name": draft["name"],
            "workflow_status": draft.get("workflow_status"),
            "counts": counts,
            "finalizable": finalizable,
            "blocking_reasons": reasons,
        }
        return outputs, [{"type": "draft", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


# ------------------------------------------------------------------- training


@handler("training.start")
class TrainingStart(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.training_runner import read_lock

        resource = adapter._resource(principal, arguments["parent_model_id"],
                                     _spec("training.start"))
        dataset = adapter.resolve(principal, "dataset", arguments["dataset_id"],
                                  ("dataset:read",))
        parent = adapter._model_row(resource.id)
        dataset_row = adapter._dataset_row(dataset.id)
        blockers = []
        if parent["status"] not in {1, 3} or not parent.get("model_object_key"):
            blockers.append({"code": "PARENT_MODEL_NOT_READY",
                             "message": "父模型必须训练成功且具有可用权重"})
        if (dataset_row.get("dataset_mode") or "single") != (parent.get("model_mode") or "single"):
            blockers.append({"code": "MODE_MISMATCH",
                             "message": "数据集与父模型的单/多模态模式不一致"})
        if (dataset_row.get("primary_modality") or "rgb") != (parent.get("primary_modality") or "rgb"):
            blockers.append({"code": "MODALITY_MISMATCH",
                             "message": "数据集主模态与父模型不一致"})
        lock = read_lock(adapter.database_path)
        if lock:
            blockers.append({"code": "TRAINING_BUSY",
                             "message": "本机正在训练其他模型，忙时不排队",
                             "holder": lock.get("owner")})
        epochs = int(arguments.get("epochs") or parent.get("epochs") or 1)
        batch_size = int(arguments.get("batch_size") or parent.get("batch_size") or 1)
        version = int(parent.get("version") or 1) + 1
        preview = {
            "effect": "write",
            "parent_model": {"id": parent["id"],
                             "name": parent.get("name") or parent["id"],
                             "version": parent.get("version") or 1},
            "dataset": {"id": dataset_row["id"], "name": dataset_row["name"],
                        "dataset_mode": dataset_row.get("dataset_mode"),
                        "status": dataset_row.get("status")},
            "parameters": {"epochs": epochs, "batch_size": batch_size},
            "new_model": {
                "name": arguments.get("name") or f"{parent.get('name') or parent['id']} v{version}",
                "version": version,
            },
            "output": "新模型版本，不覆盖父模型权重",
            "blocked": bool(blockers),
            "blocked_reasons": blockers,
        }
        return resource, preview

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.model_registry_service import ModelRegistryError, start_incremental_training
        from services.training_runner import (
            TrainingBusy,
            acquire_training_lock,
            release_training_lock,
            transfer_training_lock,
        )

        if preview.get("blocked"):
            raise ToolRefused("TRAINING_BLOCKED", "训练条件不满足",
                              preview.get("blocked_reasons") or [])
        overrides = {
            "epochs": preview["parameters"]["epochs"],
            "batch_size": preview["parameters"]["batch_size"],
        }
        if arguments.get("name"):
            overrides["name"] = arguments["name"]
        owner = f"pending:{uuid.uuid4().hex}"
        try:
            acquire_training_lock(adapter.database_path, owner, os.getpid())
        except TrainingBusy as exc:
            raise ToolRefused("TRAINING_BUSY", str(exc), [exc.holder]) from exc
        try:
            child = start_incremental_training(
                resource.id, arguments["dataset_id"], overrides, adapter.database(),
            )
        except ModelRegistryError as exc:
            release_training_lock(adapter.database_path, owner)
            raise ToolRefused("TRAINING_REFUSED", str(exc)) from exc
        except Exception:
            release_training_lock(adapter.database_path, owner)
            raise
        transfer_training_lock(adapter.database_path, owner, child.model_id)

        try:
            _launch_training(adapter, child, resource.id)
        except Exception as exc:
            release_training_lock(adapter.database_path, child.model_id)
            with adapter.connect() as conn:
                conn.execute("UPDATE models SET status=2 WHERE id=?", (child.model_id,))
            raise ToolRefused("TRAINING_LAUNCH_FAILED", f"训练进程启动失败: {exc}") from exc

        outputs = {
            "model_id": child.model_id,
            "parent_model_id": resource.id,
            "version": child.version,
            "status": 0,
            "state": "training",
            "metrics": {},
        }
        return outputs, [{"type": "model", "id": child.model_id, "version": str(child.version),
                          "project_id": resource.project_id,
                          "parent_model_id": resource.id}]


def _launch_training(adapter, child, parent_model_id: str) -> None:
    from config import TEMP_DIR

    # worker 必须继承当前 app 的库/存储/临时目录，否则训练结果写进另一份库
    storage_path = getattr(adapter.storage_client, "base_path", None)
    payload = {
        "model_id": child.model_id,
        "dataset_id": child.dataset_id,
        "base_model": "",
        "epochs": child.epochs,
        "batch_size": child.batch_size,
        "parent_model_id": parent_model_id,
        "database_path": adapter.database_path,
        "file_store_path": str(storage_path) if storage_path else None,
        "temp_root": str(adapter.work_dir or TEMP_DIR),
    }
    if callable(adapter.training_launcher):
        adapter.training_launcher(**payload)
        return
    import multiprocessing

    from services.training_runner import run_training_job, set_lock_holder

    process = multiprocessing.Process(target=run_training_job, kwargs=payload)
    process.daemon = True
    process.start()
    set_lock_holder(adapter.database_path, child.model_id, process.pid)


@handler("training.get_state")
class TrainingState(BaseHandler):
    _STATE = {0: "training", 1: "succeeded", 2: "failed", 3: "published", 4: "cancelled"}

    def prepare(self, adapter, principal, arguments):
        resource = adapter._resource(principal, arguments["model_id"],
                                     _spec("training.get_state"))
        adapter._model_row(resource.id)
        return resource, {"effect": "read"}

    def execute(self, adapter, principal, arguments, preview, resource):
        model = adapter._model_row(resource.id)
        metrics = {}
        if model.get("metrics_json"):
            try:
                metrics = json.loads(model["metrics_json"])
            except (TypeError, ValueError):
                metrics = {}
        error = None
        if model.get("train_result"):
            try:
                payload = json.loads(model["train_result"])
            except (TypeError, ValueError):
                payload = {}
            error = payload.get("error")
        outputs = {
            "model_id": resource.id,
            "status": model.get("status"),
            "state": self._STATE.get(model.get("status"), "unknown"),
            "metrics": metrics or {},
            "error": error,
        }
        return outputs, [{"type": "model", "id": resource.id, "version": resource.version,
                          "project_id": resource.project_id}]


# --------------------------------------------------------- authorized folders


@handler("directory.inspect")
class DirectoryInspect(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.authorized_directories import inspect_directory

        resource, root = _directory(adapter, principal, arguments["directory_id"])
        report = inspect_directory(root, max_samples=arguments.get("max_samples") or 50000)
        preview = {
            "effect": "read",
            "directory_id": resource.id,
            "root": report["root"],
            "issue_count": len(report["issues"]),
            "sample_count": len(report["samples"]),
            "splits": report["splits"],
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        resource, _ = _directory(adapter, principal, arguments["directory_id"])
        return resource

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.authorized_directories import inspect_directory

        _, root = _directory(adapter, principal, arguments["directory_id"])
        report = inspect_directory(root, max_samples=arguments.get("max_samples") or 50000)
        outputs = {
            "directory_id": resource.id,
            "root": report["root"],
            "splits": report["splits"],
            "class_names": report["class_names"],
            "issues": report["issues"],
            "duplicates": report["duplicates"],
            "leakage": report["leakage"],
        }
        return outputs, [{"type": "directory", "id": resource.id,
                          "version": resource.version,
                          "project_id": resource.project_id}]


def _directory(adapter, principal: int, directory_id: str, spec_name: str = "directory.inspect"):
    if adapter.directory_store is None:
        raise ValueError("A protected directory store is required")
    row = adapter.directory_store.get(directory_id)
    resource = adapter._project_resource(principal, row["project_id"], _spec(spec_name))
    if row["project_id"] != resource.id:
        raise ToolRefused("DIRECTORY_PROJECT_MISMATCH", "目录不属于该资源所在项目")
    return resource, row["root"]


@handler("directory.search")
class DirectorySearch(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        resource, root = _directory(adapter, principal, arguments["directory_id"])
        return resource, {"effect": "read", "root": str(root),
                          "query": arguments["query"]}

    def recheck(self, adapter, principal, arguments, preview):
        resource, _ = _directory(adapter, principal, arguments["directory_id"])
        return resource

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.authorized_directories import search_directory

        _, root = _directory(adapter, principal, arguments["directory_id"])
        outputs = search_directory(root, arguments["query"], arguments.get("limit") or 100)
        outputs["directory_id"] = resource.id
        return outputs, [{"type": "directory", "id": resource.id,
                          "version": resource.version,
                          "project_id": resource.project_id}]


@handler("directory.transform")
class DirectoryTransform(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.dataset_transform_service import plan_from_directory

        resource, root = _directory(adapter, principal, arguments["directory_id"],
                                    "directory.transform")
        return resource, plan_from_directory(adapter, resource, root, arguments)

    def recheck(self, adapter, principal, arguments, preview):
        resource, _ = _directory(adapter, principal, arguments["directory_id"],
                                 "directory.transform")
        return resource

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.dataset_transform_service import build_candidate_from_directory

        if preview.get("blocked"):
            raise ToolRefused("TRANSFORM_BLOCKED", "变换存在未解决冲突",
                              preview.get("conflicts") or [])
        _, root = _directory(adapter, principal, arguments["directory_id"])
        candidate = build_candidate_from_directory(adapter, resource, root, arguments, preview)
        return candidate, [{"type": "dataset_candidate", "id": candidate["candidate_id"],
                            "version": "1", "project_id": resource.project_id}]


# ------------------------------------------------------------------ documents


_DOCUMENT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@handler("document.parse")
class DocumentParse(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.document_service import describe_source

        asset = _asset(adapter, principal, arguments["asset_id"], {"document"})
        spec = _spec("document.parse")
        resource = adapter._project_resource(principal, asset["project_id"], spec)
        description = describe_source(asset)
        preview = {
            "effect": "write",
            "asset": _asset_brief(asset),
            "format": description["format"],
            "parser": description["parser"],
            "blocked": not description["supported"],
            "blocked_reasons": [] if description["supported"] else [{
                "code": "UNSUPPORTED_DOCUMENT",
                "message": description["reason"],
            }],
            "impact": "解析结果只保存文本、表格与定位信息，原文件保持只读",
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        asset = _asset(adapter, principal, arguments["asset_id"], {"document"})
        return adapter._project_resource(principal, asset["project_id"],
                                         _spec("document.parse"))

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.document_service import parse_document

        if preview.get("blocked"):
            raise ToolRefused("UNSUPPORTED_DOCUMENT", "该文档格式暂不支持",
                              preview.get("blocked_reasons") or [])
        asset = _asset(adapter, principal, arguments["asset_id"], {"document"})
        if asset["project_id"] != resource.id:
            raise ToolConflict("Document moved to another project; prepare again")
        local = adapter.file_assets.materialize(asset["bucket"], asset["object_key"])
        try:
            record = parse_document(
                database_path=adapter.database_path,
                asset=asset,
                local_path=local,
                title=arguments.get("title"),
                owner_id=principal,
            )
        finally:
            Path(local).unlink(missing_ok=True)
        outputs = {
            "document_id": record["document_id"],
            "status": record["status"],
            "page_count": record["page_count"],
            "block_count": record["block_count"],
            "warnings": record["warnings"],
        }
        return outputs, [{"type": "document", "id": record["document_id"], "version": "1",
                          "project_id": resource.project_id,
                          "source_file_id": asset["id"]}]


@handler("document.search")
class DocumentSearch(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        from services.document_service import get_document

        spec = _spec("document.search")
        resource = adapter._resource(principal, arguments["document_id"], spec)
        document = get_document(adapter.database_path, resource.id)
        if document["project_id"] != resource.project_id:
            raise ToolRefused("DOCUMENT_PROJECT_MISMATCH", "文档不属于该资源所在项目")
        return resource, {
            "effect": "read",
            "document": {"id": document["id"], "title": document["title"],
                         "status": document["status"], "page_count": document["page_count"]},
            "query": arguments["query"],
        }

    def execute(self, adapter, principal, arguments, preview, resource):
        from services.document_service import search_document

        outputs = search_document(
            adapter.database_path, resource.id, arguments["query"],
            limit=arguments.get("limit") or 20,
        )
        return outputs, [{"type": "document", "id": resource.id,
                          "version": resource.version,
                          "project_id": resource.project_id}]


# ------------------------------------------------------------------------ MCP


def _workspace(adapter, principal: int, spec_name: str = "mcp.save_connection"):
    """The caller's own workspace, with the tool's declared actions applied."""
    spec = _spec(spec_name)
    resource = adapter.resolve(principal, "workspace", str(principal), tuple(spec.global_actions))
    adapter._authorize_snapshot(principal, spec, resource)
    return resource


@handler("mcp.save_connection")
class McpSaveConnection(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        if adapter.mcp_store is None:
            raise ValueError("An MCP connection store is required")
        resource = _workspace(adapter, principal, "mcp.save_connection")
        transport = arguments["transport"]
        if transport == "stdio" and not arguments.get("command"):
            raise ToolRefused("MCP_COMMAND_REQUIRED", "本机连接需要填写启动命令")
        if transport == "http" and not arguments.get("endpoint"):
            raise ToolRefused("MCP_ENDPOINT_REQUIRED", "网络连接需要填写地址")
        existing = None
        if arguments.get("connection_id"):
            existing = adapter.mcp_store.get(arguments["connection_id"], principal)
        preview = {
            "effect": "write",
            "connection": {
                "id": existing["id"] if existing else None,
                "name": arguments["name"],
                "transport": transport,
                "endpoint": arguments.get("endpoint"),
                "command": arguments.get("command"),
                "enabled": arguments.get("enabled", True),
            },
            "action": "update" if existing else "create",
            "credentials": "认证信息交给工作区凭据存储，本表只保存引用",
        }
        return resource, preview

    def recheck(self, adapter, principal, arguments, preview):
        return _workspace(adapter, principal, "mcp.save_connection")

    def execute(self, adapter, principal, arguments, preview, resource):
        record = adapter.mcp_store.save(principal, {**arguments, "id": arguments.get("connection_id")})
        outputs = {
            "connection_id": record["id"],
            "status": record["status"],
            "enabled": record["enabled"],
        }
        return outputs, [{"type": "mcp_connection", "id": record["id"], "version": "1",
                          "project_id": resource.project_id}]


@handler("mcp.test_connection")
class McpTestConnection(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        if adapter.mcp_store is None:
            raise ValueError("An MCP connection store is required")
        resource = _workspace(adapter, principal, "mcp.test_connection")
        connection = adapter.mcp_store.get(arguments["connection_id"], principal)
        return resource, {
            "effect": "read",
            "connection": {"id": connection["id"], "name": connection["name"],
                           "transport": connection["transport"],
                           "enabled": connection["enabled"]},
            "note": "连接成功只说明端点可用，不代表任何工具已被授权调用",
        }

    def recheck(self, adapter, principal, arguments, preview):
        return _workspace(adapter, principal, "mcp.test_connection")

    def execute(self, adapter, principal, arguments, preview, resource):
        result = adapter.mcp_store.test(principal, arguments["connection_id"])
        return result, [{"type": "mcp_connection", "id": arguments["connection_id"],
                         "version": "1", "project_id": resource.project_id}]


@handler("mcp.discover_tools")
class McpDiscoverTools(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        if adapter.mcp_store is None:
            raise ValueError("An MCP connection store is required")
        resource = _workspace(adapter, principal, "mcp.discover_tools")
        connection = adapter.mcp_store.get(arguments["connection_id"], principal)
        return resource, {
            "effect": "read",
            "connection": {"id": connection["id"], "name": connection["name"]},
            "known_tools_version": connection.get("tools_version"),
        }

    def recheck(self, adapter, principal, arguments, preview):
        return _workspace(adapter, principal, "mcp.discover_tools")

    def execute(self, adapter, principal, arguments, preview, resource):
        result = adapter.mcp_store.discover(principal, arguments["connection_id"])
        return result, [{"type": "mcp_connection", "id": arguments["connection_id"],
                         "version": result.get("tools_version") or "1",
                         "project_id": resource.project_id}]


@handler("mcp.execute")
class McpExecute(BaseHandler):
    def prepare(self, adapter, principal, arguments):
        if adapter.mcp_store is None:
            raise ValueError("An MCP connection store is required")
        resource = _workspace(adapter, principal, "mcp.execute")
        tool = adapter.mcp_store.get_tool(
            arguments["connection_id"], principal, arguments["tool_name"],
        )
        if tool is None:
            raise ToolRefused("MCP_TOOL_UNKNOWN",
                              "该工具不在最近一次工具发现结果中，请先重新发现并核对授权")
        return resource, {
            "effect": "write",
            "connection_id": arguments["connection_id"],
            "tool": {"name": tool["name"], "version": tool["version"],
                     "writes": tool["writes"], "billable": tool["billable"],
                     "idempotent": tool["idempotent"], "cancellable": tool["cancellable"]},
            "arguments": arguments.get("arguments") or {},
            "impact": "该调用由外部服务执行，网站不为其实际访问权限背书",
        }

    def recheck(self, adapter, principal, arguments, preview):
        tool = adapter.mcp_store.get_tool(arguments["connection_id"], principal, arguments["tool_name"])
        if tool is None or tool["version"] != preview["tool"]["version"]:
            raise ToolRefused("MCP_TOOL_CHANGED", "MCP 连接或工具已变化，请重新发现并确认")
        if not adapter.mcp_store.get(arguments["connection_id"], principal)["enabled"]:
            raise ToolRefused("MCP_DISABLED", "MCP 连接已停用")
        return _workspace(adapter, principal, "mcp.execute")

    def execute(self, adapter, principal, arguments, preview, resource):
        result = adapter.mcp_store.invoke(
            principal, arguments["connection_id"], arguments["tool_name"],
            arguments.get("arguments") or {},
            expected_version=preview['tool']['version'],
        )
        return {
            "connection_id": arguments["connection_id"],
            "tool_name": arguments["tool_name"],
            "result": result.get("result") or {},
            "status": "failed" if (result.get("result") or {}).get("isError") else "succeeded",
            "effect": result.get("effect") or "unknown",
        }, [{"type": "mcp_connection", "id": arguments["connection_id"],
             "version": "1", "project_id": resource.project_id}]
