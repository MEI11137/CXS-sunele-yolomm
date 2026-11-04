# 海巡智检 v1.7

光伏（RGB+IR）巡检与数据质量闭环系统。当前版本 **1.7.0**，阶段 **WB17-01：账号 / 项目 / 会话 / 目录 / 个人模型连接**。

本目录是从空白工程重新开发的 v1.7 工程。v1.6 仅作为只读功能参考，不复制其源码目录；
契约（接口形态、权限规则、工具语义）可以参考，代码按本工程结构重新实现。

## 现状（重要）

WB17-01 已实现并可实际使用：

- 账号：本机 CLI 建号、登录/登出/当前用户、改密、管理员建普通账号与启停；**无注册入口、无默认账号**；
- 项目与会话：CRUD、软删除/恢复、真实消息历史；每个项目与会话都按 owner 隔离；
- 目录：管理员登记授权根目录，并**按账号授予**；普通用户只能看到/浏览/绑定自己获授的目录，
  撤销授予后已绑定的项目立即被拒绝；新增根目录默认不共享；
- 个人模型连接：按用户加密保存，Key 只回显掩码；可显式“检查连接”实测认证 / chat / 视觉 / 工具；
  证据只保留状态、耗时、环节与固定说明，上游正文（可能回显凭据）不落库、不回显；
- 设置页：连接配置、能力状态、改密、深浅色主题。

仍未实现（界面会明确标注，不会伪造结果）：

- 内部 Agent 运行时（OpenCode + 私有工具桥）与助手回复 —— WB17-02；
- 文件发现、检测、AI 核查 —— WB17-03；人工复核与数据回流 —— WB17-04；
- 数据集、模型、训练 —— WB17-05；公网 HTTPS 与正式部署 —— WB17-06。

## 目录

```text
backend/
  app.py            应用工厂（配置快照、密钥加载、蓝图注册、错误封装）
  config.py         环境变量解析与功能开关
  db.py             SQLite 连接生命周期与迁移
  auth.py           登录态、CSRF、权限装饰器
  security.py       口令哈希（scrypt/PBKDF2）、签名令牌、掩码
  crypto.py         API Key 的 AES-256-GCM 加解密
  paths.py          目录边界校验（盘符 / UNC / .. / 链接 / 联接）
  cli.py            本机管理命令行
  routes/           health / meta / auth / admin / projects / sessions / directories / connections
  services/         users / projects / sessions / connections / roots / audit
  tests/            131 项 unittest（含越权、过期、畸形令牌、密钥文件字节安全、按账号授权、脱敏、越界）
frontend/           Vue 3 + TypeScript + Pinia + Vite
deploy/serve.py     生产同源启动器（后端托管 frontend/dist）
docs/               开发文档，含 WB17-00 / WB17-01 记录
scripts/            check.ps1（测试+类型检查+构建）、smoke_http.py（真实 HTTP 烟测）
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

## 检查

```powershell
python -m unittest discover -s backend/tests -t . -v
python scripts/smoke_http.py      # 真实起服务 + Cookie/CSRF 全流程，自动停服
powershell -File scripts/check.ps1 -Python <python> -Node <node>
```

## 许可与来源

第三方组件与许可声明见 `THIRD_PARTY_NOTICES.md`。OpenCode（内部 Agent 执行层）与
vendor YOLO 将在后续阶段按上游许可引入并更新该文件。
