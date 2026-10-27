# C-11 旧功能兼容清单与对接检查点

本文件回答三件事：
1. 旧页面（C-01 之前就存在的功能）在本轮改造后是否还照旧可用；
2. 视频 / 实时检测为什么"只做权限适配、不接新流程"；
3. 第二人写任务手册时可以直接引用的真实检查点，以及每个工具的样例、错误与恢复边界。

---

## 1. 旧接口清单（全部保留，签名不变）

蓝图注册见 `app.py`，`/api/health`、`/api/file`、`/api/user`、`/api/dataset`、`/api/model`、`/api/detection`、`/api/video-detection`、`/api/realtime-detection` 全部照旧注册。

### 1.1 图像检测与复核（旧页面主流程）

```
POST   /api/detection/detect
POST   /api/detection/save
GET    /api/detection/records
GET    /api/detection/records/<record_id>
PUT    /api/detection/records/<record_id>
DELETE /api/detection/records/<record_id>
POST   /api/detection/records/<record_id>/ai-review
POST   /api/detection/records/ai-review/batch
GET    /api/detection/llm/health
POST   /api/detection/records/<record_id>/human-review
POST   /api/detection/records/<record_id>/feedback
POST   /api/detection/records/feedback/batch
POST   /api/detection/enhance-image
POST   /api/detection/analyze-defects
POST   /api/detection/generate-suggestions
```

改动只有一处：AI 复核（单条与批量）改为调用第二人的**个人模型连接**，不再由页面自己读全局密钥。
请求体、响应结构、状态字段未变。旧页面上的 AI 按钮因此和 Agent 走同一条连接、同一份额度。

### 1.2 数据集与数据集构建

```
POST   /api/dataset/upload                 （响应仅新增 class_names/split_counts/samples/warnings）
GET    /api/dataset/list
GET    /api/dataset/<dataset_id>
PUT    /api/dataset/<dataset_id>
DELETE /api/dataset/<dataset_id>
GET    /api/dataset/<dataset_id>/download
GET    /api/dataset/models/published
GET    /api/dataset/drafts/<draft_id>/samples
POST   /api/dataset/drafts/<draft_id>/samples/<sample_id>/assign
POST   /api/dataset/drafts/<draft_id>/assign
GET    /api/dataset/drafts/<draft_id>/samples/<sample_id>/preview/<kind>
POST   /api/dataset/drafts/<draft_id>/samples/<sample_id>/secondary
POST   /api/dataset/drafts/<draft_id>/finalize
POST   /api/dataset/drafts/<draft_id>/retry
POST   /api/dataset/<dataset_id>/feedback-version
```

`/upload` 的校验逻辑改为调用共享的 `dataset_import_service`，错误文案与入参保持原样。
上传后的文件状态新增 `registered` 值（表示该上传已绑定到某个业务资源）；旧代码只判断 `uploaded` 的地方需要容忍这个新值——本仓库内部已全部用白名单 `{"uploaded", "registered"}` 判断。

### 1.3 模型

```
POST   /api/model/create
POST   /api/model/import
GET    /api/model/list
GET    /api/model/<model_id>
PUT    /api/model/<model_id>
DELETE /api/model/<model_id>
POST   /api/model/<model_id>/publish
POST   /api/model/<parent_model_id>/incremental
GET    /api/model/<parent_model_id>/compare/<child_model_id>
POST   /api/model/<model_id>/unpublish
GET    /api/model/base-models
GET    /api/model/classes
```

改动：

- `/create` 与 `/<parent>/incremental` 在启动训练进程前先占 `training_lock`。忙时返回 **HTTP 409** + `{"code": 409, "msg": "本机正在训练其他模型，请等待该训练结束后再发起"}`，且不留下半条模型记录。这是新行为：以前两个训练可以同时启动。
- 训练实现抽到 `services/training_runner.run_training_job`，页面路由只负责建行 + 起进程。落库字段、状态值、失败文案不变。
- `/classes` 仍返回固定类别配置，供旧页面的下拉筛选使用；检测与复核的**真实类别**已改为读数据集映射，两者不冲突。

### 1.4 文件与用户

```
POST   /api/file/upload/<bucket>
GET    /api/file/<bucket>/<object_key>
DELETE /api/file/<bucket>/<object_key>
POST   /api/file/project/<project_id>/upload
GET    /api/file/metadata/<file_id>
```

新增的是项目上传（`/api/file/project/<project_id>/upload`）与受保护预览下载（C-02），旧 bucket 接口不变。
已被业务资源绑定的上传不再允许经 HTTP 删除入口删除。

---

## 2. 视频 / 实时检测：只做权限适配

`/api/video-detection/*` 与 `/api/realtime-detection/*` 的全部路由保持原实现与原有资源权限：

```
POST   /api/video-detection/detect
GET    /api/video-detection/progress/<record_id>
GET    /api/video-detection/records
GET    /api/video-detection/records/<record_id>
PUT    /api/video-detection/records/<record_id>
DELETE /api/video-detection/records/<record_id>

POST   /api/realtime-detection/detect
GET    /api/realtime-detection/records
GET    /api/realtime-detection/sessions
DELETE /api/realtime-detection/records/<record_id>
DELETE /api/realtime-detection/sessions/<session_id>
POST   /api/realtime-detection/reset-tracker
```

不接 Agent 流程的理由（写进任务手册时可直接引用）：

- 视频/实时是**持续占用设备与算力**的长连接，和"单机单训练"的占用模型冲突，塞进工具层会造出看不见的资源争抢；
- 其输出是帧序列会话，不是"一条可复核的检测记录"，强行套 `record` 资源会把复核语义搞乱；
- 任务书 C-11 只要求"保持旧功能与相同资源权限"，没有要求新增 Agent 入口。

因此：模型选择、权限校验、记录可见范围全部沿用旧逻辑；Agent 侧没有对应的 `video.*` / `realtime.*` 工具，第二人也不应假设存在。

---

## 3. 给第二人的任务手册检查点

以下每一行都对应一条**已经在测试里跑通的断言**，写手册时可当作验收点。

### 3.1 结构

- `list_tools(principal)` 只返回该用户动作裁决通过的工具；未注入 `file_assets`/`storage_client` 时 `dataset.import` 与 `model.import_weight` 不出现。
- 未注入 `model_client_factory` 时 `review.ai` 不出现（不会因为"连上了"就假装能调 AI）。
- 每个描述符都带 `effect`/`billable`/`supports_preview`/`supports_status_query`/`supports_cancel`/`requires`/`resource_kind`。
- `supports_cancel` 为 `false` 的工具：`review.ai`、`training.start`、`mcp.execute`。

### 3.2 配对

- 多模态模型 + 只给一张图 → `prepare` 返回 `blocked_reasons[].code == "MODALITY_PAIR_REQUIRED"`，`execute` 抛 `ToolRefused`。
- RGB 与 X 图不在同一项目 → `SECONDARY_PROJECT_MISMATCH`。
- 检测/复核时缺另一半图 → 直接失败，不降级成单模态。

### 3.3 合并（反馈 → 训练集）

- 只有 `human_review_status` 为 `confirmed`/`no_defect` 的记录能进 `feedback.add_confirmed`，否则 `NOT_HUMAN_CONFIRMED`。
- `draft.create_from_feedback` 默认**只**含已确认反馈；没有已确认样本时 `NO_CONFIRMED_SAMPLES`，不会静默拉原训练集凑数。
- 原训练集与反馈的合并只在用户明确要求时发生（`dataset.transform` / `FEEDBACK_MERGE` 提示项）。

### 3.4 反馈训练

- `training.start` 预览里的 `new_model.version == 父模型 version + 1`，执行后新旧两行都在，父权重不动。
- 训练占用被占时 `TRAINING_BUSY`，且**没有排队**：要么立刻拒绝，要么真正开始。
- 页面 `/api/model/create` 与 `training.start` 争同一把锁，谁先谁得，不会出现两个训练并行。
- 训练状态只从 `models` 行读（`training.get_state`）；重启后不自动重投训练。

### 3.5 引文

- PDF 的 `citation` 是真实页号（`第 2 页` 对应文件第 2 页），不是段落序号。
- DOCX 的 `citation` 是 `第 N 段` / `表格 N`，**没有页码**（Word 无固定分页，不编造）。
- 引文与原文读取都过 `document:read` 裁决；越权直接拒绝。

---

## 4. 每个工具的样例、错误与恢复边界

### 4.1 样例（可直接粘贴进手册）

**检测一条记录**

```json
prepare  detection.run  {"asset_id": "a1", "model_id": "m1", "confidence": 0.25}
preview  {"blocked": false, "record": {"model_id": "m1", "modality": "single"}, ...}
execute  request_key = "detect-2026-10-26-001"
result   {"status": "succeeded", "business_refs": [{"type": "record", "id": "r9"}]}
```

**AI 复核两条（含重复）**

```json
prepare  review.ai  {"record_ids": ["r1", "r1", "r2"]}
preview  {"billable": true, "requested": 2, "records": ["r1", "r2"]}
execute  request_key = "ai-review-001"
result   {"status": "succeeded",
          "item_results": [{"record_id": "r1", "status": "success"},
                           {"record_id": "r2", "status": "failed", "error": "AI服务调用失败，需要人工复核"}]}
```

**人工复核后再回流**

```json
prepare  review.submit_human  {"record_id": "r1", "expected_version": 3, "decision": "confirmed", "annotations": [...]}
execute  request_key = "human-001"     → version 变 4
prepare  feedback.add_confirmed  {"record_id": "r1", "dataset_id": "d1"}
execute  request_key = "feedback-001"  → feedback_samples 新增 1 行
```

**训练**

```json
prepare  training.start  {"parent_model_id": "m1", "dataset_id": "d-feedback", "epochs": 50}
preview  {"new_model": {"version": 4}, "modality": "single", "epochs": 50, "batch_size": 16}
execute  request_key = "train-001"
state    training.get_state → {"state": "training"}
```

**目录巡检**

```json
prepare  directory.inspect  {"directory_id": "dir1"}
preview  {"samples": ..., "class_names": [...], "splits": {...},
          "issues": [{"code": "SYMLINK_SKIPPED", "path": "..."}],
          "duplicates": [...], "leakage": [...]}
```

**文档检索**

```json
prepare  document.search  {"document_id": "doc1", "query": "缺陷宽度"}
result   {"hits": [{"block": 4, "block_type": "text", "page_no": 2,
                    "excerpt": "...", "citation": "第 2 页"}]}
```

**MCP**

```json
prepare  mcp.save_connection  {"name": "本地工具", "transport": "stdio", "command": "...", "credential_ref": "cred-1"}
prepare  mcp.test_connection  {"connection_id": "c1"}   → 未配传输时报 "本机服务需由用户自行准备"
prepare  mcp.discover_tools   {"connection_id": "c1"}   → 记录 tools_version、writes、billable、cancellable
prepare  mcp.execute          {"connection_id": "c1", "tool_name": "query_db", "arguments": {...}}
```

### 4.2 已知错误与恢复边界

| 场景 | 现象 | 允许的恢复动作 |
| --- | --- | --- |
| 资源版本在 prepare 后变化 | `failed` + `PREVIEW_STALE` | 重新 `prepare_tool`，不要改键重发 |
| 写操作中途异常 | `unknown` + `EXECUTION_UNCONFIRMED` | 先 `get_resource_state` 查真实状态，再决定；不得盲重试 |
| 付费复核失败 | `item_results` 中该项 `failed` | 该项**不**计为有效复核；重试要重新准备（会产生新费用） |
| 训练被占 | `TRAINING_BUSY`（preview 阻断 / execute 拒绝） | 等当前训练结束；系统不排队 |
| 训练进程被杀 | 模型行停在 `training`，操作 `unknown` | 人工核实后手工改状态；系统不自动重投 |
| 外部 MCP 写工具失败 | `unknown`，只调用一次 | 先查外部系统实际结果；系统不自动重发 |
| 目录越界 / 软链 | `PATH_OUTSIDE_ROOT` / `SYMLINK_SKIPPED` | 修正目录登记，不放开根限制 |
| 旧 `.doc` | `UNSUPPORTED_DOCUMENT`，提示另存为 `.docx` | 用户转换后重解析 |
| 类别无真实映射 | `CLASS_MAPPING_UNKNOWN` 阻断 | 先补数据集 `structure_info.names`，不回落到固定类别表 |

### 4.3 日志

- 操作账本表 `business_tool_operations` 记录每次 `prepare`/`execute`：工具名、参数、资源快照、指纹、`request_key`、状态、业务引用、输出、逐项结果、错误。
- 通知失败只写 `LOGGER.warning("business_change_notification_failed tool=%s")`，不影响业务提交。
- 登记类动作另写 `model_publish_audit`、`dataset_candidates`、`mcp_connections`/`mcp_tools`、`documents`/`document_blocks`。
- 账本里**不写**密钥、凭据明文、文件内容；MCP 只存 `credential_ref`。

---

## 5. 旧页面回归方式

```bash
cd CXSDet-YOLOmm/CXSDet/web-flask
python -m pytest -q --basetemp=.pytest-tmp -p no:cacheprovider
```

当前结果：**247 passed / 4 failed**。4 项失败与本次改造无关，是基线自带：

- `tests/test_batch_review_service.py` 3 项：测试夹具的 `datasets` 表没有 `structure_info` 列，而 `feedback_dataset_service.add_confirmed_record` 会读它，报 `no such column: structure_info`；测试与实现的基线不一致。
- `tests/test_llm_client_contract.py` 1 项：断言"API key 已存在"，本环境未配置 key。

这 4 项在 C-01 第 1 次记录中就已存在，单独运行同样复现。
