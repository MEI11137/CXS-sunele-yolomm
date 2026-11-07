# WB17-收尾批次3：Agent 逐动作覆盖（D）与单 IR 语义（E）

日期：2026-11-07　分支 `workbuddy/haixun-v1.7.0`　未提交 Git
来源：`WB17-整轮收尾执行_20261107.md` 的 D、E 两节（A—C 已在收尾A/共享缺陷收尾完成，不重复）。
交接要求：保留已有聊天/新业务 UI/共享修复；不 Git 提交、不付费 API、不真实训练、
不动 root 5887/5898 实例与独立证据；只改 v1.7。

## 0. 结论速览

| 项目 | 结果 |
| --- | --- |
| 后端全量（除真实训练 1 epoch 用例） | **539 passed + 20 subtests / 1 skipped / 0 failed**（最终一轮，见 §6） |
| 新增产品测试 | `test_business_wb17_b_agent_crud_ir.py` **16 passed** |
| 矩阵闭合 | 28 行全部指向真实工具（新增 11 个工具名入 MATRIX） |
| 前端 | typecheck 0 error / test:logic **56 passed** / build 通过 |
| 真实浏览器冒烟 | 成员端 11/11；复核页三栏量测 10/10（§5） |
| 修掉的批次2残留 | 设置页两个面板从非首项目进入仍选 `projects[0]` → 现在跟随当前项目 |

## 1. 批次2残留：设置页两面板的项目上下文

**故障**：从非首项目的识别记录页点侧栏「设置」，资料/手册面板与 MCP 面板的
「项目」下拉都停在 `props.projects[0]`，会把资料/新 MCP 配置写错项目。

**修法**（`frontend/src/stores/workspace.ts`、`layouts/AppLayout.vue`、
`components/DocumentsPanel.vue`、`components/McpPanel.vue`、`views/SettingsView.vue`）：

- workspace 新增 `currentProjectId`（localStorage `hx17.currentProject`，隐私模式 try/catch），
  AppLayout 在挂载与项目变化时 `rememberProject`；
- 两面板 `preferredProject()`：**当前项目在列表里就用它**，否则才回退第一个；
  `@change` 置 `pickedByUser`，watch 只在「未手动选择且当前为空」时补选，
  props 变化不重置草稿与已选项。

**证据**：`var/ui-smoke-3/（UI冒烟成员.json + UI-smoke-settings-1440-light.png）`——
副本库里新建第二个项目，从它的记录页进设置，两面板 `input_value() == 第二项目 id`（11/11）。

## 2. D：Agent 逐动作覆盖（工具 ↔ 服务 ↔ 页面）

原则：**复用页面同一个服务**（单点归属不动），只补「对象×动作」缺口；
只读工具存在 ≠ CRUD 全覆盖；`full` 按已授权范围直接执行，不重新引入计划卡。

### 2.1 新增/改动工具（全部走现有服务，无第二套实现）

| 工具 | 动作 | 底层服务 | 人工边界 |
| --- | --- | --- | --- |
| `hx_project_admin` | 项目软删/恢复 | `projects.soft_delete/restore` | 删除当前项目后网关直接拒后续调用 |
| `hx_project_update` | 项目说明/名称/需求 | `projects.update` | 不碰绑定目录与授权 |
| `hx_session_admin` / `hx_session_history` / `hx_search_sessions` | 会话删/恢复/历史/搜索 | `sessions.*`（新增 `search_messages` 字面 LIKE） | 会话必须属于当前项目；删除当前会话即断本轮 |
| `hx_dataset_admin` | 数据集删/恢复 | `datasets.soft_delete/restore` | 软删保文件；恢复撞指纹给 409 |
| `hx_reflow_draft` | 回流草稿 list/read/save/delete/restore | `reflow.list_drafts/read_draft/save_draft/delete_draft/restore_draft`（新增 read） | 定稿仍走 `hx_reflow_apply` |
| `hx_list_feedback` | 反馈池真实列表 | `records.list_feedback` | 只读 |
| `hx_document_admin` | 资料 import/delete/restore/**locate** | `documents.ingest_path/delete/restore/file_path` | locate 返回项目内相对路径+SHA+下载入口，不绕鉴权 |
| `hx_manual_admin` | 个人手册 create/update/activate/delete/restore | `manuals.*` | **内置手册仍只读**（`MANUAL_READONLY`） |
| `hx_inspect_weight` | 只读权重检查（真实模态/类别/建议 family） | `models.inspect_weight_source` → `vision_runtime.inspect_weight` | 只限本项目 rel_path 或 weight 资产；不 dump checkpoint |
| `hx_list_records` / `hx_review_state`（增强） | 记录列表/复核状态补齐字段 | `detection.list_for_project` + `review.statuses/feedback_statuses` + `attach_primary_modality` | 与页面完全同口径 |

工具总数 70。`hx_document_admin import` 统一返回 `{"document": ...}`（此前裸返回，
与页面信封不一致，已修）；`hx_reflow_draft save` 新建草稿不再误要 `draft_id`（本轮测试抓到）。

### 2.2 逐页面动作 → 入口对照（旧入口 → 新位置）

| 页面动作 | 旧入口 | Agent 现在用 |
| --- | --- | --- |
| 项目删除/恢复/改说明 | 项目页/侧栏 | `hx_project_admin` / `hx_project_update` |
| 会话删除/恢复/翻历史/搜消息 | 侧栏 + 会话页 | `hx_session_admin` / `hx_session_history` / `hx_search_sessions` |
| 数据集删除/恢复（回收站） | 数据集页回收站 | `hx_dataset_admin`（`hx_list_datasets deleted=only` 对应回收站） |
| 回流草稿管理 | 回流页草稿区 | `hx_reflow_draft` |
| 反馈池查看 | 记录页反馈池 | `hx_list_feedback` |
| 资料导入/删除/恢复/下载定位 | 设置页资料区 | `hx_document_admin`（locate 给 `/api/projects/:p/documents/:d/download`） |
| 个人手册增删改/版本 | 设置页手册区 | `hx_manual_admin` |
| 权重体检（选 family 依据） | 无（靠文件名猜） | `hx_inspect_weight` |

### 2.3 仍然不交给 Agent（本轮没有新增任何工具）

最终人工结论（复核提交）、个人 Key/凭据保存、新目录绑定授权、新 MCP 工具授权、
建号注册、停止任务——`test_human_only_rows_have_no_agent_tool` 继续把守；
内置手册删除被 `MANUAL_READONLY` 拒绝（有真实网关调用证据）。

## 3. E：单 IR 语义

- **导入**（`services/datasets.py`）：`modalities=['ir']` 合法，主模态=第一个元素；
  `ir` 主模态 + secondary 一律 400（不造第二张图）；data.yaml 写 `modalities: {ir: images}`、
  `modality_used: [ir]`。
- **配对校验补真读**：X 配图此前只查 `is_file()`，现在 `_image_size()` 真 decode
  （坏图 → 409 `IMAGE_UNREADABLE`）；不同原始尺寸合法保留（不存主图尺寸当配图元数据）。
- **训练分派**（`services/training.py::_family_for`）：主模态 ir 且无 secondary → `yolo` 单模态，
  `generate_runtime_config` 的 `modality_used=[ir]`；主模态非 rgb/ir → `DATASET_MODALITY_UNKNOWN`。
- **记录/复核/巡检/回流**：`detection.attach_primary_modality` 按模型绑定数据集批量补
  `primary_modality`，记录列表/详情、复核页 `state().image`、AI 核查 caption（ir 注明
  “没有 RGB 替身”）、回流 `_primary_for`（父数据集优先，其次所选记录的模型数据集）全链一致。
- **模型 family ↔ 权重真实模态交叉校验**（`services/models.py` + `vision/runtime.py`）：
  - 根因：vendor `DetectionModel` **默认**就挂 `MultiModalRouter`（`x_modality_type='unknown'`），
    旧判据「有 router / 类名 mm / 旧布尔值」要么把单模态说成双模态，要么把真实 YOLOMM 误拒。
  - 现在 `_weight_modality` 逐项取证：YAML 5 字段路由注解（`has_multimodal_config` /
    `has_multimodal_routing_layers`）、router 自身被配置成多模态、metadata
    `x_modality_name`/`primary_modality_name`；evidence 六项逐条给出。
  - `import_model`/`create_trained`/发布 三处共用校验：`MODEL_FAMILY_MISMATCH` /
    `MODEL_MODALITY_MISMATCH` / `TRAINED_WEIGHT_MODALITY_MISMATCH`。
  - **真实权重不误拒**：`var/acceptance-20261104/workspace/weights/best.pt`（=hotspot-mm-fp3p4，
    6.85MB）实测 `yaml_routing/routing_layers/router/metadata` 全 true、`routing_annotations=2`、
    rgb+ir → 以 `family=yolomm, secondary=ir` 导入成功；按 `family=yolo` 导入被拒。
  - 训练假 worker 按 family 选模板：YOLOMM 产物必须真的带路由
    （新增 `tests/weights.py::write_mm_weight` 极小双模态结构，~394KB）。

## 4. 测试（全部本机真实执行）

新增 `backend/tests/test_business_wb17_b_agent_crud_ir.py`（16 例）：

1. **full 语义 + 同库可读**：网关 full 删数据集（无确认卡）→ 页面默认列表不见 →
   `deleted=only` 可见 → 原文件仍在 → 重复删除 `unchanged` → 恢复后页面可见。
2. **双普通账号拒绝**：bob 的令牌删 alice 的数据集 → `DATASET_NOT_FOUND`，数据未动；
   bob 打开 alice 的项目列表接口 → 404（不再是 200 空列表）。
3. **删当前项目/会话即断**：后续工具调用 409 `PROJECT_CLOSED`/`SESSION_CLOSED`；
   页面回收站可见并可通过 HTTP 恢复。
4. **会话历史/搜索范围**：只搜本人当前项目 active 会话；软删后搜不到但消息仍在。
5. **回收站往返**：资料（locate→delete→include_deleted→restore）、手册（软删→trash=1→恢复）、
   草稿（save→list/read→delete→include_deleted→restore）。
6. **字段一致性**：`hx_list_records`/`hx_review_state` 与页面 `/detections` 的
   `human_status/review_version/feedback_status/primary_modality/rgb_filename` 逐项相等；
   反馈池 ID 工具与 HTTP 完全一致。
7. **单 IR 全链**：导入 → 导出 ZIP `data.yaml`（`path: raw`、`modalities: {ir: images}`、
   `modality_used: [ir]`）→ `_family_for=("yolo",None)` → 运行时配置 → 记录/复核 `primary_modality=ir`
   → 回流新数据集仍是 ir；`ir`+secondary 400；缺配图 `MISSING_MODALITY`；坏配图
   `IMAGE_UNREADABLE` 且源目录字节未动。
8. **权重模态**：真实 YOLOMM 识别与导入；单模态小权重不能冒充 yolomm；
   夹具自检（vendor 默认 router ≠ 多模态证据，注解 ≥2 才算）。

既有测试同步：`test_business_wb17_05_matrix.py` MATRIX 补 11 个新工具名（行 3/5/12/19/20/21/23）；
`test_business_wb17_05_documents.py` 手册用例改写为「内置只读 + 个人可增删改+软删恢复」；
`test_business_wb17_05_training.py` 假 worker 按 family 选模板（单/双模态两份真实权重模板）。

顺带修掉的真实缺陷（都是测试先红后修）：

- `datasets.soft_delete` 重复删除误报 404（「已删过」≠「不存在」）；
- `hx_reflow_draft` 新建草稿误要 `draft_id`；
- `hx_document_admin` import 返回信封与页面不一致；
- 数据集/模型/资料/手册/记录/反馈六个列表接口对**别人的项目**返回 200 空列表 →
  现在一律先 `projects.get_owned`，404（不拿空列表冒充“没数据”）；
- X 配图未真读解码（§3）。

## 5. 真实浏览器冒烟（独立副本库，未动 root 实例）

实例：`var/ui-smoke-3`（browser-check 验收库副本 + 新构建 dist），端口 5933，脚本
`check_member_ui.py` / `check_review_grid.py`，证据 JSON+PNG 都在目录内。

| 检查 | 结果 |
| --- | --- |
| 从非首项目进设置，资料/MCP 两面板选中当前项目 | ✅/✅ |
| 切分组后手册草稿不丢；创建空内容手册报错且输入保留 | ✅ |
| 浅/深主题落到 `<html data-theme>`；1440 浅 / 1280 深 / 768 截图 | ✅ |
| 1280 助手展开无横向溢出、面板可见；768 无溢出 | ✅ |
| 全程无 JS 报错 | ✅ |
| 复核页 1440+助手：主标注图整行（835px = 两列之和），IR/结果并排其下，窄屏纵排 | ✅ |

## 6. 最终基线（2026-11-07 本机）

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest backend/tests -q --deselect ...::TrainingRealWorkerTest` | **539 passed, 1 skipped**（+20 subtests），0 failed（唯一一跑失败是假 worker 模板占位符漏替换，已修并单跑通过） |
| 前端类型/逻辑/构建 | `npm run typecheck / test:logic / build` | 0 error / **56 passed** / 通过 |

## 7. 未测 / 边界（如实记录）

- **AI 核查 caption 的 IR 覆写**只有代码层修改，未加协议桩用例（核查链路其余分支
  已有 03 用例；本轮未动请求构造）。
- 真实训练（`TrainingRealWorkerTest`，1 epoch CPU）本轮按惯例未跑；训练编排部分由
  假 worker 全链覆盖。
- MCP `NoEventLoopError` 已在**批次1**修复（root 清空 UTF-8 环境后独立复测 25/25
  通过）；本批未重复测试，沿用批次1修复，不再当新待办。
- 未跑 5887/5898 root 实例与协作者独立脚本；本轮证据全部来自副本库与 pytest。
- 导出 CSV 的 scope/selected 边界沿用矩阵既有用例，本轮未重复造断言。
- 工具总数 70 **不等于**全覆盖：矩阵 28 行中 `human` 行（凭据/授权/最终结论/停止/注册）
  本来就没有工具，这是设计而不是缺口。
