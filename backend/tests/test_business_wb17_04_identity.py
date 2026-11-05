"""WB17-04 补修：老样本（指纹为 NULL）的真实身份、来源同名不等于同图、预览过期。

反例全部来自协作者的真实浏览器复现：

1. 03 导入的 ``dataset_samples`` 在 04 升级后 ``sha256/pixel_sha256/origin_sample_key``
   全为 NULL，同字节的检测图被判 ``NEW_IMAGE``，回原修订做不成；
2. 来源路径同名但内容不同，旧逻辑把 ``transformed`` 算强命中，只有一条就直接替换；
3. 改了表单仍能拿旧 draft 定稿。

这里的文件都是**真的**写在项目工作区里的，不是 mock 的哈希字符串。
"""

from __future__ import annotations

import json
import os
import secrets
import unittest
from pathlib import Path
from unittest import mock

from backend.errors import ApiError
from backend.services import identity as identity_service
from backend.services import reflow as reflow_service
from backend.services import storage

from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_04 import ReviewBase

CLASSES = ["单一热斑", "大面积热斑"]
LEGACY_SAMPLES = (
    ("train", "000084", (120, 80, 40)),
    ("val", "000220", (10, 200, 10)),
    ("test", "000096", (20, 20, 200)),
)


class LegacyIdentityBase(ReviewBase):
    """构造一个“03 导入”的数据集：文件真实、身份列全 NULL。"""

    def legacy_dataset(self) -> tuple[str, Path]:
        dataset_id = f"dst_{secrets.token_hex(8)}"
        base = self.root / "datasets" / dataset_id
        rows = []
        for split, stem, color in LEGACY_SAMPLES:
            data = png_bytes(color=color)
            rgb = base / "raw" / "images" / split / f"{stem}.png"
            x = base / "raw" / "images_ir" / split / f"{stem}.png"
            label = base / "raw" / "labels" / split / f"{stem}.txt"
            for path, payload in (
                (rgb, data),
                (x, png_bytes(color=(9, 9, 9))),
                (label, b"0 0.5 0.5 0.2 0.2\n"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            rows.append(
                (
                    f"smp_{secrets.token_hex(8)}",
                    split,
                    stem,
                    storage.relative(self.root, rgb),
                    storage.relative(self.root, x),
                    storage.relative(self.root, label),
                )
            )
        (base / "data.yaml").write_text(
            "path: .\nnc: 2\nnames: [单一热斑, 大面积热斑]\n", encoding="utf-8"
        )
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, secondary_modality,"
                " classes, root_rel, source, sample_count, split_counts, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'ir', ?, ?, 'local', ?, ?, 'ready', ?)",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    "03 导入集",
                    json.dumps(["rgb", "ir"]),
                    json.dumps(CLASSES, ensure_ascii=False),
                    storage.relative(self.root, base),
                    len(rows),
                    json.dumps({"train": 1, "val": 1, "test": 1}),
                    "2026-11-04T00:00:00Z",
                ),
            )
            # 关键：完全按 03 的列集合写入，不带 sha256 / pixel_sha256 / origin_sample_key。
            conn.executemany(
                "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel,"
                " width, height, boxes, class_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 64, 48, '[{\"class_id\": 0, \"bbox\": [4, 4, 30, 30]}]',"
                " '[0]', '2026-11-04T00:00:00Z')",
                [(row[0], dataset_id, row[1], row[2], row[3], row[4], row[5]) for row in rows],
            )
            conn.commit()
        finally:
            conn.close()
        return dataset_id, base

    def snapshot(self, base: Path) -> dict[str, tuple[int, int, str]]:
        """目录快照：大小 / mtime / sha，用来证明原版一个字节都没动。"""
        state = {}
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            state[str(path.relative_to(base))] = (
                int(stat.st_size),
                int(stat.st_mtime_ns),
                identity_service.file_digest(path.read_bytes()),
            )
        return state

    def _same_bytes_record(self) -> dict:
        model = self.make_model()
        asset = self.upload_asset(png_bytes(), "000084.png", role="rgb")
        return self.detect(model, asset["id"], key=f"legacy-{secrets.token_hex(3)}")


    def sample_row(self, dataset_id: str, split: str) -> dict:
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ? AND split = ?",
                (dataset_id, split),
            ).fetchone()
            return dict(row)
        finally:
            conn.close()


class LegacyIdentityTest(LegacyIdentityBase):
    def test_legacy_null_fingerprints_are_read_from_real_files_and_replace_in_place(self):
        """真实反例：同字节的原图必须 unique 命中老样本，回原修订只换 label。"""
        dataset_id, base = self.legacy_dataset()
        before = self.snapshot(base)

        # 老行确实是“指纹全空”，否则这个用例就没证明力。
        conn = self.conn()
        try:
            raw = conn.execute(
                "SELECT sha256, pixel_sha256, origin_sample_key FROM dataset_samples"
                " WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(all(row["sha256"] is None for row in raw))
        self.assertTrue(all(row["origin_sample_key"] is None for row in raw))

        record = self._same_bytes_record()
        self.confirm(
            record["id"], boxes=[{"class_id": 1, "bbox": [8, 8, 40, 36], "severity": "high"}]
        )

        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
            self.assertEqual(plan["conflicts"], [], plan["conflicts"])
            self.assertEqual(plan["counts"]["replace"], 1)
            self.assertEqual(plan["counts"]["kept"], 3)
            self.assertEqual(plan["items"][0]["split"], "train", "回原替换必须沿用原划分")
            self.assertEqual(plan["items"][0]["stem"], "000084")

            # 预览就把老行按真实文件补齐了（来源键也从导入布局还原）
            filled = conn.execute(
                "SELECT sha256, pixel_sha256, origin_sample_key FROM dataset_samples"
                " WHERE dataset_id = ? AND split = 'train'",
                (dataset_id,),
            ).fetchone()
            self.assertEqual(filled["sha256"], identity_service.file_digest(png_bytes()))
            self.assertTrue(filled["pixel_sha256"])
            self.assertEqual(filled["origin_sample_key"], "train/000084")

            new = reflow_service.apply(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="回原修订版",
                request_key="legacy-rev-1",
            )
            samples = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ? ORDER BY split", (new["id"],)
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(new["sample_count"], 3, "回原修订不能增加样本数")
        self.assertEqual(new["parent_dataset_id"], dataset_id)
        self.assertEqual(new["kind"], "revision")
        self.assertEqual(sorted(row["split"] for row in samples), ["test", "train", "val"])

        target = next(row for row in samples if row["split"] == "train")
        self.assertEqual(target["stem"], "000084", "文件名与相对位置保持原样")
        line = storage.resolve_rel(self.root, target["label_rel"], must_exist=True).read_text(
            encoding="utf-8"
        )
        self.assertTrue(line.startswith("1 "), line)
        # 别的样本标签原样复制
        others = [row for row in samples if row["split"] != "train"]
        for row in others:
            copied = storage.resolve_rel(self.root, row["label_rel"], must_exist=True)
            self.assertEqual(copied.read_text(encoding="utf-8"), "0 0.5 0.5 0.2 0.2\n")
        # 新版本真实 IR 与 ZIP 都在工作区
        self.assertTrue(storage.resolve_rel(self.root, target["x_rel"], must_exist=True).is_file())
        self.assertTrue(storage.resolve_rel(self.root, new["export_rel"], must_exist=True).is_file())
        # 原版未改
        self.assertEqual(self.snapshot(base), before, "原数据集目录不能被改动")

    def test_missing_sample_file_is_reported_not_treated_as_new_image(self):
        dataset_id, base = self.legacy_dataset()
        (base / "raw" / "images" / "val" / "000220.png").unlink()
        record = self._same_bytes_record()
        self.confirm(record["id"])

        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
            with self.assertRaises(ApiError) as ctx:
                reflow_service.apply(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    mode="revision",
                    dataset_id=dataset_id,
                    selections=[{"record_id": record["id"], "action": "replace"}],
                    name="不该生成",
                    request_key="legacy-missing",
                )
            total = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        finally:
            conn.close()

        codes = {item["code"] for item in plan["conflicts"]}
        self.assertIn("SAMPLE_MISSING", codes, plan["conflicts"])
        self.assertNotIn("NEW_IMAGE", codes, "文件丢失不能当成“新图”")
        self.assertEqual(ctx.exception.code, "REFLOW_CONFLICTS")
        self.assertEqual(total, 1, "有冲突时不能落任何数据集行")

    def test_changed_file_is_not_overwritten_by_cached_fingerprint(self):
        dataset_id, base = self.legacy_dataset()
        # 先跑一次预览，把老行的指纹按真实文件补上（这就是“缓存”）。
        conn = self.conn()
        try:
            identity_service.prepare_samples(conn, self.alice["id"], self.project["id"], dataset_id)
            cached = conn.execute(
                "SELECT sha256 FROM dataset_samples WHERE dataset_id = ? AND split = 'test'",
                (dataset_id,),
            ).fetchone()["sha256"]
        finally:
            conn.close()
        self.assertTrue(cached)

        # 有人把数据集里的文件换掉了：内容变了，缓存必须服从文件。
        (base / "raw" / "images" / "test" / "000096.png").write_bytes(
            png_bytes(color=(250, 250, 250))
        )

        record = self._same_bytes_record()
        self.confirm(record["id"])
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
            after = conn.execute(
                "SELECT sha256 FROM dataset_samples WHERE dataset_id = ? AND split = 'test'",
                (dataset_id,),
            ).fetchone()["sha256"]
        finally:
            conn.close()

        codes = {item["code"] for item in plan["conflicts"]}
        self.assertIn("SAMPLE_CHANGED", codes, plan["conflicts"])
        self.assertEqual(after, cached, "发现文件被改时不能拿新内容覆盖缓存当无事发生")

    def _same_length_png(self, original: bytes) -> bytes:
        """造一张与 ``original`` 等长、内容不同的 PNG（靠换颜色凑出同样的压缩长度）。"""
        target = len(original)
        colors = (
            [(red, 80, 40) for red in range(256)]
            + [(120, green, 40) for green in range(256)]
            + [(120, 80, blue) for blue in range(256)]
        )
        for color in colors:
            data = png_bytes(color=color)
            if data != original and len(data) == target:
                return data
        self.fail("没有找到与样本等长的 PNG 内容，无法复刻反例")

    def test_same_size_with_restored_mtime_still_verifies_bytes(self):
        """协作者探针的反例：内容换掉、字节长度相同、mtime 用 ``os.utime`` 改回去。

        文件戳（size + mtime）完全一致，所以戳不能证明内容没变；必须比字节。
        """
        dataset_id, base = self.legacy_dataset()
        conn = self.conn()
        try:
            identity_service.prepare_samples(
                conn, self.alice["id"], self.project["id"], dataset_id
            )
            cached = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ? AND split = 'train'",
                (dataset_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertTrue(cached["sha256"], "先要把老行的指纹按真实文件补上")

        source = base / "raw" / "images" / "train" / "000084.png"
        before = source.stat()
        replacement = self._same_length_png(source.read_bytes())
        source.write_bytes(replacement)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = source.stat()
        self.assertEqual(
            (after.st_size, after.st_mtime_ns),
            (before.st_size, before.st_mtime_ns),
            "反例的前提是戳一模一样",
        )
        self.assertEqual(int(cached["identity_size"]), after.st_size)
        self.assertEqual(int(cached["identity_mtime_ns"]), after.st_mtime_ns)

        record = self._same_bytes_record()  # 与原始 000084 同字节
        self.confirm(record["id"])
        selections = [{"record_id": record["id"], "action": "replace"}]
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=selections,
            )
            kept = conn.execute(
                "SELECT sha256, pixel_sha256 FROM dataset_samples WHERE id = ?",
                (cached["id"],),
            ).fetchone()
        finally:
            conn.close()

        codes = {item["code"] for item in plan["conflicts"]}
        self.assertIn("SAMPLE_CHANGED", codes, plan["conflicts"])
        self.assertNotIn("NEW_IMAGE", codes, "文件被改不等于新图")
        self.assertEqual(plan["counts"]["replace"], 0, "被改过的样本不能计入替换数")
        self.assertEqual(kept["sha256"], cached["sha256"], "旧指纹不能被当前文件覆盖")
        self.assertEqual(kept["pixel_sha256"], cached["pixel_sha256"])
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                reflow_service.apply(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    mode="revision",
                    dataset_id=dataset_id,
                    selections=selections,
                    name="同长度换内容",
                    request_key=f"same-size-{secrets.token_hex(4)}",
                )
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "REFLOW_CONFLICTS")

    def test_same_content_touched_file_stays_cached_without_decoding(self):
        """对照：字节没变、只是 mtime 变了 —— 仍可信，且不必再解一遍像素。"""
        dataset_id, base = self.legacy_dataset()
        conn = self.conn()
        try:
            identity_service.prepare_samples(
                conn, self.alice["id"], self.project["id"], dataset_id
            )
            cached = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ? AND split = 'train'",
                (dataset_id,),
            ).fetchone()
        finally:
            conn.close()

        source = base / "raw" / "images" / "train" / "000084.png"
        before = source.stat()
        os.utime(source, ns=(before.st_atime_ns + 10**9, before.st_mtime_ns + 10**9))
        touched = source.stat()
        self.assertEqual(touched.st_size, before.st_size)
        self.assertNotEqual(touched.st_mtime_ns, before.st_mtime_ns)

        with mock.patch.object(
            identity_service, "pixel_digest", side_effect=AssertionError("字节一致时不该再解码")
        ), mock.patch.object(
            identity_service, "visual_hash", side_effect=AssertionError("字节一致时不该再算视觉哈希")
        ):
            info = identity_service.inspect_sample(self.root, dict(cached))
        self.assertEqual(info["state"], identity_service.IDENTITY_CACHED)
        self.assertTrue(info["stat_refresh"], "戳变了要顺手刷新，避免下次又白算")
        self.assertEqual(info["identity"]["pixel_sha256"], cached["pixel_sha256"])

        record = self._same_bytes_record()
        self.confirm(record["id"])
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
        finally:
            conn.close()
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["counts"]["replace"], 1)

class SourceKeyNotEvidenceTest(LegacyIdentityBase):
    def _record_with_source_key(self, filename: str, *, color, size=(64, 48)) -> dict:
        model = self.make_model()
        asset = self.upload_asset(
            png_bytes(width=size[0], height=size[1], color=color), filename, role="rgb"
        )
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE assets SET source_path = ? WHERE id = ?",
                ("raw/images/train/000084.jpg", asset["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return self.detect(model, asset["id"], key=f"src-{secrets.token_hex(3)}")

    def test_same_source_key_different_bytes_is_only_a_candidate(self):
        dataset_id, _ = self.legacy_dataset()
        record = self._record_with_source_key("000084.png", color=(3, 4, 5))
        self.confirm(record["id"], boxes=[{"class_id": 0, "bbox": [5, 5, 25, 25]}])

        conn = self.conn()
        try:
            samples = identity_service.prepare_samples(
                conn, self.alice["id"], self.project["id"], dataset_id
            )["samples"]
            matched = identity_service.match_record(
                conn, self.alice["id"], self.project["id"], record["id"], None, samples
            )
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
        finally:
            conn.close()

        self.assertEqual(matched["status"], "candidate", matched)
        self.assertEqual(matched["matches"], [], "同名不同内容不能算强命中")
        self.assertEqual(plan["counts"]["replace"], 0)
        self.assertEqual(plan["conflicts"][0]["code"], "MATCH_NEEDS_CHOICE", plan["conflicts"][0])
        self.assertTrue(plan["conflicts"][0]["matches"], "必须把候选列出来让用户选")

        # 显式指定目标仍然被尊重（尺寸一致，坐标可信）
        train_sample = self.sample_row(dataset_id, "train")
        conn = self.conn()
        try:
            plan2 = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[
                    {
                        "record_id": record["id"],
                        "action": "replace",
                        "target_sample_id": train_sample["id"],
                    }
                ],
            )
        finally:
            conn.close()
        self.assertEqual(plan2["conflicts"], [], plan2["conflicts"])
        self.assertEqual(plan2["counts"]["replace"], 1)

    def test_explicit_target_with_incompatible_size_is_refused(self):
        dataset_id, _ = self.legacy_dataset()
        record = self._record_with_source_key("000084.png", color=(3, 4, 5), size=(40, 30))
        self.confirm(record["id"], boxes=[{"class_id": 0, "bbox": [5, 5, 25, 25]}])
        train_sample = self.sample_row(dataset_id, "train")

        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[
                    {
                        "record_id": record["id"],
                        "action": "replace",
                        "target_sample_id": train_sample["id"],
                    }
                ],
            )
        finally:
            conn.close()
        self.assertEqual(plan["conflicts"][0]["code"], "TARGET_SIZE_MISMATCH", plan["conflicts"][0])
        self.assertEqual(plan["counts"]["replace"], 0)


class StalePlanTest(LegacyIdentityBase):
    def test_finalize_refuses_when_parent_changed_after_preview(self):
        dataset_id, base = self.legacy_dataset()
        record = self._same_bytes_record()
        self.confirm(record["id"])

        conn = self.conn()
        try:
            draft = reflow_service.save_draft(
                conn,
                self.alice["id"],
                self.project["id"],
                name="过期预览",
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                strategy="even",
                ratios=None,
                seed=7,
            )
            self.assertEqual(draft["status"], "ready")
            self.assertTrue(draft["plan"]["signature"], "预览必须带指纹，定稿才有比对依据")

            # 预览之后父数据集多了样本（真实文件 + 真实行）
            extra = base / "raw" / "images" / "train" / "000300.png"
            extra.write_bytes(png_bytes(color=(1, 1, 1)))
            conn.execute(
                "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel,"
                " width, height, boxes, class_ids, created_at)"
                " VALUES (?, ?, 'train', '000300', ?, NULL, NULL, 64, 48, '[]', '[]', ?)",
                (
                    f"smp_{secrets.token_hex(8)}",
                    dataset_id,
                    storage.relative(self.root, extra),
                    "2026-11-05T00:00:00Z",
                ),
            )
            conn.commit()

            with self.assertRaises(ApiError) as ctx:
                reflow_service.finalize_draft(conn, self.alice["id"], self.project["id"], draft["id"])
            total = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
            status = conn.execute(
                "SELECT status FROM reflow_drafts WHERE id = ?", (draft["id"],)
            ).fetchone()["status"]
        finally:
            conn.close()

        self.assertEqual(ctx.exception.code, "PLAN_STALE")
        self.assertEqual(total, 1, "计划过期时不能生成数据集")
        self.assertEqual(status, "ready", "过期不是失败，用户可以重新预览")


if __name__ == "__main__":
    unittest.main()
