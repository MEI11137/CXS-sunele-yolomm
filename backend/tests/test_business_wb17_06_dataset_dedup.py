"""WB17-06：数据集导入按**内容**去重（v1.6.1 日志回归）。

独立反例：同一份合法 RGB+IR 目录用两个名字导入，v1.7 会建出两个 dataset。
这里钉住的行为：

* 内容/模态/类别/标注/划分相同 → 复用已有数据集，返回 ``reused=True``，不再建第二份；
* 改名、改内部文件名、ZIP 元数据不影响判定；
* 改标注、改划分、改 X 配图、改类别 → 视为不同版本，各自建库；
* 回收站里的同名内容不会被自动复活；
* 并发导入由唯一索引兜底，只有一个落库。
"""

from __future__ import annotations

import json
import secrets
import threading

from backend.services import datasets as dataset_service
from backend.services import storage
from backend.tests.test_business_wb17_03 import BusinessBase, png_bytes


class DatasetImportDedupTest(BusinessBase):
    def _source(self, name: str, *, color=(120, 80, 40), ir: bool = False, label: str = "0 0.5 0.5 0.2 0.2\n") -> str:
        """在项目内造一份合法数据集目录，返回相对路径。"""
        rel = f"uploads/src-{name}-{secrets.token_hex(4)}"
        base = storage.project_dir(self.root, *rel.split("/"))
        for split in ("train", "val"):
            storage.write_bytes(self.root, base / "images" / split / "a.png", png_bytes(color=color))
            storage.write_bytes(self.root, base / "labels" / split / "a.txt", label.encode())
            if ir:
                storage.write_bytes(self.root, base / "images_ir" / split / "a.png", png_bytes(color=(10, 20, 30)))
        return rel

    def _import(self, rel: str, name: str, *, ir: bool = False):
        conn = self.conn()
        try:
            return dataset_service.import_dataset(
                conn,
                self.alice["id"],
                self.project["id"],
                name=name,
                modalities=["rgb", "ir"] if ir else ["rgb"],
                secondary_modality="ir" if ir else None,
                classes=["单一热斑", "大面积热斑"],
                rel_path=rel,
            )
        finally:
            conn.close()

    def _rows(self) -> list:
        conn = self.conn()
        try:
            return conn.execute(
                "SELECT * FROM datasets WHERE project_id = ?", (self.project["id"],)
            ).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------ 复用

    def test_same_directory_imported_under_two_names_reuses_the_first(self):
        rel = self._source("dup")
        first = self._import(rel, "巡检集")
        second = self._import(rel, "巡检集-副本")
        self.assertFalse(first.get("reused"), "第一次导入是新建")
        self.assertTrue(second.get("reused"), "相同内容必须复用已有数据集")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self._rows()), 1, "不能建出第二份正式记录")

    def test_renamed_inner_files_still_match(self):
        """内容相同、内部文件名不同（改名/换压缩包）仍算同一份。"""
        rel = self._source("rename-a")
        first = self._import(rel, "A")
        base = storage.project_dir(self.root, *rel.split("/"))
        for split in ("train", "val"):
            (base / "images" / split / "a.png").rename(base / "images" / split / "renamed.png")
            (base / "labels" / split / "a.txt").rename(base / "labels" / split / "renamed.txt")
        second = self._import(rel, "B")
        self.assertTrue(second.get("reused"))
        self.assertEqual(second["id"], first["id"])

    # ------------------------------------------------------------------ 不同版本

    def test_changed_label_creates_a_new_dataset(self):
        rel = self._source("label")
        first = self._import(rel, "标签版")
        base = storage.project_dir(self.root, *rel.split("/"))
        storage.write_bytes(self.root, base / "labels" / "train" / "a.txt", b"1 0.4 0.4 0.3 0.3\n")
        second = self._import(rel, "标签版-改")
        self.assertNotEqual(second["id"], first["id"], "标注变了就是另一个有效版本")

    def test_changed_split_creates_a_new_dataset(self):
        rel = self._source("split")
        first = self._import(rel, "划分版")
        base = storage.project_dir(self.root, *rel.split("/"))
        # 把 val 的样本挪到 train：内容一样，划分不同。
        (base / "images" / "val" / "a.png").replace(base / "images" / "train" / "b.png")
        (base / "labels" / "val" / "a.txt").replace(base / "labels" / "train" / "b.txt")
        second = self._import(rel, "划分版-改")
        self.assertNotEqual(second["id"], first["id"])

    def test_changed_x_modality_creates_a_new_dataset(self):
        rel = self._source("dual", ir=True)
        first = self._import(rel, "双模态", ir=True)
        base = storage.project_dir(self.root, *rel.split("/"))
        storage.write_bytes(self.root, base / "images_ir" / "train" / "a.png", png_bytes(color=(200, 10, 10)))
        second = self._import(rel, "双模态-改", ir=True)
        self.assertNotEqual(second["id"], first["id"], "X 配图换了就是新版本")

    def test_different_content_is_not_merged_by_size(self):
        same_size_a = self._source("size-a", color=(1, 2, 3))
        first = self._import(same_size_a, "同尺寸 A")
        same_size_b = self._source("size-b", color=(4, 5, 6))
        second = self._import(same_size_b, "同尺寸 B")
        self.assertNotEqual(second["id"], first["id"], "内容不同就不能只按大小合并")

    # ------------------------------------------------------------------ 回收站

    def test_deleted_dataset_is_not_revived(self):
        rel = self._source("trash")
        first = self._import(rel, "会被删的")
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE datasets SET deleted_at = ? WHERE id = ?",
                ("2026-11-06T00:00:00Z", first["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        second = self._import(rel, "重建")
        self.assertNotEqual(second["id"], first["id"], "回收站资源不自动复活")
        self.assertFalse(second.get("reused"))

    # ------------------------------------------------------------------ 并发

    def test_concurrent_import_keeps_exactly_one(self):
        rel = self._source("race")
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            conn = self.conn()
            try:
                barrier.wait(timeout=5)
                results.append(
                    dataset_service.import_dataset(
                        conn,
                        self.alice["id"],
                        self.project["id"],
                        name=name,
                        modalities=["rgb"],
                        classes=["单一热斑", "大面积热斑"],
                        rel_path=rel,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 断言时再区分
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(f"并发-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        self.assertEqual(len({item["id"] for item in results}), 1, "并发只能落一份")
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(sum(1 for item in results if item.get("reused")), 1)
