"""工具网关闭环串测：走真实回环 HTTP + 真实文件操作，不启动模型。

用法::

    python scripts/tool_roundtrip.py

它验证的是「模型侧能触达的那条路」：

1. 在系统临时目录建隔离数据目录与一个受控工作目录（不碰工程 var/、不碰旧库）；
2. 用真实服务层建账号、授权根目录、项目、会话与一个进行中的轮次；
3. 通过网关**真实 HTTP 接口**带引擎令牌依次调用：项目概况 → 列目录 → 读文件 →
   创建 → 修改 → 移动 → 查询确认 → 删除（移入回收站）；
4. 顺带验证越界路径被拒、未知工具 404、错令牌 401、重复 call_id 不重复执行；
5. 最后用真实站点接口确认「待确认动作」在 request 模式下必须由本人点确认才执行。

全程不访问外网、不调用任何模型接口，也不会启动 OpenCode 进程。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config, db, paths  # noqa: E402
from backend.agent import identity, store  # noqa: E402
from backend.services import connections, projects, roots, sessions, users  # noqa: E402

TOKEN_HEADER = "X-HX17-Tool-Token"
PASSWORD = secrets.token_urlsafe(12)
USERNAME = "roundtrip_user"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"PASS: {message}")


class GatewayClient:
    """只用标准库打网关的私有回环端口，和 MCP 桥走的是同一个入口。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                TOKEN_HEADER: self.token,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, json.loads(body) if body else {}

    def call(self, tool: str, args: dict, call_id: str) -> tuple[int, dict]:
        return self._request("POST", f"/v1/tools/{tool}", {"args": args, "call_id": call_id})


def new_call_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="hx17-roundtrip-"))
    data_dir = workdir / "var"
    workspace = workdir / "data"
    workspace.mkdir(parents=True)
    (workspace / "readme.txt").write_text("一期巡检记录：3 号板有热斑\n", encoding="utf-8")

    settings = config.load_settings(data_dir=data_dir, serve_dist=False, port=0)
    # 指向不存在的引擎：本轮只测工具通路，绝不拉起真实 OpenCode 进程。
    import dataclasses

    settings = dataclasses.replace(settings, engine_exe=str(workdir / "missing-opencode.exe"))

    from backend.app import create_app

    app = create_app(settings=settings, serve_dist=False)
    runtime = app.config["HX17_AGENT"]
    gateway = runtime.gateway
    key = app.config["HX17_CONNECTION_KEY"]

    conn = db.connect(settings.db_path)
    try:
        user = users.create(conn, username=USERNAME, password=PASSWORD, role="admin")
        user_id = int(user["id"])
        root_row = roots.add(conn, actor_id=user_id, raw_path=str(workspace), label="串测目录")
        roots.grant(conn, actor_id=user_id, root_id=int(root_row["id"]), user_id=user_id)
        project = projects.create(conn, user_id, name="串测项目")
        project_row = projects.get_owned(conn, project["id"], user_id)
        projects.bind_root(conn, project_row, raw_path=str(workspace))
        session = sessions.create(conn, project_id=project["id"], owner_id=user_id, title="串测会话")
        connections.save(
            conn,
            user_id,
            {
                "provider": "custom",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "roundtrip-model",
                "display_name": "串测连接",
                "api_key": "sk-roundtrip-0000000000",
            },
            key=key,
        )
        connection = identity.connection_for(conn, user_id)
        assert connection is not None
        fingerprint = identity.connection_fingerprint(connection)
        turn, _created = store.create(
            conn,
            session_id=session["id"],
            project_id=project["id"],
            owner_id=user_id,
            request_id=new_call_id("req"),
            mode="request",
            provider=str(connection["provider"]),
            model="roundtrip-model",
            fingerprint=fingerprint,
        )
        store.mark_running(conn, turn["id"])
        # 会话级令牌：同一会话连续多轮共用一个引擎进程，因此不按轮签发。
        # 具体是哪一轮在执行，由网关在每次调用时按会话实时解析。
        request_token = gateway.issue_session(
            user_id=user_id,
            session_id=session["id"],
            project_id=project["id"],
            fingerprint=fingerprint,
        )
    finally:
        conn.close()

    client = GatewayClient(gateway.base_url, request_token)
    print(f"gateway={gateway.base_url} workspace={workspace}")

    # ---------------------------------------------------------------- 令牌与清单
    status, health = client._request("GET", "/v1/health")
    check(status == 200 and health["ok"] and health["tools"] >= 15, "网关回环端口可达，工具已注册")

    status, listing = client._request("GET", "/v1/tools")
    names = {item["name"] for item in listing.get("tools", [])} if status == 200 else set()
    check(status == 200 and "hx_read_file" in names, "带令牌可读取工具清单")
    check(
        "hx_write_file" in names and not any(name.startswith("hx_exec") for name in names),
        "清单里只有受控文件/项目工具，没有命令执行能力",
    )

    bad = GatewayClient(gateway.base_url, "not-a-real-token")
    status, _payload = bad.call("hx_project_overview", {}, new_call_id("bad"))
    check(status == 401, "错误令牌被拒绝（401）")

    status, unknown = client.call("hx_not_exist", {}, new_call_id("unk"))
    unknown_data = unknown.get("data", {})
    check(
        status == 200
        and unknown_data.get("status") == "error"
        and unknown_data.get("error", {}).get("code") == "TOOL_NOT_FOUND",
        "未注册工具被结构化拒绝（TOOL_NOT_FOUND）",
    )

    # ---------------------------------------------------------------- 只读链路
    status, overview = client.call("hx_project_overview", {}, new_call_id("ov"))
    data = overview.get("data", {})
    check(
        status == 200 and data.get("status") == "ok" and data["result"]["project"]["id"] == project["id"],
        "项目概况返回真实项目 ID",
    )

    status, listed = client.call("hx_list_files", {"path": "."}, new_call_id("ls"))
    names = [entry["name"] for entry in listed["data"]["result"]["entries"]]
    check(status == 200 and "readme.txt" in names, "列目录拿到真实文件清单")

    status, read = client.call("hx_read_file", {"path": "readme.txt"}, new_call_id("rd"))
    result = read["data"]["result"]
    check(
        status == 200 and result["kind"] == "text" and "3 号板有热斑" in result["text"],
        "读取文本文件内容正确",
    )

    # ---------------------------------------------------------------- 权限模式
    status, pending = client.call(
        "hx_write_file",
        {"path": "notes/日报.md", "content": "第一天\n", "mode": "create"},
        new_call_id("wr"),
    )
    payload = pending.get("data", {})
    check(
        payload.get("status") == "pending_confirmation" and payload.get("action_id"),
        "request 模式下写入不直接执行，而是生成待确认动作",
    )
    action_id = payload.get("action_id")
    check(not (workspace / "notes" / "日报.md").exists(), "待确认动作确实没有落盘")

    # 权限模式是**按轮次固定**的：切到 full 后只影响下一轮。
    conn = db.connect(settings.db_path)
    try:
        store.finish(conn, turn["id"], "completed")
    finally:
        conn.close()

    # 本轮已结束、下一轮还没起来：令牌本身没过期，但“现在没有活动轮次”，
    # 所以工具必须拒绝，而不是拿上一轮的授权继续干。
    status, stale = client.call("hx_list_files", {"path": "."}, new_call_id("stale"))
    check(
        status == 409 and stale.get("error", {}).get("code") == "TURN_FINISHED",
        "没有活动轮次时令牌不能用（409 TURN_FINISHED）",
    )

    conn = db.connect(settings.db_path)
    try:
        store.save_prefs(conn, user_id, mode="full", model=None)
        full_turn, _created = store.create(
            conn,
            session_id=session["id"],
            project_id=project["id"],
            owner_id=user_id,
            request_id=new_call_id("req-full"),
            mode="full",
            provider=str(connection["provider"]),
            model="roundtrip-model",
            fingerprint=fingerprint,
        )
        store.mark_running(conn, full_turn["id"])
    finally:
        conn.close()

    # 同一个令牌不换，第二轮仍然可用：它绑定的是会话，不是某一轮。
    full = GatewayClient(gateway.base_url, request_token)
    status, tracked = full.call("hx_list_files", {"path": "."}, new_call_id("track"))
    check(
        status == 200 and tracked.get("data", {}).get("status") == "ok",
        "同一会话令牌在第二轮继续可用（不因上一轮结束被吊销）",
    )

    status, created = full.call(
        "hx_write_file",
        {"path": "notes/日报.md", "content": "第一天\n", "mode": "create"},
        new_call_id("wr-full"),
    )
    check(
        status == 200 and created["data"]["status"] == "ok",
        "full 模式下写入直接执行（不再出确认卡）",
    )
    target = workspace / "notes" / "日报.md"
    check(target.is_file() and target.read_text(encoding="utf-8") == "第一天\n", "文件真实创建成功")

    status, updated = full.call(
        "hx_write_file",
        {"path": "notes/日报.md", "content": "第一天\n发现 2 处热斑\n", "mode": "overwrite"},
        new_call_id("wr2"),
    )
    check(status == 200 and updated["data"]["status"] == "ok", "覆盖写入被允许（full 模式）")
    check("2 处热斑" in target.read_text(encoding="utf-8"), "修改后的内容已落盘")

    status, blocked = full.call(
        "hx_move_path",
        {"from": "notes/日报.md", "to": "no-such-dir/日报.md"},
        new_call_id("mv-bad"),
    )
    blocked_error = blocked.get("data", {}).get("error", {})
    check(
        blocked_error.get("code") == "DEST_PARENT_MISSING",
        "移动到不存在的父目录被拒绝（不会顺手造目录）",
    )

    status, moved = full.call(
        "hx_move_path",
        {"from": "notes/日报.md", "to": "notes/日报-01.md"},
        new_call_id("mv"),
    )
    check(status == 200 and moved["data"]["status"] == "ok", "移动文件成功")
    check(
        not (workspace / "notes" / "日报.md").exists()
        and (workspace / "notes" / "日报-01.md").is_file(),
        "移动后原路径不存在、新路径存在",
    )

    status, confirmed = full.call("hx_list_files", {"path": "notes"}, new_call_id("ls2"))
    names = [entry["name"] for entry in confirmed["data"]["result"]["entries"]]
    check(status == 200 and names == ["日报-01.md"], "查询确认目录里只有移动后的文件")

    # ---------------------------------------------------------------- 幂等与越界
    call_id = new_call_id("dup")
    first_args = {"path": "notes/幂等.txt", "content": "v1", "mode": "create"}
    first = full.call("hx_write_file", dict(first_args), call_id)
    replay = full.call("hx_write_file", dict(first_args), call_id)
    check(
        first[1]["data"]["status"] == "ok"
        and replay[1].get("data", {}).get("replayed") is True,
        "同一 call_id + 同一参数重放返回原结果，不会重复执行",
    )
    check(
        (workspace / "notes" / "幂等.txt").read_text(encoding="utf-8") == "v1",
        "重放没有把文件写成第二次的内容",
    )

    conflict = full.call(
        "hx_write_file", {"path": "notes/幂等.txt", "content": "v2", "mode": "create"}, call_id
    )
    conflict_error = conflict[1].get("data", {}).get("error", {})
    check(
        conflict[1].get("data", {}).get("status") == "error"
        and conflict_error.get("code") == "CALL_ID_CONFLICT",
        "同一 call_id 换参数必须冲突，不能当成上一次的结果",
    )
    check(
        (workspace / "notes" / "幂等.txt").read_text(encoding="utf-8") == "v1",
        "冲突时旧文件内容没有被改动",
    )

    status, escaped = full.call(
        "hx_read_file", {"path": str(workdir / "var" / "haixun.db")}, new_call_id("esc")
    )
    error = escaped.get("data", {}).get("error", {})
    check(
        status == 200 and escaped["data"]["status"] == "error" and error.get("code"),
        f"越界路径被拒绝（{error.get('code')}）",
    )

    status, deleted = full.call("hx_delete_path", {"path": "notes/幂等.txt"}, new_call_id("rm"))
    check(
        status == 200 and deleted["data"]["status"] == "ok"
        and deleted["data"]["result"].get("recoverable") is True,
        "删除是移入回收站，可恢复",
    )
    check(not (workspace / "notes" / "幂等.txt").exists(), "删除后原文件确实不存在")

    # ---------------------------------------------------------------- 本人确认后执行
    flask_client = app.test_client()
    flask_client.get("/api/meta")
    csrf = flask_client.get_cookie("hx17_csrf")
    response = flask_client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"X-CSRF-Token": csrf.value if csrf else ""},
    )
    check(response.status_code == 200, "串测账号可登录站点")
    # 登录会轮换 CSRF 令牌，后续写操作必须用登录后的新值。
    csrf = flask_client.get_cookie("hx17_csrf")
    csrf_headers = {"X-CSRF-Token": csrf.value if csrf else ""}
    response = flask_client.post(
        f"/api/actions/{action_id}/approve",
        json={},
        headers=csrf_headers,
    )
    approved = response.get_json()["data"] if response.status_code == 200 else {}
    check(
        response.status_code == 200 and approved.get("executed") is True,
        "本人点击确认后动作真实执行",
    )
    check(target.is_file(), "确认执行后文件出现（先前确实没落盘）")

    # 同一动作再点一次：不重复执行
    response = flask_client.post(
        f"/api/actions/{action_id}/approve",
        json={},
        headers=csrf_headers,
    )
    again = response.get_json()["data"] if response.status_code == 200 else {}
    check(
        response.status_code == 200 and again.get("already") is True,
        "重复确认同一动作不会被再执行一次",
    )

    # 目录字符串不得被当成“已绑定”之外的路径
    check(paths.is_within(str(target), str(workspace)), "工具写入路径确实落在绑定目录内")

    runtime.shutdown()
    print("TOOL ROUNDTRIP OK")
    print(f"（临时目录保留，便于核对：{workdir}）")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
