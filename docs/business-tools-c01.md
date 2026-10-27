# C-01 业务工具适配接口

当前交付是仅供后端调用的 `services.business_tools.BusinessToolAdapter`。它不注册 HTTP 路由，也不从请求正文接受身份或 `approved=true`。调用方必须提供可信的 `principal`、`require_access(principal, action, resource)` 和 `resolve_resource(principal, resource_type, resource_id)`；缺少这两个回调时构造失败。`require_access` 只有明确返回 `True` 才表示通过；`False`、`None` 或异常均拒绝。第二人的正式权限服务应实现这两个回调，并为 `dataset` 返回带 `id`、`project_id`、`owner_id`、`version` 的 `ResourceSnapshot`。现有数据集表尚无项目/所有者列，因此正式权限接入前不得用允许所有资源的回调运行。

## 调用语义

```python
adapter = BusinessToolAdapter(
    database_path,
    require_access=workspace_policy.require_access,
    resolve_resource=workspace_policy.resolve_resource,
)
tools = adapter.list_tools(trusted_user_id)
prepared = adapter.prepare_tool(
    trusted_user_id, "dataset.get_state", {"dataset_id": "dataset-id"}
)
result = adapter.execute_tool(trusted_user_id, prepared["operation_id"], request_key)
same_result = adapter.get_operation(trusted_user_id, result["operation_id"])
state = adapter.get_resource_state(trusted_user_id, "dataset", "dataset-id")
```

传入可信的 `storage_exists(bucket, object_key)` 后，还可开放 `model.publish`。现有发布服务按全库同模态域归档旧模型，因此适配器额外要求明确的 `model:publish-global` 权限；仅有候选模型的 `model:publish` 权限不足。它复用 `model_registry_service.publish_model`，要求模型文件及关联数据集存在且模态匹配，预览同域将被归档的**完整模型 ID 清单**，并在同一发布事务中核对该清单；清单变化须重新预检。工具操作记录提供 `request_key` 幂等；`get_operation_by_request_key(principal, request_key)` 可在客户端丢失操作 ID 后查询结果。缺少文件存在性回调或全局发布权限时此写工具不开放。正式接入的资源解析器应返回真实模型所有者、项目和版本，不能凭请求输入自报。项目隔离发布需另行改造底层发布域，不可用此全局工具代替。

`list_tools` 返回工具版本、输入/输出结构、读写影响、计费、预览、状态和取消能力。`dataset.get_state` 是只读且不计费，实际输出来自 `datasets` 表。`prepare_tool` 只记录预览与资源版本；`execute_tool` 重新检查资源权限和版本，并把结果写入 `business_tool_operations`。相同用户的同一 `request_key` 和同一工具输入在当前及原资源权限均有效时返回原操作，即使资源版本之后发生变化；同键不同输入抛出 `ToolConflict`。查询旧操作也复核原资源授权。每个预备操作只能被认领一次，取消后不可执行。业务结果包含 `operation_id`、`status`、`business_refs`、`outputs`、`item_results`、`error`。

服务启动时可在确认没有仍在执行的旧进程后调用 `reconcile_unfinished()`，它将遗留的 `running` 标为 `unknown`，不会重发业务动作。此方法不能检测其他仍存活的执行进程，必须由启动协调方保证调用时机；若与执行交错，状态仍保守地保持 `unknown`，不会再被晚到的成功结果覆盖。`cancel_operation` 只取消尚未执行的 `prepared` 操作。数据集读取失败记录为 `failed`；模型发布开始后无法确认的失败记录为 `unknown`，即使业务事务可能已提交也不会误报未执行。权限拒绝和候选资源快照冲突发生在执行前，不生成新的执行记录；归档清单在底层发布事务中变化时记录 `PREVIEW_STALE`。调用方可用同一 `request_key` 查询原结果，必要时查询实际模型状态，不应改键盲重试。

> 更新（C-03 ~ C-11 完成后）：上面"当前没有训练、计费或外部 MCP 工具"已过期。现共 **29 个工具**，覆盖数据集导入与改造、权重导入、模型发布/下架/比较、检测、AI 复核（`billable=true`）、人工复核、反馈回流、草稿定版、训练（含机器级训练占用）、授权目录、文档解析检索、外部 MCP。完整清单、状态机、资源 kind、动作全集与错误码见 **`docs/business-tools-interface.md`**；旧页面兼容结论与逐工具样例见 **`docs/business-tools-c11-compat.md`**。

## 接入和限制

- 此模块已标准化 C-01 ~ C-11 的全部业务环节（数据集、模型、检测、复核、反馈、训练、目录、文档、MCP），每个工具都在自己的服务边界重新解析真实资源并做动作裁决。
- C-03 起工具参数按各自 `input_schema` 严格校验，注册表用 `ToolSpec` 声明资源类型与所需动作；新增项目资源范围（`kind="project"`）供“由上传创建业务资源”的工具使用，`resolve_resource` 需支持 `project`。`dataset.import` 需要同时注入 `file_assets` 与 `storage_client`，说明见 `docs/dataset-import-c03.md`。`list_tools` 只做发现，是否允许执行仍由每次 `prepare_tool` / `execute_tool` 的动作裁决决定。
- `resolve_resource` 需支持 `dataset`/`model`/`record`/`draft`/`project`/`workspace`/`directory`/`document`/`mcp_connection` 九种 kind，逐项语义见 `docs/business-tools-interface.md` §4。
- `ResourceSnapshot.version` 必须由正式资源服务给出实际版本；当前代码不能从旧数据集表推断可靠的项目归属和版本。
- 原页面的业务路由尚未全部纳入第二人的统一身份/资源权限；训练 `/create` 与 `/incremental` 已与工具层共用机器级训练占用（忙时 409）。
- 本源码副本没有 `.git` 元数据，无法记录起点提交或创建分支/PR；迁入真实仓库后再补这些信息。

