# 开发协作接口（本机）

**给谁看**：想在站外驱动同一个助手的开发工具（Codex、DeepSeekHarness、你自己的脚本）。

**一句话**：这里没有第二套 Agent。外部客户端调的是**站点已经在用的**那些接口 —— 同一个会话、
同一条消息历史、同一轮次、同一套工具与同一份权限判断。

```
外部客户端（curl / 脚本 / Codex）
        │  HTTP + Cookie 会话 + CSRF
        ▼
   Flask 站点（本项目）  ──►  会话 / 轮次 / 事件 / 停止   （已有接口，无新增特权）
        │
        ▼
   同一个引擎实例 + 同一份工具清单 + 同一套授权表
```

---

## 1. 不做的事（先看这条，避免误解）

| 不做 | 原因 |
| --- | --- |
| 另起一个"外部 Agent 循环" | 会出现两套决策同时动手，重复执行、重复计费、状态互相覆盖 |
| 提供免鉴权的 shell / SQL 接口 | 等于绕开全部权限边界，任何本机进程都能读别人的数据 |
| 暴露浏览器调试端口（CDP） | 该端口能直接操作已登录页面，暴露即等于交出账号 |
| 提供"批量自动确认动作"的接口 | 待确认动作是**人**的裁决点，只能由人在界面上点 |

外部客户端能做的事，与一个打开浏览器的普通用户**完全一样**，不多一件。

---

## 2. 认证：Cookie 会话 + CSRF 双提交

写操作要同时满足三条：已登录 Cookie、`X-CSRF-Token` 与 `hx17_csrf` Cookie 一致、Origin 与站点一致。
所以流程是"先 GET 一个安全方法拿 CSRF Cookie，再登录，再带着它写"。

```bash
BASE=http://127.0.0.1:5080/api
JAR=$(mktemp)

# 1) 拿 CSRF Cookie（任意安全方法都会下发）
curl -s -c "$JAR" "$BASE/meta" >/dev/null
CSRF=$(awk '$6=="hx17_csrf"{print $7}' "$JAR")

# 2) 登录（Cookie 会轮换 CSRF，登录后必须重新读）
curl -s -b "$JAR" -c "$JAR" -H "Content-Type: application/json" \
     -H "X-CSRF-Token: $CSRF" \
     -d '{"username":"admin","password":"123456"}' \
     "$BASE/auth/login"
CSRF=$(awk '$6=="hx17_csrf"{print $7}' "$JAR")
```

之后每个写请求都带 `-b "$JAR" -c "$JAR" -H "X-CSRF-Token: $CSRF"`。

---

## 3. 一次完整对话（照抄即可）

```bash
# 3.1 找项目与会话（都只要真实 ID）
curl -s -b "$JAR" "$BASE/projects"
curl -s -b "$JAR" "$BASE/projects/$PID/sessions"

# 3.2 发起一轮：request_id 必须自己生成，重放同一个 ID 不会重复执行
RID="cli-$(date +%s)-$RANDOM"
curl -s -b "$JAR" -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
     -d "{\"request_id\":\"$RID\",\"content\":\"这个目录里有哪些图片？\"}" \
     "$BASE/sessions/$SID/turns"
# → {"data":{"turn":{...},"events":[...],"actions":[...]}}

# 3.3 轮询事件（after=已收到的最大 seq；不要重复处理同一 seq）
curl -s -b "$JAR" "$BASE/turns/$TID/events?after=0"

# 3.4 需要低延迟时用 SSE（同一份事件，终态会补一条 type=end）
curl -sN -b "$JAR" "$BASE/turns/$TID/stream?after=0"

# 3.5 停止：只是"请求停止"，真实终态看 turn.status
curl -s -b "$JAR" -X POST -H "X-CSRF-Token: $CSRF" "$BASE/turns/$TID/stop"
```

| 方法 | 路径 | 用途 | 幂等 |
| --- | --- | --- | --- |
| GET | `/api/meta` | 版本、能力开关、下发 CSRF | 是 |
| POST | `/api/auth/login` | 登录（轮换 CSRF） | — |
| GET | `/api/projects` | 项目列表 | 是 |
| GET | `/api/projects/:pid/sessions` | 会话列表 | 是 |
| GET | `/api/sessions/:sid/messages` | 历史消息（`limit`/`before` 分页） | 是 |
| POST | `/api/sessions/:sid/turns` | **发起一轮**（`request_id` 幂等） | 是（同 ID 返回 `resumed=true`） |
| GET | `/api/turns/:tid` | 轮次状态 | 是 |
| GET | `/api/turns/:tid/events?after=` | 增量事件 | 是 |
| GET | `/api/turns/:tid/stream?after=` | SSE 增量 | 是 |
| POST | `/api/turns/:tid/stop` | 请求停止 | 是（终态时 `already=true`） |
| GET | `/api/sessions/:sid/actions` | 待确认动作 | 是 |
| POST | `/api/actions/:aid/approve` \| `/reject` | 人点确认/拒绝 | 否 |
| GET/PUT | `/api/me/preferences` | 权限模式与模型（会话下一轮生效） | 是 |

---

## 4. 事件类型（`/turns/:id/events` 返回的 `type`）

| type | 含义 | 客户端怎么用 |
| --- | --- | --- |
| `turn.status` | 轮次状态变化 | 更新 UI 状态；`completed/failed/stopped` 是终态 |
| `text` | 流式正文分片（`part_id`） | 同 `part_id` 覆盖，不同 `part_id` 之间空行分隔 |
| `tool` | 工具调用（`call_id`、`state`、`duration_ms`、`preview`） | 同 `call_id` 只留最新一条 |
| `notice` | 进度提示 | 终态后不再展示（已过期） |
| `question` | 助手反问 + 选项 | 展示给用户选，不要自动选 |
| `ui.navigate` | 建议跳转（`page`、`route`） | 由用户决定是否跳 |
| `job.summary` | 后台作业真实结果摘要 | 卡片展示用；**不是**模型回答 |

---

## 5. 错误约定

失败一律 HTTP 状态 + JSON 信封：

```json
{"code": 409, "error": "TURN_BUSY", "msg": "该会话还有一轮没有结束；先等待或停止它", "data": null}
```

`error` 是稳定的机器码，`msg` 是给人看的中文。客户端**按 `error` 分支**，不要解析 `msg`。

| 机器码 | HTTP | 什么时候 | 客户端该做什么 |
| --- | --- | --- | --- |
| `AUTH_REQUIRED` | 401 | 没登录 / 会话过期 | 重新登录，不要重试原请求 |
| `CSRF_FAILED` / `CSRF_ORIGIN` | 403 | 缺 CSRF 或来源不符 | 重新 GET 拿 CSRF；检查 Origin |
| `BAD_REQUEST_ID` | 400 | `request_id` 不是 8-64 位字母数字 | 换一个真实随机 ID |
| `VALIDATION` | 400 | 内容为空 / 超长 | 修正请求体 |
| `TURN_BUSY` | 409 | 同一会话还有一轮没结束 | 先等终态或 `stop` |
| `CONNECTION_MISSING` | 409 | 账号没配个人模型连接 | 让用户去设置页配 |
| `NO_PROJECT_ROOT` | 409 | 项目没绑目录 | 让用户先绑定 |
| `GRANT_REVOKED` | 403 | 目录授权被收回 | 停手，让用户找管理员 |
| `PROJECT_CLOSED` | 409 | 项目被删/不可用 | 停手，不要重试 |
| `TURN_NOT_FOUND` 等 `*_NOT_FOUND` | 404 | ID 不存在或不属于当前账号 | 重新拉列表，不要猜 ID |

---

## 6. 边界（与网页完全一致）

* **动作确认只能由人做**：`approve`/`reject` 是人的操作；脚本替人点等于替人担责，本接口不为此提供批量入口。
* **模型与权限按账号偏好**：`/me/preferences` 改的是下一轮；不能改正在跑的轮次。
* **作业是异步的**：`POST /turns` 返回的是轮次，不是最终结论；结论看事件与记录。
* **没有 CDP、没有免鉴权端口**：服务只监听回环地址，外部访问请自建受控入口（反代 + 鉴权）。
* **并发**：同一会话串行（`TURN_BUSY` 拦住）；不同会话可并行，但受服务端工作线程数限制。

---

## 7. 最小可用客户端（Python，无第三方依赖）

```python
import json, urllib.request, http.cookiejar

BASE = "http://127.0.0.1:5080/api"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def call(path, method="GET", payload=None, csrf=""):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    with opener.open(req) as response:
        return json.loads(response.read().decode())

def csrf_token():
    return next((c.value for c in jar if c.name == "hx17_csrf"), "")

call("/meta")
call("/auth/login", "POST", {"username": "admin", "password": "123456"}, csrf_token())
token = csrf_token()

project = call("/projects")["data"]["projects"][0]
session = call(f"/projects/{project['id']}/sessions")["data"]["sessions"][0]
turn = call(
    f"/sessions/{session['id']}/turns",
    "POST",
    {"request_id": "cli-demo-0001", "content": "这个目录里有哪些图片？"},
    token,
)["data"]["turn"]

seq = 0
while True:
    payload = call(f"/turns/{turn['id']}/events?after={seq}")["data"]
    for event in payload["events"]:
        seq = event["seq"]
        print(event["type"], event.get("payload", {}))
    if payload["turn"]["status"] in {"completed", "failed", "stopped"}:
        break
```

---

## 8. 和"任务记忆"的关系

会话上下文由引擎自己持有（同一个会话 ID 复用同一份记忆），外部客户端**不需要**上传历史：
历史消息用 `/sessions/:sid/messages` 读，重新接上就调 `/turns/:tid/events?after=<最后一个 seq>`。
不要把引擎的上下文压缩摘要当成"对用户的回答"转发出去 —— 回答只认落库的 assistant 消息。
