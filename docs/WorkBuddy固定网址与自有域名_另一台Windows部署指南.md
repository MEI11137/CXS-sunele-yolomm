# 海巡智检 v1.7：另一台 Windows 服务器部署指南

更新日期：2026-11-08。读者：负责学校另一台 Windows 电脑配置的 Agent 和项目所有者。

本指南包含两条可并行使用的公网入口：**已发布的 WorkBuddy 固定链接**，以及**用户购买的自有域名**。两条入口最终访问同一台服务器、同一份数据库与工作区。真实域名已确定为 `haixunzhijian.top`，拟用 `demo.haixunzhijian.top` 作为项目入口；审核及 DNS 接入状态须在执行时复核。真实域名专用模板已整合进仓库 `deploy/cloudflare/config.example.yml`（下文 `demo.example.com` 只在说明形状时出现，实际以该模板为准）。

**已确定的部署决策（2026-11-08）：最终后端在学校电脑，采用“自有域名 + 公网中转”，优先正式 Cloudflare Tunnel。** 不以学校电脑具有可入站公网 IP 为前提，不配置学校路由器端口转发，不要求开放入站 80/443/8080。学校电脑仅在获准的出站网络范围内主动连接中转。下文提及直连公网 IP 的方案仅为背景备选，不是本次执行步骤。

## 1. 部署目标与当前真实状态

### 已存在的 WorkBuddy 入口

<https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT>

- 已通过 WorkBuddy 原生发布 HTTP 服务，托管的是现有 v1.7 Vue 前端构建产物，以及同源 API 网关。
- 本机普通账号、公网登录、项目读写、授权目录绑定、数据集页面、真实模型回复与刷新保留均已验收。**这些是开发机上的结果，不代替新服务器验收。**
- 开发机在 `127.0.0.1:8080` 做过完整验收；**2026-11-08 上午那次检查时该端口确实未运行，这是历史记录**。同一天下午 Codex 已恢复真实 Waitress 服务（仍是同一 data-dir `var/demo-20261107/server`、V21，没有换成测试桩），本文写作时 `/api/health` 返回 `haixun-1.7-backend 1.7.0`。但**这些仍然只是开发机结果，不代替新服务器验收**。服务器电脑允许用其他端口，连接器必须同步修改 `--upstream`。
- 服务器电脑不必安装或登录 WorkBuddy。只有管理、更新云端这个应用时，需要原发布账号及原应用权限。
- 迁移后继续使用同一云端应用，不重新创建应用、不重新申请链接。**目录同名不代表平台会自动关联到原应用**：更新前必须在 WorkBuddy 里核对**应用 ID、名称与归属账号**，确认要更新的是原来那个应用，禁止顺手新建一个“看起来一样”的替代应用。
- 首次访问保留完整 `?sharecode=...` 参数。使用不带分享参数的裸深链接，可能被平台拦截。

### 自有域名入口

审核通过后，例如使用 `https://demo.你的真实域名`。建议通过正式 Cloudflare Tunnel 接到新服务器 `http://127.0.0.1:8080`，直接提供完整前端和 `/api`，无需把自有域名 CNAME 到 WorkBuddy 站点。

**固定的是入口地址，不是在线保证。** 后端、对应连接器/隧道、网络和托管应用必须正常运行；域名还需保持注册与有效 DNS。WorkBuddy 应用下线或删除会使原入口不可用。

## 2. 两条公网链路

```text
入口 A：WorkBuddy 固定链接
评委浏览器 → WorkBuddy 云端 server.js（前端 + /api 网关）
                                  ↕ 主动外连的反向通道
                   新服务器 connector.js → 127.0.0.1:8080

入口 B：用户自有域名（审核、DNS、正式 Tunnel 完成后）
评委浏览器 → https://demo.真实域名 → Cloudflare 正式 Tunnel
                                  ↕
                    新服务器 cloudflared → 127.0.0.1:8080

两个入口最终都是：真实后端 → 本机账号/个人模型连接 → 已授权项目工作区 → YOLO/GPU
```

入口 A 的 `connector.js` 与入口 B 的 `cloudflared` 是不同程序，令牌也不同。两者可以在**同一台正式服务器**同时运行。不要让同一 WorkBuddy 应用的连接器同时连向开发机和正式服务器，否则请求可能落到不同数据库。

## 3. 需要准备、传输的文件

| 材料 | 获取位置/方式 | 处理要求 |
|---|---|---|
| v1.7 主项目源码 | 仓库 `https://github.com/MEI11137/CXS-sunele-yolomm.git`，候选分支 `workbuddy/haixun-v1.7.0` | 根目录应有 `backend/`、`frontend/`、`deploy/`、`VERSION`；不要误部署旧 v1.6 子目录 |
| WorkBuddy 本机连接器 | **就在主仓库里**：`deploy/workbuddy/connector.js` | 与网关同源同版本；不要用教学问答页面的 SDK 代替，也不要在仓库外另存一份再各自修改 |
| 连接启动器 | 主仓库根目录 `启动WorkBuddy云端连接.cmd` | 相对本项目定位，默认指向固定分享链接与本机 8080；换端口用 `HX17_UPSTREAM` 或第一个参数 |
| 云端网关与页面 | 主仓库 `deploy/workbuddy/`（`server.js`、`package.json`、`web/`） | `web/` 由 `node deploy\workbuddy\prepare.mjs` 从 `frontend/dist` 生成，不随 Git 提供 |
| WorkBuddy 隧道身份文件 | `.tunnel-token` | **单独私密传输**到 `deploy/workbuddy/`；与云端现存值匹配，不重生成、不打印、不上传 GitHub |
| 本部署指南与 Agent 提示词 | 与交付文件一起携带 | 不以本机绝对路径作为另一台电脑的真实路径 |
| 数据集和权重 | 用户选定的真实业务文件 | 放进新服务器自己的业务工作区（仓库内 `测试工作区\` 只是骨架），检查 RGB/IR/标签配对和校验和 |
| 自有域名 Tunnel 客户端 | 官方 `cloudflared` Windows 版本 | 开发机现有副本在 `工作区整理\v1.7-public-access\cloudflared.exe`；也可按官方说明在服务器安装 |

**重要：网关源码与连接器已整合进主仓库 `deploy/workbuddy/`，`git clone` 之后就能拿到 `server.js`、`connector.js`、`package.json`、`prepare.mjs`、`preflight.mjs`。**

仍然**不会**由 Git 提供两样东西，且这是有意的：
1. `.tunnel-token` —— 与云端配套的私有凭据，必须**单独私密传输**到 `deploy/workbuddy/.tunnel-token`（不重生成、不贴聊天、不入库）；
2. `web/` —— 前端构建产物，`git clone` 后先在 `frontend` 里 `npm ci && npm run build`，再执行 `node deploy\workbuddy\prepare.mjs` 生成。

只跑后端 + 连接器时**不需要** `web/`；只有要重新发布 WorkBuddy 云端应用时才需要它。

编写本指南时本地主项目 HEAD 为 `f3efce2b25d1ca080bccf11c38ca4f81e094e60d` —— 这是**历史记录，不是最新提交**。本轮之后的修订（含 IR 导出再导入修正与完整 WorkBuddy 网关/连接器）由 Codex 负责通过本机现有代理提交并推送到同一分支；**本文不记录那次提交的 SHA**，因为它写于提交之前，写死一个“未来提交”的哈希只会误导。部署时必须在 `git clone` / `git pull` 之后自己执行 `git rev-parse HEAD`，记录实际拉到的提交并与 Codex 报告的提交比对；拉取失败就如实报告，不能用旧版替代后声称已部署最新版。

### 不要互相混用的凭据

| 凭据/数据 | 用途 | 换机策略 |
|---|---|---|
| WorkBuddy 登录账号 | 管理已发布的云端应用 | 跑后端和跑连接器都**不需要**登录 WorkBuddy；只有更新原应用页面时才用原发布账号 |
| `.tunnel-token` | 新服务器连接器向现有 WorkBuddy 云网关证明身份 | 与云端配套，必须私密带过去；不是模型 API Key |
| 网站管理员/评委账号 | 登录你服务器上的海巡智检 | 使用新服务器已有账号，或首次新建；不能假设开发机账号密码相同 |
| 模型 API Key、模型 ID、API 地址 | 对话和 AI 核查 | 使用服务器所有者提供的配置，按网站用户分别保存和检查 |
| `haixun.db` 与 `secrets/` | 网站运行数据及密文解密、会话签名 | 已有服务器必须保留自己的；不要用开发/QA 库覆盖 |
| Cloudflare Tunnel UUID、凭据 JSON、`cert.pem` | 自有域名公网通道 | 与 WorkBuddy 令牌无关，只由所有者授权取得，留服务器私有目录 |

不要把 `.tunnel-token`、Cloudflare 凭据、数据库、私钥、原始日志或模型 API Key 提交到 Git。复制目录时确认资源管理器显示隐藏文件；仅复制可见文件可能漏掉令牌。

## 4. Agent 开工前必须完成的只读盘点

1. 确认是 Windows，列出真实 Python、Node、Git、OpenCode 的路径和版本。
2. 查现有服务进程、8080 端口、原数据目录、已绑定工作区。已有服务先核对身份，不结束陌生进程，不重复启动同一数据库。
3. 确认本次是新部署，还是升级服务器已有实例。升级前备份服务器自己的私有运行目录和业务工作区。
4. 核对源码版本、连接器版本和私密令牌是否齐全。只检查密钥文件存在和匹配，不回显内容。
5. 检查 GPU 驱动、已有可用 YOLO/PyTorch 环境。不要为了本项目直接升级或覆盖原训练环境。
6. 向所有者确认正式目录、正式评委账号和本次允许的模型 API。用户不应在聊天中粘贴 API Key 或密码；可在本机网站/安全输入完成。
7. 自有域名审核未完成时，先完成本机与 WorkBuddy 入口，记录域名步骤待办，不伪造 DNS 成功。

后文命令使用 **Windows PowerShell 5.1**，一行一条，不使用 `&&`、`set VAR=...`。外部命令失败后要检查 `$LASTEXITCODE`，不能只依赖 `$ErrorActionPreference`。

## 5. 建议的服务器目录和代码获取

下列 D 盘路径是示例，Agent 按服务器实际情况选定并统一替换：

```powershell
$hxCodeRoot = 'D:\Apps\海巡智检v1.7'
$hxPrivateRoot = 'D:\HaixunPrivate\v1.7'
$hxWorkRoot = 'D:\HaixunWorkspace'
$hxConnectorRoot = 'D:\Apps\海巡智检云端连接'
```

- `$hxCodeRoot`：代码、项目独立 `.venv`、本机构建的前端。
- `$hxPrivateRoot`：正式数据库、密钥、运行记录、引擎隔离目录；**不能作为项目可浏览工作区授权出去**。
- `$hxWorkRoot`：用户希望评委通过 Agent 操作的业务文件，产物也留在其项目工作区内。
- `$hxConnectorRoot`：连接器、启动脚本、私密 `.tunnel-token`。

### 首次通过 Git 拉取

```powershell
git ls-remote --heads https://github.com/MEI11137/CXS-sunele-yolomm.git workbuddy/haixun-v1.7.0
# 只有网络请求成功且输出目标分支后继续；空输出或网络失败不等于已经取得代码。
git clone --single-branch --branch workbuddy/haixun-v1.7.0 https://github.com/MEI11137/CXS-sunele-yolomm.git "$hxCodeRoot"
if ($LASTEXITCODE -ne 0) { throw '源码拉取失败，请先解决网络或分支问题' }
git -C "$hxCodeRoot" rev-parse HEAD
```

### 已有仓库更新

先执行 `git status --short`、核对当前分支、备份本机数据和工作区，再在干净工作树上 `git fetch`、`git pull --ff-only`。有本机改动就先审查；不能用 `git reset --hard`、`git clean` 或整目录覆盖来解决冲突。

新建目录可以使用 `New-Item -ItemType Directory -Force`，但不得因此清空已有目录。不要复制开发机的 `.venv`、`node_modules`、测试数据库或运行中的 `run/server.json`。

## 6. 运行环境、引擎与前端构建

建议在项目独立环境部署；Python 需 3.10+。Node 建议 22.6+（≥18 是硬要求），它的用途是**构建前端**和**跑 WorkBuddy 云端连接器**；后端服务本身不需要 Node。先 `py -0p` 与实际解释器 `--version`，防止误用 Windows Store 的 `python.exe` 别名。不要照搬开发机 AppData 路径。

```powershell
Set-Location -LiteralPath $hxCodeRoot
py -3 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败' }
$hxPython = Join-Path $hxCodeRoot '.venv\Scripts\python.exe'
& $hxPython -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw '基础依赖安装失败' }
Set-Location -LiteralPath (Join-Path $hxCodeRoot 'frontend')
npm ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
npm run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
Set-Location -LiteralPath $hxCodeRoot
```

已有 `.venv` 时先检查，避免重建覆盖。使用 `py -3` 前确认其选择的 Python 版本满足依赖；如需指定版本/解释器，换为盘点得到的真实路径。

OpenCode 引擎当前版本闸门为 **1.19.4**。使用实际安装路径设置环境变量，不修改版本闸门或启用假引擎蒙混启动：

```powershell
$env:HX17_ENGINE_EXE = 'D:\Tools\OpenCode\opencode.exe' # 替换成实际安装路径
if (!(Test-Path -LiteralPath $env:HX17_ENGINE_EXE)) { throw 'OpenCode 路径不存在' }
& $env:HX17_ENGINE_EXE --version
```

`setx` 只影响以后新开的进程，不能代替当前窗口的 `$env:...`。本项目不自动读取任意 `.env` 文件；不要只写 `.env` 就声称配置生效。

### YOLO / GPU

先检查现有驱动、CUDA 与训练环境，再决定项目环境的 torch/torchvision 安装方式，随后按 `backend/requirements-vision.txt` 补依赖。主项目旧部署说明包含 grad-cam、albumentations、双模态依赖的实测经验，可作为排错参考；**不能机械照抄某一 CUDA wheel 或全局升级原训练环境**。

在实际后端将使用的解释器中检查，而不是只在另一个 Conda 环境检查：

```powershell
& $hxPython -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
& $hxPython -c "import cv2, yaml, PIL; print('basic vision imports OK')"
```

后台服务、检测及训练的解释器/依赖是否一致，要用真实日志与作业结果确认。网页能打开不代表 CUDA、权重导入或训练可用。

## 7. 同一个私有数据目录下建号、授权与启动

**CLI 建号、目录授权、启动后端三者必须使用同一个 `HX17_DATA_DIR`。** 默认目录与旧实例不一致会产生“创建了账号却登录不了”的假象。

```powershell
Set-Location -LiteralPath $hxCodeRoot
$env:HX17_DATA_DIR = $hxPrivateRoot
& $hxPython -m backend.cli status
& $hxPython -m backend.cli list-users
& $hxPython -m backend.cli list-roots
```

新部署且尚无正式账号时，交互创建；已有账号则沿用，不擅自重置其密码：

```powershell
& $hxPython -m backend.cli create-user --username admin_server --role admin
& $hxPython -m backend.cli create-user --username judge_server --role member
# 以上是示例用户名，不是系统预置账号；密码从本机安全交互输入。
```

创建业务目录并授权给实际评委账号。若目录已登记，使用 `grant-root`；不要因重复登记报错就扩大授权范围：

```powershell
New-Item -ItemType Directory -Force -Path $hxWorkRoot | Out-Null
& $hxPython -m backend.cli add-root "$hxWorkRoot" --label '评委巡检工作区' --grant-to judge_server
# 目录已有时改用下一条，不要把两种情况盲目都执行：
# & $hxPython -m backend.cli grant-root "$hxWorkRoot" --username judge_server
```

普通账号在网页里新建项目，绑定自己获授的服务器目录。这里选择的是**服务器磁盘路径**，不是评委浏览器电脑的本地磁盘。不要把整个 C/D 盘、用户目录或私有运行目录授权出去。

### 后端前台启动（窗口 A）

```powershell
Set-Location -LiteralPath $hxCodeRoot
$env:HX17_DATA_DIR = $hxPrivateRoot
$env:HX17_COOKIE_SECURE = '1'
$env:HX17_ALLOW_FAKE_ENGINE = '0'
# HX17_ENGINE_EXE 在这个窗口中也必须正确。
& $hxPython deploy\serve.py --check --data-dir "$hxPrivateRoot"
if ($LASTEXITCODE -ne 0) { throw '后端环境检查失败' }
& $hxPython deploy\serve.py start --host 127.0.0.1 --port 8080 --data-dir "$hxPrivateRoot"
```

保持该窗口运行。另开 PowerShell 执行：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/health'
Invoke-RestMethod 'http://127.0.0.1:8080/api/meta'
```

期望健康信息为 v1.7 对应服务，`serve_dist=true`，`initialized=true`。后端仍只绑定回环地址，公网由已配置入口进入。启用 Secure Cookie 后，通过公网 HTTPS 登录；本机 HTTP 健康检查不受影响，不应因为本机 HTTP 登录问题就关闭公网保护。

## 8. 每个网站账号配置自己的模型 API

1. 用新服务器的实际账号登录，而非使用开发机旧测试凭据。
2. 进入设置中的个人模型连接，填写新服务器所有者提供的 API 地址、API Key、真实模型 ID。
3. 检查所选模型与保存的 ID 是否一致；显示名称不等于真实模型标识。
4. 明确授权后执行“检查连接与能力”；该操作会真实调用 API 并可能计费。文字、工具调用、看图能力必须分别以实际结果为准。
5. 需要 AI 看图核查时，选实测支持图片输入的模型；不能仅根据“原生多模态”的宣传或名称放行。
6. 普通评委账号也必须检查自己的连接。管理员已配置，不代表每个普通账号自动继承；OpenCode 运行时可执行文件也不等于网站个人连接。
7. 不复制开发机 `secrets/connection.key` 去“修复”新服务器 API。新服务器自己的密钥与本机数据库密文必须配套。

不要把 API Key 发回聊天、写进公网前端或 Git；日志/验收报告只记录模型 ID、状态和必要的去敏错误码。

## 9. WorkBuddy 固定入口：连接器迁移与切换

### 切换前

- 后端先在新服务器本机通过健康、账号和基础依赖检查。
- 连接器与启动器就在主仓库：`deploy\workbuddy\connector.js`、根目录 `启动WorkBuddy云端连接.cmd`；把私密传输来的 `.tunnel-token` 放进 `deploy\workbuddy\`。
- Node 在运行连接器的 Windows 用户 PATH 中可见（或用 `HX17_NODE` 指定完整路径，启动器优先用它）。连接器只用 Node 标准库，无需 `npm install`。
- **运行连接器不需要 WorkBuddy 登录**；WorkBuddy 账号只在“管理/重新发布已上线的那个应用”时才需要。
- 检查是否存在环境变量 `HX17_TUNNEL_TOKEN`；它优先于文件，旧环境变量可能覆盖正确文件。不要打印值，只核对来源。
- 记录旧机连接器 PID/启动方式，安排没有进行中作业的切换时间；不要按名称杀掉所有 Node/Python。
- **开发机上的连接器现在正被使用**（用户在公网测试）：不要重启、替换或抢占它。切换只在约定的空档进行，先停旧再起新。
- Git 里的源码已经能完整换机运行（`deploy/workbuddy/` 带网关与连接器，`web/` 用 prepare 生成）；但**网站账号与模型 API Key 是每台机器各自配置**，不能拿开发机账号库/密钥覆盖新机，`.tunnel-token` 单独私密带过去。同一个云入口不能同时接两台不同数据库的机器。

### 切换顺序

1. **先停止旧电脑指向此 WorkBuddy 应用的连接器**。不需要删除应用，也不需要强停旧机后台任务。
2. 在新电脑窗口 B 启动连接器，`--upstream` 指向新电脑本机后端。
3. 通过新账号与新项目的独有标记确认公网请求真的到了新机。`/health` 版本相同并不能证明机器已切换。
4. 通过全部验收后保留新机连接器，禁止旧机连接器又被自动启动。

窗口 B 的命令：

```powershell
$hxRoot = 'D:\Apps\haixun-v1.7'   # 实际 clone 位置，含中文/空格也可以
Set-Location -LiteralPath $hxRoot
if (!(Test-Path -LiteralPath '.\deploy\workbuddy\connector.js')) { throw '缺少连接器' }
if (!(Test-Path -LiteralPath '.\deploy\workbuddy\.tunnel-token')) { throw '缺少私密传输的配套令牌' }
node --check .\deploy\workbuddy\connector.js
if ($LASTEXITCODE -ne 0) { throw '连接器语法检查失败' }
node .\deploy\workbuddy\preflight.mjs --upstream 'http://127.0.0.1:8080'
if ($LASTEXITCODE -ne 0) { throw '启动前检查未通过（Node / 令牌 / 本机后端）' }
node .\deploy\workbuddy\connector.js --check --public 'https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT' --upstream 'http://127.0.0.1:8080'
node .\deploy\workbuddy\connector.js --public 'https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT' --upstream 'http://127.0.0.1:8080' --lanes 4
```

双击根目录的 `启动WorkBuddy云端连接.cmd` 等价于最后一段（它先跑 `preflight.mjs`，通过后才启动连接器）；
`set HX17_DRY_RUN=1` 再双击则只自检、不向云端发起任何连接，适合排错。

**当前 `--check` 仅是辅助检查：其 `selfCheck` 对可解析的错误 JSON 未严格检查所有 HTTP 状态，退出 0 不能独立证明令牌正确。** 必须看持续连接是否出现 `tunnel token rejected`，并完成真实公网登录与业务请求。这里如实记录现状，不修改保护来让检查变绿。

不能通过“在另一账号重新发布一个相似页面”“把自己的域名 CNAME 到 WorkBuddy 域名”“删除原应用重建”来完成迁移。

## 10. 自有域名公网入口：审核完成后的配置

本节是部署方案与模板，**目前没有真实域名/DNS/正式 Tunnel 验收结果**。用户完成审核并提供真实域名后，Agent 再执行授权范围内的配置。

### 10.1 域名不是服务器

购买域名后，还需要有效 DNS、HTTPS 和到服务器的访问通道。建议使用子域名，例如 `demo.你的域名`。默认采用“正式命名 Tunnel”，不用每次重启就变地址的 `trycloudflare.com` 临时入口。

采用 Cloudflare 完整 DNS 接入方式时，先在 Cloudflare 添加域名，再在注册商配置指定 NS，等待站点 Active。**修改 NS 前盘点并保留原有 A/AAAA/CNAME/MX/TXT，避免影响已有网站、邮箱与域名验证；不要整区清空。** 域名审核、登录授权、DNS 所有者操作由用户在本机安全完成，不向聊天发送 Token。[官方建隧道说明](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)

如果用户已有其他成熟公网接入方式，Agent 先核对现状，不强制迁移 DNS。若采用公网 IP + 反向代理，则必须实际具备可达 IP/端口、TLS 证书和代理配置，不能把内网 IP 写成公网 A 记录。

### 10.2 本地管理的正式 Cloudflare Tunnel

以下使用已由官方渠道安装的 `cloudflared.exe`，用户名、路径和 Tunnel 名按实际情况确定。需要的通常是出站连通性；不要为了隧道开放数据库或后端管理接口的入站端口。[官方先决条件与连通性](https://developers.cloudflare.com/tunnel/get-started/)

```powershell
$hxCloudflared = 'D:\Tools\cloudflared\cloudflared.exe'
& $hxCloudflared --version
& $hxCloudflared tunnel login
# 用户在浏览器授权正确的 Cloudflare 域名；Agent 不获取聊天中的账号密码。
& $hxCloudflared tunnel list
# 仅在没有可复用的目标 Tunnel 时创建，避免重复：
& $hxCloudflared tunnel create haixun-production
```

记录返回的 UUID 和凭据 JSON 的实际路径，存进服务器私有配置，不把这些文件放进项目可浏览目录。若创建/读取凭据的命令可能显示秘密，应在安全本机终端处理并对记录去敏。

真实域名模板只有一个，在主仓库里：`deploy\cloudflare\config.example.yml`。它已经写好
`haixunzhijian.top` 与 `demo.haixunzhijian.top` 两个 Host、本机 `http://127.0.0.1:8080`、
与浏览器地址一致的 `httpHostHeader`、以及 `/api/service` 的 403 拦截；Tunnel UUID 与凭据路径仍是占位符。
**以那个文件为准**，本节的 yaml 只是说明形状；`docs\cloudflared-config.example.yml` 已经改成指向它的说明，
不再保留第二份可执行模板。

```yaml
tunnel: REPLACE_WITH_TUNNEL_UUID
credentials-file: REPLACE_WITH_ABSOLUTE_PATH_TO_TUNNEL_CREDENTIALS_JSON
ingress:
  - hostname: demo.haixunzhijian.top
    path: '^/api/service(/.*)?$'
    service: http_status:403
  - hostname: demo.haixunzhijian.top
    service: http://127.0.0.1:8080
    originRequest:
      httpHostHeader: demo.haixunzhijian.top
  - service: http_status:404
```

> 域名现状（2026-11-08）：`haixunzhijian.top` 的 NS 已在阿里云改为
> `aaron.ns.cloudflare.com` / `kia.ns.cloudflare.com`，**Zone 是否 Active 尚未核实**；
> Tunnel 还没有创建。因此自有域名入口目前**不算上线**，本节所有结果以实际验证为准。

首条是显式管理路由拦截，末条是必须的兜底规则；不要只依赖路径正则作为唯一权限边界。[官方 ingress 配置](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)

`httpHostHeader` 必须是实际公网主机名，与浏览器的 Origin 对应，不能写成 `127.0.0.1:8080`。后端仍执行 CSRF 双提交和来源校验；不要关闭它们。`httpHostHeader` 的官方含义是设置转发给本地服务的 Host。[官方源站参数](https://developers.cloudflare.com/tunnel/reference/origin-parameters/)

在实际配置文件上校验、建 DNS、启动：

```powershell
$hxTunnelConfig = 'D:\HaixunPrivate\cloudflared\config.yml'
$hxPublicHost = 'demo.example.com' # 必须先替换为用户真实子域名
if ($hxPublicHost -eq 'demo.example.com') { throw '尚未填写真实域名，不能执行 DNS 配置' }
& $hxCloudflared tunnel --config "$hxTunnelConfig" ingress validate
if ($LASTEXITCODE -ne 0) { throw 'Tunnel ingress 校验失败' }
& $hxCloudflared tunnel --config "$hxTunnelConfig" ingress rule "https://$hxPublicHost/api/service/status"
# 确认命中 403 规则后再配置 DNS；已有同名记录先审查，不强制覆盖。
& $hxCloudflared tunnel route dns haixun-production "$hxPublicHost"
if ($LASTEXITCODE -ne 0) { throw 'DNS 路由创建失败' }
& $hxCloudflared tunnel --config "$hxTunnelConfig" run haixun-production
```

DNS 指向命名 Tunnel 的 `<UUID>.cfargotunnel.com`，实际访问仍依赖隧道在线。仅 DNS 成功不能宣称服务上线。[官方 DNS 路由说明](https://developers.cloudflare.com/tunnel/concepts/routing/)

如果服务器已经使用仪表盘管理的 Tunnel，就沿用其路由与 Token 管理方式，不把两种模式的配置文件/Token 命令混用。

### 10.3 自有域名与 WorkBuddy 链接怎样共存

- 自有域名直接转到完整后端；后端同时托管本地构建的 `frontend/dist`，因此也有相同界面。
- 不修改 WorkBuddy 云网关固定的 `HX17_PUBLIC_ORIGIN` 来适配另一个入口；两条入口分别保持自己的 Host/Origin 同源关系。
- 不把自有域名浏览器里的前端 API 地址改成 WorkBuddy 域名，也不把 WorkBuddy 前端 API 地址改成自有域名；两端都沿用相对 `/api`。
- 两个域名的 Cookie 不共享，各自登录是正常现象；后台账号、项目和数据仍由同一服务器数据库决定。
- 同一服务器上可以同时运行 WorkBuddy 连接器和正式 `cloudflared`；不要让同一生产 Tunnel 的另一台旧服务器同时提供不同数据。
- 自有域名入口不是 WorkBuddy 发布地址的别名；提交材料里应如实标注两者用途，不把自有域名伪称 WorkBuddy 原生链接。

## 11. 公网接口与前后端请求规则

### 学校电脑部署的执行边界

- 学校电脑只跑真实后端、GPU 作业和出站连接器；Cloudflare 云端负责自有域名公网中转，现有 WorkBuddy 网关负责另一条入口。
- 先完成域名账号、Zone、实际 NS 与 DNS 接管；这些可以现在在管理电脑准备。正式 Tunnel 的最终常驻运行位置应是学校电脑，不要把开发机当前运行状态当成学校部署完成。
- Cloudflare Tunnel 需要到其网络的出站连通性，官方要求核对 `7844` 端口。UDP 不可用时可在获准的 TCP 7844 可用条件下试用 `--protocol http2`；不能声称只要普通 HTTPS 443 可用就保证 Tunnel 可用。
- 学校不允许或无法连接所选中转时，记录具体失败、向网络管理员核实允许方式，再考虑用户授权的其他中转。不擅改校园网设备、不关闭防火墙或 TLS 校验来强行连通。
- 现有 WorkBuddy 连接器通过 HTTPS 主动外连，可单独验收。自有域名通道不通不能伪称已由 WorkBuddy 自动接管自有域名，也不随意将域名 CNAME 到 WorkBuddy 域名。
- 原有前后端 `/api`、登录、普通账号权限和人工复核保持原样；中转不等于新增远程桌面或任意系统命令权限。
- 正式验收需要学校电脑保持运行。中转解决网络入口，不替代学校电脑的 GPU、数据、后端或模型 API。

出站连通性依据：[Cloudflare Tunnel 官方先决条件](https://developers.cloudflare.com/tunnel/get-started/)。

两条入口都保留项目已有 API，不需要新造业务接口：

| 入口 | 网页地址 | 同源 API 根 |
|---|---|---|
| WorkBuddy | 完整分享链接 | `https://haixun-inspection.app.workbuddy.host/api`，先通过分享链接取得平台 Cookie |
| 自有域名 | `https://demo.真实域名` | `https://demo.真实域名/api` |
| 服务器本机自检 | `http://127.0.0.1:8080` | `http://127.0.0.1:8080/api` |

| 接口 | 方法 | 作用与预期 |
|---|---|---|
| `/api/health` | GET | 匿名存活/版本检查，不回传私有路径或密钥 |
| `/api/meta` | GET | 功能、初始化和引擎版本元信息；可建立 CSRF Cookie |
| `/api/auth/login` | POST JSON | 使用该服务器账号登录；不要把密码放 URL/终端命令历史 |
| `/api/auth/me` | GET | 登录态校验；未登录应为 401，不能返回另一个用户 |
| `/api/projects` | GET / POST | 当前账号项目列表/创建，写请求必须合法 CSRF 与来源 |
| `/api/projects/{projectId}/datasets` | GET | 当前账号有权访问的项目数据集 |
| `/api/agent/capabilities` | GET | 当前账号工具与个人模型连接状态，不应返回原始 API Key |
| `/api/sessions/{sessionId}/turns` | POST | 发起真实 Agent 轮次；按当前源码要求提供内容和幂等 request_id |
| `/api/turns/{turnId}` | GET | 查询真实轮次状态；页面与后端事件/轮询共同续拉 |
| `/api/service/**` | 本机管理专用 | **公网必须拒绝**，不得向前端/第三方发服务管理令牌 |

业务 API 的完整字段以本次拉取版本的 `backend/routes/` 和 `frontend/src/api/` 为准，不凭本表猜参数。上传、下载、双模态图像和训练管理继续复用这些接口。

### 登录与 CSRF

浏览器先 GET 元信息，保留 Cookie，再通过本站前端登录。登录成功会轮换 CSRF Cookie；写请求使用**当前 Cookie**对应的 `X-CSRF-Token`，保留 HttpOnly 会话 Cookie。浏览器同源请求自动产生正确 Origin，不能用关闭 CSRF、允许所有 CORS 或把任意来源改写成本站来修登录。

WorkBuddy 平台边缘曾实测改写 Cookie SameSite 属性，因此必须保留网关来源校验与后端双提交；不能仅依赖 SameSite。自有域名也要独立验收 Cookie、HTTPS 和来源关系。

调用这些接口不代表具有任意 Windows 命令或任意目录权限。账号、项目归属和目录授权在后端仍有效；人工最终复核按产品规则保留。

## 12. 验收清单：每一项单独记录结果

### 基础与来源身份

- [ ] 记录代码 SHA、版本、真实解释器/引擎版本、后端 PID/端口、私有数据目录和工作区；不记录秘密。
- [ ] 本机 `/api/health`、`/api/meta` 正常，后端不是假引擎，GPU 在实际解释器可见。
- [ ] 旧机 WorkBuddy 连接器已停止，新机持续运行；不是两个数据库混接。
- [ ] WorkBuddy 公网健康成功；使用新机独有的验收项目/文件证明请求到达新机。
- [ ] 自有域名完成审核、DNS 与 HTTPS 后，另测相同链路；未完成项写“待完成”。

### 普通评委账号，不只测管理员

- [ ] 真实浏览器无开发者登录态打开完整分享链接，普通账号登录成功。
- [ ] 能新建/读取自己的项目，绑定获授的服务器目录，看不到未获授目录。
- [ ] 手工上传数据集/权重功能保留；验证真实文件被保存、界面状态与后台一致。
- [ ] 对话“请只查询当前项目的数据集与模型，不修改文件”，得到工具实际结果与最终回复；刷新后历史仍在。
- [ ] 图片/文件预览、下载、CSV 导出、深层页面刷新和对话停靠正常。
- [ ] 允许使用付费 API 后，只取明确范围的小批量，例如 3 张/置信度 0.25，跑检测与 AI 核查；记录所用实际模型 ID，不自动扩大范围。
- [ ] RGB 和 IR 各自在原图坐标下编辑，缩放不改坐标；仅 IR 可见缺陷不能直接判断误报。
- [ ] 人工提交后，回原修订/另建数据集的真实图片、标签、划分与来源可追溯；不只看成功提示。
- [ ] 训练不是发布入口的前提。只有用户明确授权训练范围后才跑微型真实训练，验证实际进程/产物；不拿“已提交任务”冒充训练成功。

### 权限、连通与故障恢复

- [ ] 未登录业务接口返回 401；无权项目不泄露其他账号数据。
- [ ] 跨站 Origin、缺 CSRF 的写请求被拒；不能只验收正确请求。
- [ ] 公网 `/api/service/status` 等管理路径返回 403/404，编码变形也不能获得管理能力。
- [ ] 断开新机连接器后，WorkBuddy 入口明确提示后端未连接；重新连上恢复，不重复执行旧写入。
- [ ] 网页不会因平台更新/重连永久卡住；真实回答保存完成，滚动与停止行为正常。
- [ ] 两条入口都经过真实外网浏览器/另一网络验证，而非只在服务器补一个 Origin 头发 HTTP 请求。

传输上限要如实记录：WorkBuddy 网关代码对齐 512 MiB，上次仅实测到 64 MB，并未证明 512 MiB 全程可用；自有域名还受实际 CDN/代理套餐的请求大小限制。大权重/数据集可先放授权工作区，再由内嵌 Agent 登记导入，不能用调大本地变量冒充突破平台限制。

## 13. 日常启动、升级与回退

### 日常启动顺序

1. 后端读取选定的正式数据目录并健康。
2. 启动本机 WorkBuddy 连接器。
3. 如果使用自有域名，再启动该服务器的正式 `cloudflared`。
4. 两个公网入口分别验证；常驻运行期间保证机器不睡眠、网络和 API 额度可用。

前台验证通过后，Agent 可按用户授权配置任务计划程序/专用服务账号，使后端、连接器、Tunnel 常驻并保存去敏日志。任务使用绝对路径、明确工作目录和同一数据目录/解释器；设置防重复启动和有限重启策略。不能把一次后台启动当作开机自启验收完成。

Windows 服务的运行用户与交互用户不同，Cloudflare 凭据路径、OpenCode 路径和目录权限必须实测。官方服务安装说明见 [Windows 服务运行](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/windows/)。不要盲目覆盖已经存在的 cloudflared 服务、注册表配置或其他项目的任务。

### 更新

备份本机数据库/密钥与业务工作区 → 等待真实任务结束 → 正常停止本实例 → 更新经过审查的代码 → 用本机环境重新构建 → 使用原数据目录启动 → 验收。业务源码/前端变更如果需要同步到 WorkBuddy，还必须通过原账号**更新原云端应用**，本机 `npm run build` 不会自动更新线上。

停止后端时，显式传入同一私有数据目录：

```powershell
& $hxPython deploy\serve.py --status --data-dir "$hxPrivateRoot"
& $hxPython deploy\serve.py --stop --data-dir "$hxPrivateRoot"
```

有活动任务或状态未知时正常停止会拒绝。不要默认 `--force`，不要按 `python.exe`/`node.exe` 名字批量杀进程。

### 切换失败回退

先停新机 WorkBuddy 连接器，再启旧机连接器；不要同时保留两者。若已在新机产生新业务数据，回旧机前明确数据差异，不能假设两台数据库自动同步。自有域名切回旧 Tunnel/服务前也要核实数据一致性并保留旧 DNS 配置。回退不需要删除 WorkBuddy 原应用。

## 14. 常见问题定位

| 现象 | 优先核查 |
|---|---|
| 页面正常，API 503 `TUNNEL_OFFLINE` | 新机连接器是否运行、访问云端是否正常、是否有被环境变量覆盖的错误令牌 |
| 连接器 `tunnel token rejected` | 文件与云端是否同一套；有没有漏拷隐藏文件、额外旧环境变量；不要重新随机生成 |
| WorkBuddy 403“链接不完整” | 首次是否用了含 sharecode 的完整链接，是否绕开平台入口直接打开深链接 |
| `AUTH_INVALID` / 登录 401 | 当前服务数据目录与本机账号是否一致；不要擅自重置现有账号或换成开发机测试库 |
| `CSRF_ORIGIN` / `ORIGIN_REJECTED` | 自有域名 Host 是否保留真实公网主机名；WorkBuddy 是否仍是精确平台域名；不要关闭防护 |
| `CSRF_FAILED` | 登录后是否沿用旧 CSRF；Cookie 是否被接受、是否同域、是否用 HTTPS |
| 能登录但不能发消息 | 个人模型连接、项目绑定目录、目录授权、引擎版本分别检查，不只看管理员配置 |
| 对话 401/402/429 | 模型 API 身份、余额、限额、真实模型 ID，和网站登录状态是不同层次 |
| 数据集/权重看不到 | 文件是否实际在新服务器、项目目录是否正确、账号是否获授、是否只是旧机路径 |
| 自有域名无法解析/证书未就绪 | 审核、NS、站点 Active、DNS 记录和证书状态；不要绕过浏览器证书警告 |
| 自有域名 502/1033 等上游/隧道错误 | 按实际错误码核查 cloudflared 连接、目标服务、Host 和端口；DNS 存在不代表 Tunnel 正常 |
| 上传 413 | 分别核对 CDN/平台、网关和后端限制；大文件走授权工作区导入 |
| 更新后页面仍旧版 | 原 WorkBuddy 应用是否执行了更新发布，不能只更新本机 dist |
| 刷新/请求结果忽然变另一套数据 | 检查旧机连接器或同 Tunnel 的另一台服务器是否仍运行 |

## 15. Agent 最终交接报告

在服务器本地保存去敏的部署报告，包含：

1. 仓库、分支、实际提交 SHA；未提交改动与远端不可达等情况如实列出。
2. 四类目录、后端 PID/端口、解释器、引擎/GPU版本；不得包含口令、原始 API Key 或令牌。
3. WorkBuddy 原分享链接是否保持不变，新机独有标记如何证实切换。
4. 自有域名实际主机名、DNS/Tunnel 状态、HTTPS与接口验收；域名未审核就标待完成。
5. 普通账号测试结果、实际模型 ID 与能力、检测/核查/训练哪些做了、哪些没做。
6. 启停与常驻运行方式、日志位置、回退步骤、剩余问题。

部署完成后，给用户实际链接和操作入口，而不是只说环境安装完成。不要承诺“所有功能全通过”，除非逐项保留了真实验收证据。
