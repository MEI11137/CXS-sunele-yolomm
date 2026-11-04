"""测试夹具：隔离数据目录 + 真实登录。

不使用任何“镜像实现”的假服务：每个用例都通过 HTTP 接口或真实 SQLite 断言行为。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from backend import db
from backend.app import create_app
from backend.services import projects, roots, sessions, users

PASSWORD = "correct-horse-9"


class Harness:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx17-test-")
        self.data_dir = Path(self._tmp.name)
        self.app = create_app(data_dir=self.data_dir, serve_dist=False)
        self.settings = self.app.config["HX17"]

    def close(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 数据

    def conn(self):
        connection = db.connect(self.settings.db_path)
        return connection

    def create_user(
        self,
        username: str,
        *,
        password: str = PASSWORD,
        role: str = "member",
        display_name: str | None = None,
    ) -> dict:
        connection = self.conn()
        try:
            return dict(
                users.create(
                    connection,
                    username=username,
                    password=password,
                    role=role,
                    display_name=display_name,
                )
            )
        finally:
            connection.close()

    def add_root(
        self, path: str | Path, label: str = "", grant_to: list[str] | None = None
    ) -> dict:
        """登记授权根目录；默认不授予任何账号，必须显式给出用户名。"""
        connection = self.conn()
        try:
            row = roots.add(connection, actor_id=None, raw_path=str(path), label=label)
            for username in grant_to or []:
                roots.grant_by_username(
                    connection, actor_id=None, root_id=int(row["id"]), username=username
                )
            return dict(row)
        finally:
            connection.close()

    def grant_root(self, root_id: int, username: str) -> None:
        connection = self.conn()
        try:
            roots.grant_by_username(
                connection, actor_id=None, root_id=int(root_id), username=username
            )
        finally:
            connection.close()

    def revoke_root(self, root_id: int, user_id: int) -> bool:
        connection = self.conn()
        try:
            return roots.revoke(
                connection, actor_id=None, root_id=int(root_id), user_id=int(user_id)
            )
        finally:
            connection.close()

    def create_project(self, owner_id: int, name: str = "测试项目") -> dict:
        connection = self.conn()
        try:
            return dict(projects.create(connection, owner_id, name=name))
        finally:
            connection.close()

    def create_session(self, project_id: str, owner_id: int, title: str = "测试会话") -> dict:
        connection = self.conn()
        try:
            return dict(sessions.create(connection, project_id=project_id, owner_id=owner_id, title=title))
        finally:
            connection.close()

    def make_dir(self, name: str) -> Path:
        target = self.data_dir / "workspace" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ------------------------------------------------------------------ 客户端

    def client(self):
        return self.app.test_client()


def csrf_headers(client) -> dict[str, str]:
    cookie = client.get_cookie("hx17_csrf")
    return {"X-CSRF-Token": cookie.value} if cookie is not None else {}


def login(client, username: str, password: str = PASSWORD):
    # 先访问一个安全方法，拿到 CSRF Cookie（浏览器里由任意页面加载完成）。
    client.get("/api/meta")
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["user"]


def api(client, method: str, path: str, payload: Any = None):
    headers = {} if method == "GET" else csrf_headers(client)
    if method != "GET":
        headers["Content-Type"] = "application/json"
    return client.open(path, method=method, json=payload, headers=headers)


def body(response) -> dict:
    data = response.get_json()
    assert data is not None, response.get_data(as_text=True)
    return data
