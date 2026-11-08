# WorkBuddy 云端入口（网关 + 连接器）

这个目录是 v1.7 的**一整套 WorkBuddy 发布材料**：云端静态网关 `server.js`、本机连接器 `connector.js`、
页面产物 `web/`（由 `prepare` 从 `frontend/dist` 生成）。它不包含也不需要后端源码、数据库、账号或模型凭据。

```
浏览器 ── https://haixun-inspection.app.workbuddy.host ──▶ 云端 server.js
                                                          ├─ /             → web/（Vue 构建产物，SPA 回落、gzip）
                                                          ├─ /api/service/** → 403（永不进隧道）
                                                          └─ /api/**      → NDJSON 反向隧道
                                                                              │ 本机主动外连，不开入站端口
                                                                              ▼
                                                              connector.js ─▶ 127.0.0.1:8080（后端，未改造）
```

浏览器只看到**一个域名**，所以 Cookie、CSRF 双提交、上传下载、轮询与 SSE 都走同一条同源路径；
没有放开 CORS、没有关闭 CSRF、没有改后端代码。

## 目录

| 文件 | 说明 | 进 Git |
| --- | --- | --- |
| `server.js` | 云端单端口网关：静态托管 + `/api` 隧道 + `/api/service` 403 + `Origin` 精确白名单 | 是（原字节复用已验证版本） |
| `connector.js` | 本机连接器：长轮询上行、SSE/大响应分块回传、`Host` 改写为公网域名 | 是 |
| `package.json` | 仅声明 Node ≥18 与两个快捷命令，无第三方依赖 | 是 |
| `prepare.mjs` | 从 `frontend/dist` 生成 `web/`；未构建时明确报错 | 是 |
| `preflight.mjs` | 启动前检查 Node 版本、隧道令牌是否就位、本机后端是否可用 | 是 |
| `web/` | 前端构建产物，**每次 `prepare` 重新生成** | 否（`.gitignore`） |
| `.tunnel-token` | 连接器身份凭据，**单独私密传输，不重生成、不打印、不入库** | 否（`.gitignore`） |

## 准备与运行

```powershell
# 1) 构建前端（首次或前端改动后）
Set-Location frontend
npm ci
npm run build
Set-Location ..

# 2) 生成发布用的 web/
node deploy\workbuddy\prepare.mjs

# 3) 启动本机后端（保持原有入口，数据目录不变）
.\启动海巡智检.cmd

# 4) 启动云端连接（另开一个窗口，或双击根目录的 启动WorkBuddy云端连接.cmd）
node deploy\workbuddy\connector.js --public "https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT" --upstream http://127.0.0.1:8080 --lanes 4
```

> `--public` 不能省：连接器没有它（也没设 `HX17_PUBLIC`）会直接以退出码 2 结束。
> `sharecode` 是已授权的公开分享参数，用来让平台边缘认这个入口，不是 API 凭据。

`启动WorkBuddy云端连接.cmd` 只是上面第 4 步的封装：它先跑 `preflight.mjs` 检查 Node / 令牌 / 本机服务，
缺哪一样就报哪一样，检查通过才启动连接器；不会把令牌写进命令行或日志，也不会谎报“已上线”。
`preflight.mjs` 还会核对健康响应确实是本产品（`data.service=haixun-1.7-backend`、`data.status=ok`、
有版本字段），**只接受本机 http 回环地址**——同端口上换成别的服务会直接报错，而不是当成成功。

想换本机端口：`设定 HX17_UPSTREAM=http://127.0.0.1:8090` 或给启动器传参；想换入口链接：同理设 `HX17_PUBLIC`。
可用 `HX17_DRY_RUN=1` 只做自检、不启动连接器（排查用，退出码仍为 0 表示自检通过）。

## 发布

把本目录（`server.js`、`connector.js`、`package.json`、`web/`、`.tunnel-token`）作为 WorkBuddy 站点的
发布材料上传到沙箱即可。**不要**把后端 `var/`、数据库、账号、模型 API Key、测试文件一起上传。

**发布到哪个应用要人工核对，不能假设“目录一样就是同一个应用”。** 正确的做法是在 WorkBuddy 里打开
**原来那个应用**（核对它的应用 ID / 名称 / 归属账号，与当前固定分享链接对应），在它上面更新页面，
不要顺手新建一个应用再期待链接不变。管理/更新已发布应用需要原发布账号；只在服务器上跑连接器**不需要**
登录 WorkBuddy。`.tunnel-token` 必须与云端当前值一致；手动改了它就会和线上连接器对不上，**不要重新生成**。

## 已知限制（与实测一致，不夸大）

1. 必须有人开着连接器：巡检机断网/关机/关窗口，网页只剩登录壳，`/api` 返回 503 `TUNNEL_OFFLINE`。
2. 首次访问要带 `?sharecode=`（平台边缘访问控制），之后站内路由不再需要。
3. 单文件上传上限 512 MiB（与后端 `MAX_UPLOAD_BYTES` 对齐），实测到 64 MB 通过，未做 512 MB 端到端实跑。
4. 平台边缘会把后端下发的 `SameSite=Lax` 改写成 `Secure; SameSite=None`，所以 CSRF 只能靠
   网关 `Origin` 精确白名单 + 后端双提交令牌两道，二者都已实测拒绝伪造/缺失请求。
5. 管理与状态接口不会经隧道转发：规范路径 `/api/service/**` 返回 403；`//api/service/...` 这种歧义地址
   返回 400 `BAD_URL`，`%73ervice`、`./service`、`%2F` 等变形实测也都止步于网关（403），没有到后端。

## 来源

`server.js` / `connector.js` / `package.json` 是本工程在 WorkBuddy 发布阶段自己写的，2026-11-07 的公网浏览器
验收用的是同一份实现；整合进仓库时按**字节复制**（`server.js` sha256 `065a06ccda7a…`），这一轮没有改它俩的逻辑。
没有另写第二套反向代理；`prepare.mjs` / `preflight.mjs` / 本 README 是本轮新写的，属于集成脚本，
不要当成“长期运行验证过的历史组件”。两份 `server.js` 的差异请用 `git diff --no-index` 对比，不要手工改其中一边。
