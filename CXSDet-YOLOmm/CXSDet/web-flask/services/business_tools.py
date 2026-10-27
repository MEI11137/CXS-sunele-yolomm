"""Server-only business tool boundary for the existing Flask services.

The caller must supply the trusted identity and the workspace team's real
resource resolver and access check. No HTTP route or permissive fallback is
registered here.

Each tool is described once in :mod:`services.business_tool_specs` and
implemented in :mod:`services.tool_handlers`. This module owns the parts that
must not vary per tool:

* strict argument validation against the declared schema,
* resolution of the business resource and the permission check on its real
  owner/project/version (never an id from the request),
* the durable operation record: ``prepared`` → ``running`` → one terminal state,
  with ``request_key`` replay and prepared-snapshot re-checks,
* error classification: a ``ValueError``/``PermissionError``/``FileNotFoundError``
  means the business refused the call before writing anything, so the operation
  is ``failed``; an unexpected ``RuntimeError`` during a write may have left an
  effect behind, so the operation is ``unknown`` and must be reconciled by
  querying the business resource, never by retrying blindly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import asdict
from typing import Any, Callable

from services.business_tool_specs import TOOL_SPECS
from services.dataset_import_service import DatasetImportError
from services.tool_common import ResourceSnapshot, ToolConflict

LOGGER = logging.getLogger(__name__)

__all__ = ["BusinessToolAdapter", "ResourceSnapshot", "ToolConflict"]

# 执行入口。人工页面与 Agent 是**两个服务端入口**，不能靠参数里的一个布尔值区分。
ENTRY_AGENT = "agent"
ENTRY_HUMAN_PAGE = "human_page"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    from services.tool_common import utc_now

    return utc_now()


# ------------------------------------------------------------------ validation


def _check_number(name: str, value, rule: dict) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if "minimum" in rule and number < rule["minimum"]:
        raise ValueError(f"{name} is below the minimum")
    if "maximum" in rule and number > rule["maximum"]:
        raise ValueError(f"{name} is above the maximum")
    return number


def _check_object(name: str, value, rule: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    properties = rule.get("properties")
    if properties is not None:
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"Unsupported arguments: {', '.join(sorted(unknown))}")
        for field in rule.get("required", []):
            if field not in value:
                raise ValueError(f"{name}.{field} is required")
        return {
            key: _check_value(f"{name}.{key}", item, properties[key])
            for key, item in value.items()
        }
    value_rule = rule.get("values")
    if value_rule is not None:
        if "minProperties" in rule and len(value) < rule["minProperties"]:
            raise ValueError(f"{name} has too few entries")
        for key, item in value.items():
            _check_value(f"{name}.{key}", key, {"type": "string"})
            _check_value(f"{name}.{key}", item, value_rule)
        return dict(value)
    return dict(value)


def _check_value(name: str, value, rule: dict) -> Any:
    """Strict JSON-schema subset validation; unknown keywords fail closed."""
    kind = rule.get("type")
    if isinstance(kind, list):
        for option in kind:
            try:
                return _check_value(name, value, {**rule, "type": option})
            except ValueError:
                continue
        raise ValueError(f"{name} must be one of {kind}")
    if kind == "string":
        if not isinstance(value, str) or isinstance(value, bool):
            raise ValueError(f"{name} must be a string")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"{name} must be one of {rule['enum']}")
        if len(value.strip()) < rule.get("minLength", 0):
            raise ValueError(f"{name} is too short")
        if "maxLength" in rule and len(value) > rule["maxLength"]:
            raise ValueError(f"{name} is too long")
        return value
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        _check_number(name, value, rule)
        return value
    if kind == "number":
        return _check_number(name, value, rule)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{name} must be an array")
        if "minItems" in rule and len(value) < rule["minItems"]:
            raise ValueError(f"{name} has too few items")
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            raise ValueError(f"{name} has too many items")
        item_rule = rule.get("items") or {}
        return [_check_value(f"{name}[{index}]", item, item_rule)
                for index, item in enumerate(value)]
    if kind == "object":
        return _check_object(name, value, rule)
    raise ValueError(f"Unsupported schema type for {name}")


def _validate_arguments(spec, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    schema = spec.descriptor["input_schema"]
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    if unknown:
        raise ValueError(f"Unsupported arguments: {', '.join(sorted(unknown))}")
    for field in schema.get("required", []):
        if field not in arguments:
            raise ValueError(f"{field} is required")
    return {
        key: _check_value(key, value, properties[key])
        for key, value in arguments.items()
    }


class _DatabaseHandle:
    """Minimal ``get_connection``/``query``/``execute`` facade over SQLite."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=30)

    def query(self, sql: str, params: tuple = (), fetchone: bool = False):
        with sqlite3.connect(self.database_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        if fetchone:
            return dict(rows[0]) if rows else None
        return [dict(row) for row in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        with sqlite3.connect(self.database_path, timeout=30) as conn:
            return conn.execute(sql, params).rowcount


class BusinessToolAdapter:
    """Fixed tool registry with mandatory policy injection and durable records."""

    def __init__(
        self,
        database_path: str,
        *,
        require_access: Callable,
        resolve_resource: Callable,
        storage_exists: Callable[[str, str], bool] | None = None,
        file_assets=None,
        storage_client=None,
        model_client_factory: Callable | None = None,
        review_client_factory: Callable | None = None,
        detector_loader: Callable | None = None,
        detector_predictor: Callable | None = None,
        notify: Callable | None = None,
        work_dir=None,
        directory_store=None,
        mcp_store=None,
        candidate_root=None,
        training_launcher: Callable | None = None,
        model_scope_resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        if not callable(require_access) or not callable(resolve_resource):
            raise ValueError("A real access check and resource resolver are required")
        self.database_path = str(database_path)
        self.require_access = require_access
        self.resolve_resource = resolve_resource
        self.storage_exists = storage_exists
        # ``file_assets`` must expose resolve(principal, asset_id),
        # materialize(bucket, object_key) and (optionally) bind_with(conn, ...).
        self.file_assets = file_assets
        # ``storage_client`` is the protected local store adapter (upload/delete)
        # used to keep the validated archive that a dataset row points at.
        self.storage_client = storage_client
        # The current user's own model client; supplied per call by the
        # workspace so no shared key is ever held here.
        self.model_client_factory = model_client_factory
        # 复核专用：零参数工厂，返回实现了 ``review_detection_structured`` 的客户端。
        # 由装配层按当前用户注入（含视觉能力校验）；未注入时退回 model_client_factory。
        self.review_client_factory = review_client_factory
        self.detector_loader = detector_loader
        # Inference entry point; injected by tests or by a deployment that wraps
        # the runtime differently.
        self.detector_predictor = detector_predictor
        # Business change notification sink owned by the workspace.
        self.notify = notify
        self.work_dir = str(work_dir) if work_dir is not None else None
        self.directory_store = directory_store
        self.mcp_store = mcp_store
        self.candidate_root = str(candidate_root) if candidate_root is not None else None
        # Starts the worker that runs the training. Defaults to a daemon
        # process around ``training_runner.run_training_job``; injected in tests.
        self.training_launcher = training_launcher
        # 发布/归档的合法范围：``model_id -> [同一真实 owner+project 的模型 ID]``。
        # 未注入时范围只有模型自己——不会按整表同模态去归档别人的模型。
        self.model_scope_resolver = model_scope_resolver
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS business_tool_operations (
                    operation_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    request_key TEXT,
                    status TEXT NOT NULL,
                    business_refs_json TEXT NOT NULL DEFAULT '[]',
                    outputs_json TEXT NOT NULL DEFAULT '{}',
                    item_results_json TEXT NOT NULL DEFAULT '[]',
                    error_json TEXT,
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(business_tool_operations)")}
            if "preview_json" not in columns:
                conn.execute(
                    "ALTER TABLE business_tool_operations "
                    "ADD COLUMN preview_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_business_tool_request "
                "ON business_tool_operations(principal_id, request_key) "
                "WHERE request_key IS NOT NULL"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ tools

    @staticmethod
    def _spec(tool_name: str):
        spec = TOOL_SPECS.get(tool_name)
        if spec is None:
            raise ValueError("Unknown business tool")
        return spec

    @staticmethod
    def _handlers() -> dict:
        from services.tool_handlers import HANDLERS

        return HANDLERS

    def _dependency_error(self, tool_name: str) -> str | None:
        spec = self._spec(tool_name)
        if tool_name == "review.ai" and not (
            callable(self.review_client_factory) or callable(self.model_client_factory)
        ):
            return "review.ai requires the current user's personal model client"
        for attribute, message in spec.deps:
            value = getattr(self, attribute, None)
            if value is None:
                return f"{self._label(tool_name)} {message}"
        return None

    @staticmethod
    def _label(tool_name: str) -> str:
        return {
            "dataset.import": "Dataset import",
            "model.import_weight": "Weight import",
            "detection.run": "Detection",
            "model.publish": "Model publish",
        }.get(tool_name, tool_name)

    @staticmethod
    def _require_entry(tool_name: str, spec, entry: str) -> None:
        """人工页面专属动作只能从人工页面入口调用。

        服务端只认入口参数（由受保护的路由传 ``ENTRY_HUMAN_PAGE``），不认请求体里
        的任何 "human=true" 标记，也不认一张普通的确认卡。
        """
        if spec.agent_executable:
            return
        if entry == ENTRY_HUMAN_PAGE:
            return
        raise PermissionError(
            f"{tool_name} 只能由受保护的人工页面提交，Agent 执行入口不提供该动作"
        )

    def list_tools(self, principal: int) -> list[dict]:
        self._check_access(principal, "tool:list", None)
        names = []
        for name, spec in TOOL_SPECS.items():
            if not spec.agent_executable:
                # 人工决定与扩权配置不进 LLM 工具清单
                continue
            if self._dependency_error(name) is not None:
                continue
            if spec.requires and not self._has_actions(principal, spec.requires):
                continue
            names.append(name)
        return [
            json.loads(_json(TOOL_SPECS[name].descriptor))
            for name in names
        ]

    def _has_actions(self, principal: int, actions: tuple[str, ...]) -> bool:
        try:
            return all(self.require_access(principal, action, None) is True
                       for action in actions)
        except PermissionError:
            return False

    # ----------------------------------------------------------------- policy

    def _check_access(self, principal: int, action: str, resource: ResourceSnapshot | None) -> None:
        if self.require_access(principal, action, resource) is not True:
            raise PermissionError("Access denied")

    def _authorize_snapshot(self, principal: int, spec, snapshot: ResourceSnapshot) -> None:
        for action in spec.global_actions:
            self._check_access(principal, action, None)
        for action in spec.resource_actions:
            self._check_access(principal, action, snapshot)

    def resolve(self, principal: int, kind: str, resource_id: str,
                actions: tuple[str, ...] = ()) -> ResourceSnapshot:
        """Resolve one business resource and check the given actions on it."""
        resource = self.resolve_resource(principal, kind, resource_id)
        if not isinstance(resource, ResourceSnapshot) or resource.id != resource_id:
            raise ValueError("Resource resolver returned an invalid snapshot")
        for action in actions:
            self._check_access(principal, action, resource)
        return resource

    def _resource(self, principal: int, resource_id: str, spec) -> ResourceSnapshot:
        for action in spec.global_actions:
            self._check_access(principal, action, None)
        resource = self.resolve(principal, spec.kind, resource_id)
        self._authorize_snapshot(principal, spec, resource)
        return resource

    def _project_resource(self, principal: int, project_id: str, spec) -> ResourceSnapshot:
        return self._resource(principal, project_id, spec)

    # ------------------------------------------------------------ read model

    def database(self):
        """Service-layer facade (``get_connection``/``query``/``execute``)."""
        return _DatabaseHandle(self.database_path)

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def _read_model(self, model_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, status, model_bucket, model_object_key, dataset_id, "
                "model_mode, primary_modality, secondary_modality, publish_status "
                "FROM models WHERE id=?", (model_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Model is not registered")
        return dict(row)

    def _model_row(self, model_id: str) -> dict:
        """Full row as stored; older databases may miss newer columns."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Model is not registered")
        return dict(row)

    def _authorised_model_scope(self, principal: int, model_id: str) -> list[str]:
        """发布/归档的合法范围。

        ``model_scope_resolver`` 由装配层注入（按真实 owner + project 计算）。
        没有注入时只返回模型自己：宁可"不归档任何旧模型"，也不能归档别人的。
        范围内的每个模型都要重新判一次 ``model:publish``——范围本身不代替授权。
        """
        if callable(self.model_scope_resolver):
            raw = self.model_scope_resolver(model_id) or []
        else:
            raw = [model_id]
        scope = {str(item) for item in raw if item}
        scope.add(str(model_id))
        for candidate in sorted(scope):
            try:
                resource = self.resolve(principal, "model", candidate)
            except (PermissionError, ValueError, FileNotFoundError) as exc:
                raise PermissionError(
                    f"归档清单里的模型 {candidate} 无权限或不存在：{exc}"
                ) from exc
            self._check_access(principal, "model:publish", resource)
        return sorted(scope)

    def _publish_model(self, model_id: str, principal: int,
                       expected_archived_model_ids: list[str],
                       scope_model_ids: list[str] | None = None) -> dict:
        from services.model_registry_service import ModelPublishConflict, publish_model

        # 每次执行都重新核一遍授权；确认时的范围只能缩小，不能被参数扩大
        scope = set(self._authorised_model_scope(principal, model_id))
        if scope_model_ids:
            scope &= {str(item) for item in scope_model_ids}
        scope = sorted(scope)
        try:
            result = publish_model(
                model_id, principal, _DatabaseHandle(self.database_path), self.storage_exists,
                expected_archived_model_ids=expected_archived_model_ids,
                scope_model_ids=scope,
            )
        except ModelPublishConflict as exc:
            raise ToolConflict(str(exc)) from exc
        payload = result.to_dict()
        payload["archived_model_ids"] = sorted(
            payload.get("archived_model_ids") or expected_archived_model_ids or []
        )
        return payload

    def _read_dataset(self, dataset_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, status, dataset_mode, primary_modality, "
                "secondary_modality FROM datasets WHERE id=?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Dataset is not registered")
        return dict(row)

    def _dataset_row(self, dataset_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, status, dataset_mode, primary_modality, "
                "secondary_modality, structure_info, zip_bucket, zip_object_key, "
                "file_size FROM datasets WHERE id=?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Dataset is not registered")
        return dict(row)

    def _read_record(self, record_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, user_id, model_id, human_review_status, review_version, "
                "corrected_annotations, detection_result, original_image_bucket, "
                "original_image_object_key, secondary_image_bucket, "
                "secondary_image_object_key, ai_review_status, ai_review_result "
                "FROM detection_records WHERE id=?", (record_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Detection record is not registered")
        return dict(row)

    def _read_draft(self, draft_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, workflow_status, dataset_mode, source_type, "
                "source_dataset_id, error_message FROM dataset_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Draft dataset is not registered")
        return dict(row)

    # handlers use this to turn a domain model row into tool output
    @staticmethod
    def model_output(model: dict) -> dict:
        metrics = model.get("metrics_json")
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics or "{}")
            except json.JSONDecodeError:
                metrics = {"raw": metrics}
        return {
            "id": model["id"],
            "name": model.get("name"),
            "status": model.get("status"),
            "publish_status": model.get("publish_status"),
            "dataset_id": model.get("dataset_id"),
            "model_mode": model.get("model_mode"),
            "primary_modality": model.get("primary_modality"),
            "secondary_modality": model.get("secondary_modality"),
            "version": model.get("version") or 1,
            "parent_model_id": model.get("parent_model_id"),
            "metrics": metrics or {},
        }

    # -------------------------------------------------------------- lifecycle

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict, resource: ResourceSnapshot) -> str:
        payload = _json({"tool": tool_name, "arguments": arguments, "resource": asdict(resource)})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation(row: sqlite3.Row) -> dict:
        return {
            "operation_id": row["operation_id"],
            "status": row["status"],
            "business_refs": json.loads(row["business_refs_json"]),
            "outputs": json.loads(row["outputs_json"]),
            "item_results": json.loads(row["item_results_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    def prepare_tool(self, principal: int, tool_name: str, arguments: dict,
                     *, entry: str = ENTRY_AGENT) -> dict:
        spec = self._spec(tool_name)
        self._require_entry(tool_name, spec, entry)
        unavailable = self._dependency_error(tool_name)
        if unavailable:
            raise ValueError(unavailable)
        args = _validate_arguments(spec, arguments)
        handler = self._handlers()[tool_name]
        resource, business_preview = handler.prepare(self, principal, args)

        operation_id = str(uuid.uuid4())
        timestamp = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO business_tool_operations "
                "(operation_id, principal_id, tool_name, arguments_json, resource_json, "
                "fingerprint, status, preview_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id, str(principal), tool_name, _json(args),
                    _json(asdict(resource)), self._fingerprint(tool_name, args, resource),
                    "prepared", _json(business_preview), timestamp, timestamp,
                ),
            )
        return {
            "operation_id": operation_id,
            "status": "prepared",
            "preview": {
                **business_preview,
                "resource_id": resource.id,
                "project_id": resource.project_id,
                "version": resource.version,
            },
        }

    def _replay_existing(self, principal: int, prepared: sqlite3.Row,
                         existing: sqlite3.Row, spec) -> dict:
        if (existing["tool_name"] != prepared["tool_name"] or
                existing["arguments_json"] != prepared["arguments_json"]):
            raise ToolConflict("request_key is already bound to different input")
        previous_snapshot = ResourceSnapshot(**json.loads(existing["resource_json"]))
        self._authorize_snapshot(principal, spec, previous_snapshot)
        return self._operation(existing)

    def _recheck_scope(self, principal: int, tool_name: str, spec, arguments: dict,
                       preview: dict) -> ResourceSnapshot:
        handler = self._handlers()[tool_name]
        recheck = getattr(handler, "recheck", None)
        if callable(recheck):
            return recheck(self, principal, arguments, preview)
        return self._resource(principal, arguments[spec.id_field], spec)

    def execute_tool(self, principal: int, prepared_id: str, request_key: str,
                     *, entry: str = ENTRY_AGENT) -> dict:
        if not isinstance(request_key, str) or not 0 < len(request_key) <= 128:
            raise ValueError("request_key must contain 1 to 128 characters")
        with self._connect() as conn:
            prepared = conn.execute(
                "SELECT * FROM business_tool_operations WHERE operation_id=?", (prepared_id,)
            ).fetchone()
        if prepared is None or prepared["principal_id"] != str(principal):
            raise PermissionError("Operation is not accessible")
        spec = self._spec(prepared["tool_name"])
        self._require_entry(prepared["tool_name"], spec, entry)
        arguments = json.loads(prepared["arguments_json"])
        preview = json.loads(prepared["preview_json"] or "{}")
        prior_resource = ResourceSnapshot(**json.loads(prepared["resource_json"]))
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM business_tool_operations WHERE principal_id=? AND request_key=?",
                (str(principal), request_key),
            ).fetchone()
        if existing:
            return self._replay_existing(principal, prepared, existing, spec)
        resource = self._recheck_scope(
            principal, prepared["tool_name"], spec, arguments, preview,
        )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM business_tool_operations WHERE principal_id=? AND request_key=?",
                (str(principal), request_key),
            ).fetchone()
            if existing:
                return self._replay_existing(principal, prepared, existing, spec)
            current_preparation = conn.execute(
                "SELECT status FROM business_tool_operations WHERE operation_id=?",
                (prepared_id,),
            ).fetchone()
            if current_preparation is None or current_preparation["status"] != "prepared":
                raise ToolConflict("Only an unclaimed preparation can be executed")
            if resource != prior_resource:
                raise ToolConflict("Resource changed since preparation; prepare again")
            operation_id = str(uuid.uuid4())
            timestamp = _now()
            conn.execute(
                "UPDATE business_tool_operations SET status='running', updated_at=? "
                "WHERE operation_id=? AND status='prepared'",
                (timestamp, prepared_id),
            )
            conn.execute(
                "INSERT INTO business_tool_operations "
                "(operation_id, principal_id, tool_name, arguments_json, resource_json, "
                "fingerprint, request_key, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id, str(principal), prepared["tool_name"],
                    prepared["arguments_json"], prepared["resource_json"],
                    prepared["fingerprint"], request_key, "running", timestamp, timestamp,
                ),
            )

        handler = self._handlers()[prepared["tool_name"]]
        try:
            outputs, refs = handler.execute(self, principal, arguments, preview, resource)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE business_tool_operations SET status='succeeded', outputs_json=?, "
                    "business_refs_json=?, updated_at=? WHERE operation_id IN (?, ?) "
                    "AND status='running'",
                    (_json(outputs), _json(refs), _now(), operation_id, prepared_id),
                )
        except Exception as exc:
            status, error_detail, item_results = self._classify_failure(
                exc, spec, prepared["tool_name"],
            )
            with self._connect() as conn:
                conn.execute(
                    "UPDATE business_tool_operations SET status=?, error_json=?, "
                    "item_results_json=?, updated_at=? WHERE operation_id IN (?, ?) "
                    "AND status='running'",
                    (status, _json(error_detail), _json(item_results), _now(),
                     operation_id, prepared_id),
                )
            raise
        self._notify_change(principal, prepared["tool_name"], outputs, refs)
        return self.get_operation(principal, operation_id)

    @staticmethod
    def _classify_failure(exc: Exception, spec, tool_name: str):
        """Business refusals are ``failed``; a broken write may be ``unknown``."""
        if isinstance(exc, ToolConflict):
            return "failed", {"code": "PREVIEW_STALE", "message": "Resource changed; prepare again"}, []
        if isinstance(exc, DatasetImportError):
            return "failed", {"code": exc.code, "message": str(exc)}, [
                issue.to_dict() for issue in getattr(exc, "issues", [])
            ]
        code = getattr(exc, "code", None)
        if isinstance(exc, (ValueError, PermissionError, FileNotFoundError)):
            return "failed", {
                "code": code or "BUSINESS_RULE_REFUSED",
                "message": str(exc),
            }, list(getattr(exc, "issues", []) or [])
        if spec.descriptor.get("effect") == "write":
            return "unknown", {
                "code": "EXECUTION_UNCONFIRMED",
                "message": "Check the business resource state before retrying",
            }, []
        return "failed", {"code": "BUSINESS_READ_FAILED", "message": str(exc)}, []

    def _notify_change(self, principal: int, tool_name: str, outputs: dict,
                       refs: list[dict]) -> None:
        if self.notify is None or not refs:
            return
        event = {
            "principal_id": principal,
            "tool": tool_name,
            "business_refs": refs,
            "summary": {
                key: outputs[key]
                for key in ("status", "name", "sample_count", "total_samples",
                            "publish_status", "human_review_status", "workflow_status")
                if key in outputs
            },
        }
        try:
            self.notify(principal, event)
        except Exception:  # the workspace owns retrying its own notifications
            LOGGER.warning("business_change_notification_failed tool=%s", tool_name)

    # -------------------------------------------------------------- lifecycle

    def get_operation(self, principal: int, operation_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM business_tool_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None or row["principal_id"] != str(principal):
            raise PermissionError("Operation is not accessible")
        spec = self._spec(row["tool_name"])
        previous_snapshot = ResourceSnapshot(**json.loads(row["resource_json"]))
        self._authorize_snapshot(principal, spec, previous_snapshot)
        return self._operation(row)

    def get_operation_by_request_key(self, principal: int, request_key: str) -> dict:
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("request_key is required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT operation_id FROM business_tool_operations "
                "WHERE principal_id=? AND request_key=?",
                (str(principal), request_key),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Operation is not registered")
        return self.get_operation(principal, row["operation_id"])

    _RESOURCE_READERS = {
        "dataset": ("dataset.get_state", "_read_dataset"),
        "model": ("model.get_state", "_model_row"),
        "record": ("detection.get_state", "_read_record"),
        "draft": ("draft.get_state", "_read_draft"),
    }

    def get_resource_state(self, principal: int, resource_type: str, resource_id: str) -> dict:
        if resource_type not in self._RESOURCE_READERS:
            raise ValueError("Unsupported resource type")
        tool_name, reader = self._RESOURCE_READERS[resource_type]
        spec = TOOL_SPECS[tool_name]
        resource = self._resource(principal, resource_id, spec)
        state = getattr(self, reader)(resource_id)
        return {"resource": asdict(resource), "state": state}

    def cancel_operation(self, principal: int, operation_id: str) -> dict:
        operation = self.get_operation(principal, operation_id)
        if operation["status"] != "prepared":
            raise ToolConflict("Running and completed operations cannot be cancelled here")
        with self._connect() as conn:
            conn.execute(
                "UPDATE business_tool_operations SET status='cancelled', updated_at=? "
                "WHERE operation_id=? AND status='prepared'",
                (_now(), operation_id),
            )
        return self.get_operation(principal, operation_id)

    def reconcile_unfinished(self) -> int:
        """A restart never claims unfinished external effects succeeded or retries them."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE business_tool_operations SET status='unknown', updated_at=? "
                "WHERE status='running'",
                (_now(),),
            )
            return cursor.rowcount


def _default_detector_loader():
    from services.yolo_runtime import load_detector

    return load_detector
