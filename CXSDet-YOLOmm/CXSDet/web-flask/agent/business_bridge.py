"""INT-02：B（Agent 后端）与 C（业务工具适配层）之间的工具契约桥。

两边各自都有 ``list_tools / prepare / execute`` 这套名字，但**签名和数据结构
不同**，不能直接互相调用：

============  ==================================  ======================================
能力          B（本包）                          C（``services/business_tools.py``）
============  ==================================  ======================================
清单          ``list_tools(actor) -> [ToolSpec]``  ``list_tools(principal) -> [dict]``
预备          ``prepare(actor, spec, args, ctx)``  ``prepare_tool(principal, name, args)``
执行          ``execute(actor, spec, args, ...)``  ``execute_tool(principal, prepared_id, request_key)``
查询操作      ``get_operation(actor, op_id)``      ``get_operation(principal, op_id)``
资源状态      ``get_resource_state(actor, ref)``   ``get_resource_state(principal, type, id)``
取消          ``cancel_operation(actor, op_id)``   ``cancel_operation(principal, op_id)``
============  ==================================  ======================================

桥接规则（对应整合文档第 7 节）：

* **身份**：``ActorContext.user_id`` 转 ``principal``，服务端身份，不接受模型自报；
* **描述符**：``input_schema -> parameters``、``output_schema -> returns``、
  ``effect -> risk``（训练/发布升为 critical）、``supports_status_query ->
  supports_status``；``supports_cancel='prepared_only'`` 只保留元数据，不当作
  "运行中也能取消"；
* **命名**：模型侧用安全名 ``business_detection_run``，内部映射回 ``detection.run``，
  映射稳定、可逆、无冲突；
* **绑定**：``prepare`` 产出的 ``prepared_id`` 必须随确认卡持久化，执行时复用同一
  条预备记录，绝不重新预备一份不同预览；
* **幂等**：同一次用户动作的 ``request_key`` 不随重试变化，超时按它向业务查询，
  不改键重发；
* **ID 分离**：Agent 操作 ID（``op_*``）与业务操作 ID 分开保存，不互相冒充；
* **资源**：``business_refs[].type -> kind``，``record`` 与 ``detection_record``
  互为别名；索引 ID（``res_*``）与真实业务 ID 不混用。

这里只做"翻译"，不重写 C 的 handlers，也不放宽任何权限判定。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from agent.errors import AgentError, Code, ExecutionState
from agent.identity import ActorContext
from agent.tools.contract import (
    CANCEL_SCOPE_NONE,
    CANCEL_SCOPE_PREPARED,
    CANCEL_SCOPE_RUNNING,
    SOURCE_BUSINESS,
    OperationResult,
    ToolSpec,
)

LOGGER = logging.getLogger(__name__)

# 模型侧安全名前缀：业务工具一律 ``business_`` 开头，与控制工具、替身工具不冲突
SAFE_PREFIX = "business_"

# 训练与发布属于不可逆或影响面大的动作，单独升为 critical
CRITICAL_TOOLS = {"training.start", "model.publish", "model.unpublish"}

# 业务侧业务引用类型 -> B 的 kind（record 与 detection_record 是同一件事）
TYPE_TO_KIND = {
    "record": "detection_record",
    "detection_record": "detection_record",
}

# B 的 kind -> 业务侧可核对状态资源类型（C 只支持这四种）
KIND_TO_RESOURCE_TYPE = {
    "dataset": "dataset",
    "dataset_candidate": "dataset_candidate",
    "model": "model",
    "record": "record",
    "detection_record": "record",
    "draft": "draft",
}

# 业务侧数字状态编码 → B 可判断的语义状态。
# 来源：web-flask/utils/models.py 的 MODEL_STATUS 与 DATASET_STATUS，不改业务语义，只做翻译。
_STATUS_CODES: Dict[str, Dict[str, str]] = {
    "model": {
        "0": "training",   # 训练中：请求已受理，不代表训练完成
        "1": "completed",  # 训练完成
        "2": "failed",
        "3": "published",  # 已发布：既是完成，也是更强状态
        "4": "cancelled",  # 训练终止
    },
    "dataset": {
        "0": "pending",    # 待验证
        "1": "completed",  # 校验通过
        "2": "failed",     # 校验失败
    },
}

# 显式标题表：C 的 descriptor 没有 title 字段，不假定它存在
TOOL_TITLES = {
    "dataset.get_state": "数据集状态",
    "dataset.import": "导入数据集",
    "dataset.transform": "派生数据集",
    "dataset.register_candidate": "登记候选数据集",
    "model.import_weight": "导入模型权重",
    "model.get_state": "模型状态",
    "model.publish": "发布模型",
    "model.unpublish": "取消发布模型",
    "model.compare": "对比模型",
    "detection.run": "运行检测",
    "detection.get_state": "检测记录状态",
    "review.ai": "AI 复核",
    "review.submit_human": "提交人工复核",
    "feedback.add_confirmed": "新增已确认反馈",
    "draft.create_from_feedback": "由反馈生成草稿",
    "draft.assign_splits": "分配数据划分",
    "draft.finalize": "定稿草稿",
    "draft.get_state": "草稿状态",
    "training.start": "启动训练",
    "training.get_state": "训练状态",
    "directory.inspect": "查看授权目录",
    "directory.search": "搜索授权目录",
    "directory.transform": "授权目录转换",
    "document.parse": "解析文档",
    "document.search": "检索文档",
    "mcp.save_connection": "保存 MCP 连接",
    "mcp.test_connection": "测试 MCP 连接",
    "mcp.discover_tools": "发现 MCP 工具",
    "mcp.execute": "执行外部 MCP 工具",
}


# --------------------------------------------------------------------------- #
# 名称映射
# --------------------------------------------------------------------------- #
def safe_tool_name(business_name: str) -> str:
    """``detection.run`` -> ``business_detection_run``。"""
    return SAFE_PREFIX + str(business_name).replace(".", "_").replace("-", "_")


def business_tool_name(safe_name: str) -> Optional[str]:
    """反向查询；不在业务清单里的名字返回 None（不猜）。"""
    for name in _business_names():
        if safe_tool_name(name) == safe_name:
            return name
    return None


def _business_names() -> List[str]:
    try:
        from services.business_tool_specs import TOOL_SPECS

        return list(TOOL_SPECS.keys())
    except Exception:  # noqa: BLE001 - 业务模块缺失时由调用方报"未接入"
        return []


def _business_spec(name: str):
    """取 C 的 TOOL_SPECS 条目（含 kind/requires），模块缺失时返回 None。"""
    try:
        from services.business_tool_specs import TOOL_SPECS

        return TOOL_SPECS.get(name)
    except Exception:  # noqa: BLE001 - 业务模块不可用时不影响描述符转换
        return None


# --------------------------------------------------------------------------- #
# 描述符转换
# --------------------------------------------------------------------------- #
def _risk_for(name: str, effect: Optional[str]) -> str:
    if name in CRITICAL_TOOLS:
        return "critical"
    return "write" if str(effect or "").lower() == "write" else "read"


def _cancel_flags(raw: Any) -> Tuple[bool, str]:
    """把业务的 ``supports_cancel`` 分解成"动作级能否取消"和"取消范围"。"""
    if raw is True:
        return True, CANCEL_SCOPE_RUNNING
    if isinstance(raw, str) and raw.strip():
        scope = raw.strip()
        # prepared_only：只能撤回"预备了还没执行"的记录，不等于运行中可取消
        return scope == CANCEL_SCOPE_RUNNING, scope
    return False, CANCEL_SCOPE_NONE


def descriptor_to_spec(descriptor: Dict[str, Any]) -> Optional[ToolSpec]:
    """把 C 的一条 dict 描述符转成 B 的 ToolSpec；无法识别返回 None。"""
    if not isinstance(descriptor, dict):
        return None
    business_name = str(descriptor.get("name") or "").strip()
    if not business_name:
        return None

    spec_entry = _business_spec(business_name)
    if spec_entry is None:
        # 业务清单与本地 TOOL_SPECS 不一致：仍然转换（标题回退工具名），
        # 但留一条可核实的告警，避免新工具悄悄消失。
        LOGGER.warning("business_descriptor_unknown name=%s", business_name)

    supports_cancel, cancel_scope = _cancel_flags(descriptor.get("supports_cancel"))
    effect = descriptor.get("effect")

    return ToolSpec(
        name=safe_tool_name(business_name),
        title=TOOL_TITLES.get(business_name) or business_name,
        description=str(descriptor.get("description") or business_name),
        parameters=descriptor.get("input_schema")
        or {"type": "object", "properties": {}},
        version=str(descriptor.get("version") or "1"),
        returns=descriptor.get("output_schema") or {},
        risk=_risk_for(business_name, effect),
        billable=bool(descriptor.get("billable")),
        source=SOURCE_BUSINESS,
        supports_preview=bool(descriptor.get("supports_preview", True)),
        supports_status=bool(descriptor.get("supports_status_query", True)),
        supports_cancel=supports_cancel,
        cancel_scope=cancel_scope,
    )


def _to_business_ref(raw: Any) -> Optional[Dict[str, Any]]:
    """业务引用转 B 的引用：``type -> kind``，ID 原样保留（不造 res_*）。"""
    if not isinstance(raw, dict):
        return None
    ref_type = raw.get("type") or raw.get("kind")
    ref_id = raw.get("id") or raw.get("source_id")
    if not ref_type or not ref_id:
        return None
    kind = TYPE_TO_KIND.get(str(ref_type), str(ref_type))
    ref: Dict[str, Any] = {"kind": kind, "id": str(ref_id)}
    if raw.get("version") is not None:
        ref["version"] = raw.get("version")
    for key in ('project_id', 'display_name', 'source_file_id', 'source_dataset_id', 'parent_model_id'):
        if raw.get(key) is not None:
            ref[key] = raw[key]
    return ref


# 严重度：数字越大越"不成功"。用于外层 succeeded 与逐项 partial_failed 的取舍。
_STATUS_SEVERITY = {
    "succeeded": 0,
    "completed": 0,
    "completed_with_warnings": 1,
    "partial": 2,
    "partial_failed": 3,
    "failed": 4,
}


def _effective_status(outer: str, outputs: Dict[str, Any]) -> str:
    """外层 operation 可能整体 succeeded，但逐项结果里已经失败。

    批量核查的逐项结果在 ``outputs.items``，聚合结论在 ``outputs.status``。
    这里取"更差"的那个：一条部分失败的批量操作不能显示为全部成功。
    """
    inner = str(outputs.get("status") or "").strip().lower()
    outer_key = str(outer or "").strip().lower()
    if not inner or inner not in _STATUS_SEVERITY or outer_key not in _STATUS_SEVERITY:
        return outer
    return inner if _STATUS_SEVERITY[inner] > _STATUS_SEVERITY[outer_key] else outer


def _execution_state(status: str) -> str:
    value = str(status or "").lower()
    if value in ("succeeded", "completed", "running"):
        return ExecutionState.EXECUTED
    if value in ("unknown",):
        return ExecutionState.UNKNOWN
    if value in ("partial", "partial_failed"):
        return ExecutionState.PARTIAL
    if value in ("cancelled",):
        return ExecutionState.CANCELLED
    return ExecutionState.NOT_EXECUTED


class BusinessBackendBridge:
    """把业务适配层包装成 B 的 ``ToolProvider`` 契约。"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    # ---------------------------------------------------------------- 身份
    @staticmethod
    def principal_of(actor: Any) -> int:
        """只认服务端身份；模型自报的 user_id/actor 已在参数校验里剔除。"""
        user_id = getattr(actor, "user_id", None)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise AgentError(
                Code.FORBIDDEN,
                "无法确定当前用户身份，不能调用业务工具",
                http_status=403,
            )
        return user_id

    def _business_name(self, spec: ToolSpec) -> str:
        name = business_tool_name(spec.name)
        if name is None:
            raise AgentError(
                Code.TOOL_UNKNOWN,
                f"业务工具不存在：{spec.name}",
                http_status=400,
                details={"tool": spec.name},
            )
        return name

    def _dependency_message(self, business_name: str) -> Optional[str]:
        """C 的依赖未就绪时给出原因，避免被当成参数错误。"""
        probe = getattr(self._backend, "_dependency_error", None)
        if not callable(probe):
            return None
        try:
            return probe(business_name)
        except Exception:  # noqa: BLE001 - 探测失败不影响主流程
            return None

    # ---------------------------------------------------------------- 清单
    def list_tools(self, actor: ActorContext) -> List[ToolSpec]:
        principal = self.principal_of(actor)
        try:
            descriptors = self._backend.list_tools(principal) or []
        except PermissionError:
            # 无权查看工具清单：如实返回空，不伪造可用工具
            LOGGER.warning("business_tools_not_visible principal=%s", principal)
            return []
        specs: List[ToolSpec] = []
        for descriptor in descriptors:
            # Connections/credentials belong to fixed settings APIs. External
            # calls are registered individually with SOURCE_MCP and version grants.
            if str((descriptor or {}).get("name", "")).startswith("mcp."):
                continue
            spec = descriptor_to_spec(descriptor)
            if spec is None:
                LOGGER.warning(
                    "business_descriptor_unusable principal=%s name=%s",
                    principal,
                    (descriptor or {}).get("name") if isinstance(descriptor, dict) else "?",
                )
                continue
            specs.append(spec)
        return specs

    # ---------------------------------------------------------------- 预备
    def prepare(
        self,
        actor: ActorContext,
        spec: ToolSpec,
        arguments: Dict[str, Any],
        context: Any,
    ) -> Dict[str, Any]:
        principal = self.principal_of(actor)
        business_name = self._business_name(spec)

        unavailable = self._dependency_message(business_name)
        if unavailable:
            raise AgentError(
                Code.TOOL_UNAVAILABLE,
                unavailable,
                http_status=409,
                details={"tool": business_name},
            )

        try:
            raw = self._backend.prepare_tool(
                principal, business_name, dict(arguments or {})
            )
        except PermissionError as exc:
            raise AgentError(
                Code.TOOL_NOT_GRANTED,
                f"没有权限执行「{spec.title}」：{exc}" if str(exc) else f"没有权限执行「{spec.title}」",
                http_status=403,
                details={"tool": business_name},
            ) from exc
        except FileNotFoundError as exc:
            raise AgentError(
                Code.RESOURCE_NOT_FOUND,
                f"目标资源不存在：{exc}",
                http_status=404,
                details={"tool": business_name},
            ) from exc
        except ValueError as exc:
            raise _translate_value_error(exc, business_name, spec.title) from exc
        except Exception as exc:  # noqa: BLE001
            translated = _translate_conflict(exc, spec.title)
            if translated is not None:
                raise translated from exc
            raise

        if not isinstance(raw, dict) or not raw.get("operation_id"):
            # 不允许"没有预备记录却可以确认执行"
            raise AgentError(
                Code.PREPARE_FAILED,
                f"「{spec.title}」的预检没有返回预备记录，暂不能确认执行",
                http_status=409,
                details={"tool": business_name},
            )

        preview = raw.get("preview") or {}
        if not isinstance(preview, dict):
            preview = {"value": preview}
        if preview.get("blocked"):
            reasons = preview.get("blocked_reasons") or []
            detail = "；".join(str(item) for item in reasons if item) or "业务预检未通过"
            raise AgentError(
                Code.PREPARE_BLOCKED,
                f"「{spec.title}」现在不能执行：{detail}",
                http_status=409,
                details={"tool": business_name, "blocked_reasons": reasons},
            )

        prepared_id = str(raw["operation_id"])
        preview_payload = dict(preview)
        preview_payload.setdefault("prepared_id", prepared_id)
        preview_payload.setdefault("business_tool", business_name)
        return {
            "prepared_id": prepared_id,
            "prepared_status": raw.get("status") or "prepared",
            "preview": preview_payload,
            "business_preview": preview,
        }

    # ---------------------------------------------------------------- 执行
    def execute(
        self,
        actor: ActorContext,
        spec: ToolSpec,
        arguments: Dict[str, Any],
        *,
        request_key: str,
        context: Any,
        prepared_id: Optional[str] = None,
    ) -> OperationResult:
        principal = self.principal_of(actor)
        business_name = self._business_name(spec)

        if not prepared_id:
            # 只读工具没有确认卡，但业务侧仍然要求先预备再执行；
            # 这里在服务端补一次预备，预览不展示给用户（只读、无副作用）。
            prepared = self.prepare(actor, spec, arguments, context)
            prepared_id = str(prepared["prepared_id"])

        try:
            raw = self._backend.execute_tool(principal, prepared_id, request_key)
        except PermissionError as exc:
            raise AgentError(
                Code.TOOL_NOT_GRANTED,
                f"没有权限执行「{spec.title}」",
                http_status=403,
                details={"tool": business_name},
            ) from exc
        except FileNotFoundError as exc:
            raise AgentError(
                Code.RESOURCE_NOT_FOUND,
                f"目标资源不存在：{exc}",
                http_status=404,
                details={"tool": business_name},
            ) from exc
        except ValueError as exc:
            raise _translate_value_error(exc, business_name, spec.title) from exc
        except Exception as exc:  # noqa: BLE001
            translated = _translate_conflict(exc, spec.title)
            if translated is not None:
                raise translated from exc
            raise

        result = self._to_result(raw, spec)
        # 业务成功后再登记真实归属：失败只留痕，绝不重跑或重造业务对象。
        if result.status in ("succeeded", "completed", "partial", "running"):
            from agent import resources

            project_hint = getattr(context, "project_id", None)
            stats = resources.register_refs(
                principal,
                result.business_refs,
                project_id=project_hint,
                state_changed=str(spec.risk or "read") != "read",
            )
            if stats.get("failed"):
                LOGGER.warning(
                    "resource_registration_failed tool=%s principal=%s failed=%s",
                    business_name,
                    principal,
                    stats["failed"],
                )
        return result

    def _to_result(self, raw: Any, spec: ToolSpec) -> OperationResult:
        if not isinstance(raw, dict):
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                f"「{spec.title}」返回了无法识别的结果",
                http_status=502,
            )
        status = str(raw.get("status") or "").strip()
        if not status:
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                f"「{spec.title}」没有返回执行状态",
                http_status=502,
            )
        refs = [
            ref
            for ref in (_to_business_ref(item) for item in (raw.get("business_refs") or []))
            if ref
        ]
        error = raw.get("error")
        if isinstance(error, str):
            error = {"message": error}
        outputs = raw.get("outputs") or {}
        # 逐项失败要能穿透外层的 succeeded
        effective = _effective_status(status, outputs)
        if (business_tool_name(spec.name) == 'training.start' or raw.get('tool_name') == 'training.start') and status in ('succeeded', 'completed') and outputs.get('state') == 'training':
            effective = 'running'
        if effective in ('failed', 'partial_failed') and effective != status and error is None:
            failed = outputs.get("failed")
            error = {
                "code": "PARTIAL_ITEM_FAILURE",
                "message": f"逐项结果里有 {failed} 项未成功" if failed else "逐项结果里有未成功项",
            }
        return OperationResult(
            status=effective,
            operation_id=raw.get("operation_id"),
            business_refs=refs,
            outputs=outputs,
            item_results=raw.get("item_results") or [],
            error=error,
            is_stub=False,
            execution_state=_execution_state(effective),
        )

    # ------------------------------------------------------------ 查询/取消
    def get_operation(self, actor: ActorContext, operation_id: str) -> OperationResult:
        principal = self.principal_of(actor)
        external_id = self._external_operation_id(actor, operation_id)
        try:
            raw = self._backend.get_operation(principal, external_id)
        except PermissionError as exc:
            raise AgentError(
                Code.NOT_FOUND, "操作记录不存在", http_status=404
            ) from exc
        except FileNotFoundError as exc:
            raise AgentError(Code.NOT_FOUND, "操作记录不存在", http_status=404) from exc
        except ValueError as exc:
            raise AgentError(Code.VALIDATION, str(exc), http_status=400) from exc
        return self._to_result(raw, ToolSpec(name=operation_id, title=operation_id, description=""))

    def _external_operation_id(self, actor: ActorContext, operation_id: str) -> str:
        """Agent 操作 ID（``op_*``）换业务操作 ID；其它原样当业务 ID。"""
        if not str(operation_id).startswith("op_"):
            return str(operation_id)
        from agent import operations as operation_store

        record = operation_store.get(operation_id)
        if record is None:
            raise AgentError(Code.NOT_FOUND, "操作记录不存在", http_status=404)
        # 归属核对：有动作记录时必须属于当前用户
        action_id = record.get("action_id")
        if action_id:
            from agent import actions as action_store

            action_store.get_action(actor, action_id)
        external = record.get("operation_id")
        if not external:
            raise AgentError(
                Code.NOT_FOUND,
                "该操作还没有业务侧编号，无法向业务查询",
                http_status=404,
            )
        return str(external)

    def get_resource_state(self, actor: ActorContext, ref: Dict[str, Any]) -> Dict[str, Any]:
        principal = self.principal_of(actor)
        ref = ref or {}
        kind = ref.get("kind") or ref.get("type")
        ref_id = ref.get("id") or ref.get("source_id")
        if not kind or not ref_id:
            raise AgentError(Code.VALIDATION, "资源引用缺少类型或 ID")
        if str(ref_id).startswith("res_"):
            # 索引 ID 不是业务 ID，不能拿去查业务
            raise AgentError(
                Code.VALIDATION,
                "资源引用里是索引 ID，无法用于业务状态核对",
                details={"kind": kind, "id": ref_id},
            )
        resource_type = KIND_TO_RESOURCE_TYPE.get(str(kind))
        if resource_type is None:
            raise AgentError(
                Code.TOOL_UNAVAILABLE,
                f"业务侧无法直接核对 {kind} 类型的状态",
                http_status=409,
                details={"kind": kind},
            )
        try:
            raw = self._backend.get_resource_state(principal, resource_type, str(ref_id))
        except PermissionError as exc:
            raise AgentError(Code.RESOURCE_FORBIDDEN, "无权查看该资源状态") from exc
        except FileNotFoundError as exc:
            raise AgentError(Code.RESOURCE_NOT_FOUND, f"资源不存在：{exc}") from exc
        except ValueError as exc:
            raise AgentError(Code.VALIDATION, str(exc)) from exc
        return _flatten_state(raw, str(ref_id), kind=str(kind))

    def cancel_operation(self, actor: ActorContext, operation_id: str) -> OperationResult:
        """只有"预备了还没执行"的业务操作能撤销；在途训练不能这样取消。"""
        principal = self.principal_of(actor)
        target = str(operation_id)
        if target.startswith("act_"):
            from agent import actions as action_store

            action = action_store.get_action(actor, target)
            if action["status"] != action_store.STATUS_PENDING:
                return OperationResult(
                    status="failed",
                    operation_id=target,
                    error={
                        "code": "BUSINESS_CANCEL_NOT_ALLOWED",
                        "message": "该动作已经进入执行，业务侧无法按预备记录撤销，请核对真实状态",
                    },
                    execution_state=ExecutionState.EXECUTED,
                )
            prepared = action.get("prepared_id")
            if not prepared:
                return OperationResult(
                    status="failed",
                    operation_id=target,
                    error={
                        "code": "BUSINESS_CANCEL_NO_PREPARATION",
                        "message": "该动作没有业务预备记录，直接作废确认卡即可",
                    },
                )
            target = str(prepared)

        try:
            raw = self._backend.cancel_operation(principal, target)
        except Exception as exc:  # noqa: BLE001 - 取消失败要如实返回，不抛崩
            translated = _translate_conflict(exc, "撤销")
            message = translated.message if translated else str(exc)[:160]
            return OperationResult(
                status="failed",
                operation_id=target,
                error={"code": "BUSINESS_CANCEL_FAILED", "message": message},
            )
        return self._to_result(
            raw, ToolSpec(name=target, title="撤销", description="")
        )

    def release_prepared(self, actor: ActorContext, prepared_id: Optional[str]) -> bool:
        """确认卡被拒绝/作废后，把业务侧预备记录一并撤销，避免留下悬挂记录。"""
        if not prepared_id:
            return False
        try:
            principal = self.principal_of(actor)
            self._backend.cancel_operation(principal, str(prepared_id))
            return True
        except Exception:  # noqa: BLE001 - 清理失败不影响用户已做的决定
            LOGGER.warning("business_prepared_release_failed prepared_id=%s", prepared_id)
            return False


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _translate_value_error(
    exc: Exception, business_name: str, title: str
) -> AgentError:
    message = str(exc) or "业务参数校验未通过"
    return AgentError(
        Code.TOOL_ARGS_INVALID,
        f"「{title}」参数或前置条件不符合要求：{message}",
        http_status=400,
        details={"tool": business_name},
    )


def _translate_conflict(exc: Exception, title: str) -> Optional[AgentError]:
    """业务冲突（资源已变化/预备已被占用）转成"需要重新确认"。"""
    try:
        from services.business_tools import ToolConflict
    except Exception:  # noqa: BLE001 - 业务模块缺失时不猜测
        return None
    if isinstance(exc, ToolConflict):
        return AgentError(
            Code.ACTION_STALE,
            f"「{title}」的目标资源已变化或预备记录已失效，请重新发起并确认",
            http_status=409,
            details={"reason": str(exc)},
        )
    return None


def _flatten_state(raw: Any, ref_id: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """把业务返回的 ``{resource: {...}, state: {...}}`` 展开成 B 能判断的平铺结构。

    业务侧的 ``status`` 有两种形态：

    * 字符串（``succeeded`` / ``failed`` …），可以直接比较；
    * 数字编码（模型 ``0-4``、数据集 ``0-2``），必须先按资源类型映射成语义状态，
      否则 ``3``（已发布）永远不等于 ``succeeded``，任务会一直卡在等待业务。

    原始编码保留在 ``business_status`` 里，排查时仍能看到业务真实值。
    """
    if not isinstance(raw, dict):
        return {"status": "unknown", "reason": "业务未返回状态", "resource_id": ref_id}
    resource = raw.get("resource") if isinstance(raw.get("resource"), dict) else {}
    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    flat: Dict[str, Any] = {}
    flat.update(resource)
    flat.update(state)
    status = (
        state.get("status")
        if state.get("status") is not None
        else state.get("workflow_status")
        or state.get("publish_status")
        or resource.get("status")
    )
    code_map = _STATUS_CODES.get(str(kind or "").lower())
    normalized = "unknown" if status is None else str(status).strip().lower()
    if code_map is not None and normalized in code_map:
        # 数字编码：映射成 B 能判断的语义状态，同时保留原值
        flat["business_status"] = status
        flat["status"] = code_map[normalized]
    else:
        flat["status"] = normalized or "unknown"
    flat["resource_id"] = resource.get("id") or ref_id
    if resource.get("project_id"):
        flat["project_id"] = resource.get("project_id")
    if resource.get("version") is not None:
        flat["resource_version"] = resource.get("version")
    return flat
