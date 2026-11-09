"""分块上传（第一批）：大文件走多请求，小文件仍走原 multipart。

覆盖的是**服务端契约**，不是前端实现：归属、CSRF/Origin、块校验、幂等、限额、
过期、临时文件清理，以及“老接口不被放宽”。
"""

from __future__ import annotations

import hashlib
import io
import threading
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from backend.services import projects as project_service
from backend.tests.support import Harness, body, csrf_headers, login

OCTET = {"Content-Type": "application/octet-stream"}


def zip_bytes(payload: bytes, name: str = "sample.bin") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


def png_bytes(width: int = 16, height: int = 12) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 60, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ChunkedUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.alice = self.harness.create_user("alice_upload")
        self.bob = self.harness.create_user("bob_upload")

        self.root = self.harness.make_dir("alice-workspace")
        self.harness.add_root(self.root, label="alice", grant_to=["alice_upload"])
        self.project = self.harness.create_project(self.alice["id"], name="上传项目")
        self.harness.bind_project(self.project["id"], self.root, "alice_upload")

        self.bob_root = self.harness.make_dir("bob-workspace")
        self.harness.add_root(self.bob_root, label="bob", grant_to=["bob_upload"])
        self.bob_project = self.harness.create_project(self.bob["id"], name="Bob 项目")
        self.harness.bind_project(self.bob_project["id"], self.bob_root, "bob_upload")

        self.client = self.harness.client()
        login(self.client, "alice_upload")
        self.bob_client = self.harness.client()
        login(self.bob_client, "bob_upload")

    # ------------------------------------------------------------------ 辅助

    def init(
        self,
        size: int,
        *,
        chunk_size: int | None = 4096,
        filename: str = "dataset.zip",
        digest: str | None = None,
        role: str | None = None,
        project_id: str | None = None,
        client=None,
    ):
        client = client or self.client
        payload = {
            "filename": filename,
            "size": size,
            "chunk_size": chunk_size,
        }
        if digest:
            payload["sha256"] = digest
        if role:
            payload["role"] = role
        return client.post(
            f"/api/projects/{project_id or self.project['id']}/uploads",
            json=payload,
            headers=csrf_headers(client),
        )

    def put(self, upload_id: str, index: int, data: bytes, *, digest: str | None = None, project_id=None, client=None):
        client = client or self.client
        headers = {**csrf_headers(client), **OCTET, "X-Chunk-Sha256": digest or sha(data)}
        return client.put(
            f"/api/projects/{project_id or self.project['id']}/uploads/{upload_id}/chunks/{index}",
            data=data,
            headers=headers,
        )

    def finish(self, upload_id: str, *, project_id=None, client=None):
        client = client or self.client
        return client.post(
            f"/api/projects/{project_id or self.project['id']}/uploads/{upload_id}/finish",
            json={},
            headers=csrf_headers(client),
        )

    def start(self, data: bytes, chunk_size: int = 4096, **kwargs) -> dict:
        response = self.init(len(data), chunk_size=chunk_size, **kwargs)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["upload"]

    def upload_all(self, upload: dict, data: bytes, order=None):
        unit = upload["chunk_size"]
        count = upload["chunk_count"]
        for index in order or range(count):
            start = index * unit
            piece = data[start:start + unit]
            response = self.put(upload["id"], index, piece)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def assets(self) -> list[dict]:
        response = self.client.get(f"/api/projects/{self.project['id']}/assets")
        self.assertEqual(response.status_code, 200)
        return body(response)["data"]["assets"]

    def upload_dir_leftovers(self) -> list[str]:
        cache = self.root / ".cache" / "uploads"
        if not cache.exists():
            return []
        return [path.name for path in cache.iterdir()]

    def abort(self, upload_id: str):
        return self.client.post(
            f"/api/projects/{self.project['id']}/uploads/{upload_id}/abort",
            json={},
            headers=csrf_headers(self.client),
        )

    def session_state(self, upload_id: str) -> dict:
        response = self.client.get(
            f"/api/projects/{self.project['id']}/uploads/{upload_id}"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["upload"]

    def patch_session(self, upload_id: str, **columns) -> None:
        """直接改库，模拟“响应丢失/进程中断/时间流逝”这类外部状态。"""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        connection = self.harness.conn()
        try:
            connection.execute(
                f"UPDATE upload_sessions SET {assignments} WHERE id = ?",
                (*columns.values(), upload_id),
            )
            connection.commit()
        finally:
            connection.close()

    def expire(self, upload_id: str) -> None:
        self.patch_session(upload_id, expires_at=1)

    def chunk_path(self, upload_id: str, index: int) -> Path:
        return self.root / ".cache" / "uploads" / upload_id / f"{index:05d}.part"

    # ------------------------------------------------------------------ 正常链路

    def test_chunked_upload_roundtrip_keeps_bytes_and_sha(self) -> None:
        data = zip_bytes(b"a" * 5000)
        upload = self.start(data)
        self.assertEqual(upload["chunk_count"], 2)
        self.upload_all(upload, data)
        finished = self.finish(upload["id"])
        self.assertEqual(finished.status_code, 200, finished.get_data(as_text=True))
        asset = body(finished)["data"]["asset"]
        self.assertEqual(asset["sha256"], sha(data))
        self.assertEqual(asset["size"], len(data))
        self.assertEqual(asset["kind"], "archive")

        content = self.client.get(
            f"/api/projects/{self.project['id']}/assets/{asset['id']}/content"
        )
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.get_data(), data)
        content.close()
        self.assertEqual(self.upload_dir_leftovers(), [])

    def test_out_of_order_chunks_are_allowed(self) -> None:
        data = zip_bytes(b"b" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data, order=[2, 0, 1])
        finished = self.finish(upload["id"])
        self.assertEqual(finished.status_code, 200, finished.get_data(as_text=True))
        self.assertEqual(body(finished)["data"]["asset"]["sha256"], sha(data))

    def test_finish_is_idempotent_and_creates_one_asset(self) -> None:
        data = zip_bytes(b"c" * 3000)
        upload = self.start(data)
        self.upload_all(upload, data)
        first = body(self.finish(upload["id"]))["data"]["asset"]
        second_response = self.finish(upload["id"])
        self.assertEqual(second_response.status_code, 200, second_response.get_data(as_text=True))
        second = body(second_response)["data"]["asset"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.assets()), 1)

    def test_duplicate_chunk_is_idempotent_but_conflict_is_rejected(self) -> None:
        data = zip_bytes(b"d" * 3000)
        upload = self.start(data)
        first_piece = data[: upload["chunk_size"]]
        self.assertEqual(self.put(upload["id"], 0, first_piece).status_code, 200)
        again = self.put(upload["id"], 0, first_piece)
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertEqual(body(again)["data"]["upload"]["received_bytes"], len(first_piece))

        changed = bytes([first_piece[0] ^ 0xFF]) + first_piece[1:]
        conflict = self.put(upload["id"], 0, changed)
        self.assertEqual(conflict.status_code, 409, conflict.get_data(as_text=True))
        self.assertEqual(body(conflict)["error"], "CHUNK_CONFLICT")

    # ------------------------------------------------------------------ 失败与校验

    def test_missing_chunk_blocks_finish_with_details(self) -> None:
        data = zip_bytes(b"e" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data, order=[0])
        blocked = self.finish(upload["id"])
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        payload = body(blocked)
        self.assertEqual(payload["error"], "CHUNKS_MISSING")
        self.assertEqual(payload["data"]["missing_count"], 2)
        self.assertEqual(payload["data"]["missing"], [1, 2])

        self.upload_all(upload, data, order=[1, 2])
        done = self.finish(upload["id"])
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))

    def test_chunk_hash_and_size_are_verified(self) -> None:
        data = zip_bytes(b"f" * 3000)
        upload = self.start(data)
        piece = data[: upload["chunk_size"]]

        wrong_hash = self.put(upload["id"], 0, piece, digest=sha(b"something else"))
        self.assertEqual(wrong_hash.status_code, 400)
        self.assertEqual(body(wrong_hash)["error"], "CHUNK_HASH_MISMATCH")

        short = self.put(upload["id"], 0, piece[:-1])
        self.assertEqual(short.status_code, 400)
        self.assertEqual(body(short)["error"], "CHUNK_SIZE_MISMATCH")

        bad_header = self.put(upload["id"], 0, piece, digest="not-a-hash")
        self.assertEqual(bad_header.status_code, 400)
        self.assertEqual(body(bad_header)["error"], "BAD_HASH")

    def test_declared_sha_mismatch_stops_registration_and_allows_retry(self) -> None:
        data = zip_bytes(b"g" * 3000)
        upload = self.start(data, digest=sha(b"other-content"))
        self.upload_all(upload, data)
        failed = self.finish(upload["id"])
        self.assertEqual(failed.status_code, 409, failed.get_data(as_text=True))
        self.assertEqual(body(failed)["error"], "UPLOAD_SHA_MISMATCH")
        self.assertEqual(self.assets(), [])

        # 失败后状态回到 uploading，可以取消（而不是把会话卡死）
        aborted = self.client.post(
            f"/api/projects/{self.project['id']}/uploads/{upload['id']}/abort",
            json={},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(aborted.status_code, 200, aborted.get_data(as_text=True))
        self.assertEqual(self.upload_dir_leftovers(), [])

    def test_expired_session_is_rejected_and_cleaned(self) -> None:
        data = zip_bytes(b"h" * 3000)
        upload = self.start(data)
        conn = self.harness.conn()
        try:
            conn.execute("UPDATE upload_sessions SET expires_at = 1 WHERE id = ?", (upload["id"],))
            conn.commit()
        finally:
            conn.close()
        expired = self.put(upload["id"], 0, data[: upload["chunk_size"]])
        self.assertEqual(expired.status_code, 410, expired.get_data(as_text=True))
        self.assertEqual(body(expired)["error"], "UPLOAD_EXPIRED")

    # ------------------------------------------------------------------ 归属与鉴权

    def test_unauthenticated_and_cross_user_are_denied(self) -> None:
        anonymous = self.harness.client()
        init = self.init(10, client=anonymous)
        self.assertEqual(init.status_code, 401, init.get_data(as_text=True))

        data = zip_bytes(b"i" * 3000)
        upload = self.start(data)
        # Bob 用 Alice 的 project 路径
        wrong_project = self.put(
            upload["id"], 0, data[: upload["chunk_size"]], project_id=self.project["id"], client=self.bob_client
        )
        self.assertEqual(wrong_project.status_code, 404, wrong_project.get_data(as_text=True))
        # Bob 用自己的 project 路径 + Alice 的 upload_id
        wrong_upload = self.put(
            upload["id"], 0, data[: upload["chunk_size"]], project_id=self.bob_project["id"], client=self.bob_client
        )
        self.assertEqual(wrong_upload.status_code, 404, wrong_upload.get_data(as_text=True))
        self.assertEqual(self.finish(upload["id"], client=self.bob_client).status_code, 404)

    def test_chunk_requires_csrf_and_rejects_forged_origin(self) -> None:
        data = zip_bytes(b"j" * 3000)
        upload = self.start(data)
        piece = data[: upload["chunk_size"]]
        url = f"/api/projects/{self.project['id']}/uploads/{upload['id']}/chunks/0"

        no_csrf = self.client.put(url, data=piece, headers=OCTET)
        self.assertEqual(no_csrf.status_code, 403, no_csrf.get_data(as_text=True))
        self.assertEqual(body(no_csrf)["error"], "CSRF_FAILED")

        forged = self.client.put(
            url,
            data=piece,
            headers={**csrf_headers(self.client), **OCTET, "Origin": "https://evil.example", "X-Chunk-Sha256": sha(piece)},
        )
        self.assertEqual(forged.status_code, 403, forged.get_data(as_text=True))
        self.assertEqual(body(forged)["error"], "CSRF_ORIGIN")

    def test_non_upload_routes_stay_json_only(self) -> None:
        """octet-stream 只对分块端点放行，别的写接口不能借机放宽。"""
        response = self.client.post(
            f"/api/projects/{self.project['id']}/assets/register",
            data=b"not-json",
            headers={**csrf_headers(self.client), **OCTET},
        )
        self.assertEqual(response.status_code, 415, response.get_data(as_text=True))

    # ------------------------------------------------------------------ 限额与兼容

    def test_limits_are_enforced(self) -> None:
        too_large = self.init(513 * 1024 * 1024)
        self.assertEqual(too_large.status_code, 413, too_large.get_data(as_text=True))
        self.assertEqual(body(too_large)["error"], "FILE_TOO_LARGE")

        sessions = [self.start(zip_bytes(bytes([index]) * 100)) for index in range(4)]
        blocked = self.init(1000)
        self.assertEqual(blocked.status_code, 429, blocked.get_data(as_text=True))
        self.assertEqual(body(blocked)["error"], "UPLOAD_LIMIT")

        # 取消一个就能继续
        upload = sessions[0]
        self.client.post(
            f"/api/projects/{self.project['id']}/uploads/{upload['id']}/abort",
            json={},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(self.init(1000).status_code, 200)

        # 再取消一个，腾出槽位测单块大小限制
        self.client.post(
            f"/api/projects/{self.project['id']}/uploads/{sessions[1]['id']}/abort",
            json={},
            headers=csrf_headers(self.client),
        )

        # 单块超过 4MiB 在路由层就被拒绝
        current = self.start(zip_bytes(b"k" * 200))
        oversized = self.client.put(
            f"/api/projects/{self.project['id']}/uploads/{current['id']}/chunks/0",
            data=b"x" * (4 * 1024 * 1024 + 1),
            headers={**csrf_headers(self.client), **OCTET, "X-Chunk-Sha256": sha(b"x")},
        )
        self.assertEqual(oversized.status_code, 413, oversized.get_data(as_text=True))

    def test_filename_cannot_smuggle_path(self) -> None:
        data = zip_bytes(b"l" * 500)
        upload = self.start(data, filename="../../evil.zip")
        self.upload_all(upload, data)
        asset = body(self.finish(upload["id"]))["data"]["asset"]
        self.assertNotIn("/", asset["filename"])
        self.assertNotIn("..", asset["filename"])

    def test_old_multipart_route_still_works(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project['id']}/assets/upload",
            data={"file": (io.BytesIO(png_bytes()), "tiny.png")},
            headers=csrf_headers(self.client),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        asset = body(response)["data"]["asset"]
        self.assertEqual(asset["kind"], "image")

    # ------------------------------------------------------------------ 审查反例

    def test_tampered_chunk_on_disk_blocks_finish(self) -> None:
        """块登记后磁盘被改（大小不变）→ finish 必须重新读盘核对并拒绝。"""
        data = zip_bytes(b"m" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data)
        part = self.chunk_path(upload["id"], 0)
        original = part.read_bytes()
        part.write_bytes(b"X" + original[1:])

        blocked = self.finish(upload["id"])
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertEqual(body(blocked)["error"], "CHUNK_CORRUPTED")
        self.assertIn(0, body(blocked)["data"]["chunks"])
        self.assertEqual(self.assets(), [])
        self.assertEqual(self.session_state(upload["id"])["status"], "uploading")

        # 重传该块后可以正常完成
        repaired = self.put(upload["id"], 0, data[: upload["chunk_size"]])
        self.assertEqual(repaired.status_code, 200, repaired.get_data(as_text=True))
        done = self.finish(upload["id"])
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))
        self.assertEqual(body(done)["data"]["asset"]["sha256"], sha(data))

    def test_lost_chunk_part_blocks_finish_and_can_be_reuploaded(self) -> None:
        data = zip_bytes(b"n" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data)
        self.chunk_path(upload["id"], 1).unlink()

        blocked = self.finish(upload["id"])
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertEqual(body(blocked)["error"], "CHUNK_CORRUPTED")
        self.assertIn(1, body(blocked)["data"]["chunks"])

        unit = upload["chunk_size"]
        again = self.put(upload["id"], 1, data[unit: 2 * unit])
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        done = self.finish(upload["id"])
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))
        self.assertEqual(body(done)["data"]["asset"]["sha256"], sha(data))

    def test_duplicate_chunk_repairs_damaged_part(self) -> None:
        """同内容重传不是无脑返回成功：磁盘坏了就用这次字节修好。"""
        data = zip_bytes(b"o" * 9000)
        upload = self.start(data)
        piece = data[: upload["chunk_size"]]
        self.assertEqual(self.put(upload["id"], 0, piece).status_code, 200)
        part = self.chunk_path(upload["id"], 0)
        part.write_bytes(b"\0" * len(piece))

        again = self.put(upload["id"], 0, piece)
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertEqual(part.read_bytes(), piece)

        self.upload_all(upload, data, order=range(1, upload["chunk_count"]))
        done = self.finish(upload["id"])
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))

    def test_expired_session_is_rejected_everywhere(self) -> None:
        """过期语义对所有入口一致：status / finish / abort 都是 410。"""
        for action in ("status", "finish", "abort"):
            data = zip_bytes(b"p" * 3000)
            upload = self.start(data)
            self.upload_all(upload, data)
            self.expire(upload["id"])

            if action == "status":
                response = self.client.get(
                    f"/api/projects/{self.project['id']}/uploads/{upload['id']}"
                )
            elif action == "finish":
                response = self.finish(upload["id"])
            else:
                response = self.abort(upload["id"])
            self.assertEqual(response.status_code, 410, f"{action}: {response.get_data(as_text=True)}")
            self.assertEqual(body(response)["error"], "UPLOAD_EXPIRED")

        self.assertEqual(self.assets(), [])
        self.assertEqual(self.upload_dir_leftovers(), [])

    def test_completed_session_survives_expiry(self) -> None:
        """已完成的幂等取回不受过期影响，不会把用户已保存的资产说没。"""
        data = zip_bytes(b"q" * 3000)
        upload = self.start(data)
        self.upload_all(upload, data)
        asset = body(self.finish(upload["id"]))["data"]["asset"]
        self.expire(upload["id"])

        again = self.finish(upload["id"])
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertEqual(body(again)["data"]["asset"]["id"], asset["id"])

    def test_abort_while_assembling_is_busy_not_success(self) -> None:
        data = zip_bytes(b"r" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data)
        self.patch_session(upload["id"], status="assembling")

        busy = self.abort(upload["id"])
        self.assertEqual(busy.status_code, 409, busy.get_data(as_text=True))
        self.assertEqual(body(busy)["error"], "UPLOAD_BUSY")
        # 取消没有真的发生：会话与块都还在，不会出现“取消成功但资产后来产生”
        self.assertEqual(self.session_state(upload["id"])["status"], "assembling")
        self.assertTrue(self.chunk_path(upload["id"], 0).is_file())
        self.assertEqual(self.assets(), [])

        # 合并卡死（占位时间过旧）后可重新认领，且只产生一份资产
        self.patch_session(upload["id"], updated_at="2000-01-01T00:00:00Z")
        done = self.finish(upload["id"])
        self.assertEqual(done.status_code, 200, done.get_data(as_text=True))
        self.assertEqual(len(self.assets()), 1)

    def test_rebound_root_invalidates_upload_and_keeps_old_files(self) -> None:
        """项目改绑目录后，旧会话必须失效，且不能按新根乱删/乱查旧块。"""
        data = zip_bytes(b"s" * 9000)
        upload = self.start(data)
        self.upload_all(upload, data)
        old_part = self.chunk_path(upload["id"], 0)
        self.assertTrue(old_part.is_file())

        new_root = self.harness.make_dir("alice-workspace-2")
        self.harness.add_root(new_root, label="alice-2", grant_to=["alice_upload"])
        connection = self.harness.conn()
        try:
            row = project_service.get_owned(connection, self.project["id"], self.alice["id"])
            project_service.bind_root(connection, row, raw_path=str(new_root), replace=True)
        finally:
            connection.close()

        changed = self.finish(upload["id"])
        self.assertEqual(changed.status_code, 409, changed.get_data(as_text=True))
        self.assertEqual(body(changed)["error"], "UPLOAD_ROOT_CHANGED")
        status_after = self.client.get(
            f"/api/projects/{self.project['id']}/uploads/{upload['id']}"
        )
        self.assertEqual(status_after.status_code, 409, status_after.get_data(as_text=True))
        put_after = self.put(upload["id"], 0, data[: upload["chunk_size"]])
        self.assertEqual(put_after.status_code, 409, put_after.get_data(as_text=True))

        # 旧根里的块原样保留（没有被“按新根清理”误删），资产一条也没有
        self.assertTrue(old_part.is_file())
        self.assertEqual(self.assets(), [])

    def test_orphan_upload_dir_is_swept(self) -> None:
        orphan = self.root / ".cache" / "uploads" / "upl_orphan"
        orphan.mkdir(parents=True)
        (orphan / "00000.part").write_bytes(b"x")

        live = self.start(zip_bytes(b"t" * 500))
        self.assertFalse(orphan.exists())
        self.assertIn(live["id"], self.upload_dir_leftovers())

    def test_shared_root_sweep_keeps_other_projects_active_chunks(self) -> None:
        """同一目录根绑定两个项目时，另一个项目 init/status 的惰性清理不能删掉仍在传的块。"""
        other = self.harness.create_project(self.alice["id"], name="同根项目")
        self.harness.bind_project(other["id"], self.root, "alice_upload")

        data = zip_bytes(b"a" * 5000)
        first = self.start(data)
        piece = data[: first["chunk_size"]]
        response = self.put(first["id"], 0, piece)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(self.chunk_path(first["id"], 0).exists())

        # 另一个项目在同一根上初始化新上传，触发孤儿扫描。
        second = self.start(zip_bytes(b"b" * 900), project_id=other["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(
            self.chunk_path(first["id"], 0).exists(),
            "共享根下另一个项目的 init 删掉了本项目的活动块目录",
        )

        for index in range(1, first["chunk_count"]):
            start = index * first["chunk_size"]
            response = self.put(first["id"], index, data[start : start + first["chunk_size"]])
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        finished = self.finish(first["id"])
        self.assertEqual(finished.status_code, 200, finished.get_data(as_text=True))
        self.assertEqual(body(finished)["data"]["asset"]["sha256"], sha(data))

    def test_asset_saved_but_state_lost_recovers_without_duplicate(self) -> None:
        """模拟“资产已登记、完成状态没写”：重试要回到同一个资产，不能造第二份。"""
        data = zip_bytes(b"u" * 3000)
        upload = self.start(data)
        self.upload_all(upload, data)
        first = body(self.finish(upload["id"]))["data"]["asset"]

        self.patch_session(
            upload["id"],
            status="assembling",
            asset_id=None,
            updated_at="2000-01-01T00:00:00Z",
        )
        again = self.finish(upload["id"])
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertEqual(body(again)["data"]["asset"]["id"], first["id"])
        self.assertEqual(len(self.assets()), 1)

    def test_role_is_kept_for_chunked_upload(self) -> None:
        data = zip_bytes(b"v" * 3000)
        response = self.init(len(data), chunk_size=4096, role="ir")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        upload = body(response)["data"]["upload"]
        self.upload_all(upload, data)
        asset = body(self.finish(upload["id"]))["data"]["asset"]
        self.assertEqual(asset["role"], "ir")

    def test_concurrent_same_chunk_keeps_one_row_and_right_bytes(self) -> None:
        data = zip_bytes(b"w" * 9000)
        upload = self.start(data)
        piece = data[: upload["chunk_size"]]
        codes: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            client = self.harness.client()
            login(client, "alice_upload")
            response = self.put(upload["id"], 0, piece, client=client)
            with lock:
                codes.append(response.status_code)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(codes, [200, 200, 200, 200])
        connection = self.harness.conn()
        try:
            rows = connection.execute(
                "SELECT COUNT(*) AS c FROM upload_chunks WHERE upload_id = ? AND index_no = 0",
                (upload["id"],),
            ).fetchone()["c"]
            received = connection.execute(
                "SELECT received_bytes FROM upload_sessions WHERE id = ?", (upload["id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(rows, 1)
        self.assertEqual(received, len(piece))
        self.assertEqual(self.chunk_path(upload["id"], 0).read_bytes(), piece)

    def test_concurrent_conflicting_chunk_rejects_loser_without_damage(self) -> None:
        data = zip_bytes(b"y" * 9000)
        upload = self.start(data)
        unit = upload["chunk_size"]
        first = data[:unit]
        second = bytes([first[0] ^ 0xFF]) + first[1:]
        codes: list[int] = []
        lock = threading.Lock()

        def worker(piece: bytes) -> None:
            client = self.harness.client()
            login(client, "alice_upload")
            response = self.put(upload["id"], 0, piece, client=client)
            with lock:
                codes.append(response.status_code)

        threads = [threading.Thread(target=worker, args=(piece,)) for piece in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(codes), [200, 409])
        winner = self.chunk_path(upload["id"], 0).read_bytes()
        self.assertIn(winner, (first, second))
        # 磁盘上的赢家必须与库里登记的一致
        connection = self.harness.conn()
        try:
            recorded = connection.execute(
                "SELECT sha256 FROM upload_chunks WHERE upload_id = ? AND index_no = 0",
                (upload["id"],),
            ).fetchone()["sha256"]
        finally:
            connection.close()
        self.assertEqual(recorded, sha(winner))

    def test_concurrent_init_respects_session_limit(self) -> None:
        results: list[int] = []
        lock = threading.Lock()
        # 先在主线程登录好各自的 client：并发 login 会争同一个 cookie jar，那是测试脚手架问题。
        clients = []
        for _ in range(8):
            client = self.harness.client()
            login(client, "alice_upload")
            clients.append(client)

        def worker(client) -> None:
            response = self.init(1000, client=client)
            with lock:
                results.append((response.status_code, response.get_data(as_text=True)[:160]))

        threads = [threading.Thread(target=worker, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        codes = [code for code, _ in results]
        self.assertEqual(codes.count(200), 4, results)
        self.assertEqual(codes.count(429), 4, results)
        connection = self.harness.conn()
        try:
            live = connection.execute(
                "SELECT COUNT(*) AS c FROM upload_sessions WHERE owner_id = ? AND project_id = ?"
                " AND status != 'completed'",
                (self.alice["id"], self.project["id"]),
            ).fetchone()["c"]
        finally:
            connection.close()
        self.assertEqual(live, 4)


if __name__ == "__main__":
    unittest.main()
