"""Declarative descriptions of every business tool the adapter exposes.

A descriptor is the contract the second person's planner reads: name, version,
argument and output shape, whether the call writes or bills, and whether it can
be previewed, queried or cancelled. ``ToolSpec`` adds the resource scope the
adapter must resolve and the permissions it must have before preparing.

Nothing here grants access. Every action listed here is adjudicated by the
workspace policy service supplied at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    descriptor: dict[str, Any]
    # Resource kind passed to ``resolve_resource`` and the argument that names it.
    kind: str
    id_field: str
    global_actions: tuple[str, ...] = ()
    resource_actions: tuple[str, ...] = ()
    # Global actions that must be granted for the tool to be listed at all.
    requires: tuple[str, ...] = ()
    # ``False`` 表示该动作只能由受保护的人工页面提交：不进 LLM 工具清单，
    # Agent 执行入口直接拒绝。人工决定与扩权配置不能由模型代做。
    agent_executable: bool = True
    # (adapter attribute, message) that must exist for the tool to be usable.
    deps: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _tool(
    name: str,
    description: str,
    input_schema: dict,
    output: dict,
    *,
    effect: str = "write",
    version: str = "1",
    billable: bool = False,
    preview: bool = True,
    status: bool = True,
    cancel: bool | str = "prepared_only",
    kind: str,
    id_field: str,
    global_actions: tuple[str, ...] = (),
    resource_actions: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    deps: tuple[tuple[str, str], ...] = (),
    agent_executable: bool = True,
) -> ToolSpec:
    descriptor = {
        "name": name,
        "version": version,
        "description": description,
        "input_schema": input_schema,
        "output_schema": _object(output),
        "effect": effect,
        "billable": billable,
        "supports_preview": preview,
        "supports_status_query": status,
        "supports_cancel": cancel,
        # 入口归属随描述一起下发：前端据此不在聊天里提示这些动作
        "audience": "agent" if agent_executable else "human_page",
    }
    return ToolSpec(
        descriptor=descriptor,
        kind=kind,
        id_field=id_field,
        global_actions=global_actions,
        resource_actions=resource_actions,
        requires=requires,
        deps=deps,
        agent_executable=agent_executable,
    )


_ASSET = {"type": "string", "minLength": 1, "maxLength": 64}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 120}
_CLASS_LIST = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 120},
    "maxItems": 200,
}

_FILE_DEPS = (
    ("file_assets", "requires a protected file asset gateway"),
    ("storage_client", "requires the protected storage adapter"),
)
_MODEL_DEPS = (
    ("model_client_factory", "requires the current user's personal model client"),
)


_SPECS: list[ToolSpec] = [
    # ------------------------------------------------------------- datasets
    _tool(
        "dataset.get_state",
        "Read the current registered dataset state and modality metadata.",
        _object({"dataset_id": {"type": "string", "minLength": 1}}, ["dataset_id"]),
        {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "integer"},
            "dataset_mode": {"type": "string"},
            "primary_modality": {"type": "string"},
            "secondary_modality": {"type": ["string", "null"]},
        },
        effect="read",
        kind="dataset",
        id_field="dataset_id",
        resource_actions=("dataset:read",),
    ),
    _tool(
        "dataset.import",
        "Validate one uploaded YOLO archive and register it as a dataset. The "
        "preparation preview lists classes, modality, sample counts, every "
        "structural or pairing problem, and the dataset that would be created.",
        _object(
            {
                "asset_id": _ASSET,
                "name": _TEXT,
                "description": {"type": "string", "maxLength": 500},
                "dataset_mode": {"type": "string", "enum": ["single", "multimodal"]},
                "secondary_modality": {"type": "string", "minLength": 1, "maxLength": 32},
                "class_names": _CLASS_LIST,
            },
            ["asset_id", "name", "dataset_mode"],
        ),
        {
            "dataset_id": {"type": "string"},
            "name": {"type": "string"},
            "dataset_mode": {"type": "string"},
            "primary_modality": {"type": "string"},
            "secondary_modality": {"type": ["string", "null"]},
            "class_names": {"type": "array", "items": {"type": "string"}},
            "split_counts": {"type": "object"},
            "total_samples": {"type": "integer"},
            "status": {"type": "integer"},
            "source_file_id": {"type": "string"},
            "reused": {"type": "boolean"},
        },
        kind="project",
        id_field="asset_id",
        resource_actions=("file:read", "dataset:create"),
        deps=_FILE_DEPS,
    ),
    _tool(
        "dataset.transform",
        "Plan a derived dataset from one registered dataset: explicit class "
        "mapping, class filter, split policy and optional merge of its "
        "human-confirmed feedback. The preview lists every conflict, the split "
        "leakage check and the candidate output; execution only writes a "
        "candidate directory and manifest, never the source dataset.",
        _object(
            {
                "dataset_id": {"type": "string", "minLength": 1},
                "name": _TEXT,
                "description": {"type": "string", "maxLength": 500},
                "class_map": {"type": "object", "values": {"type": "string"}},
                "keep_classes": _CLASS_LIST,
                "class_names": _CLASS_LIST,
                "split_policy": {
                    "type": "object",
                    "values": {"type": "number"},
                    "minProperties": 1,
                },
                "include_feedback": {"type": "boolean"},
            },
            ["dataset_id", "name"],
        ),
        {
            "candidate_id": {"type": "string"},
            "output_dir": {"type": "string"},
            "class_names": {"type": "array", "items": {"type": "string"}},
            "split_counts": {"type": "object"},
            "sample_count": {"type": "integer"},
            "warnings": {"type": "array"},
            "manifest_path": {"type": "string"},
        },
        kind="dataset",
        id_field="dataset_id",
        resource_actions=("dataset:read", "dataset:create"),
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "dataset.inspect_samples",
        "Read actual image identity and sample paths inside a registered dataset, including dimensions, "
        "file and decoded-pixel hashes, duplicates, cross-split leakage and label conflicts. "
        "Match current confirmed feedback or one authorized record to original samples, including old JPEG95 conversions. "
        "Returns existing/new/ambiguous evidence; filenames alone never authorize replacement. Read-only, does not deduplicate or change labels.",
        _object({'dataset_id': {'type': 'string', 'minLength': 1}, 'record_id': {'type': 'string', 'minLength': 1},
                 'page': {'type': 'integer', 'minimum': 1}, 'page_size': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                 'search': {'type': 'string', 'maxLength': 200}}, ['dataset_id']),
        {'dataset_id': {'type': 'string'}, 'total_samples': {'type': 'integer'}, 'scan_complete': {'type': 'boolean'},
         'duplicate_groups': {'type': 'integer'}, 'duplicates': {'type': 'array'},
         'feedback_matches': {'type': 'array'}, 'samples': {'type': 'array'}},
        effect='read', kind='dataset', id_field='dataset_id', resource_actions=('dataset:read',),
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "dataset.feedback_revision",
        "Return current human-confirmed feedback to its source dataset as a new revision. "
        "Preserve the source archive and existing train/val/test assignments; replace annotations "
        "of content-matched images, append genuinely new images to the explicitly selected split. "
        "Preview exact counts and conflicts, then register the revision in the project workspace. "
        "Does not train. For a separate feedback-only dataset use draft.create_from_feedback then assign_splits/finalize.",
        _object({'dataset_id': {'type': 'string', 'minLength': 1}, 'name': _TEXT,
                 'new_sample_split': {'type': 'string', 'enum': ['train', 'val', 'test']}},
                ['dataset_id', 'name', 'new_sample_split']),
        {'dataset_id': {'type': 'string'}, 'source_dataset_id': {'type': 'string'},
         'name': {'type': 'string'}, 'total_samples': {'type': 'integer'},
         'split_counts': {'type': 'object'}, 'output_dir': {'type': 'string'}, 'candidate_id': {'type': 'string'}},
        kind='dataset', id_field='dataset_id', resource_actions=('dataset:read', 'dataset:create'),
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "dataset.split",
        "Split or re-split a complete registered dataset into train/val/test using explicit ratios. Preview real counts and conflicts, then create and register a new dataset version; never overwrite the source or train a model.",
        _object({"dataset_id": {"type": "string", "minLength": 1}, "name": _TEXT,
                 "split_policy": {"type": "object", "values": {"type": "number"}, "minProperties": 2}},
                ["dataset_id", "name", "split_policy"]),
        {"dataset_id": {"type": "string"}, "source_dataset_id": {"type": "string"},
         "name": {"type": "string"}, "split_counts": {"type": "object"}, "total_samples": {"type": "integer"},
         "candidate_id": {"type": "string"}, "output_dir": {"type": "string"}},
        kind="dataset", id_field="dataset_id", resource_actions=("dataset:read", "dataset:create"), deps=_FILE_DEPS,
    ),
    _tool(
        "dataset.register_candidate",
        "Validate a previously built candidate directory as a YOLO archive and "
        "register it as a new dataset. The source data is left untouched.",
        _object(
            {
                "candidate_id": {"type": "string", "minLength": 1},
                "description": {"type": "string", "maxLength": 500},
            },
            ["candidate_id"],
        ),
        {
            "dataset_id": {"type": "string"},
            "name": {"type": "string"},
            "class_names": {"type": "array", "items": {"type": "string"}},
            "split_counts": {"type": "object"},
            "total_samples": {"type": "integer"},
        },
        kind="project",
        id_field="candidate_id",
        resource_actions=("dataset:create",),
        deps=_FILE_DEPS,
    ),
    # --------------------------------------------------------------- models
    _tool(
        "model.import_weight",
        "Register an uploaded .pt weight as a draft model. The source dataset "
        "fixes the modality and class mapping, so the preview states them "
        "explicitly and refuses to guess: a dataset without a class mapping, or "
        "a weight that cannot load under that modality, blocks the import.",
        _object(
            {
                "asset_id": _ASSET,
                "dataset_id": {"type": "string", "minLength": 1},
                "name": _TEXT,
                "description": {"type": "string", "maxLength": 500},
            },
            ["asset_id", "dataset_id", "name"],
        ),
        {
            "model_id": {"type": "string"},
            "name": {"type": "string"},
            "dataset_id": {"type": "string"},
            "model_mode": {"type": "string"},
            "primary_modality": {"type": "string"},
            "secondary_modality": {"type": ["string", "null"]},
            "class_names": {"type": "array", "items": {"type": "string"}},
            "publish_status": {"type": "string"},
            "source_file_id": {"type": "string"},
            "reused": {"type": "boolean"},
        },
        kind="project",
        id_field="asset_id",
        resource_actions=("file:read", "model:create"),
        deps=_FILE_DEPS,
    ),
    _tool(
        "model.get_state",
        "Read the registered model, its dataset binding, version and publish state.",
        _object({"model_id": {"type": "string", "minLength": 1}}, ["model_id"]),
        {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "integer"},
            "publish_status": {"type": "string"},
            "dataset_id": {"type": "string"},
            "model_mode": {"type": "string"},
            "primary_modality": {"type": "string"},
            "secondary_modality": {"type": ["string", "null"]},
            "version": {"type": "integer"},
            "parent_model_id": {"type": ["string", "null"]},
            "metrics": {"type": "object"},
        },
        effect="read",
        kind="model",
        id_field="model_id",
        resource_actions=("model:read",),
    ),
    _tool(
        "model.publish",
        "Publish a completed model and archive the prior model in its modality domain.",
        _object({"model_id": {"type": "string", "minLength": 1}}, ["model_id"]),
        {
            "model_id": {"type": "string"},
            "previous_model_id": {"type": ["string", "null"]},
            "publish_status": {"type": "string"},
            "archived_model_ids": {"type": "array", "items": {"type": "string"}},
        },
        kind="model",
        id_field="model_id",
        # 普通用户发布自己项目里的模型，不再要求全局发布权；范围由 owner/project 决定
        resource_actions=("model:publish",),
        deps=(("storage_exists", "requires a storage existence check"),),
    ),
    _tool(
        "model.unpublish",
        "Take one published model offline. The preview names the model and the "
        "archived models that will NOT be restored automatically.",
        _object({"model_id": {"type": "string", "minLength": 1}}, ["model_id"]),
        {
            "model_id": {"type": "string"},
            "publish_status": {"type": "string"},
            "archived_model_ids": {"type": "array", "items": {"type": "string"}},
        },
        kind="model",
        id_field="model_id",
        # 普通用户发布自己项目里的模型，不再要求全局发布权；范围由 owner/project 决定
        resource_actions=("model:publish",),
    ),
    _tool(
        "model.compare",
        "Compare a child model with its parent using the metrics each run "
        "recorded. Unequal evaluation conditions are reported, not hidden.",
        _object(
            {
                "parent_model_id": {"type": "string", "minLength": 1},
                "child_model_id": {"type": "string", "minLength": 1},
            },
            ["parent_model_id", "child_model_id"],
        ),
        {
            "parent_model_id": {"type": "string"},
            "child_model_id": {"type": "string"},
            "comparison": {"type": "object"},
            "per_class": {"type": "object"},
            "conditions": {"type": "object"},
        },
        effect="read",
        kind="model",
        id_field="parent_model_id",
        resource_actions=("model:read",),
    ),
    # ------------------------------------------------------------ detection
    _tool(
        "detection.run",
        "Run one image through a readable published model and store a real "
        "detection record in the chosen output project. The model's own "
        "project/owner is never changed. A multimodal model without its "
        "X-modality image stops here instead of inventing a pair.",
        _object(
            {
                "asset_id": _ASSET,
                "model_id": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0.01, "maximum": 1},
                "secondary_asset_id": _ASSET,
                # 输出归属：默认取原图所在项目，可显式指定（必须可写）
                "output_project_id": {"type": "string", "minLength": 1},
                "title": {"type": "string", "maxLength": 200},
            },
            ["asset_id", "model_id"],
        ),
        {
            "record_id": {"type": "string"},
            "model_id": {"type": "string"},
            "model_name": {"type": "string"},
            "detection_count": {"type": "integer"},
            "detections": {"type": "array"},
            "original_image_url": {"type": "string"},
            "result_image_url": {"type": "string"},
            "secondary_image_url": {"type": ["string", "null"]},
            "secondary_image_origin": {"type": ["string", "null"]},
            "output_project_id": {"type": "string"},
        },
        kind="project",
        id_field="asset_id",
        resource_actions=("file:read", "detection:create"),
        deps=_FILE_DEPS,
    ),
    _tool(
        "detection.get_state",
        "Read one detection record: annotation inputs, AI review state and human "
        "review state with its version.",
        _object({"record_id": {"type": "string", "minLength": 1}}, ["record_id"]),
        {
            "id": {"type": "string"},
            "model_id": {"type": "string"},
            "detection_result": {"type": "array"},
            "ai_review": {"type": ["object", "null"]},
            "human_review_status": {"type": ["string", "null"]},
            "review_version": {"type": "integer"},
            "corrected_annotations": {"type": "array"},
        },
        effect="read",
        kind="record",
        id_field="record_id",
        resource_actions=("detection:read",),
    ),
    _tool(
        "review.ai",
        "Ask the current user's own model to review detections for false "
        "positives and misses. One item per record; failures are reported per "
        "record and never retried automatically because the call is billable.",
        _object(
            {
                "record_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 50,
                }
            },
            ["record_ids"],
        ),
        {
            "requested": {"type": "integer"},
            "succeeded": {"type": "integer"},
            "failed": {"type": "integer"},
            "items": {"type": "array"},
        },
        effect="write",
        billable=True,
        cancel=False,
        kind="record",
        id_field="record_ids",
        resource_actions=("detection:review",),
        deps=_MODEL_DEPS,
    ),
    # --------------------------------------------------------------- review
    _tool(
        "review.submit_human",
        "Submit a human annotation decision for one detection record. Only this "
        "call produces a human-confirmed sample; AI results never do.",
        _object(
            {
                "record_id": {"type": "string", "minLength": 1},
                "expected_version": {"type": "integer", "minimum": 0},
                "decision": {
                    "type": "string",
                    "enum": ["confirmed", "no_defect", "rejected"],
                },
                "annotations": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "properties": {
                            "class_name": {"type": "string", "minLength": 1, "maxLength": 120},
                            "class_id": {"type": "integer", "minimum": 0},
                            "bbox": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 4,
                                "maxItems": 4,
                            },
                        },
                        "required": ["class_name", "class_id", "bbox"],
                        "additionalProperties": False,
                    },
                },
            },
            ["record_id", "expected_version", "decision"],
        ),
        {
            "record_id": {"type": "string"},
            "human_review_status": {"type": "string"},
            "review_version": {"type": "integer"},
            "annotation_count": {"type": "integer"},
        },
        kind="record",
        id_field="record_id",
        resource_actions=("detection:review",),
        # 最终人工标签只能由受保护的人工复核页面提交：模型看得到就会替人做决定
        agent_executable=False,
    ),
    _tool(
        "feedback.add_confirmed",
        "Add one human-confirmed record to a feedback dataset. Records that are "
        "not human-confirmed, or whose classes do not match the dataset, are refused.",
        _object(
            {
                "record_id": {"type": "string", "minLength": 1},
                "dataset_id": {"type": "string", "minLength": 1},
            },
            ["record_id", "dataset_id"],
        ),
        {
            "sample_id": {"type": "string"},
            "record_id": {"type": "string"},
            "dataset_id": {"type": "string"},
            "created": {"type": "boolean"},
        },
        kind="record",
        id_field="record_id",
        resource_actions=("detection:read", "dataset:update"),
    ),
    _tool(
        "draft.create_from_feedback",
        "Build a draft dataset that contains only human-confirmed feedback "
        "samples. The original training set is never mixed in implicitly.",
        _object({"dataset_id": {"type": "string", "minLength": 1}}, ["dataset_id"]),
        {
            "draft_id": {"type": "string"},
            "name": {"type": "string"},
            "sample_count": {"type": "integer"},
            "workflow_status": {"type": "string"},
        },
        kind="dataset",
        id_field="dataset_id",
        resource_actions=("dataset:read", "draft:create"),
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "draft.assign_splits",
        "Assign draft samples to train/val/test/excluded. This is the human "
        "split step; nothing is inferred from file names.",
        _object(
            {
                "draft_id": {"type": "string", "minLength": 1},
                "sample_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 5000,
                },
                "split": {
                    "type": "string",
                    "enum": ["train", "val", "test", "excluded"],
                },
            },
            ["draft_id", "sample_ids", "split"],
        ),
        {"draft_id": {"type": "string"}, "updated": {"type": "integer"}},
        kind="draft",
        id_field="draft_id",
        resource_actions=("draft:update",),
        # 按用户明确指令划分，沿用真实服务、样本ID校验和业务确认卡。
        agent_executable=True,
    ),
    _tool(
        "draft.finalize",
        "Check the draft against the real finalization conditions and, when it "
        "passes, write the archive and register a trainable dataset version.",
        _object({"draft_id": {"type": "string", "minLength": 1}}, ["draft_id"]),
        {
            "dataset_id": {"type": "string"},
            "name": {"type": "string"},
            "split_counts": {"type": "object"},
            "sample_count": {"type": "integer"},
        },
        kind="draft",
        id_field="draft_id",
        resource_actions=("draft:read", "draft:finalize", "dataset:create"),
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "draft.get_state",
        "Read a draft dataset, its split counts and whether it may be finalized.",
        _object({"draft_id": {"type": "string", "minLength": 1}}, ["draft_id"]),
        {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "workflow_status": {"type": "string"},
            "counts": {"type": "object"},
            "finalizable": {"type": "boolean"},
            "blocking_reasons": {"type": "array"},
        },
        effect="read",
        kind="draft",
        id_field="draft_id",
        resource_actions=("draft:read",),
    ),
    # -------------------------------------------------------------- training
    _tool(
        "training.start",
        "Start one incremental training run from a parent model and a finalized "
        "dataset. The machine runs at most one training at a time and rejects a "
        "second request instead of queueing it.",
        _object(
            {
                "parent_model_id": {"type": "string", "minLength": 1},
                "dataset_id": {"type": "string", "minLength": 1},
                "name": {"type": "string", "maxLength": 120},
                "epochs": {"type": "integer", "minimum": 1, "maximum": 10000},
                "batch_size": {"type": "integer", "minimum": 1, "maximum": 1024},
            },
            ["parent_model_id", "dataset_id"],
        ),
        {
            "model_id": {"type": "string"},
            "parent_model_id": {"type": "string"},
            "version": {"type": "integer"},
            "status": {"type": "integer"},
            "metrics": {"type": "object"},
        },
        kind="model",
        id_field="parent_model_id",
        resource_actions=("model:read", "training:create"),
        cancel=False,
        deps=(("storage_client", "requires the protected storage adapter"),),
    ),
    _tool(
        "training.get_state",
        "Read the persisted training state of one model row. A restart reports "
        "the stored state and never re-submits a run.",
        _object({"model_id": {"type": "string", "minLength": 1}}, ["model_id"]),
        {
            "model_id": {"type": "string"},
            "status": {"type": "integer"},
            "state": {"type": "string"},
            "metrics": {"type": "object"},
            "error": {"type": ["string", "null"]},
        },
        effect="read",
        kind="model",
        id_field="model_id",
        resource_actions=("model:read",),
    ),
    # ------------------------------------------------------- authorized dirs
    _tool(
        "directory.inspect",
        "Inspect one explicitly registered directory: real path containment, "
        "symlink escapes, label parsing, pair completeness, duplicate content, "
        "class ids and per-split counts.",
        _object(
            {
                "directory_id": {"type": "string", "minLength": 1},
                "max_samples": {"type": "integer", "minimum": 1, "maximum": 200000},
            },
            ["directory_id"],
        ),
        {
            "directory_id": {"type": "string"},
            "root": {"type": "string"},
            "splits": {"type": "object"},
            "class_names": {"type": "array"},
            "issues": {"type": "array"},
            "duplicates": {"type": "array"},
            "leakage": {"type": "object"},
        },
        effect="read",
        kind="directory",
        id_field="directory_id",
        resource_actions=("directory:read",),
        deps=(("directory_store", "requires the authorized directory store"),),
    ),
    _tool(
        "directory.search",
        "Search file names inside one authorized directory without leaving it.",
        _object(
            {
                "directory_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["directory_id", "query"],
        ),
        {
            "directory_id": {"type": "string"},
            "matches": {"type": "array"},
            "truncated": {"type": "boolean"},
        },
        effect="read",
        kind="directory",
        id_field="directory_id",
        resource_actions=("directory:read",),
        deps=(("directory_store", "requires the authorized directory store"),),
    ),
    _tool(
        "directory.transform",
        "Build a candidate dataset directory from one authorized directory with "
        "an explicit class mapping, optional class filter and split policy. The "
        "output is a new candidate; source files are never modified.",
        _object(
            {
                "directory_id": {"type": "string", "minLength": 1},
                "name": _TEXT,
                "description": {"type": "string", "maxLength": 500},
                "class_map": {"type": "object", "values": {"type": "string"}},
                "keep_classes": _CLASS_LIST,
                "class_names": _CLASS_LIST,
                "split_policy": {
                    "type": "object",
                    "values": {"type": "number"},
                    "minProperties": 1,
                },
            },
            ["directory_id", "name"],
        ),
        {
            "candidate_id": {"type": "string"},
            "output_dir": {"type": "string"},
            "class_names": {"type": "array"},
            "split_counts": {"type": "object"},
            "sample_count": {"type": "integer"},
            "warnings": {"type": "array"},
            "manifest_path": {"type": "string"},
        },
        kind="directory",
        id_field="directory_id",
        resource_actions=("directory:read", "dataset:create"),
        deps=(("directory_store", "requires the authorized directory store"),),
    ),
    # -------------------------------------------------------------- documents
    _tool(
        "document.parse",
        "Parse one uploaded PDF or DOCX into ordered text and tables with real "
        "page order (PDF) or paragraph/table anchors (Word). Legacy .doc is "
        "refused with a conversion hint.",
        _object(
            {
                "asset_id": _ASSET,
                "title": {"type": "string", "maxLength": 200},
            },
            ["asset_id"],
        ),
        {
            "document_id": {"type": "string"},
            "status": {"type": "string"},
            "page_count": {"type": "integer"},
            "block_count": {"type": "integer"},
            "warnings": {"type": "array"},
        },
        kind="project",
        id_field="asset_id",
        resource_actions=("file:read", "document:create"),
        deps=_FILE_DEPS,
    ),
    _tool(
        "document.search",
        "Search parsed document text and return the exact citation anchor: PDF "
        "page number or Word paragraph/table position.",
        _object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "document_id": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query", "document_id"],
        ),
        {
            "hits": {"type": "array"},
            "truncated": {"type": "boolean"},
        },
        effect="read",
        kind="document",
        id_field="document_id",
        resource_actions=("document:read",),
    ),
    # ------------------------------------------------------------------- MCP
    _tool(
        "mcp.save_connection",
        "Register or update one user-configured MCP endpoint. Credentials are "
        "handed to the workspace secret store, never stored in this table.",
        _object(
            {
                "connection_id": {"type": "string", "maxLength": 64},
                "name": _TEXT,
                "transport": {"type": "string", "enum": ["stdio", "http"]},
                "endpoint": {"type": "string", "maxLength": 500},
                "command": {"type": "string", "maxLength": 500},
                "arguments": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 200},
                    "maxItems": 32,
                },
                "enabled": {"type": "boolean"},
                "credential_ref": {"type": "string", "maxLength": 200},
            },
            ["name", "transport"],
        ),
        {
            "connection_id": {"type": "string"},
            "status": {"type": "string"},
            "enabled": {"type": "boolean"},
        },
        kind="workspace",
        id_field="connection_id",
        resource_actions=("mcp:manage",),
        deps=(("mcp_store", "requires the MCP connection store"),),
        # 连接配置（含启动命令与凭据引用）只能由用户在设置界面修改
        agent_executable=False,
    ),
    _tool(
        "mcp.test_connection",
        "Open one connection and report transport status. Connecting never "
        "authorises any tool call.",
        _object({"connection_id": {"type": "string", "minLength": 1}}, ["connection_id"]),
        {
            "connection_id": {"type": "string"},
            "connected": {"type": "boolean"},
            "server": {"type": "object"},
            "error": {"type": ["string", "null"]},
        },
        effect="read",
        kind="mcp_connection",
        id_field="connection_id",
        resource_actions=("mcp:manage",),
        deps=(("mcp_store", "requires the MCP connection store"),),
    ),
    _tool(
        "mcp.discover_tools",
        "List the tools one connection advertises with their schemas and a "
        "content version, so the workspace can re-check its authorisation when "
        "the external service changes.",
        _object({"connection_id": {"type": "string", "minLength": 1}}, ["connection_id"]),
        {
            "connection_id": {"type": "string"},
            "tools": {"type": "array"},
            "tools_version": {"type": "string"},
            "changed": {"type": "boolean"},
        },
        effect="read",
        kind="mcp_connection",
        id_field="connection_id",
        resource_actions=("mcp:manage",),
        deps=(("mcp_store", "requires the MCP connection store"),),
    ),
    _tool(
        "mcp.execute",
        "Call one external MCP tool after the workspace has authorising it. The "
        "effect flags of the external tool decide whether it can be retried; an "
        "unknown outcome is never resent automatically.",
        _object(
            {
                "connection_id": {"type": "string", "minLength": 1},
                "tool_name": {"type": "string", "minLength": 1, "maxLength": 200},
                "arguments": {"type": "object"},
            },
            ["connection_id", "tool_name"],
        ),
        {
            "connection_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "result": {"type": "object"},
            "effect": {"type": "string"},
        },
        billable=False,
        cancel=False,
        kind="mcp_connection",
        id_field="connection_id",
        resource_actions=("mcp:execute",),
        deps=(("mcp_store", "requires the MCP connection store"),),
    ),
]


TOOL_SPECS: dict[str, ToolSpec] = {spec.descriptor["name"]: spec for spec in _SPECS}
