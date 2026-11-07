"""WB17-05：权重必须是真实可加载的模型，而不是“有 names 就算数”。

独立探针（``verify_phase05_nonmodel_checkpoint.py``）造的是
``torch.save({'names': {0: ..., 1: ...}})``：只有类别表、没有 model/ema。
这种文件以前会被 ``inspect_weight`` 当成合法权重（``kind=unknown``），
``models.create_trained`` 于是把它登记成“训练成功”的模型，等到真正推理才炸。

这里的用例全部走**真实的**安全加载与架构校验（不注入假实现）：
文本伪 PT、空字典、只有 names、model 不是模型对象都必须拒绝；
真实的 DetectionModel（含 ema-only 的形态）必须继续被接受。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.tests.weights import NAMES, write_real_weight


class WeightValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from backend import config
        from backend.vision import runtime as vision_runtime

        cls.tmp = Path(tempfile.mkdtemp(prefix="hx17-weight-"))
        # 权重加载会读 YOLO/Matplotlib 配置：固定到临时目录，不往用户主目录写文件。
        vision_runtime.configure(
            config.load_settings(data_dir=cls.tmp / "private", serve_dist=False)
        )

    def setUp(self) -> None:
        try:
            import torch  # noqa: F401
        except Exception:  # pragma: no cover - 无 torch 的环境直接跳过
            self.skipTest("本机没有 torch，跳过权重反序列化用例")
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))

    # ------------------------------------------------------------------ 工具

    def _inspect(self, name: str):
        from backend.vision import runtime as vision_runtime

        return vision_runtime.inspect_weight(self.case / name)

    def _reject(self, name: str, code: str) -> None:
        from backend.vision import runtime as vision_runtime

        with self.assertRaises(vision_runtime.VisionError) as ctx:
            self._inspect(name)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)

    def _load_real_checkpoint(self) -> dict:
        from backend.vision import safe_weight

        path = write_real_weight(self.case / "source.pt")
        return safe_weight.load(path)

    # ------------------------------------------------------------------ 接受

    def test_real_detection_model_is_accepted(self):
        write_real_weight(self.case / "real.pt")
        info = self._inspect("real.pt")
        self.assertEqual(info["kind"], "DetectionModel")
        self.assertEqual(info["class_count"], len(NAMES))
        self.assertEqual(
            info["classes"],
            [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}],
        )

    def test_ema_only_checkpoint_is_accepted(self):
        """训练态权重可能把模型放在 ema、model 为 None：必须继续支持。"""
        import torch

        checkpoint = self._load_real_checkpoint()
        torch.save({"model": None, "ema": checkpoint["model"], "names": dict(NAMES)}, self.case / "ema.pt")
        info = self._inspect("ema.pt")
        self.assertEqual(info["kind"], "DetectionModel")
        self.assertEqual(info["class_count"], len(NAMES))

    def test_ema_wins_over_model_like_vendor(self):
        """vendor 加载是 ``(ema or model)``：两个键同时非空时必须选 ema。"""
        import torch

        checkpoint = self._load_real_checkpoint()
        torch.save(
            {"model": torch.nn.Linear(2, 2), "ema": checkpoint["model"], "names": dict(NAMES)},
            self.case / "ema_wins.pt",
        )
        info = self._inspect("ema_wins.pt")
        self.assertEqual(info["kind"], "DetectionModel", "检查器必须和 vendor 一样先看 ema")

    def test_invalid_ema_is_rejected_even_when_model_is_valid(self):
        """反向组合：model 合法、ema 不合法 → vendor 会加载坏 ema，检查器也必须拒绝。"""
        import torch

        checkpoint = self._load_real_checkpoint()
        torch.save(
            {"model": checkpoint["model"], "ema": torch.nn.Linear(2, 2), "names": dict(NAMES)},
            self.case / "bad_ema.pt",
        )
        self._reject("bad_ema.pt", "VISION_WEIGHT_NOT_MODEL")

    # ------------------------------------------------------------------ 拒绝

    def test_names_only_checkpoint_is_rejected(self):
        import torch

        torch.save({"names": {0: "单一热斑", 1: "大面积热斑"}}, self.case / "names_only.pt")
        self._reject("names_only.pt", "VISION_WEIGHT_NOT_MODEL")

    def test_empty_checkpoint_is_rejected(self):
        import torch

        torch.save({}, self.case / "empty.pt")
        self._reject("empty.pt", "VISION_WEIGHT_NOT_MODEL")

    def test_explicit_none_model_with_names_is_rejected(self):
        import torch

        torch.save({"model": None, "names": {0: "单一热斑"}}, self.case / "none.pt")
        self._reject("none.pt", "VISION_WEIGHT_NOT_MODEL")

    def test_non_model_object_is_rejected(self):
        """有 model 键但内容是普通数据：不是可运行的网络，同样拒绝。"""
        import torch

        torch.save({"model": {"layers": 3}, "names": {0: "单一热斑"}}, self.case / "dict_model.pt")
        self._reject("dict_model.pt", "VISION_WEIGHT_NOT_MODEL")

    def test_text_file_named_pt_is_rejected(self):
        (self.case / "text.pt").write_bytes(b"fake-trained-weight")
        self._reject("text.pt", "VISION_WEIGHT_INVALID")

    # ------------------------------------------------------------------ 白名单

    def test_official_yolo11_block_symbols_are_audited(self):
        """官方 YOLO11 权重引用 ``nn/modules/block`` 下的 C2PSA/PSABlock。

        现场（2026-11-07）：用户导入官方 ``yolo11n.pt`` 被
        ``VISION_WEIGHT_UNTRUSTED（C2PSA、PSABlock）`` 拒绝——白名单只有 MM 版的
        ``extraction.*`` 同名类，没有上游 ``nn.modules.block.*`` 路径。这里锁住两条
        已审核符号，并要求它们解析成 vendor 里真实的 ``nn.Module`` 层，而不是把
        闸门放宽成“任意符号都放行”。
        """
        import torch

        from backend.vision import runtime as vision_runtime
        from backend.vision import safe_weight

        vision_runtime.activate_vendor()
        for ref in (
            "ultralytics.nn.modules.block.PSABlock",
            "ultralytics.nn.modules.block.C2PSA",
        ):
            self.assertIn(ref, safe_weight.ALLOWED_SYMBOLS)
            resolved = safe_weight._resolve_approved(ref)
            self.assertTrue(issubclass(resolved, torch.nn.Module), ref)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
