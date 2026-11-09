# 海巡智检 v1.8

光伏（RGB+IR）巡检与数据质量闭环系统。当前版本 **1.8.0**，功能阶段 **WB17-06（收尾）**：
从检测到复核、反馈、回流、训练、发布的业务闭环，以及“网页 + 助手共用同一套服务”的
主要业务闭环已实测；范围与局限见 [v1.8 发布验收摘要](docs/v1.8发布验收摘要.md)。对外演示用单端口同源部署（Waitress WSGI）。

> v1.8 相对 v1.7 只做发布整合：**外部 MCP 工具的真实调用尚未完成验收，v1.8 隐藏了它的
> 设置入口**（组件与后端扩展代码保留），并把版本、目录、文档与固定入口统一到 1.8.0。
> 内部 Agent 依赖的 hx 业务工具网关与 OpenCode 连接不受影响。

> 来源说明（如实）：本工程是从空白工程重新开发的 v1.7。v1.6 / v1.6.1 仅作为**只读功能
> 参考**（成熟服务与视觉交互有来源地适配），不复制其源码目录冒充新开发；开发过程按
> WorkBuddy 批次推进，逐批日志与验收证据在 `docs/workbuddy-development/`，不倒写历史。

## 能做什么（按页面）

| 入口（顶栏） | 能力 |
| --- | --- |
| 项目对话 | 主对话默认页。真实 OpenCode 1.19.4 引擎驱动助手；发送图片/文件走真实字节；工具调用经私有回环网关，`request/assist/full` 三种权限模式真实接通 |
| 人工复核 | 画框/改类别/严重程度、保存草稿、提交“有缺陷/无缺陷”、驳回、**驳回后重开为新草稿再提交**；生效结论 = 最近一次已提交版本 |
| 数据集 | 目录/ZIP 导入（RGB、RGB+IR、**单 IR**），逐样本校验图片可解码、标签合法、类别对齐；查重、划分、导出 ZIP、回收站 |
| 模型管理 | 导入 PT（yolo / yolomm，按真实路由注解与 metadata 判模态，不靠文件名）、发布/撤销/删除、训练计划与受控子进程训练（锁、取消、重启核对）、本机设备能力报告 |
| 图片巡检 | 选已发布模型 → 上传/选择图片 → 真实 vendor ultralytics 推理 → 生成记录 |
| 识别记录 | 筛选/分页、批量核查（AI，用本人连接）、人工状态与反馈状态、加入反馈池、软删除/恢复、CSV 导出（限 page/selected） |
| 回流 | 从反馈池把人工确认样本回流成数据集修订版或新数据集；草稿可保存/恢复 |
| 设置 | 个人模型连接（加密存 Key、能力检查）、项目资料与手册（版本化、软删恢复）、账号与外观；外部 MCP 面板在 v1.8 暂不显示 |
| 回收站 | 项目/会话/数据集/记录/手册的软删与恢复，原文件一概不动 |

人工边界（不会交给 Agent）：最终人工结论、个人凭据/API Key、目录授权、新增 MCP 工具授权、
停止服务。`full` 模式只影响“已授权范围内是否还出确认卡”，不扩大任何权限。

## Agent 工具与页面同源

助手工具（`hx_*`，当前注册表共 71 个，含 `hx_project_bind`）与页面走**同一份服务层**，没有第二套实现；查询口径一致
（反馈池“待整理”、记录字段、复核状态等）。工具清单与权限见
`backend/agent/tools.py`；动作映射的开发记录见收尾批次日志（下文索引）。

## 目录

```text
backend/
  app.py            应用工厂（配置快照、密钥、蓝图、错误封装、Agent 运行时）
  config.py         环境变量与功能开关（引擎定位：显式配置 → PATH → 未配置）
  db.py             SQLite 连接/迁移（含 Windows 只读打开探测）
  auth.py security.py crypto.py paths.py cli.py
  agent/            引擎适配器 / 网关 / MCP 桥 / 轮次运行器 / 工具 / 权限策略 / 提示词
  routes/           health meta auth admin projects sessions directories connections
                    agent assets datasets models records reviews training mcp documents service
  services/         业务服务（含 datasets/models/detection/review/records/reflow/training/…）
  vision/           vendor ultralytics 受控入口 + 受限权重反序列化（torch>=2.6 API）
  vendor/           ultralytics 8.3.163（派生，AGPL-3.0，见目录内 LICENSE）
  tests/            659 项 unittest 用例（本机受测口径：657 通过、2 跳过；真实网关/文件/轮次/训练编排/权重校验/部署接口…；详见 [v1.8 发布验收摘要](docs/v1.8发布验收摘要.md)）
frontend/           Vue 3 + TypeScript + Pinia + Vite（npm run build 后由后端同源托管）
deploy/serve.py     单端口启动器：Waitress WSGI + frontend/dist + 运行记录/停止核验
deploy/workbuddy/   WorkBuddy 云端入口：server.js（网关）+ connector.js（本机连接器）
                    + prepare.mjs（从 frontend/dist 生成 web/）+ preflight.mjs（启动前自检）
deploy/cloudflare/  自有域名 Tunnel 配置模板（真实域名、占位 UUID，不代表已上线）
测试工作区/          数据集/权重/输出骨架：给评委账号授权的业务目录（真实内容不入库）
启动海巡智检.cmd / 停止海巡智检.cmd / 准备运行环境.cmd    Windows 双击入口
启动WorkBuddy云端连接.cmd                              云端入口的本机连接器（先跑 preflight 再连）
docs/               开发日志与验收记录
scripts/            smoke/contract/engine 协议检查、deploy_smoke.py（部署启停冒烟）
```

## 环境要求（按实证）

- **Python 3.10+**（本机验证 3.14.6）。3.10 上 MCP 的 ExceptionGroup 走
  `exceptiongroup` backport（随 mcp 依赖自动安装），错误展平语义与 3.11+ 一致；
  不要用“提高版本”掩盖兼容问题，也不要升级用户原有 YOLO 环境——用项目独立 venv。
- **Node 22.6+**（本机验证 22.22.2）：`npm run test:logic` 依赖
  `--experimental-strip-types`；构建本身 18+ 也能跑，但部署建议直接用 22.6+。
  **后端服务本身不需要 Node**；需要 Node 的是两件事：构建前端，以及运行 WorkBuddy
  云端连接器 `deploy/workbuddy/connector.js`（它只读 Node 标准库，不用 `npm install`）。
- 视觉推理（可选）：`backend/requirements-vision.txt`，**torch>=2.6**
  （`safe_weight.py` 用的 `torch.serialization.get_unsafe_globals_in_checkpoint`
  在 2.6.0 才进入 `__all__`；低于它服务端显式报 `TORCH_TOO_OLD`，不会误报权重损坏，
  也绝不退回 `weights_only=False`）。torch/torchvision 按部署机 CUDA 驱动选 wheel，
  不要照搬开发机版本。

## 快速开始（Windows，双击）

1. 双击 **准备运行环境.cmd**（仅首次/更新代码后）：装后端依赖 + 构建前端。
   需要真实推理时加参数 `--with-vision` 再运行一次。
2. 双击 **启动海巡智检.cmd**：Waitress 起在 `http://127.0.0.1:8080`。
   支持含空格/中文的路径；可带参数 `--port 9000 --data-dir D:\数据`。
3. 首次进入会提示本机还没有账号，按页面指引建号授权（见下节）。
4. 双击 **停止海巡智检.cmd**：只停止“运行记录里自报身份一致”的那个实例；
   有进行中的对话/业务/训练时会明确拒绝，确认加 `--force` 才停。
   端口被陌生程序占用时只报错退出，**不杀任何进程**。

换机部署（GitHub 拉取、目录授权、Cloudflare 入口等）见 `docs/WB17-06-换机部署.md`
与 `docs/WorkBuddy固定网址与自有域名_另一台Windows部署指南.md`。

### WorkBuddy 云端入口（可选）

固定分享链接在浏览器里看到的是「云端网关 + 本机后端」，本机只需要跑连接器：

```powershell
# 1) 生成发布用的 web/（首次或前端改动后；只跑后端+连接器时不需要）
node deploy\workbuddy\prepare.mjs

# 2) 先起后端（上一步的 启动海巡智检.cmd），再双击根目录的
#    启动WorkBuddy云端连接.cmd
#    等价命令（--public 不能省，否则连接器会以退出码 2 直接退出）：
node deploy\workbuddy\connector.js --public "https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT" --upstream http://127.0.0.1:8080 --lanes 4
```

- `deploy/workbuddy/.tunnel-token` 是**私有凭据**，不入库、不打印、不重生成；换机时单独私密传输。
- 双击启动器会先跑 `preflight.mjs` 检查 Node / 令牌 / 本机后端（并核对健康响应确实是本产品），
  缺哪样报哪样、失败时退出码非 0；`HX17_DRY_RUN=1` 只自检不连云端。
- 换机场景：**Git 里的源码足够完整运行**（网关与连接器都在 `deploy/workbuddy/`，`web/` 用 prepare 生成），
  但**网站账号、模型 API Key 每台机器各自配置**，`.tunnel-token` 单独私密带过去。
- 同一个云入口**不能同时接两台不同数据库的机器**：切换时先停旧机连接器，再启新机连接器。
  现在这台正在被用户使用，**不要重启或替换它在跑的连接器**。
- 发布更新要在 WorkBuddy 里打开**原来那个应用**并核对应用 ID / 名称 / 归属账号；目录相同不代表平台自动
  认成同一个应用，管理发布仍需原账号（服务器跑连接器不需要登录）。
- 固定入口（保持原应用/URL/sharecode 不变）：
  <https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT>
- 源码仓库：`https://github.com/MEI11137/CXS-sunele-yolomm.git`，发布分支
  `workbuddy/haixun-v1.8.0`，标签 `v1.8.0`。首次拉取：
  `git clone --single-branch --branch workbuddy/haixun-v1.8.0 https://github.com/MEI11137/CXS-sunele-yolomm.git`
  拉取后执行 `git rev-parse HEAD` 记录实际提交，不要用旧快照替代。
- 详情（发布材料、限制、安全边界）见 `deploy/workbuddy/README.md`。

## 首次初始化（没有默认账号）

下面一律写成 `.venv\Scripts\python`（直接指定解释器，不依赖当前窗口是否已激活 venv）。

```powershell
.venv\Scripts\python -m backend.cli status
# 用户名只能用 3-32 位小写字母/数字/点/下划线/连字符（这是示例，不是预置账号）
.venv\Scripts\python -m backend.cli create-user --username admin_local --role admin
# 密码在交互提示中输入，不要写进命令或脚本；另一个示例：judge_local --role member

# 授权普通用户可以绑定的服务器目录（只能是具体目录，不能是磁盘根）
# 仓库里的 测试工作区\ 就是给评委准备的现成骨架（数据集/权重/输出），
# 授权这个具体目录即可；不要授权源码根目录或磁盘根。
.venv\Scripts\python -m backend.cli add-root "D:\巡检数据" --label 训练与巡检数据 --grant-to 你的用户名
.venv\Scripts\python -m backend.cli grant-root "D:\巡检数据" --username 另一个账号
```

`grant-root` 追加授权、`revoke-root` 收回；其它命令：`reset-password`、`set-status`、
`list-users`、`list-roots`、`remove-root`。

## 运行（命令行 / 开发）

```powershell
# 对外/演示（Waitress，单端口）。PowerShell 5.1 不支持 && 串联，一行一条。
.venv\Scripts\python deploy\serve.py --check          # 检查依赖、构建产物、数据目录
.venv\Scripts\python deploy\serve.py                  # 前台启动（默认 127.0.0.1:8080）
.venv\Scripts\python deploy\serve.py --status         # 运行记录 vs 端口上的服务
.venv\Scripts\python deploy\serve.py --stop           # 有进行中任务会拒绝
.venv\Scripts\python deploy\serve.py --stop --force   # 确认可中断时才用（会标记轮次中断）
```

```powershell
# 开发（前端热更新）：两个窗口各跑一条
.venv\Scripts\python backend\app.py                   # API 5080，数据目录 var\
cd frontend
npm run dev                                           # 5173，/api 代理到 5080
```

## 配置

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `HX17_HOST` / `HX17_PORT` | 监听地址/端口（启动器） | `127.0.0.1` / 8080（开发 5080） |
| `HX17_DATA_DIR` | 运行数据目录（工厂创建时固定） | `<工程>/var` |
| `HX17_SERVE_DIST` | 是否托管前端构建产物 | 启动器强制开启 |
| `HX17_COOKIE_SECURE` | Cookie 带 Secure（HTTPS 下开启） | 关闭 |
| `HX17_SESSION_HOURS` | 登录有效期（小时） | 12 |
| `HX17_ENGINE_EXE` | OpenCode 可执行文件（显式优先） | 先查 PATH 里的 `opencode`，找不到留空并在用时明确报 `ENGINE_MISSING` |
| `HX17_ENGINE_PYTHON` | 跑 stdio MCP 桥的解释器 | 当前解释器 |
| `HX17_ENGINE_VERSION` | 期望引擎版本 | 1.19.4（严格校验，不匹配拒绝启动） |
| `HX17_TURN_TIMEOUT` | 单轮最长运行时间（秒） | 900 |
| `HX17_ALLOW_FAKE_ENGINE` | 允许注入假引擎（**仅测试**） | 关闭 |
| `HX17_SERVICE_TOKEN` | 本机启停接口令牌（仅 `deploy/serve.py` 设置） | 未设置时管理接口等同不存在 |

数据目录（`HX17_DATA_DIR`，默认 `var/`）内含 `haixun.db`、`secrets/{session,connection}.key`、
`run/server.json`（运行记录：PID/端口/数据目录/令牌哈希）、引擎隔离目录——都是服务端私有区，
全部被 `.gitignore` 覆盖。**业务文件不在这里**：数据集、原图、权重、回流产物、训练输出都在
各项目绑定的授权目录（`projects.root_path`）下，所以冷备要连项目目录一起备，详见
`docs/WB17-06-换机部署.md` 第 5 节。

## 安全边界（要点）

- 口令 scrypt（不可用退 PBKDF2）；登录节流；会话 Cookie HttpOnly + 服务端会话表即时吊销；
- 写操作 `X-CSRF-Token` 双提交 + Origin 校验；`/api/health`、`/api/meta` 不泄露内部路径；
- 目录绑定 realpath 归一化 + 包含判断；授权按账号存 `root_grants`，撤权立即生效；
- 权重加载只走 `vision/safe_weight.py`（`weights_only=True` + 白名单 + vendor 校验）；
  推理只用 `backend/vendor/ultralytics`，缺件显式报错，绝不返回空框假装成功；
- Agent 引擎子进程隔离（随机端口/口令/隔离目录），工具经私有回环网关，
  令牌绑定 (用户, 项目, 会话, 连接指纹)，轮次归属每调用查库；
- 停止服务只能由启动脚本对“自报身份一致的实例”发起（instance + PID + 数据目录三处比对）；
  管理接口无令牌时 404，带 `Forwarded`/`X-Forwarded-*`/`CF-*` 等转发头一律 403。
  真正的边界是进程启动时生成的随机令牌（只写进数据目录的 `run/server.json`，不回显到
  health/meta/页面），所以**不要**把这个管理端口暴露到公网——它不是面向公网的认证方案。

## 检查与测试

`backend/requirements.txt` 只装**运行期**依赖，**不含 pytest**；回归用标准库 `unittest`，
不需要为跑测试额外装框架（机器上已有 pytest 的话也可以照用，属可选）。

```powershell
# 完整非训练回归（真实微训练用例默认跳过）
.venv\Scripts\python -m unittest discover -s backend/tests -t .

.venv\Scripts\python scripts\smoke_http.py          # 真实起服务 + Cookie/CSRF 全流程
.venv\Scripts\python scripts\contract_frontend.py   # 真实前端适配器 → 真实 HTTP
.venv\Scripts\python scripts\contract_frontend.py --with-model   # 加真实 best.pt 上传/导入/发布
.venv\Scripts\python scripts\engine_smoke.py        # 引擎进程烟测（--version/健康检查）
.venv\Scripts\python scripts\engine_protocol_integration.py   # 引擎 + 私有 MCP 桥 + 回环桩上游
.venv\Scripts\python scripts\deploy_smoke.py --python .venv\Scripts\python.exe   # 部署启停冒烟
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -Python .venv\Scripts\python.exe
```

```powershell
# 前端（在 frontend 目录，一行一条；PowerShell 5.1 不支持 &&）
cd frontend
npm run typecheck
npm run test:logic
npm run build
```

**真实训练用例**：`test_business_wb17_05_training.py::TrainingRealWorkerTest` 会真的启动一次
1 epoch CPU 微训练（约 1–3 分钟，64px 小图，无 GPU、无下载），因此默认跳过；需要时显式执行：

```powershell
$env:HX17_RUN_REAL_TRAINING_TEST = '1'
.venv\Scripts\python -m unittest backend.tests.test_business_wb17_05_training.TrainingRealWorkerTest.test_real_worker_end_to_end_registers_publishes_and_resolves_labels
Remove-Item Env:\HX17_RUN_REAL_TRAINING_TEST
```

跳过不代表训练通过；最终回归会显式列出这条排除项。

## 已知未测 / 限制（如实）

- **外部 MCP**：v1.8 只隐藏界面，未完成真实外部 MCP 服务的连接/调用验收；组件与后端扩展保留，
  不宣称“外部 MCP 可用”。
- **D4**：已确认记录直接“去回流”会进入空池，需先在反馈池加入反馈；**D7**：平台数据集从根目录
  （data.yaml + raw）导入会返回 409，需要选择 raw 子目录。
- **训练取消**：全量负载下取消训练偶发不生效，需按运行记录核对；不通过删测试掩盖。
- 付费模型质量、真实 GPU 长训练未测（零付费、无 GPU 训练）；训练闭环用注入 worker 与
  一次真实 CPU 微训练验证编排，不宣称模型效果。
- AI 核查的模型回复质量依赖用户自己的连接；上游是本机协议桩时只验证协议与落库。
- 全站审计仍有未逐项覆盖的控件/接口（原 556 行入口清单中的 UNTESTED 项）；B 组“批次筛选”
  因无真实 flow_id 未测。不要把“主要链路实测”理解为全部界面/模型效果都有保证。
- Cloudflare/公网入口需要用户自己的域名与账号，本项目不代买、不改 DNS；自有域名
  haixunzhijian.top 未验证接通，本版本不把它写成已上线。
- 部署证据与逐页独立核验（root 产出）见 `docs/workbuddy-development/WB17-06-最终验收.md`。

## 开发日志索引

- 批次记录：`docs/workbuddy-development/WB17-00.md` … `WB17-05.md`、
  `WB17-06-UI-01/02/03A/03B.md`、`WB17-06-数据集导入去重.md`、`WB17-共享缺陷收尾.md`、
  `WB17-收尾A_设置巡检复核回流.md`、`WB17-收尾批次3_Agent逐动作覆盖与单IR.md`、
  `WB17-收尾批次4_Root独立反例修正.md`、`WB17-06-最终验收.md`、`WB17-06-部署独立反例.md`
- 审查记录：`REVIEW-WB17-00-01.md` … `REVIEW-WB17-04.md`、`DEV-BRIDGE.md`

## 许可与来源

第三方组件与许可声明见 `THIRD_PARTY_NOTICES.md`。OpenCode 1.19.4（MIT，内部 Agent 执行层）
以外部可执行文件方式调用，保留其原始环境变量名；`backend/vendor/ultralytics` 为派生定制版
（8.3.163），按 **AGPL-3.0** 引用，许可证原文随代码放在 `backend/vendor/ultralytics/LICENSE`
（源码未修改，该文件是唯一新增），对外提供服务时需按 AGPL 提供对应源码。
