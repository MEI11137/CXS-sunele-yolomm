"""测试夹具：隔离数据目录 + 真实登录。

不使用任何“镜像实现”的假服务：每个用例都通过 HTTP 接口或真实 SQLite 断言行为。
"""

from __future__ import annotations

import atexit
import dataclasses
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from backend import config, db
from backend.app import create_app
from backend.services import connections, projects, roots, sessions, users
from backend.tests.fake_engine import FakeEngine, fake_spawner

PASSWORD = "correct-horse-9"

# 进程内创建的测试临时目录。故意不在 Harness.close() 立即删除（Windows 上
# sqlite/子进程句柄释放有延迟，删除会让后续用例偶发文件锁），但必须在测试进程
# 退出时收尾；否则全量套件会在仓库根留下上千个 hx17-test-* 目录。
_TEMP_DIRS: list[Path] = []


def _cleanup_temp_dirs() -> None:
    while _TEMP_DIRS:
        target = _TEMP_DIRS.pop()
        for attempt in range(3):
            try:
                shutil.rmtree(target, ignore_errors=False)
                break
            except OSError:
                time.sleep(0.2)


def make_temp_dir(prefix: str) -> Path:
    """创建会被测试进程统一清理的临时目录（不要在用例里裸调 mkdtemp）。"""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(path)
    return path


atexit.register(_cleanup_temp_dirs)


class Harness:
    def __init__(self, *, fake_engine: bool = False) -> None:
        # 用 mkdtemp 而不是 TemporaryDirectory：后者在对象被回收时会**随时**删除目录，
        # 而后台轮次线程可能还在写库，删除时机不可控。
        self.data_dir = Path(tempfile.mkdtemp(prefix="hx17-test-"))
        # 业务工作区必须与数据目录分开：数据目录是服务端私有区（数据库/密钥/引擎），
        # 测试也不该为了让文件工具跑通而放宽这条生产边界。
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="hx17-ws-"))
        _TEMP_DIRS.extend([self.data_dir, self.workspace_dir])
        self.fake: FakeEngine | None = None
        settings = config.load_settings(data_dir=self.data_dir, serve_dist=False, port=5099)
        spawner = None
        if fake_engine:
            self.fake = FakeEngine()
            self.fake.start()
            # 假引擎只在显式打开的测试配置里可用；生产默认不允许。
            settings = dataclasses.replace(settings, allow_fake_engine=True)
            spawner = fake_spawner(self.fake)
        else:
            # 默认用例不该启动真实引擎进程：指向不存在的可执行文件，让轮次快速失败。
            settings = dataclasses.replace(
                settings, engine_exe=str(self.data_dir / "missing-opencode.exe")
            )
        self.app = create_app(settings=settings, serve_dist=False, engine_spawner=spawner)
        self.settings = self.app.config["HX17"]

    def close(self) -> None:
        try:
            self.app.config["HX17_AGENT"].shutdown()
        except Exception:
            pass
        if self.fake is not None:
            self.fake.stop()
        # 不主动删除临时目录：Windows 上 sqlite 句柄释放有延迟，
        # 删除过程中偶发的文件锁会干扰后续用例。进程退出后由系统清理。
        return

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
        target = self.workspace_dir / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_connection(
        self,
        user_id: int,
        *,
        base_url: str = "http://127.0.0.1:9/v1",
        model: str = "probe-model",
        provider: str = "custom",
        api_key: str = "test-key-1234567890",
    ) -> None:
        """直接写库保存个人连接（不触发任何真实网络请求）。"""
        connection = self.conn()
        try:
            connections.save(
                connection,
                int(user_id),
                {
                    "provider": provider,
                    "base_url": base_url,
                    "model": model,
                    "display_name": "测试连接",
                    "api_key": api_key,
                },
                key=self.app.config["HX17_CONNECTION_KEY"],
            )
        finally:
            connection.close()

    def delete_connection(self, user_id: int) -> None:
        connection = self.conn()
        try:
            connections.delete(connection, int(user_id))
        finally:
            connection.close()

    def set_mode(self, user_id: int, mode: str) -> None:
        connection = self.conn()
        try:
            from backend.agent import store as turn_store

            turn_store.save_prefs(connection, int(user_id), mode=mode, model=None)
        finally:
            connection.close()

    def bind_project(self, project_id: str, path: Path, username: str) -> None:
        connection = self.conn()
        try:
            row = projects.get_owned(
                connection, project_id, users.find_by_username(connection, username)["id"]
            )
            projects.bind_root(connection, row, raw_path=str(path))
        finally:
            connection.close()

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


def wait_turn(client, turn_id: str, *, timeout: float = 20.0, after: int = 0) -> dict:
    """轮询事件直到轮次进入终态；返回最后一次响应。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = api(client, "GET", f"/api/turns/{turn_id}?after=0")
        assert response.status_code == 200, response.get_data(as_text=True)
        last = body(response)["data"]
        if last["turn"]["status"] in {"completed", "failed", "stopped"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"轮次未在 {timeout}s 内结束：{last}")
