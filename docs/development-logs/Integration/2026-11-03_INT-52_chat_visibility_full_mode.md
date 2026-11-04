# INT-52 聊天可见性与完全访问模式修复

日期：2026-11-03
分支：`release/haixun-v1.5.0`
执行：DeepSeekHarness；方案审查与独立验收：Codex。
范围：`CXSDet-YOLOmm/CXSDet/web-flask`、`CXSDet-YOLOmm/CXSDet/web-haixun`

## 1. 背景与根因

1. 网站会话把海巡引擎（HX engine）的**上下文压缩**私有消息写入了站点 `agent_message`：
   引擎消息 `info` 带 `summary=true`、`agent/mode=compaction`，`parts` 仍是普通 `text`。
   旧 `_watch` 只排除 `synthetic` 与 `reasoning`，于是约 3000 字的 Objective / Important Details /
   Work State 被保存成普通 assistant 答复（`refs=[]`），前端直接展示。
2. 同一轮里，压缩消息带 `finish=stop`、`time.completed` 甚至 `MessageAbortedError` 时，旧完成判定
   可能把它当最终答复，导致真实 final 被提前截断。
3. 完全访问（full）模式仍为删除、恢复、已有文件修改等生成确认卡；用户已明确要求 full 不再出现
   任何“操作确认卡”，但**最终人工标注结论**（confirmed / no_defect / rejected）必须由本人在复核页提交，
   不能被 AI 或 full 策略冒充。
4. 原生权限自动批准最初直接调用 `self.confirm_action`，会以 `decision_source=human` 记账，形成
   伪“人工点击”；且缺少预检后的连接/工作区/参数新鲜度校验。

## 2. 代码变更

### 2.1 Fix A：内部消息分类与 _watch 公开范围

新增 `agent/engine_messages.py`：

- `is_internal_info(info)`：严格按 `info.summary is True` 或 `agent`/`mode == 'compaction'` 判定，
  不按正文关键词，避免误删正常回答。
- `is_aborted_info(info)`：识别 `MessageAbortedError` / `aborted`。
- `public_text(row)`：只拼 `type=text` 且非 `synthetic` 的 part；内部行直接返回空串（防线）。

`agent/hx_engine_runtime.py` `_watch`：

- 内部压缩行不保存、不采集 error、不参与工具过程与最终回答/完成判定。
- `completed` 整个表达式排除 aborted，避免 `time.completed` 分支把中止的普通 assistant 当完成。
- 无公开最终答复时继续等待，不提前 fallback；真实 final 照常保存且只保存一次。

### 2.2 Fix B：旧摘要标记、API/前端过滤

新增 `agent/internal_context.py`：

- 引擎库路径：`<HX_ENGINE_STATE_DIR 或 TEMP_DIR/hxengine>/<sha256(原始 DATABASE 字符串)[:12]>/<owner>/<sha256(session_id)[:24]>/data/opencode/opencode.db`；
  `database_key()` 与 `hx_engine_runtime._engine` 共用完全相同的**原始字符串**算法（Windows 反斜杠不 posix 化）。
- 只读 URI 连接，无缓存（避免漏看 WAL 新写），只认 `role=assistant` 且结构化内部标记的 message。
- `internal_refs_sql()` 使用 `json_each` 的 `type` 列（`internal_ref.type='object'`）判断，不再对字符串值
  二次 `json_type`；非法 JSON / `["string"]` / NULL 都不会报 malformed JSON。
- `internal_id_clause()` 按精确 `oc_` id 分块排除；`backfill_session()` 幂等追加
  `{"type":"internal_context"}` 到 `refs_json`（正文保留作审计，不删原文），
  只统计**站点里实际存在**的内部消息数（引擎库未保存的压缩消息不误报为历史摘要）。
- 提供 CLI：`python -m agent.internal_context --session <id>`（可重复）或 `--all --limit N`。
  不自动启动，不写 live 库。

`agent/sessions.py`：

- `list_messages` / `read_history` 均改为 SQL 层先过滤结构标记与精确内部 id，再做 LIMIT/OFFSET，
  不加载全量历史；`limit` 语义保持不变（返回最近 N 条可见消息）。

`web-haixun/src/services/mappers.ts`：`mapMessage` 遇到 `refs` 含 `internal_context` 返回 `null`，
`real-adapter` 的 `.filter(Boolean)` 丢弃；`execution_note` 折叠逻辑不变。

### 2.3 Fix C：full 模式零确认卡与人工结论边界

`agent/business_approval.py`：

- `is_builtin_business_scope()`：仅平台内置、`SOURCE_BUSINESS`、无 server_id、非外部 MCP 代理。
- `full_denial()`：外部代理工具、复核最终人工结论、`workspace_manage_project:bind`、
  带 `root_directory` 的新目录创建，在 full 下明确拒绝/引导，不生成卡。
- `automatic_mode(..., arguments)`：assist 仍只自动原 ROUTINE 清单；full 下**除 `full_denial` 外全部内置业务**
  自动执行（含 rename/create/delete/restore/draft 等），不再出卡。

`agent/runtime.py`：

- 在 `_guard_arguments` 后加 `_guard_human_decision` 钩子；need_confirm 且 full 拒绝时调用
  `_deny_full_confirmation`，返回 AWAITING_HUMAN 说明，不建卡。
- 跨项目自动模式降级后，full 改为明确拒绝并提示切换项目，不再退回待点击卡片。
- 自动领取事件补 `decision_source=session_policy`。

`agent/hx_engine_runtime.py`：

- `_guard_human_decision`：仅 full 下拦截复核最终三操作，`set_waiting(kind='human')`、AWAITING_HUMAN、
  引导 `/review/<record_id>`、事件 `review.manual_required`；request/assist 保持原确认卡流程。
- `_permissions`：full 下范围无法完整证明时直接 reject + 说明，不建卡；自动路径统一走
  `_confirm_native_policy`。
- `_confirm_native_action`：人工点击与内部自动领取共用的完整新鲜度校验——owner、连接 fingerprint、
  工具版本、工作区重新绑定、真实 pending 请求一致性；session_policy 还要求当前 mode 一致并按当前
  `decide` + `confined_permission` 重新证明范围（管理员 full 也需完整证明）。
- `_confirm_native_policy`：领取失败（陈旧/已决/越权）时拒绝该 pending、回 reject 给引擎、写 turn note，
  不退回卡片；成功事件 `human_clicked=false`。
- `actions.confirm` 增加来源约束：`decision_source` 仅 `human`/`session_policy`，`human_clicked`
  由来源推导且显式矛盾时报 VALIDATION；HTTP 路由仍只接受 `expected_revision`，不能伪造来源。
- 系统提示同步：full 下删除/恢复/已有文件修改直接执行、不出卡；最终人工标注必须本人提交；
  外部 MCP、新授权、越界/未知影响命令明确拒绝；删除记录等文案按模式区分。
- 系统提示新增最小策略指导（2026-11-03 晚补）：文件删除/修改命令必须单独一条执行，不得与
  `Test-Path`/`Get-Item`/`Get-Content` 等核验命令拼在同一条命令里；命令返回后再用独立的
  read/glob 读取工具核验结果。给出单独删除安全范例：
  `Remove-Item -LiteralPath '项目内一次性测试文件' -Force`。
  仅补充提示文本，未扩大解析器范围、未弱化 full/request/assist 的任何校验。

`agent/native_policy.py`：

- 删除类命令严格预检：逐条 DELETE 命令、所有路径参数必须是 literal 且明确、每条命令至少一个目标；
  动态表达式、未知参数、缺失目标、Rename/Move 目的地不合规一律不自动。
- 清空文件内容（`Set-Content -Value ''`）同样整条命令预检（不早返回），且目标文件必须存在。
- full 下项目内明确目标删除/恢复、已有文件修改自动执行；assist 保持原逻辑。
- `confined_permission` 改为**范围证明**（内部复用 `command_decision(..., 'full')`），不再依赖传入模式的
  `auto`，修复普通账号 request/assist 明确删除被误拒的问题。

### 2.4 前端

- `components/ChatPermissionPicker.vue`：full 描述、scope-note、bottom-note 改为“不生成确认卡，
  最终人工结论本人在复核页提交，外部 MCP/越界命令明确拒绝”。
- `components/AgentProgress.vue`：`pendingActions` / `waitingApproval` 排除
  `preview.approval.source === 'session_policy'` 的短暂自动领取卡，人工卡仍显示。
- `types/index.ts`：`ActionPreview.approval` 可选字段。

## 3. 测试

新增/更新：

- `tests/test_engine_response_lifecycle.py`：工具消息后延迟 final 不被截断；压缩内部摘要（含 reasoning
  与正文）不保存、不提前收尾、后续 final 正常保存；reasoning 永不保存；aborted 普通 assistant 不得
  判完成（超时收尾 INTERRUPTED）。
- `tests/test_engine_native_stale.py`：预检后会话模式变化 / 连接 fingerprint 变化 / 工作区重新绑定 /
  pending 参数变化均不自动执行（stale→reject，无自动批准事件）。
- `tests/test_internal_context.py`：SQL 对 `["string"]`、非法 JSON、NULL 不报错；精确 id 排除；
  Windows 原始路径 db_key 一致；8 条引擎候选仅 5 条站点消息时回填统计为 5/5/5、二跑 0、正文保留。
- `tests/test_native_policy.py`：更新 full/assist 语义；新增删除 literal 全量预检、Rename/Move 目的地、
  清空后越界读取、无效 AST、混合命令、confined 范围证明组合；真实 PowerShell AST 用例在沙箱不可用时 skip。
- `tests/test_business_approval_modes.py`：full 删除自动、full 复核最终结论等待本人、request/assist 仍出卡。
- `tests/test_v15_judge_regressions.py`：普通账号 request/assist 明确删除可出卡并可人工领取；full 自动且事件
  `human_clicked=false`；未知/混合/重定向保持拒绝。
- `tests/test_engine_prompt_guidance.py`：request 与 full 系统提示都要求删除/修改命令单独一条执行、
  用独立 read/glob 核验，并包含单独 `Remove-Item -LiteralPath` 安全范例（只断言提示文本，不扩大解析器范围）。
- `tests/test_workspace_tools.py`：full 下项目管理创建自动（无卡）、assist 仍出确认卡（保留原测试，仅更新断言）。

执行端沙箱结果（Python 3.14，使用隔离的 PYTHONPATH 与临时目录、`-p no:cacheprovider`）：

```
tests/test_internal_context.py tests/test_engine_response_lifecycle.py
tests/test_engine_native_stale.py tests/test_native_policy.py
tests/test_business_approval_modes.py tests/test_v15_judge_regressions.py
tests/test_engine_prompt_guidance.py
tests/test_workspace_tools.py::test_export_automatic_and_management_still_have_specific_confirmation
=> 41 passed, 3 skipped
```

说明：执行端沙箱不能启动真实 PowerShell AST，相关 3 个用例跳过；最终以以下宿主环境的独立验收为准。

## 4. 真实库回填方式（宿主执行，未自动运行）

```bash
# 先备份 yyxz_sqlite.db（宿主负责），再按会话回填：
python -m agent.internal_context --session ses_xxx
# 或最近 200 个会话：
python -m agent.internal_context --all --limit 200
```

只追加 `internal_context` 结构标记，不修改正文、不删除记录，重复执行幂等（二跑 marked=0）。

## 5. Codex 独立验收

- 宿主原有 Python 环境执行完整后端测试：**598 passed、20 skipped、38 subtests passed**。
  跳过项主要为显式启用的引擎集成测试；真实 Windows PowerShell AST 用例实际运行通过。
- 最后仅增加命令拆分提示与对应测试，再运行提示构建、回答生命周期及原生请求陈旧校验：**9 passed**。
- 前端：**34 项测试通过**，`vue-tsc --noEmit` 与 Vite 生产构建通过。
- 修改前先通过 SQLite backup 备份本机隔离数据库，完整性检查为 `ok`。
  从引擎结构化元数据精确匹配站点内 **5 条**误显示的压缩摘要，追加隐藏标记；重复回填为 0。
  对比所有原消息正文的数量与内容摘要，确认完全不变。
- 普通演示账号通过真实 HTTP 登录入口读取历史：5 条内部摘要不再经消息 API 返回，正常最终回答保留。
  浏览器实际核验：旧摘要不显示，执行过程默认折叠，当前权限模式为完全访问。
- 首次真实模型测试完成新建、修改与读取；模型把删除和核验拼成混合命令时被拒绝，未执行该调用。
  补充了拆分命令的系统指导，未扩大解析器权限。
- 加载最终代码后再次从新会话发出完整任务：普通账号一轮完成专用测试文件的新建、修改、读取、删除，
  以及会话改名；**6 项工具操作、4 次策略自动批准、0 张操作确认卡**，最终回答正常返回。
  测试文件实际不存在；所有自动批准均标为 `session_policy`、`human_clicked=false`。
- 测试未修改原有数据集、模型、识别记录，未启动训练；API 与浏览器验证使用同一套真实业务服务。
- 本机测试服务已加载最终代码，保持仅监听回环地址；日志与验收截图留在忽略目录，没有上传账号、密码、
  API Key、令牌、完整用户聊天、数据库或运行数据。

训练与大规模巡检不属于本次验收范围；最终人工标注仍由本人在复核页提交，范围不明或越界的命令明确拒绝。

## 6. 2026-11-04 补充修复（AI 复核演示可用性）

- 单次请求超时：`agent/llm/provider.py` `complete()` 增加可选 `timeout`，仅在调用方传入时使用
  `with_options(timeout=..., max_retries=0)` 放宽本次请求；`services/personal_review_client.py`
  复核传 180s、改进建议传 120s，聊天默认行为不变。未做图片降采样（保留原图，避免小热斑信息损失）。
- 批量错误脱敏：`services/batch_review_service.py` 遇 `AgentError` 时，仅白名单错误码展示站内提示
  并先经 `agent.guard.redact` 脱敏；白名单之外统一固定文案，避免上游原始报错中的 sk/Bearer 透出。
- 第三根因（私有桥身份）：聊天内 Agent 工具经私有 MCP 桥回调站点时没有站点 JWT，`flask.g.user_id`
  为空，个人复核/模型工厂抛 `AGENT_UNAUTHENTICATED`。修复：`agent/hx_engine_runtime.py`
  `_bridge_engine()` 在认证引擎令牌并核对 owner/session 后写回 `g.user_id = actor.user_id`
  （与 `agent/inspection_loop.py:506` 既有做法一致）。手工页面批量复核走站点 JWT，不会暴露该问题；
  页面单条 200 不能证明 Agent→私有 MCP→复核工厂这条链可用。新增非付费单测
  `tests/test_engine_bridge_principal.py`：有效令牌得到真实 owner、伪造/失效令牌 401 且不写/不覆盖身份、
  非本机来源拒绝、伪造请求头不能覆盖 token owner 对应的身份。
- 测试：新增 `tests/test_agent_review_timeout.py`；`tests/test_integration_int06.py` 的 FakeGateway
  接受新增 timeout 参数（原关键断言保留，未删/未跳过用例）。最终 5 文件 30 passed；扩展相关回归
  50 passed、18 skipped、9 subtests passed。日志：`.integration-checks/20261104-final-review-tests.txt`。
  本轮未提交、未推送。

### 6.1 私有 MCP 桥 + 真实付费核查的独立验收（宿主，2026-11-04）

根线程（Codex）用普通演示账号通过真实 HTTP 入口独立验收，证据：
`.integration-checks/20261104_bridge_acceptance.json`（未上传、未提交）：

- 会话 `ses_02e5938b212743d28512`，任务 `tsk_36a247c30bd24aa5bfc4`，最终 `status=completed`。
- 调用链：Agent → 私有 MCP 桥 → `business_review_ai`（个人模型连接），单条记录
  `6dc4e187-d917-4ee0-88c5-005724543db3`（项目 `prj_c46be19bcb324262906a`，review_version 0）。
- 结果：`requested=1, succeeded=1, failed=0`，AI 结论 `consistent`（检测框与红外热斑位置吻合），
  未重试、未核查其他记录、未创建检测、未改标注、未人工确认、未回流、未训练。
- 同任务随后返回正常最终回答（含结果摘要与人工复核提醒），说明 `_bridge_engine()` 写回
  `g.user_id` 后 Agent→私有 MCP→个人复核工厂这条链在真实服务上可用，身份修复得到独立验证。
- 该证据由根线程产生并记录，本次执行端未重复付费调用、未重启网站、未推送。

剩余风险（本轮未追加大改）：

- `agent/hx_engine_mcp.py:26` 私有 MCP relay 的 HTTP `open(timeout=90)` 仍短于视觉单次 SDK
  timeout（180s）；Agent 批量复核工具串行处理 3 张图时可能超过 90s 而 relay 侧先超时。
  上面的独立验收是单条记录；批量 3 张图的整链仍需宿主按需核验，并视需要提高 relay 超时
  或为批量路径设置独立预算。本轮不重复付费调用、不重启运行实例。
