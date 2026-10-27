# 第三人业务工具接口（C-01 ~ C-11 汇总）

本文是第三人交给第二人（Agent 后端）的接口契约与装配说明。它只描述**已经实现并跑通**的能力，不描述计划。
任务书 §6 要求的六个入口、工具描述符字段、执行请求/返回结构、业务状态集合，都在这里落到具体方法上。

代码位置：`CXSDet/web-flask/services/`

| 文件 | 职责 |
| --- | --- |
| `business_tool_specs.py` | 29 个工具的声明式注册表（描述符、资源类型、所需动作、依赖能力） |
| `business_tools.py` | 适配器：参数校验、资源解析、动作裁决、操作账本、幂等、错误分类、变更通知 |
| `tool_handlers.py` | 每个工具一个 handler，`prepare` 出预览、`execute` 落地业务 |
| `tool_common.py` | `ResourceSnapshot`、`ToolRefused`、`ToolConflict` 等共享原语 |
| `training_runner.py` | 页面路由与工具层共用的单次训练实现 + 机器级训练占用 |
| `dataset_transform_service.py` | C-08 数据集/目录改造与候选版本 |
| `authorized_directories.py` | C-07 目录登记与扫描 |
| `document_service.py` | C-09 PDF/DOCX 解析与检索 |
| `mcp_registry.py` | C-10 外部 MCP 连接、工具发现与执行 |

---

## 1. 六个入口

```python
from services.business_tools import BusinessToolAdapter

adapter = BusinessToolAdapter(
    database_path,
    require_access=policy.require_access,      # 必填
    resolve_resource=policy.resolve_resource,  # 必填
    # 以下按本机实际能力注入，缺哪个就关闭对应工具
    storage_exists=file_client.exists,
    file_assets=file_gateway,          # LocalFileAssetGateway
    storage_client=file_client,
    model_client_factory=user_model_client_factory,
    detector_loader=load_detector,
    detector_predictor=None,           # 默认走 yolo_runtime
    notify=workspace_notify,
    work_dir=..., candidate_root=..., directory_store=..., mcp_store=...,
    training_launcher=None,            # 默认 daemon Process + run_training_job
)
```

| 入口 | 签名 | 说明 |
| --- | --- | --- |
| `list_tools` | `(principal) -> list[dict]` | 只做发现。按注入能力与动作裁决过滤，缺能力的工具不出现 |
| `prepare_tool` | `(principal, tool_name, arguments) -> dict` | 校验参数、解析真实资源、执行每个 `requires` 动作裁决、写 `prepared` 记录，返回预览 |
| `execute_tool` | `(principal, prepared_id, request_key) -> dict` | 复核权限与资源版本，认领预备记录，执行业务动作，落 `succeeded/failed/unknown` |
| `get_operation` | `(principal, operation_id) -> dict` | 只回本人的操作；查询时复核**原资源**授权 |
| `get_operation_by_request_key` | `(principal, request_key) -> dict` | 客户端丢操作 ID 后按请求键找回结果 |
| `get_resource_state` | `(principal, resource_type, resource_id) -> dict` | `dataset`/`model`/`record`/`draft` 的真实状态 |
| `cancel_operation` | `(principal, operation_id) -> dict` | 只取消 `prepared`；`running` 之后不可取消 |
| `reconcile_unfinished` | `() -> int` | 重启后把遗留 `running` 标为 `unknown`，**不重发** |

`principal` 必须是第二人服务端解析出的可信用户 ID（int）。适配器不接受请求体里的 `user_id`，也不接受模型或浏览器自报的 `approved=true`。

---

## 2. 工具描述符

`list_tools` 每项形如：

```json
{
  "name": "detection.run",
  "version": "1",
  "title": "执行检测",
  "description": "用已发布模型对一张图（多模态可带 X 图）跑检测",
  "input_schema": {"type": "object", "properties": {"...": {}}, "required": ["..."]},
  "output_schema": {"type": "object", "properties": {"...": {}}},
  "effect": "write",
  "billable": false,
  "supports_preview": true,
  "supports_status_query": true,
  "supports_cancel": "prepared_only",
  "requires": "",
  "resource_kind": "project"
}
```

字段语义：

- `effect`：`read` 或 `write`。决定失败时落 `failed` 还是 `unknown`。
- `billable`：`review.ai` 为 `true`（走用户自己的模型额度），其余为 `false`。
- `supports_cancel`：`prepared_only`（多数）或 `false`（`review.ai`、`training.start`、`mcp.execute` — 已发出的付费/外部/长任务不假装能撤回）。
- `requires`：需要额外动作才能出现的工具。`model.publish`/`model.unpublish` 需要 `model:publish-global`。
- `resource_kind`：该工具解析的资源类型，见 §4。

`supports_cancel: false` 的工具在 `list_tools` 里就写明，第二人的前端不该给它挂"取消"按钮。

---

## 3. 操作记录与状态机

`prepare_tool` 返回：

```json
{"operation_id": "uuid", "status": "prepared",
 "preview": {"blocked": false, "blocked_reasons": [], "ignored_arguments": [], "...": "工具自己的预览字段",
             "resource_id": "...", "project_id": "...", "version": "..."}}
```

`execute_tool` 返回（与 `get_operation` 同构）：

```json
{"operation_id": "uuid", "status": "succeeded",
 "business_refs": [{"type": "record", "id": "..."}],
 "outputs": {"...": "工具自己的输出"},
 "item_results": [{"...": "逐项结果"}],
 "error": null}
```

业务状态只有这八个：`prepared / running / succeeded / partial_failed / failed / cancelled / interrupted / unknown`。
当前实现会写出 `prepared`、`running`、`succeeded`、`failed`、`cancelled`、`unknown`。
`partial_failed` 与 `interrupted` 不写：批量工具在 `item_results` 里逐项给成败（`review.ai` 部分成功时整体仍记 `succeeded`，逐项失败在 `item_results`），避免"整体状态"掩盖个别失败；进程被杀属 `unknown`，不假装 `interrupted`。

关键约定：

1. **幂等**：`(principal_id, request_key)` 唯一。同键同输入返回原操作；同键不同输入抛 `ToolConflict`。
   同键重放即使资源版本随后变了也返回原结果 —— 但会重新裁决**原资源**权限。
2. **一次认领**：一个 `prepared` 只能执行一次。取消后不可执行。
3. **版本校验**：`prepare` 时记资源快照；`execute` 时重新解析，快照不等抛 `ToolConflict("Resource changed since preparation; prepare again")`，操作落 `failed` + `PREVIEW_STALE`。
4. **unknown 的含义**：写操作中途异常（数据库已提交但回执丢失、外部服务断连）落 `unknown` + `EXECUTION_UNCONFIRMED`。
   第二人拿到 `unknown` **只能先查业务状态再决定**，不能改键重发。读操作异常落 `failed` + `BUSINESS_READ_FAILED`。
5. **重启**：启动协调方在确认没有存活执行进程后调用 `reconcile_unfinished()`，它只标记不重发。
   它无法检测其他存活进程；调用时机由第二人保证。

---

## 4. `resolve_resource` 必须支持的 kind

```python
ResourceSnapshot(id: str, project_id: str, owner_id: int, version: str)
```

| kind | 用于 | version 应该是什么 |
| --- | --- | --- |
| `dataset` | 数据集读取/改造/草稿 | 数据集行版本（改了样本或结构要变） |
| `model` | 模型查询/发布/比较/训练 | 模型 `version` 字段 |
| `record` | 检测记录、AI 复核、人工复核、反馈 | `review_version` |
| `draft` | 草稿拆分/定版 | 草稿 `update_time` 或自增版本 |
| `project` | 项目范围内的上传资产（导入数据集、导入权重、跑检测、解析文档） | 项目版本（项目内文件集合变化要变） |
| `workspace` | MCP 连接登记（`mcp.save_connection`） | 用户工作区版本，`project_id` 可为空串 |
| `directory` | 授权目录 | 目录登记记录的 `updated_at` |
| `document` | 文档检索 | 文档解析状态/版本 |
| `mcp_connection` | MCP 连接测试/发现/执行 | 连接的 `updated_at` |

`owner_id` 必须是真实所有者，`project_id` 必须真实存在。**不能凭请求输入自报**。
工具层用 `resource.project_id` 做跨项目阻断（例如 `MODEL_PROJECT_MISMATCH`、`SECONDARY_PROJECT_MISMATCH`、`DIRECTORY_PROJECT_MISMATCH`、`DOCUMENT_PROJECT_MISMATCH`）。

`require_access(principal, action, resource)` 必须返回**恰好 `True`** 才放行；返回 `None`、`1`、真值对象都算拒绝。资源级工具传 `ResourceSnapshot`，全局工具传 `None`。

需要裁决的动作（出现即会调用）：

```
file:read            dataset:read          dataset:create        dataset:write
model:read           model:publish         model:publish-global  model:train
record:read          record:review         record:feedback
draft:read           draft:write
directory:read       directory:write
document:read        document:create
mcp:manage           mcp:execute
```

---

## 5. 工具清单（29 个）

### 5.1 C-03 数据集与权重导入

| 工具 | 参数（必填加粗） | 预览要点 |
| --- | --- | --- |
| `dataset.get_state` | **dataset_id** | 类别、模态、样本数、split、状态 |
| `dataset.import` | **asset_id**、**name**、**dataset_mode**、description、secondary_modality、class_names | 结构校验报告、类别、模态、各 split 样本数、问题清单、`proposed_registration`、`blocked_reasons`、`ignored_arguments`、sha256 |
| `model.import_weight` | **asset_id**、**dataset_id**、**name**、description | `weight_contract`（来源、模态、类别）、阻断项 `DATASET_NOT_VERIFIED`/`CLASS_MAPPING_UNKNOWN`/`NAME_CONFLICT` |
| `model.get_state` | **model_id** | 状态、版本、父模型、指标、发布状态 |
| `model.publish` | **model_id** | 同模态域将被归档的**完整模型 ID 清单**；需要 `model:publish-global` |
| `model.unpublish` | **model_id** | 影响面：受影响模型 ID 清单 |
| `model.compare` | **parent_model_id**、**child_model_id** | `comparable`：评测条件是否一致；不一致时明确"不能直接归因" |

类别解析规则：检测/复核/改造的类别**只来自数据集真实映射**（`structure_info.names`）。没有真实映射时返回空列表并阻断（`CLASS_MAPPING_UNKNOWN`），**不会**回落到固定类别表。旧页面 `/api/model/classes` 的固定列表与本层无关。

### 5.2 C-04 检测与 AI 复核

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `detection.run` | **asset_id**、**model_id**、confidence、secondary_asset_id、title | 阻断项：`MODEL_PROJECT_MISMATCH`/`MODEL_NOT_PUBLISHED`/`MODALITY_PAIR_REQUIRED`/`SECONDARY_PROJECT_MISMATCH` |
| `detection.get_state` | **record_id** | 检测状态、人工复核状态、复核版本 |
| `review.ai` | **record_ids**（1~50） | `billable=true`、`cancel=false`；同一 ID 只调用一次 |

`review.ai` 的边界：

- 走第二人注入的 `model_client_factory`，用**该用户自己的**模型客户端，本层不持有任何密钥。
- 多模态记录必须能拿到真实 RGB + X 两张图，缺一张即失败，不做单模态降级。
- AI 输出只作为"疑似/建议"，写入的是 AI 复核字段，**不会**写成人确认结论。
- 批量逐项记成败；失败项不会被当作有效复核重试。
- 去重后再调用：`["r1","r1","r2"]` → 2 次付费调用，`requested=2`。

### 5.3 C-05 人工复核与反馈

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `review.submit_human` | **record_id**、**expected_version**、**decision**（`confirmed`/`no_defect`/`rejected`）、annotations | 版本不符抛 `RECORD_VERSION_CONFLICT`；落真实人工状态、版本、记录 |
| `feedback.add_confirmed` | **record_id**、**dataset_id** | 只有 `confirmed`/`no_defect` 才允许回流，否则 `NOT_HUMAN_CONFIRMED` |
| `draft.create_from_feedback` | **dataset_id** | 默认只含已确认反馈，不与原训练集自动混合；`NO_CONFIRMED_SAMPLES` 时阻断 |
| `draft.assign_splits` | **draft_id**、**sample_ids**、**split**（`train`/`val`/`test`/`excluded`） | 拆分泄漏会拒绝（`DRAFT_SPLIT_REFUSED`） |
| `draft.get_state` | **draft_id** | 各 split 真实计数、可定版条件 |
| `draft.finalize` | **draft_id** | 人工拆分 + 真实条件不满足时阻断（`FINALIZE_CONDITIONS_UNMET`） |

### 5.4 C-06 训练与版本

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `training.start` | **parent_model_id**、**dataset_id**、name、epochs、batch_size | 预览含 `new_model.version = 父版本+1`、模态、参数、输出与资源归属；`TRAINING_BUSY` 时明确拒绝，**不排队** |
| `training.get_state` | **model_id** | 从 `models` 行读真实状态，不重投训练 |

`training.start` 与页面 `/api/model/create`、`/api/model/<id>/incremental` 共用 `training_lock` 单行表（`services/training_runner.py`）：
本机同时只允许一次训练；占用方存活则拒绝，进程已死则回收。训练永远建新版本，绝不覆盖父权重。

### 5.5 C-07 授权目录

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `directory.inspect` | **directory_id**、max_samples | split、类别、配对、重复、泄漏；`SYMLINK_SKIPPED`/`PATH_OUTSIDE_ROOT` 等真实路径问题 |
| `directory.search` | **directory_id**、**query**、limit | 不越出授权根；返回相对路径，不回传根路径 |

"后端能读该路径"不等于"站点账号有权限"。工具先按 `directory_id` 查登记记录，再按登记时的 `real_root` 解析，任何解析结果跳出根目录即拒绝。

### 5.6 C-08 数据改造

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `dataset.transform` | **dataset_id**、**name**、description、class_map、keep_classes、class_names、split 规则 | 先出改造清单、类别映射、冲突与 diff 预览；冲突存在时阻断 |
| `directory.transform` | 同上（源为 **directory_id**） | 同上 |
| `dataset.register_candidate` | **candidate_id**、description | 候选版本经业务校验后才登记为正式训练数据 |

落地产物：新目录/新版本（默认保留源）、`data.yaml`、`manifest.json`（来源、规则、逐条映射、校验报告）、`images/`、`images_<modal>/`、`labels/`。
不猜映射：缺 `class_map` 或 `class_names` 时阻断（`CLASS_MAP_UNKNOWN`/`CLASS_NAMES_REQUIRED`）。
训练/验证/测试泄漏存在时暂停输出确认（`SPLIT_LEAKAGE`）。
只有人工确认过的标注才进标签改写，视觉建议保持待复核状态。
只有用户明确要求时才合并原数据集与反馈。

### 5.7 C-09 文档

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `document.parse` | **asset_id**、title | PDF/DOCX 首版；旧 `.doc` 明确要求先另存为 `.docx`（`UNSUPPORTED_DOCUMENT`） |
| `document.search` | **query**、**document_id**、limit | 返回 `block`/`block_type`/`page_no`/`paragraph_no`/`table_no`/`excerpt`/`citation` |

PDF 保留真实页序（`第 N 页`）；Word 用标题/段落/表格锚点（`第 N 段`、`表格 N`），**不编造页码**。
扫描页在无解析能力时标记"可能是扫描页"，并给出成本/失败/限制说明。
引文与原文读取都过个人权限裁决。
不做文档编辑/重排，不强制向量库。

### 5.8 C-10 外部 MCP

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `mcp.save_connection` | **name**、**transport**（`stdio`/`http`）、connection_id、endpoint、command、arguments、credential_ref、enabled | 凭据走第二人的独立凭据存储，只存引用 |
| `mcp.test_connection` | **connection_id** | 未配置传输时明确报"本机服务需用户自行准备" |
| `mcp.discover_tools` | **connection_id** | 记录工具版本（sha256 前缀）、读写/计费/幂等/可取消声明、变更标记 |
| `mcp.execute` | **connection_id**、**tool_name**、arguments | 只执行**已发现**的工具；`cancel=false` |

- 连接成功不等于可以调用全部工具：`mcp.execute` 必须带具体 `tool_name`，且该工具已在发现清单里，否则 `MCP_TOOL_UNKNOWN`。
- 非幂等写工具调用异常 → 操作 `unknown`，**不自动重发**（"外部工具执行结果未知，未自动重发"）。
- 外部服务能力/版本变化通过 `discover` 的 `changed` 标记通知第二人重新裁决授权。
- 本层不能让模型装服务、改启动配置或提交任意系统命令；本机服务由用户自备。

---

## 6. 稳定错误码

拒绝类（`ToolRefused` → 操作 `failed`，`error.code` 稳定）：

```
ANNOTATION_INVALID  CANDIDATE_BLOCKED  DETECTION_BLOCKED  DIRECTORY_PROJECT_MISMATCH
DOCUMENT_PROJECT_MISMATCH  DRAFT_NOT_EDITABLE  DRAFT_SPLIT_REFUSED  FEEDBACK_REFUSED
FILE_KIND_MISMATCH  FILE_NOT_READY  FINALIZE_CONDITIONS_UNMET  MCP_COMMAND_REQUIRED
MCP_ENDPOINT_REQUIRED  MCP_TOOL_UNKNOWN  MODEL_DATASET_MISMATCH  MODEL_NOT_PUBLISHED
MODEL_NOT_READY  MODEL_WEIGHT_MISSING  NOT_HUMAN_CONFIRMED  NO_CONFIRMED_SAMPLES
PROJECT_REQUIRED  RECORD_VERSION_CONFLICT  TRAINING_BLOCKED  TRAINING_BUSY
TRAINING_LAUNCH_FAILED  TRAINING_REFUSED  TRANSFORM_BLOCKED  UNSUPPORTED_DOCUMENT
WEIGHT_IMPORT_BLOCKED
```

预览阻断原因（`preview.blocked_reasons[].code`，`prepare` 阶段就返回，不消耗执行）：

```
ANNOTATIONS_DROPPED  CLASS_MAPPING_UNKNOWN  CLASS_MAP_DUPLICATE  CLASS_MAP_UNKNOWN
CLASS_NAMES_REQUIRED  CLASS_FILTER_EMPTY  CLASS_FILTER_UNKNOWN  DATASET_NOT_VERIFIED
DUPLICATES_SKIPPED  FEEDBACK_MERGE  FINALIZE_CONDITIONS_UNMET  MODALITY_MISMATCH
MODALITY_PAIR_REQUIRED  MODEL_NOT_PUBLISHED  MODEL_PROJECT_MISMATCH  MODE_MISMATCH
NAME_CONFLICT  NOT_HUMAN_CONFIRMED  NO_CONFIRMED_SAMPLES  NO_SAMPLES
PARENT_MODEL_NOT_READY  RECORD_VERSION_CONFLICT  SECONDARY_PROJECT_MISMATCH
SPLIT_LEAKAGE  SPLIT_POLICY_EMPTY  SPLIT_POLICY_INVALID  SPLIT_POLICY_SUM
SPLIT_UNKNOWN  TRAINING_BUSY  UNSUPPORTED_DOCUMENT
```

适配器级：

```
PREVIEW_STALE  资源版本在准备后变化，需要重新 prepare
EXECUTION_UNCONFIRMED  写操作结果未知，先查业务状态
BUSINESS_READ_FAILED   读操作失败
BUSINESS_RULE_REFUSED  业务规则拒绝但未带更细 code
```

第二人应把这些 code 当作契约；中文文案可改，code 不要改。

---

## 7. 本机问题清单（第一人视角，不改也要知道）

| 项 | 现状 |
| --- | --- |
| 旧页面 | 数据集、检测、复核、数据集构建、模型页面路由全部保留，全量测试覆盖 |
| 旧 AI 按钮 | 与工具层共用第二人的个人模型连接（`model_client_factory`），不再各自持有密钥 |
| 视频/实时 | 保持旧功能与旧资源权限，不接入新的 Agent 流程 |
| 上传状态 | 新增 `registered`（已绑定业务资源），预览/下载不变 |
| 固定类别下拉 | 旧 `/api/model/classes` 仍返回固定配置供旧页面筛选；检测/复核/改造的类别已走数据集真实映射 |

详细兼容清单见 `docs/business-tools-c11-compat.md`。
