# WB17 收尾批次4：Root 独立反例的最小共享修正

日期：2026-11-07。范围：只处理 root 反例清单里的具体项；不改已通过的 IR 链路、聊天正文/流处理；不 Git 提交、不付费 API、不训练、不部署；**未跑全量后端**（按本批指示）；未操作 root 5887/5898 实例，未修改 root 的测试脚本与失败证据（核验脚本只运行**独立副本**，输出另存 `var/ui-smoke-4`）。

---

## 1. 复核重开状态（`verify_review_reopen_ui.py` 5 项失败）

**根因**（与 root 判断一致）：`ReviewView.editable` 只看 `latest_submitted.status !== 'rejected'`；`state.human_status/queue`、`statuses_for_records`、`review.queue()` 也都"最新已提交优先"，显式重开生成的新草稿被旧驳回盖住。

**修法（共享规则只有一份）**：`backend/services/review.py` 新增

- `_draft_supersedes(latest, draft)`：草稿存在且（无已提交版本，或草稿版本更大）才是"当前编辑中"。
- `_bucket_for(latest, draft)`：唯一分桶。只有两种情况算 draft——首次复核的草稿；**最近已提交是 rejected 且草稿版本更大**。已确认记录上另存的草稿**不**把状态降级成 draft（否则记录页反馈入口会被误锁）。
- `state()`：`human_status/queue` 都用 `_bucket_for`；新增 `reopened`（驳回后又有更新草稿）；`pending` 要求"无已提交且无草稿"。
- `statuses_for_records()`（列表人工状态）与 `queue()`（三个视图）改用同一 `_bucket_for`，重开记录回到草稿队列、离开驳回队列。
- `effective_review()` **不动**：重开后生效结论仍是那条 rejected，旧 confirmed 不能复活加入反馈（`add_feedback` 依旧 `REVIEW_NOT_CONFIRMED`）。
- `reject()` 幂等分支修正：重复驳回不涨版本的前提是"没有更新的草稿"；重开→改完→再驳回会真正落成新版本 vN+1 并清掉草稿，不再被幂等分支吞掉。最初 QA 的"重复驳回 403/应幂等"口径未被重写——重复驳回（无新草稿）仍然 `created=False`。

**前端**（`ReviewView.vue` / `types.ts`）：

- `editable = state.queue !== 'rejected'`（服务端唯一分桶决定，不再前端猜）。
- 标题改中性：`最近人工结论 vN · 已驳回 / 已确认`，不再把驳回叫"已确认版本"；重开时显示"已驳回 · 已重开（草稿待提交），可直接编辑后重新提交"。
- `重开为草稿` 按钮在 `reopened` 时隐藏；`ReviewState` 增加 `reopened`。
- **编辑卡移位**：原 `.review-editor` 整卡移入 `.review-grid`、`grid-column: 1/-1`，紧跟主标注图之后、两张辅图之前；按钮/事件/版本逻辑一字未改，标注算法与三 pane 独立变换保留。

**真实浏览器证据**（`var/ui-smoke-4/check_reopen_ui.py`，独立副本库，**19/19**，`UI重开核验.json` + 两张截图）：真实 HTTP reject→reopen 后，state.queue=draft、草稿队列含该记录、驳回队列清零；标题不再出现"已确认版本"；保存/提交/加框全部 enabled、重开按钮隐藏；DOM 顺序 主图(835px 整行) → 编辑卡 → 辅图(412px×2)；点加框→保存草稿（版本 3→4 真实落库）→提交有缺陷 → v3 confirmed、草稿清空、队列 confirmed、标题变"已确认"，无 JS 报错。

## 2. 网关预检失败的其余修正（`verify_agent_actions_gateway.py` 6 项中 4 类）

- **权重模态**：批次3 已修（vendor 默认 router 不算证据，按 YAML 路由注解/`has_multimodal_config`/metadata 取证），本批**未重复返工**；用 root 的 `verify_weight_modality_metadata.py` 副本独立复跑 4 份权重（真 MM、真单模态、真单模态 EMA、names 字典）`all_passed=true`。
- **`hx_list_feedback`**：schema/描述改为真实状态词表 `pending|reflowed|void|all`（原来写 used）。页面 `/feedback?status=reflowed` 与工具返回同库同结果（测试断言两边 ID 相等）。
- **`hx_reflow_draft` save 无 draft_id**：批次3 已改为"无 ID 新建、有 ID 更新"，本批补上 request 模式的回归（见下）；root 的"仅服务建 fixture"未当作 Agent 已创建，测试里新建用例走真实网关 full 模式（批次3 已有）。
- **按 action 计算 risk**：`ToolSpec.risk_for`（机制已存在）接入两个共用工具——`hx_reflow_draft` list/read=read，save/delete/restore=write；`hx_document_admin` locate=read，import/delete/restore=write。request 模式下只读子动作直接执行（真实网关验证返回 200 ok），delete 仍出确认卡（`pending_confirmation`）；`policy.decide` 层有单测。

## 3. 回收站 ID 可发现（不只给 restore 写工具）

- `hx_list_projects` / `hx_list_sessions` 增加 `include_deleted`：默认仍是活跃列表（页面行为不变）；带上后返回真实 `status` 与 `deleted_at`，范围仍限本账号 / 当前项目。恢复流程 = 先查 ID 再 `hx_project_admin/hx_session_admin restore`（测试覆盖：删除→默认列表不见→include_deleted 可见→restore 成功）。
- 数据集 `deleted=only`、手册 `include_deleted`、草稿 `include_deleted` 批次3 已有真实结果用例（回收站往返），本批复核未改。

## 4. 原生对话的 UI 即时刷新（改名不刷新反例）

- 新增 `frontend/src/lib/sync.ts`（纯函数：事件→资源域映射 + 按 seq 去重）与 `frontend/src/stores/sync.ts`（按资源域递增版本号）。
- `stores/agent.ts` 在 `adopt()`/`poll()` 里消费轮次事件（先去重再合并），`projects.changed/sessions.changed` 触发 `workspace.loadProjects()/loadSessions(当前项目)`——项目标题、侧栏立即更新。
- `AppLayout.vue` 两条 watcher：当前项目从列表消失（`projectsReady` 之后）→ 回项目列表；当前会话从已加载列表消失 → 回项目主页（本人手动删除本来就有路由收尾，这里补的是后台触发路径）。
- 后端为后台改动补齐事件：`hx_rename_project`（原先不发）、`hx_document_admin` import/delete/restore、`hx_manual_admin` 全动作、`hx_reflow_draft` save/delete/restore 现在都 emit 对应 `*.changed`。
- 组件订阅：`DocumentsPanel`（documents/manuals）、`DatasetsView`（datasets/reflow）、`ModelsView`（models/datasets）watch 版本号后用**自己的 loader** 重读；不清筛选、不覆盖正在编辑的草稿/表单、不动消息缓存。
- 证据：root 的 `verify_native_new_tool_chat.py` **副本**（输出与临时目录改到 `var/ui-smoke-4`）**11/11**，包括 `project_heading_refreshes_without_reload`、`sidebar_project_refreshes_without_reload` —— 这两项在批次前是 fail。全程无 JS 报错、无确认卡、最终回答落库。
- 前端逻辑测试 `tests/sync.test.ts`（+5，共 61）：去重、乱序按 seq、未知事件不触发、独立资源域。

## 5. 日志勘误

- 批次3 日志 §7 的"MCP `NoEventLoopError` 既有待办未处理"改为："已在批次1修复（root 清空 UTF-8 环境后独立复测 25/25）；本批未重复测试，沿用批次1修复"。

## 6. 测试基线（本批）

| 项 | 结果 |
| --- | --- |
| 新增 `backend/tests/test_business_wb17_c_reopen_tools.py` | **11 passed**（重开语义 4 + 网关反例 5 + risk/schema 2） |
| 受影响子集（04 / matrix / 05_documents / b_crud_ir / agent_gateway / c） | **113 passed, 0 failed** |
| 03 / 05_mcp / 05_weight_validation | **68 passed, 0 failed** |
| 前端 typecheck / test:logic / build | 0 error / **61 passed**（+5 sync）/ 构建通过 |
| 真实浏览器：重开 UI（`var/ui-smoke-4`） | **19/19**，截图 2 张 + JSON |
| 真实浏览器：原生新增工具对话（副本脚本） | **11/11**（含改名即时刷新 2 项） |
| 权重模态独立复跑（root 脚本副本，4 份权重） | `all_passed=true` |

未跑全量后端（按本批指示）；root 的 `verify_agent_actions_gateway.py`、`verify_review_reopen_ui.py` 未在本机执行（依赖 root 夹具/实例），由 root 复跑。

## 7. 未测 / 边界（如实记录）

- **删除当前项目/会话后的路由收尾**：本人 UI 删除路径（项目详情抽屉）本就 `router.push` 收尾；Agent 触发路径靠 `projectsReady` watcher（与已验证的"改名即时刷新"是同一条 loadProjects 通路），但没有单独的浏览器级用例专门触发 `hx_project_admin delete` 后观察跳转——root 复跑网关脚本时如发现缺口再补。
- `ui.navigate` 事件仍未在前端消费（本轮反例未涉及，保持现状）。
- 原生对话核验只覆盖 `hx_project_update` 一个新工具的完整链路（与 root 同一份脚本）；其余新工具的"真实引擎对话"验证由 root 后续脚本负责。
- `hx_list_projects include_deleted` 返回的 `deleted_at` 是服务端软删时间戳；会话列表 include_deleted 限 50 条（与原列表一致）。
- 未改 root 夹具（`var/root-business-refinement`、`var/root-verifications`）与 root 证据 JSON；本批全部证据在 `var/ui-smoke-4/`。

## 8. 旧入口 → 新位置

| 旧 | 新 |
| --- | --- |
| `ReviewView.editable` 看 `latest_submitted.status` | 看 `state.queue`（服务端 `_bucket_for` 唯一分桶） |
| 列表 `human_status` / `queue()` 各自判断 | `review.statuses_for_records` / `review.queue` / `state` 共用 `_bucket_for` |
| 标题"已确认版本 vN" | "最近人工结论 vN · 已驳回/已确认" + 重开提示 |
| 编辑卡在两张辅图之后 | 编辑卡在主图之后、辅图之前（栅格整行） |
| `hx_list_feedback` status `used` | `reflowed`（与库一致） |
| `hx_reflow_draft`/`hx_document_admin` 整体 RISK_WRITE | 按 action：list/read/locate = read |
| 回收站 ID 只能靠记忆 | `hx_list_projects/hx_list_sessions include_deleted` 先查再恢复 |
| `projects.changed` 无人消费 | `lib/sync.ts` + `stores/sync.ts`，AppLayout/三个组件订阅 |
