# WB17-整轮收尾 A：设置/巡检/复核/回流 + 两个前置兼容小修

执行时间：2026-11-07。范围：`WB17-整轮收尾执行_20261107.md` 的 **A 部分**（先 UI-04B 设置与巡检，再 UI-03 复核/回流），外加派发点名、批次 1 未覆盖的两个前置小修。数据集列表（UI-03A 已通过）未重写；UI02 对话、UI03A/UI03B 已认可改动全部保留；D/E 不在本批。未 Git 提交，未动 root 5887/5898 与三份独立证据，未跑 GPU/付费 API。

---

## 一、前置小修（root 复测若已绿即无需重复验证）

### 1. 数据集 restore 旧非空 v1 指纹兼容（root 第 6 项 `legacy_v1_deleted_restore_has_conflict=false`）

- 位置：`backend/services/datasets.py`
- 改动：新增 `_stored_fingerprint_pair(conn,row,root)`（按已落库样本重算 v2+v1，X 图缺失返回 None）与 `_restore_conflict(...)`（恢复/并发兜底共用）。三条口径都查：
  1. 直接口径：库里存的指纹 / 现场重算 v2 → 命中即 409；
  2. v1 口径：回收站记录存旧 v1、活跃侧是 v2（或反向：活跃 v1、回收站 v2）→ 用现场重算的另一算法值对上；
  3. v1 命中必须**类别表逐项相等**才算冲突（`'|'.join` 碰撞如 `['a','b']` vs `['a|b']` 不得误挡恢复）。
- `restore()` 主路径与 `except sqlite3.IntegrityError` 并发兜底都改走 `_restore_conflict`；回收站记录的指纹不被改写，文件/历史不动。
- 测试：`backend/tests/test_business_wb17_07_shared_defects.py` 新增 3 例——回收站 v1+活跃 v2→409（root 反例原样）、反方向活跃 v1+回收站 v2→409、v1 碰撞不同类别→不冲突可恢复。文件内指纹专项 8/8 过。

### 2. 受控命令 ForEach-Object 位置参数 MemberName（root 第 5 项 `positional_member_delete_rejected`）

- 位置：`backend/agent/shell.py`
- 改动：按派发的最短可靠办法，**整个 ForEach-Object 进 `_DENIED_CMDLETS`**（含别名 `foreach`、`%`），理由写明 `-MemberName` Position=0 可省略参数名；不再尝试实现参数绑定器。删除原先只在带参数名时拦截 `-MemberName/-Method` 的参数层特判（已被整体拒绝覆盖）。文档字符串同步。
- 只做 AST 分析，未执行任何被测命令；正常 `Get-Content`/`Select-Object` 管道不受影响（测试钉住）。
- 测试：`test_foreach_object_member_invocation_is_rejected` 扩到 6 个变体（`-MemberName`/`-m`/`-Method`/**位置 `ForEach-Object Delete`**/`foreach Delete`/`% Delete`）。shell 相关全部通过。

---

## 二、UI-04B 设置（SettingsView 重排为四分组）

- **四分组**：模型连接｜项目资料与手册｜工具连接｜账号与外观，`.segmented` 切换，默认只展开「模型连接」。用 `v-show` 保留组件常驻：切组不丢任何草稿（表单/密码输入都在）。
- **模型连接默认摘要**：真实模型 ID + 显示名、接口、Key 掩码、检查时间、四个能力（认证/Chat/视觉/工具）的**实测结论**（未检查/已失效一律显示「未验证」，不说成已验证）；认证判定一句话；检查证据收进「诊断详情」折叠。首次无连接直接进编辑。
- **修改连接**：点「修改连接」才进编辑；可「取消（恢复为已保存值）」；Key 不回显（只显掩码占位）；编辑中外部连接刷新不再覆盖草稿（watch 加 `editing` 判断）；保存成功回摘要。
- 资料与手册（DocumentsPanel）、工具连接（McpPanel）各归各组，功能未改；「关于」收进账号与外观组的折叠，「未实现的开关在后端确实是关闭的…不是这里藏起来了」等自证文案精简为一句事实说明。

## 三、UI-04B 图片巡检（InspectView）

- 字段顺序改为操作顺序：**已发布模型 → RGB 图片 →（配图）→ 置信度阈值 → 开始检测**（按钮文案「运行检测」→「开始检测」）。
- 删掉首屏实现细节长段（“当前主链路…推理引擎自带的流式能力不会被网页直接调用”），换一行短说明：支持静态图片；按目录批量请在对话里让助手执行；暂不支持视频/实时画面。
- 手动上传 RGB/配图、工作区图片下拉、缺 IR 真实拒绝、edge 派生勾选全部保留（服务端校验未动）。
- 「本次结果」kv 改 `dl.kv > div > dt/dd`，阈值一行明确标注“本次运行的阈值，不是框分数”。

## 四、UI-03 复核（ReviewView）

- **RGB/IR 各自真实尺寸（root 反例修复）**：三张 img 不再统一绑主图 `image.width/height`。RGB 以接口尺寸为基准（标注坐标不变）；配套图/结果图按自身 `naturalWidth/Height` 登记（`paneImage`），`fitView`/`100%`/滚轮中心缩放/± 按钮全部按 pane 自己的图像尺寸与**现量边界**计算。`paneRect()` 每次现量 `getBoundingClientRect`（不再用挂载时缓存）+ ResizeObserver 维持 `paneSize` 新鲜——切助手/调窗口后再点「适应窗口」按当前边界 fit。最终框坐标仍只用主图原始像素（`clampPoint(toImage(...), image)` 路径未动）。
- **布局**：常用标注工具（选择/平移/画框/加框/删框/撤销 + 框数）从底部编辑卡上移，紧邻 RGB 画布上方；底部编辑卡保留类别/严重度/备注/提交动作。1280 仍两列、768 单列（CSS 断点未变）。
- **标题**：文件名上首行、人工状态、`已确认版本 vN` 或 **尚未提交人工结论**（不再出现“已确认版本 v0”）；草稿标注“（未提交）”；记录 ID、AI 建议、类别来源、版本历史收进「记录详情」折叠。
- 提交按钮改「提交人工结论：有缺陷/无缺陷」，草稿按钮标注“不产生人工结论”；驳回补充说明“不会新增图片，也不会重复产生任务”。

## 五、UI-03 回流（ReflowView）

- 卡片按流程重排：**① 选择样本（待整理反馈）→ ② 回流方式与目标 → ③ 保存草稿并预览/定稿**；预览/定稿按钮独立成动作行。
- revision（回原修订）模式隐藏划分方式/种子/比例（该模式不需要，动作列的“回原替换=沿用父划分、追加=逐条选划分”逻辑不变）。
- 空态改可点击：**前往人工复核 / 前往识别记录** 两个真实路由按钮。
- 参数变化旧预览失效（`formSnapshot`/`planStale`）、同图替换不增样本、真实新图显式划分——共享服务与既有逻辑未动。

## 六、记录行置信度与反馈状态（RecordsView + 服务层）

- **紧凑行保留实际检测置信度**：「命中」列改「命中 · 最高分」，命中数旁显示**最高框分数**（如 `2 · 0.91`，title 标注最高框类别）；无命中的已完成记录显示 `0 · —`；未完成不显示假值。明确区别于阈值 `record.confidence`（详情里改名「检测阈值」并注明）与 AI 建议置信度。
- **反馈状态口径**（服务层统一，页面与 Agent 共用）：
  - `backend/services/review.py` 新增 `feedback_statuses_for_records()`：按**当前生效人工确认版本**（`effective_review`）查 `record_feedback`，返回 `none/pending/reflowed`；`void` 按未入池。
  - `GET /detections` 列表与 `GET /detections/:id` 单读都附 `feedback_status`（同口径）。
  - 行内「人工/反馈」列显示 未入池/已入反馈池/已回流；`feedbackEligible` 只计 `confirmed && feedback_status==='none'`——已入池/已回流不再计可反馈；旧版本回流不阻止新人工版本（新版本状态回到未入池）。
  - 「更多→加入反馈」对已在池的记录禁用并注明；批量按钮 title 同步。
  - `addFeedbackIds` 区分后端 `created`：回执分「新增 N 条 / 已在池 M 条」；**失败保留勾选与页面状态**（不刷新、可直接重试）；成功后刷新当前筛选范围使可反馈数真实。
- 详情弹窗 kv 平铺 span 改 `dl.kv > div > dt/dd`（共享样式），并新增「反馈」行。

## 七、模型页与 kv 结构（ModelsView / TrainingPanel）

- 「去检测」（实际跳 records）改为**「去巡检」真实入口**：跳 `/projects/:pid/inspect?model=<已发布模型ID>`，InspectView 读取 `?model=` 真实预选（只选中不自动跑）。
- 模型详情 kv、训练计划 kv、父子指标对比 kv 全部改共享 `dl.kv > div > dt/dd` 结构，折叠展开紧凑可读。
- 删自证文案：「不会替换别人的模型」→「发布只作用于当前项目」；「不补假分」→「训练完成后会在这里显示真实数值」。

---

## 八、旧入口 → 新位置映射

| 旧入口/位置 | 新位置 |
| --- | --- |
| 模型页「去检测」（跳 records） | 模型页「去巡检」→ 巡检页并预选模型；查看某模型的记录仍可用记录页「模型」筛选 |
| 设置页所有卡片平铺 | 设置页四分组 tab（模型连接/资料与手册/工具连接/账号与外观）；「关于」在账号与外观折叠 |
| 模型连接编辑表单常驻 | 默认摘要 + 「修改连接」进入；取消恢复已保存值 |
| 检查证据表格常驻展开 | 摘要内「诊断详情（检查证据）」折叠 |
| 巡检页长范围说明 + 置信度在模型后 | 一行短说明；顺序 模型→图→置信度 |
| 复核页底部工具条 | RGB 画布正上方工具条；ID/AI 建议/历史在「记录详情」折叠 |
| 记录行「命中」列 | 「命中 · 最高分」列（最高框分数同行） |
| 详情「置信度」字段 | 详情「检测阈值」（注明不是框分数）；框分数见框表与行内最高分 |
| 回流页方式卡在前、空态纯文字 | ① 样本 → ② 方式 → ③ 预览；空态给「前往人工复核/前往识别记录」按钮 |

## 九、已测（真实运行）

- 后端专项：`test_business_wb17_a_feedback_status.py`（4，新增）+ `test_business_wb17_07_shared_defects.py`（24，含 3 例指纹新反例与 6 变体 shell）+ `test_business_wb17_04.py` —— **56 passed**（含 6 subtests）。
- 前端：`vue-tsc --noEmit` 0 错；`test:logic` 56/56；`npm run build` 通过。
- 页面冒烟 `var/selfcheck-a-smoke/verify_a_batch.py`（隔离站点 + 普通 member + 桩检测器，连跑两遍）**38/38 全过**，证据 `A批页面冒烟.json` + 截图：
  - 复核宽高比（root 同款反例）：RGB natural[160,90]=display[160,90]，**IR natural[90,120]=display[90,120]**（修复前 display=[160,90]）；
  - 设置：默认摘要组、其它组隐藏、切组草稿保留、校验失败保留输入、取消恢复、三分组可达；
  - 记录：行内最高分 0.91、未入池→（真实菜单加入反馈）→已入反馈池、详情 kv 结构与「检测阈值」字段；
  - 模型「去巡检」跳转预选生效；巡检字段顺序 模型→RGB→置信度阈值、无实现细节长文；
  - 回流空态「前往人工复核」可点且落到复核队列；
  - light-1280（助手展开）/light-768/dark-1440 下 settings/review/records 无横向溢出、无页面错误。

## 十、未测 / 待 root 独立验收

- **未跑完整后端全量**（按派发：本批只跑相关专项；代码稳定后由 root 统一跑）。改动面：datasets/review/routes/records/shell/前端多页——若全量出现与本批文件相关的意外失败，优先怀疑 `feedback_status` 附加字段与 ForEach-Object 整体拒绝的影响面。
- root 5887/5898 实例与三份失败证据未动；root 会按新 UI 调整 `verify_review_native_aspect.py` 的定位（三列画布布局保留，但 pane 结构未强制）。
- stdio 乱码修复（批次 1）只针对 Python 子进程；Node MCP 子进程默认 UTF-8 未另测（沿批次 1 记录）。
- 1280「助手开」按默认 dock 展开验证（localStorage 未设折叠键）；没有做助手宽度拖拽的极端值测试。
- 回流的预览生成/冲突/定稿链路本批只改了布局与空态，共享服务未动，未重跑 reflow 后端专项（既有测试未涉及改动面）。
- `verify_dataset_dedup_contract.py` / `verify_phase05_shell_edges.py` 属 root 脚本，未运行未修改；本批两个前置小修是否有必要复跑由 root 决定。
