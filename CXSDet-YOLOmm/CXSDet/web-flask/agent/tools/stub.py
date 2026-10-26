"""业务工具替身（开发期使用，绝不在真实运行中静默替代业务）。

任务书要求第二人在第三人工具未就绪时也能独立演示全部机制，因此提供一组
行为确定的替身工具；所有结果都带 ``is_stub=True``，前端应明确标注"替身"。
``AGENT_ALLOW_STUB=0`` 时替身不会出现在工具清单里。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code
from agent.store import connect, query_all, query_one
from agent.tools.contract import (
    RISK_CRITICAL,
    RISK_READ,
    RISK_WRITE,
    SOURCE_STUB,
    OperationResult,
    ToolSpec,
)

_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="query_project_overview",
        title="查看项目概况",
        description="查询当前项目的名称、说明、资源数量与待处理事项。只读。",
        parameters={
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
        source=SOURCE_STUB,
    ),
    ToolSpec(
        name="list_project_resources",
        title="列出项目资源",
        description="列出项目内的数据集、模型、检测记录等资源及其版本。只读。",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "kinds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
        source=SOURCE_STUB,
    ),
    ToolSpec(
        name="inspect_dataset",
        title="检查数据集结构",
        description="检查数据集的类别、模态、划分和标注概况。只读，不修改文件。",
        parameters={
            "type": "object",
            "properties": {"dataset_id": {"type": "string"}},
            "required": ["dataset_id"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
        source=SOURCE_STUB,
        supports_preview=True,
    ),
    ToolSpec(
        name="check_rgb_pairs",
        title="检查多模态配对",
        description="检查 RGB 与配套图片的配对情况，返回缺失配对清单。只读。",
        parameters={
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "secondary_suffix": {"type": "string"},
            },
            "required": ["dataset_id"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
        source=SOURCE_STUB,
        supports_preview=True,
    ),
    ToolSpec(
        name="import_dataset",
        title="导入数据集",
        description="把校验通过的候选版本登记为正式数据集。写入型操作。",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "dataset_name": {"type": "string"},
                "candidate_ref": {"type": "string"},
            },
            "required": ["project_id", "dataset_name"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
        source=SOURCE_STUB,
        supports_preview=True,
    ),
    ToolSpec(
        name="run_detection",
        title="图片检测",
        description="用已发布模型对图片执行检测并生成检测记录。计费型写入操作。",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "model_id": {"type": "string"},
                "image_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_id", "model_id", "image_refs"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
        billable=True,
        source=SOURCE_STUB,
        supports_status=True,
    ),
    ToolSpec(
        name="ai_review_records",
        title="AI 辅助核查",
        description="对检测记录做第二意见核查。计费型写入操作，逐条返回结果。",
        parameters={
            "type": "object",
            "properties": {
                "record_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["record_ids"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
        billable=True,
        source=SOURCE_STUB,
        supports_status=True,
    ),
    ToolSpec(
        name="start_training",
        title="启动训练",
        description="用指定数据集启动初始或增量训练。关键计费操作，不自动排队。",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "dataset_id": {"type": "string"},
                "parent_model_id": {"type": "string"},
                "model_name": {"type": "string"},
                "epochs": {"type": "integer"},
            },
            "required": ["project_id", "dataset_id", "model_name"],
            "additionalProperties": False,
        },
        risk=RISK_CRITICAL,
        billable=True,
        source=SOURCE_STUB,
        supports_status=True,
        supports_cancel=False,
    ),
    ToolSpec(
        name="get_training_status",
        title="查询训练状态",
        description="按业务任务 ID 查询训练真实状态与指标。只读。",
        parameters={
            "type": "object",
            "properties": {"business_task_id": {"type": "string"}},
            "required": ["business_task_id"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
        source=SOURCE_STUB,
        supports_status=True,
    ),
    ToolSpec(
        name="publish_model",
        title="发布模型",
        description="把训练完成的模型发布为本人可用模型，会影响本人项目内当前生效模型。",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "model_id": {"type": "string"},
            },
            "required": ["project_id", "model_id"],
            "additionalProperties": False,
        },
        risk=RISK_CRITICAL,
        source=SOURCE_STUB,
        supports_preview=True,
    ),
]

_OPERATIONS: Dict[str, Dict[str, Any]] = {}


class StubToolProvider:
    """确定性替身：同一 request_key 返回同一结果，便于验证幂等与恢复。"""

    def list_tools(self, actor) -> List[ToolSpec]:
        return list(_SPECS)

    def prepare(self, actor, spec: ToolSpec, arguments: Dict[str, Any], context) -> Dict[str, Any]:
        return {
            "preview": {
                "tool": spec.name,
                "arguments": arguments,
                "is_stub": True,
                "note": "替身预览：真实业务接入后由第三人提供实际差异与影响范围",
            }
        }

    def execute(
        self,
        actor,
        spec: ToolSpec,
        arguments: Dict[str, Any],
        *,
        request_key: str,
        context,
    ) -> OperationResult:
        if request_key in _OPERATIONS:
            cached = _OPERATIONS[request_key]
            return OperationResult(**{**cached, "is_stub": True})

        result = self._simulate(actor, spec, arguments)
        _OPERATIONS[request_key] = result.to_dict()
        return result

    def get_operation(self, actor, operation_id: str) -> OperationResult:
        for payload in _OPERATIONS.values():
            if payload.get("operation_id") == operation_id:
                data = dict(payload)
                data["is_stub"] = True
                return OperationResult(**data)
        raise AgentError(Code.NOT_FOUND, "替身操作不存在", http_status=404)

    def get_resource_state(self, actor, ref: Dict[str, Any]) -> Dict[str, Any]:
        kind = (ref or {}).get("kind")
        ref_id = (ref or {}).get("id")
        if kind == "detection_record":
            conn = connect()
            try:
                row = query_one(
                    conn,
                    "SELECT id, human_review_status, ai_review_status FROM detection_records "
                    "WHERE id = ?",
                    (ref_id,),
                )
            finally:
                conn.close()
            if not row:
                return {"status": "unknown", "reason": "记录不存在", "is_stub": True}
            return {
                "status": "ok",
                "human_review_status": row.get("human_review_status"),
                "ai_review_status": row.get("ai_review_status"),
                "revision": 0,
                "is_stub": True,
            }
        if kind == "training":
            payload = _OPERATIONS.get(str(ref_id))
            if payload:
                return {**payload, "status": payload.get("status", "succeeded"), "is_stub": True}
            return {"status": "unknown", "reason": "未找到训练任务", "is_stub": True}
        return {"status": "unknown", "reason": f"替身不支持查询 {kind}", "is_stub": True}

    def cancel_operation(self, actor, operation_id: str) -> OperationResult:
        return OperationResult(
            status="failed",
            operation_id=operation_id,
            error={
                "code": "AGENT_CANCEL_UNSUPPORTED",
                "message": "替身与真实业务均不支持撤销已启动的训练，请如实告知用户",
            },
            is_stub=True,
            execution_state="executed",
        )

    # -- 内部模拟 -----------------------------------------------------------
    def _simulate(
        self, actor, spec: ToolSpec, arguments: Dict[str, Any]
    ) -> OperationResult:
        name = spec.name
        if name == "query_project_overview":
            project_id = arguments["project_id"]
            conn = connect()
            try:
                project = query_one(
                    conn, "SELECT name, description FROM agent_project WHERE id = ?", (project_id,)
                )
                counts = query_all(
                    conn,
                    "SELECT kind, COUNT(*) AS total FROM agent_resource "
                    "WHERE project_id = ? GROUP BY kind",
                    (project_id,),
                )
            finally:
                conn.close()
            return OperationResult(
                status="succeeded",
                operation_id=f"stub_op_{project_id[-6:]}",
                outputs={
                    "project": project or {},
                    "resource_counts": {row["kind"]: int(row["total"]) for row in counts},
                    "is_stub": True,
                },
            )

        if name == "list_project_resources":
            project_id = arguments["project_id"]
            kinds = arguments.get("kinds") or []
            sql = "SELECT id, kind, display_name, revision FROM agent_resource WHERE project_id = ?"
            params: List[Any] = [project_id]
            if kinds:
                sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                params.extend(kinds)
            conn = connect()
            try:
                rows = query_all(conn, sql, params)
            finally:
                conn.close()
            return OperationResult(
                status="succeeded",
                outputs={"resources": rows, "count": len(rows), "is_stub": True},
            )

        if name == "inspect_dataset":
            dataset_id = arguments["dataset_id"]
            return OperationResult(
                status="succeeded",
                business_refs=[{"kind": "dataset", "id": dataset_id}],
                outputs={
                    "dataset_id": dataset_id,
                    "classes": ["bird_shit", "crack", "dirt", "panel"],
                    "modality": "multimodal",
                    "splits": {"train": 2620, "valid": 328, "test": 328},
                    "is_stub": True,
                    "note": "替身数据，用于验证流程而非真实统计",
                },
            )

        if name == "check_rgb_pairs":
            dataset_id = arguments["dataset_id"]
            missing = ["IR_0007.jpg", "IR_0031.jpg"]
            return OperationResult(
                status="partial_failed",
                business_refs=[{"kind": "dataset", "id": dataset_id}],
                outputs={"missing_secondary": missing, "is_stub": True},
                item_results=[
                    {"item": "pairing", "status": "failed", "detail": "缺 2 张配套图"},
                ],
            )

        if name == "import_dataset":
            dataset_id = f"ds_{arguments['dataset_name']}"
            return OperationResult(
                status="succeeded",
                business_refs=[{"kind": "dataset", "id": dataset_id}],
                outputs={
                    "dataset_id": dataset_id,
                    "project_id": arguments["project_id"],
                    "is_stub": True,
                },
            )

        if name == "run_detection":
            refs = arguments.get("image_refs") or []
            record_ids = [f"rec_stub_{index + 1}" for index in range(len(refs))]
            return OperationResult(
                status="succeeded",
                business_refs=[
                    {"kind": "detection_record", "id": record_id} for record_id in record_ids
                ],
                outputs={"record_ids": record_ids, "is_stub": True},
                item_results=[
                    {"item": ref, "status": "succeeded"} for ref in refs
                ],
            )

        if name == "ai_review_records":
            record_ids = arguments.get("record_ids") or []
            return OperationResult(
                status="succeeded",
                business_refs=[
                    {"kind": "detection_record", "id": record_id} for record_id in record_ids
                ],
                outputs={"reviewed": len(record_ids), "is_stub": True},
                item_results=[
                    {
                        "item": record_id,
                        "status": "succeeded",
                        "result": "consistent",
                    }
                    for record_id in record_ids
                ],
            )

        if name == "start_training":
            business_task_id = f"train_stub_{arguments['dataset_id'][-6:]}"
            return OperationResult(
                status="running",
                operation_id=business_task_id,
                business_refs=[{"kind": "training", "id": business_task_id}],
                outputs={
                    "business_task_id": business_task_id,
                    "model_name": arguments["model_name"],
                    "is_stub": True,
                },
                execution_state="executed",
            )

        if name == "get_training_status":
            task_id = arguments["business_task_id"]
            payload = _OPERATIONS.get(task_id)
            status = (payload or {}).get("status", "running")
            return OperationResult(
                status=status if status != "running" else "running",
                operation_id=task_id,
                business_refs=[{"kind": "training", "id": task_id}],
                outputs={
                    "business_task_id": task_id,
                    "metrics": {"mAP50": 0.561, "precision": 0.681},
                    "is_stub": True,
                },
            )

        if name == "publish_model":
            model_id = arguments["model_id"]
            return OperationResult(
                status="succeeded",
                business_refs=[{"kind": "model", "id": model_id}],
                outputs={
                    "model_id": model_id,
                    "replaced_model_id": "default-model-001",
                    "is_stub": True,
                },
            )

        return OperationResult(
            status="failed",
            error={"code": Code.NOT_IMPLEMENTED, "message": f"替身未实现 {name}"},
            is_stub=True,
        )
