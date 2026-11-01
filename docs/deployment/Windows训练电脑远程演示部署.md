# 海巡智检 Windows训练电脑远程演示部署说明

适用对象：在另一台Windows训练电脑上配置环境的本人或Agent。编写日期：2026-10-30。

目标：复用现有YOLO环境和海巡智检前后端，让外地评委通过HTTPS网址使用受控演示环境。当前机器没有域名，首选Tailscale Funnel分配的`*.ts.net`网址。没有在开发电脑上注册通道、登录外部账号或发布真实外网网址。

## 1 先理解网络入口

现有`127.0.0.1:5173`是开发者本机地址，不能直接发给评委。远程入口的链路是：

```text
外地评委的普通浏览器
  → HTTPS网址 https://服务器名.网络名.ts.net
  → Tailscale Funnel
  → 训练电脑127.0.0.1:8080（受控入口：访问口令、普通账号、前端与业务API）
  → 同一进程中的Flask业务/Agent/YOLO
       └─ 127.0.0.1:8092（仅海巡引擎内部MCP回调，绝不映射到外网）
```

外部访问通道：服务器主动建立到中转服务的连接，外地浏览器通过分配的网址访问网站，不要求你具有公网IP，也不需要路由器端口映射。公网直连入口：直接把公网IP/域名指向服务器，另行配置端口映射、防火墙和TLS证书。本方案采用前一种。

Funnel是公网发布功能，不是只对VPN成员开放的Serve。评委不需要装Tailscale，但必须获得网站访问口令和指定演示账号。Funnel网址本身不是权限控制，不能只因地址难猜就认为安全。

## 2 本次代码提供什么

- `deploy/serve.py`：隔离演示环境的初始化、检查、生产启动和本机准备模式。
- `deploy/gateway.py`：前端dist与API同源，受邀访问口令，评委普通账号限制，内部接口隔离。
- `deploy/start-demo.ps1`、根目录`启动远程演示.cmd`：Windows启动入口，不自动打开外部通道。
- `deploy/requirements.txt`：只新增Waitress，不要求重装当前Torch/CUDA。
- `deploy/tests/`：临时目录与真实本机HTTP的访问控制、SSE等测试。
- `deploy/patches/hxengine-workspace-schema.patch`：兼容任务上下文绑定的新管理工具；显式要求project_id的旧工具仍禁止跨项目。

这是受控比赛演示入口，不是经过完整审计的多租户公网SaaS，也不提供操作系统级沙箱。不能将整台个人训练电脑的管理员/PowerShell能力直接交给访客。

## 3 公开模式与本机模式的明确区别

| 能力 | 外网评委入口8080 | 本机准备8091 |
|---|---|---|
| 账号 | 只允许配置中的普通演示账号 | 原有账号与权限 |
| 访问 | 访问口令＋网站账号 | 仅服务器本机地址，不开Funnel |
| 已准备模型/数据、检测、AI核查、人工复核、反馈版本 | 按原业务权限可操作 | 按原业务权限可操作 |
| 上传图片 | 支持，单次请求上限32MiB | 支持 |
| 新上传ZIP、PT、PDF/Word等 | 公开模式阻止；由团队先准备 | 支持原有入口，模型权重须来自可信来源 |
| API Key、模型连接、权限模式、MCP/目录授权、账号管理 | 不允许在外网修改；允许必要的脱敏模型读取 | 由本人预先配置 |
| 原生文件/PowerShell、管理员原始文件夹扫描 | 不提供管理员能力 | 管理员原有能力保留 |

现有`workspace_discover_inputs`和自动本机文件发现闭环属于管理员能力，普通评委账号不会获得它们。外网演示使用预登记的数据集/模型、已上传图片和业务工具完成流程；完整管理员发现文件夹的演示由本人在服务器本机运行或录屏。不应声称外网普通账号能执行所有管理员操作。

资料文件可在本机准备后，通过普通账号读取/预览。部分需要新增ZIP的版本登记操作可能需要回到本机准备模式；以实际业务响应为准。

## 4 给另一台电脑Agent的任务约束

1. 先检查系统、源码路径和现有YOLO Python环境，确认本地GPU训练仍正常。不要修改训练代码或强制重装Torch/CUDA。
2. 不覆盖队友提交、不强制推送Git，不导入开发者私有数据库/Key，不复制当前电脑的密钥。
3. 只从官方渠道安装Tailscale/海巡引擎；账号登录、首次授权及口令输入由本人完成。不要让本人把API Key或通道凭据发到聊天里。
4. 不运行`tailscale serve reset`或`funnel reset`，不覆盖现有通道；发现已有Funnel规则先核对归属。不要给路由器开放8080、8091、8092、5020或4096。
5. 本任务先做本机配置与验收，再按本人授权启用公网通道。不能把“启动成功”写成“外地已实测成功”。
6. 所有演示业务文件应在独立演示项目目录，不绑定整块盘、用户主目录或私人科研数据目录。

## 5 使用这份完整源码包

本目录是2026-10-31的完整工作树快照，已包含所有前后端、Agent、定制YOLO源码及远程部署入口，也已应用海巡引擎桥接修正。无需从GitHub拉取源码、无需叠加增量包、不要再次应用deploy/patches里的历史补丁。

把整个压缩包解压到例如E:\HaixunServer，进入解压出的源码根目录（能看到start.ps1、deploy和CXSDet-YOLOmm的目录），从下一节配置运行环境。随包包含最新dist，可直接用于远程部署；未修改前端时可跳过npm构建，开发模式仍须安装Node依赖。

源码基于feature/opencode-agent / b2c96e1aabd4897eae9f5ee464b91ff4d5224ca0，并包括截至打包时的未提交改动。没有.git历史；若后续要与队友协作，使用独立Git克隆并比对合并，不要覆盖队友目录。

包内不含个人数据库、Key、Python/Node/海巡引擎程序、node_modules、测试数据和训练权重。首次运行初始化空业务状态，上传你自己的数据/权重并配置个人API。不要复制另一人的密钥和虚拟环境。

## 6 复用现有训练环境

以下路径是示例，Agent必须替换成服务器真实路径：

```powershell
$env:HAIXUN_PYTHON = 'E:\你的训练环境\python.exe'
$env:HX_ENGINE_BIN = 'E:\你的海巡引擎目录\hxengine.exe'
& $env:HAIXUN_PYTHON -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
& $env:HX_ENGINE_BIN --version
```

海巡引擎应为本站验证的1.19.4。保持当前训练环境；后端`requirements.txt`包含历史锁定的Torch/CUDA版本，不要对已有环境盲目执行整份升级。先备份环境清单、比对缺包，由Agent按缺失依赖补齐。全新环境安装可参考仓库README。

安装新增生产服务依赖，并按需要补原Agent能力依赖：

```powershell
& $env:HAIXUN_PYTHON -m pip install -r deploy\requirements.txt
# 已安装的MCP/PDF依赖不必重复重装；按缺失情况处理requirements-agent.txt。
Set-Location CXSDet-YOLOmm\CXSDet\web-haixun
npm ci
npm run build
Set-Location ..\..\..
```

构建应保持`VITE_API_BASE`为空，让浏览器请求同源`/api`，不要把localhost:5020写进公网前端。部署入口运行打包后的dist，不运行Vite开发服务器。

## 7 安装并登录Tailscale 获取无需购买的主机网址

参考官方Windows安装入口：`https://tailscale.com/download/windows`。本人完成登录、必要的设备授权；Agent不接管验证码或同意额外权限。

PowerShell示例：

```powershell
$tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
& $tailscale status
$status = (& $tailscale status --json | ConvertFrom-Json)
$demoHost = $status.Self.DNSName.TrimEnd('.')
$publicUrl = 'https://' + $demoHost
$publicUrl
```

只展示本机DNSName和目标网址，不把包含其他设备信息的完整status JSON发到聊天或提交仓库。若DNSName为空或无权限，先由本人处理登录/MagicDNS/HTTPS/Funnel设置，不编造域名。

此时只取得设备名，还没有开启公网访问。后续客户端版本/账号策略提示以官方页面为准。

## 8 初始化独立演示数据和账号

先创建一个空素材目录，例如`E:\HaixunDemoProject`，不要选择整个E盘。运行：

```powershell
New-Item -ItemType Directory -Path E:\HaixunDemoProject -ErrorAction Stop
& $env:HAIXUN_PYTHON deploy\serve.py --init --public-url $publicUrl --username judge_demo --project-root E:\HaixunDemoProject
```

目录已经存在时先检查里面的内容，不要删除或清空。初始化只允许独立的普通演示账号，不覆盖已有账号。终端会要求本人分别输入两次：外层访问口令、网站评委账号密码。每种口令至少16字符，使用足够不同的字符；程序不把口令打印到日志。不要使用API Key作为密码。

生成的`.demo-state/server.local.json`包含访问口令的scrypt摘要和会话签名密钥，不含明文访问口令。它与演示数据库、存储、Agent密钥、缓存全部被隔离在`.demo-state`，不要上传GitHub。

初始化失败时先查看原因和现有状态，不要反复删数据库、重设账号或覆盖目录。如果公网网址以后变化，停止演示后运行：

```powershell
& $env:HAIXUN_PYTHON deploy\serve.py --set-public-url --public-url 'https://实际的新网址.ts.net'
```

## 9 先在本机准备模型和演示素材

不要启用Funnel。启动本机准备模式：

```powershell
.\启动远程演示.cmd -SetupLocal
```

只在服务器电脑浏览器打开`http://127.0.0.1:8091/`，登录刚创建的`judge_demo`，在该账号“设置 → 大模型连接”配置专用模型连接。账号API配置互相隔离，不会继承admin的连接。

该项目已由本机维护脚本绑定素材目录。通过原手动入口上传可信ZIP和PT，核对类别/模态、发布自用模型，并上传少量RGB/IR演示图片。资料需要的话也在此账号上传。先完成一次本机检测、AI核查、人工复核、反馈及修订版检查，确认文件在该项目目录内。

建议最初只准备3张图、置信度0.25；是否调用付费视觉模型由本人确定，模型必须实际支持图片输入。不要提前跑完整数据或GPU训练来代替入口验收。

结束本机准备时，待任务结束后按Ctrl+C关闭这个服务。它和远程模式共用8092，不应同时启动；脚本遇端口占用会报错而不会杀其他进程。

## 10 启动受控入口 再启用Funnel

```powershell
.\启动远程演示.cmd -Check
.\启动远程演示.cmd
```

看到`DEMO READY`后保持窗口运行。另一PowerShell检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/__demo_health
& $tailscale funnel status
```

若该电脑已经配置了其他Funnel网站，先确认归属与443端口路径，不能直接覆盖。没有其他配置且本人已授权发布时：

```powershell
& $tailscale funnel --bg --https=443 http://127.0.0.1:8080
& $tailscale funnel status
```

首次启用可能要求本人在浏览器开启HTTPS证书或Funnel权限，按官方提示处理；不能假装授权已经完成。只映射8080，绝不把8091/8092或独立海巡引擎管理端口映射出去。

Funnel输出实际`https://*.ts.net`地址后，与初始化时网址核对；不一致先更新部署配置并重启，不能关掉域名校验绕过问题。

访问方式：评委打开HTTPS链接 → 输入访问口令 → 使用judge_demo登录网站。访问口令会话持续2小时，过期刷新后重输；退出外层授权可POST `/__demo_logout`。后台任务不会因关浏览器自动取消。

## 11 真正的外地访问验收

不能仅在服务器本机打开网址就算通过。用手机关闭Wi-Fi、开启蜂窝网络，或让外地队友验证：

- HTTPS没有证书警告；未输入访问口令时不能读取页面业务或API。
- 只能登录指定普通演示账号，admin/test等账号被外部入口拒绝。
- 进入项目、刷新深层复核网址仍能打开；RGB/IR图片、原文与导出下载正常。
- 对话发送一次，能看到真实回复与工具过程；没有输出到一半被代理缓冲。
- 用已准备模型做小批检测/核查，并由人提交复核，检查生成文件和版本。
- `/api/agent/dev/...`、`/api/agent/engine/...`、目录管理等不能从外网调用。
- 大ZIP/PT等上传明确被拒绝，而不是被误认为上传成功。

记录实测日期、访问网络、实际URL、通过项和失败项。不记录密码、API Key或会话Cookie。训练性能/模型质量不是本轮网络验收结论。

不同地区、校园网出口、代理与服务商可达性存在差异，本部署包无法在当前开发电脑证明另一台电脑到外地网络的连通性。若Funnel不可达，保留同一个本机8080入口，换主办方认可且支持长连接/SSE的通道，再实测；不要把开发服务器直接暴露作临时绕过。

## 12 关闭与持续运行

比赛期间保持服务器电脑联网、接电，关闭自动睡眠；不要在任务进行中重启服务。Windows无人值守运行设置参考官方说明。`--bg`使Funnel配置可在后台保持，但不代表Python网站已作为开机服务安装；重启电脑后仍需启动远程演示进程并复查。

关闭外部入口时，先检查`funnel status`确认443确为本作品独占，再执行：

```powershell
& $tailscale funnel --https=443 off
```

不要使用reset清除其他网站。随后待任务完成，回到Python演示窗口按Ctrl+C。关闭外部通道不会自动终止正在进行的GPU训练或付费调用。

## 13 重要限制与可替代方案

- Tailscale Funnel分配主机网址，无需自购域名；没有承诺中国各网络环境的可达性、比赛期间SLA或无限资源。
- 若之后有域名，可研究Cloudflare命名隧道，继续指向本机8080。普通Cloudflare Quick Tunnel仅适合临时页面测试；官方明确它不支持SSE，而当前聊天使用事件流，不能只看首页打开就当整站通过。
- Cloudflare及其他代理可能限制单次上传大小或连接时长；本部署入口又将公开上传限制为图片、32MiB，数据集/权重应本机预置。
- 外层访问口令与普通账户是受控演示措施，不取代OS级隔离、用户配额或完整安全审计。文件/模型只用可信来源，演示账号API额度单独管理；不接受陌生人任意PT权重。
- 这只解决“外地评委能用链接进入网站”。是否必须WorkBuddy官方分享链接，仍须向组委会确认。

## 14 官方资料

查阅日期2026-10-30；安装与账号设置以实际官方提示为准。

- Funnel概念与限制：https://tailscale.com/docs/features/tailscale-funnel
- Funnel CLI：https://tailscale.com/docs/reference/tailscale-cli/funnel
- 分享本机服务：https://tailscale.com/docs/use-cases/application-testing/share-local-dev-server-with-internet
- Windows安装：https://tailscale.com/download/windows
- Windows无人值守：https://tailscale.com/docs/how-to/run-unattended
- Flask使用Waitress：https://flask.palletsprojects.com/en/stable/deploying/waitress/
- Cloudflare命名隧道：https://developers.cloudflare.com/tunnel/get-started/
- Quick Tunnel限制：https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/

## 15 交给另一台电脑Agent的执行请求

> 请按这份说明在本Windows训练电脑部署海巡智检受控远程演示。先检查现有YOLO环境，不重装Torch/CUDA；确认Git基线并合并部署包与最小桥接补丁；准备dist和Waitress；由本人登录Tailscale、输入访问口令与网站密码、配置模型Key；初始化独立普通演示账号和素材项目；先用8091本机准备模型/数据，再关闭本机准备、启动8080受控入口并检查。发现已有Funnel配置先确认归属；只在本人授权后映射8080。最后用手机流量/外地网络验证HTTPS、登录、聊天事件流、图片、人工复核和反馈，不把本机成功冒充外网成功。不要暴露管理员、8091/8092、独立海巡引擎服务、原始数据库或API Key。把实际步骤、结果及未完成项写入本机部署日志，不记录凭据。
