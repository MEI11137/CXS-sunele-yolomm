# 海巡智检 v1.7

光伏（RGB+IR）巡检与数据质量闭环系统。当前版本 **1.7.0**，阶段 **WB17-02：网站内嵌 OpenCode Agent 与真实工具交互**。

本目录是从空白工程重新开发的 v1.7 工程。v1.6 仅作为只读功能参考，不复制其源码目录；
契约（接口形态、权限规则、工具语义）可以参考，代码按本工程结构重新实现。

## 现状（重要）

WB17-01 + WB17-02 已实现并可实际使用：

- 账号：本机 CLI 建号、登录/登出/当前用户、改密、管理员建普通账号与启停；**无注册入口、无默认账号**；
- 项目与会话：CRUD、软删除/恢复、真实消息历史；每个项目与会话都按 owner 隔离；
- 目录：管理员登记授权根目录，并**按账号授予**；普通用户只能看到/浏览/绑定自己获授的目录，
  撤销授予后已绑定的项目立即被拒绝；新增根目录默认不共享；
- 个人模型连接：按用户加密保存，Key 只回显掩码；可显式“检查连接”实测认证 / chat / 视觉 / 工具；
  证据只保留状态、耗时、环节与固定说明，上游正文（可能回显凭据）不落库、不回显；
- 设置页：连接配置、能力状态、改密、深浅色主题。
- **Agent 运行时（WB17-02）**：会话页的「海巡助手」由真实 OpenCode 1.19.4 引擎驱动（隐藏窗口、只绑 127.0.0.1、
  随机口令、每个用户+会话+连接指纹一个隔离运行目录）。模型循环与工具调用由引擎负责，本工程提供：
  - 私有 stdio MCP 工具桥（绝对脚本路径 + `-u` 启动，不依赖 `PYTHONPATH`）+ 独立回环网关
    （会话级 32 字节随机令牌，工具里没有 `user_id`）；
  - 真实工具：项目/会话查询、受控文件列表/搜索/分页读取/创建/修改/移动/删除（限绑定目录、删除进回收站）、
    界面跳转、提问、任务状态；**没有 SQL、没有命令执行、没有目录授权能力**；
  - 权限模式真实接通：`request` 写入出确认卡、`assist` 常规写入自动/高风险确认、`full` 授权范围内直接执行并审计，
    `full` 不扩大任何边界；
  - 轮次/事件/动作表与状态机：每会话单活动轮次、`request_id` 幂等、事件按 `seq` 增量续拉、
    刷新或关侧栏都不中断后台任务，stop 以引擎真实状态为准；工具调用按 `call_id` 在数据库里
    **原子认领**，并发同 ID 只执行一次，同 ID 换参数直接冲突；
  - 助手回复用 marked 解析 + DOMPurify 净化后渲染，工具详情默认收起。

仍未实现（界面会明确标注，不会伪造结果）：

- 文件发现、图片检测、AI 核查 —— WB17-03；人工复核与数据回流 —— WB17-04；
- 数据集、模型、训练 —— WB17-05；受控命令执行 —— WB17-05；公网 HTTPS 与正式部署 —— WB17-06。
- 本轮**没有调用任何真实付费模型**：运行时用真实引擎 + 本地 stub 模型与假引擎用例验证，
  真实模型的工具调用质量未验收。

## 目录

```text
backend/
  app.py            应用工厂（配置快照、密钥加载、蓝图注册、错误封装、Agent 运行时启动）
  config.py         环境变量解析与功能开关
  db.py             SQLite 连接生命周期与迁移（含建连可写性探测）
  auth.py           登录态、CSRF、权限装饰器
  security.py       口令哈希（scrypt/PBKDF2）、签名令牌、掩码
  crypto.py         API Key 的 AES-256-GCM 加解密
  paths.py          目录边界校验（盘符 / UNC / .. / 链接 / 联接）
  cli.py            本机管理命令行
  agent/            引擎适配器 / 网关 / MCP 桥 / 轮次运行器 / 工具 / 权限策略 / 系统提示
  routes/           health / meta / auth / admin / projects / sessions / directories / connections / agent
  services/         users / projects / sessions / connections / roots / audit
  tests/            238 项 unittest（含 Agent 网关/文件/轮次/生命周期用例与假引擎）
frontend/           Vue 3 + TypeScript + Pinia + Vite
deploy/serve.py     生产同源启动器（后端托管 frontend/dist）
docs/               开发文档，含 WB17-00 / WB17-01 / WB17-02 记录
scripts/            check.ps1、smoke_http.py、tool_roundtrip.py、engine_smoke.py、
                    mcp_bridge_handshake.py、engine_protocol_integration.py
```

## 环境

- Python 3.10+（本机验证：3.14.6，Flask 3.1.3，cryptography 50.0.2）
- Node 20+（本机验证：22.2.0，npm 10.7.0）

不重装 Torch/CUDA，不安装训练相关大包；vendor YOLO 在后续阶段以第三方组件方式引入。

## 首次初始化（没有默认账号）

```powershell
# 在工程根目录；密码在提示符里输入，或从 stdin 读入，不要写在命令行
python -m backend.cli status
python -m backend.cli create-user --username 你的名字 --role admin

# 授权普通用户可以绑定的服务器目录（只能是具体目录，不能是磁盘根）
python -m backend.cli add-root "D:\巡检数据" --label 训练与巡检数据 --grant-to 你的用户名

# 之后给别的账号追加 / 收回同一目录
python -m backend.cli grant-root "D:\巡检数据" --username 另一个账号
python -m backend.cli revoke-root "D:\巡检数据" --username 另一个账号
```

其它命令：`reset-password`、`set-status`、`list-users`、`list-roots`、`remove-root`。
`add-root` 不带 `--grant-to` 时只是登记目录（默认不共享）；管理员页面也能逐条授予/收回。
账号密码不会出现在命令行参数里，也不会写进 shell 历史。

## 运行（开发）

```powershell
python backend/app.py          # 默认 5080，数据目录 var/

cd frontend
npm ci
npm run dev                    # 5173，/api 代理到 5080
```

## 运行（同源生产模式）

```powershell
cd frontend
npm run build
cd ..
$env:HX17_PORT = '8080'
python deploy\serve.py --check
python deploy\serve.py
```

浏览器访问 `http://127.0.0.1:8080/`。深层链接（如 `/projects`）回落到 `index.html`，
`/api/*` 不会被前端路由截获。当前仍是 Flask 自带服务器，仅供本机验证；正式对外部署
与 HTTPS 入口在 WB17-06 单独验收。

> HTTPS 入口后面请设置 `HX17_COOKIE_SECURE=1`，让会话与 CSRF Cookie 带上 Secure。

## 配置

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `HX17_PORT` | 监听端口 | 5080（`deploy/serve.py` 下为 8080） |
| `HX17_DATA_DIR` | 运行数据目录（工厂创建时固定） | `<工程>/var` |
| `HX17_SERVE_DIST` | 是否托管前端构建产物 | 关闭 |
| `HX17_COOKIE_SECURE` | Cookie 是否带 Secure（HTTPS 下开启） | 关闭 |
| `HX17_SESSION_HOURS` | 登录有效期（小时） | 12 |
| `HX17_ENGINE_EXE` | OpenCode 可执行文件路径 | `E:/OpenCode/bin/opencode.exe`（本机） |
| `HX17_ENGINE_PYTHON` | 跑 stdio MCP 桥的解释器 | 当前解释器 |
| `HX17_ENGINE_VERSION` | 期望引擎版本 | 1.19.4 |
| `HX17_ENGINE_STRICT_VERSION` | 版本必须严格匹配 | 开启（不匹配拒绝启动） |
| `HX17_ENGINE_IDLE_SECONDS` | 空闲引擎回收时间 | 见 `config.py` |
| `HX17_TURN_TIMEOUT` | 单轮最长运行时间（秒） | 900 |
| `HX17_ALLOW_FAKE_ENGINE` | 是否允许注入假引擎（**仅测试**） | 关闭（生产不允许） |
| `VITE_BACKEND_ORIGIN` | 开发代理目标 | `http://127.0.0.1:5080` |

数据目录内包含 `haixun.db` 与 `secrets/{session,connection}.key`；两者都被 `.gitignore`
覆盖，数据库被单独拷走也无法解出 API Key。

## 安全边界（实现要点）

- 口令用 `scrypt`（不可用时 PBKDF2）加盐哈希；登录失败按 IP+用户名节流；
- 令牌用 itsdangerous 签名（不再自维护协议），放 HttpOnly + SameSite=Lax Cookie；服务端会话表可即时吊销；
  停用/改密立即失效；任何畸形 Cookie 一律按未登录处理，不会 500；
- 主密钥只在文件不存在时创建（完整内容 + 原子硬链接）；已有文件损坏/不可读时明确失败，
  **绝不覆盖**，避免毁掉旧密文的解密关系；
- 写操作要求 `X-CSRF-Token` 双提交 + Origin 校验，且只接受 `application/json`；
- 目录绑定先 `realpath` 归一化再做包含判断，盘符相对路径、设备前缀、UNC、`..`、
  符号链接/NTFS 联接越界全部拒绝；替换已有绑定必须显式 `replace=true`；
- 目录授权按账号存于 `root_grants`；列表、浏览、绑定、已绑定项目的访问都重新校验授予；
- 连接能力检查只在“200 且返回真实回复”时记认证通过；402/429/5xx/404 一律不当作 Key 已被接受；
- `/api/health`、`/api/meta` 不返回数据目录、端口、密钥路径。

### Agent 运行时边界（WB17-02）

- 引擎子进程：隐藏窗口、只绑 `127.0.0.1` 随机端口、随机 `OPENCODE_SERVER_PASSWORD`、
  隔离 XDG/HOME/TEMP（在 `var/engines/` 下），**不读全局 `~/.config/opencode` 的认证与配置**；
- 工具只能通过私有回环网关调用，令牌绑定 `(用户, 项目, 会话, 连接指纹)`；**当前正在执行哪一轮**
  不写死在令牌里，而是每次调用按会话查库解析（所以停止/换连接/撤权后下一次调用立即失败）；
- 令牌过期与“被明确撤销”分开处理：过期可以带新凭据重建引擎（保留会话存储与历史），
  撤销一律拒绝，不会被自动续期复活；
- Native 会话映射同时校验**存储代次**与**实际存在**（只有明确 404 才算不存在）；
  需要重建时把站点里已公开的近期对话作为上下文恢复，不重放已完成的动作；
- 停止请求一旦落下，新的工具调用与待确认动作都会被拒绝；abort 未确认空闲时隔离该引擎进程，
  并把仍在执行中的任务如实标为“结果未知”；
- 发起前必须拿到**确定的**消息 ID 边界：读取失败只会做有界只读重试，仍不确定就让本轮在
  发送模型请求**之前**失败（`BASELINE_UNAVAILABLE`）——绝不把“读不到”当成“空历史”，
  否则上一轮的回答会被重新当成这一轮的终答落库；
- 用户项目里的 `.opencode/`、`opencode.json` 不能覆盖受控配置：引擎带
  `OPENCODE_DISABLE_PROJECT_CONFIG=1` 启动；
- 文件工具限制在项目绑定目录内（realpath + 包含判断，拒绝 ADS/保留设备名/末尾空格；
  服务端数据目录与密钥目录即使被误设成项目目录也不可见），删除移入项目内 `.hx17-trash/` 并留元清单；
- 工具清单里没有 SQL、命令执行、目录授权、账号管理、人工复核结论、个人凭据；
- `full` 模式只影响「已授权范围内是否还出确认卡」，不扩大任何权限；
- 助手回复一律按不可信内容处理：marked 解析后经 DOMPurify 净化再渲染。

## 检查

```powershell
python -m unittest discover -s backend/tests -t . -v
python scripts/smoke_http.py        # 真实起服务 + Cookie/CSRF 全流程，自动停服
python scripts/tool_roundtrip.py    # 真实回环 HTTP + 真实文件操作串测（列目录→读→建→改→移→查→删）
python scripts/engine_smoke.py      # 真实引擎进程烟测（--version/健康检查，不发 prompt）
python scripts/mcp_bridge_handshake.py      # 真实 MCP stdio 握手（initialize/list_tools/call_tool，桩网关）
python scripts/engine_protocol_integration.py  # 真实引擎 + 真实桥 + 回环桩上游的协议集成检查
                                               # （普通成员账号；含令牌过期后重建的一轮）
powershell -File scripts/check.ps1 -Python <python> -Node <node>
```

前端纯逻辑测试：`cd frontend; npm run test:logic`（事件合并、工具卡聚合、流式分段、终态去重判定、
贴底判定、轮询退避）。

两个 `scripts/*.py` 里的“上游”都是**只监听回环的桩**：固定脚本返回合法工具调用与最终文本，
不是真实模型，不计费。

## 许可与来源

第三方组件与许可声明见 `THIRD_PARTY_NOTICES.md`。OpenCode 1.19.4（MIT，内部 Agent 执行层）以外部
可执行文件方式调用，保留其原始环境变量名；vendor YOLO 将在后续阶段按上游许可引入并更新该文件。
