"""WB17-05：训练锁、受控子进程、取消/重启核对、父子对比与模型完整管理。

这里的 worker 全部是**测试注入**的假进程（``training.set_worker_command``）：
产品默认走 ``backend.training_worker``，没有任何开关能让生产环境“假成功”。
假 worker 仍然是真的独立进程，所以锁、PID 身份校验、取消与终态判定都是真实路径。

假 worker 的 ``best.pt`` 使用**真实可校验的权重模板**（``backend/tests/weights.py``
即时构建的极小 ``DetectionModel`` checkpoint）：产物登记走的是生产同一条安全加载与
架构校验路径，文本文件、只有 ``names`` 的字典改名成 best.pt 都会被拒绝（有反例用例）。
专门有一条用例用**真实**
``backend.training_worker`` 跑一次 1 epoch CPU 微训练，验证运行时配置 →
vendor 数据解析器 → 真实训练 → 登记发布全链路。它会真的启动训练进程，默认
**跳过**，需 ``HX17_RUN_REAL_TRAINING_TEST=1`` 显式开启。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from backend.errors import ApiError
from backend.services import jobs as job_service
from backend.services import models as model_service
from backend.services import storage
from backend.services import training as training_service

from backend.tests.support import csrf_headers
from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_04 import ReviewBase
from backend.tests.weights import write_mm_weight, write_real_weight


def psutil_pid_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(int(pid)).is_running()
    except psutil.NoSuchProcess:
        return False

FAKE_METRICS = {"map50": 0.81, "map50_95": 0.52, "precision": 0.75, "recall": 0.7}

# 成功 worker：复制真实可校验权重模板到 best.pt（产物登记要过安全加载）。
# 双模态训练必须产出**真的有路由**的权重，否则登记会以 TRAINED_WEIGHT_MODALITY_MISMATCH
# 拒绝——所以按 params.json 的 family 选模板，而不是拿单模态重量冒充 YOLOMM 产物。
_SUCCESS_WORKER = '''
import json, pathlib, shutil, sys
run_dir = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1]).resolve()
root = run_dir.parents[1]
weight = run_dir / "best.pt"
params = json.loads((run_dir / "params.json").read_text(encoding="utf-8"))
template = r"%(mm_template)s" if params.get("family") == "yolomm" else r"%(template)s"
shutil.copyfile(template, weight)
rel = weight.relative_to(root).as_posix()
(run_dir / "worker.json").write_text(json.dumps({
    "status": "succeeded",
    "metrics": %(metrics)s,
    "conditions": params["conditions"],
    "classes": ["单一热斑", "大面积热斑"],
    "split": "val",
    "source": "测试假 worker（不算真实训练）",
    "images": 1,
    "epochs_run": 1,
    "weight_rel": rel,
    "notes": "假 worker 产出（复制真实可校验权重模板），仅用于验证服务编排",
}, ensure_ascii=False), encoding="utf-8")
''' % {"template": "%(template)s", "mm_template": "%(mm_template)s", "metrics": json.dumps(FAKE_METRICS)}

# 文本 best.pt 反例：生产登记必须拒绝（安全加载读不出类别表）。
_TEXT_WEIGHT_WORKER = '''
import json, pathlib, sys
run_dir = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1]).resolve()
root = run_dir.parents[1]
weight = run_dir / "best.pt"
weight.write_bytes(b"fake-trained-weight")
rel = weight.relative_to(root).as_posix()
(run_dir / "worker.json").write_text(json.dumps({
    "status": "succeeded",
    "metrics": %(metrics)s,
    "classes": ["单一热斑", "大面积热斑"],
    "class_map": {"0": 0, "1": 1},
    "weight_rel": rel,
}, ensure_ascii=False), encoding="utf-8")
''' % {"metrics": json.dumps(FAKE_METRICS)}

# 只有类别表、没有模型对象的“权重”：能过类别解析，但根本不是可运行模型。
_NAMES_ONLY_WORKER = '''
import json, pathlib, sys
import torch
run_dir = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1]).resolve()
weight = run_dir / "best.pt"
torch.save({"names": {0: "单一热斑", 1: "大面积热斑"}}, weight)
rel = weight.relative_to(run_dir.parents[1]).as_posix()
(run_dir / "worker.json").write_text(json.dumps({
    "status": "succeeded",
    "metrics": {"map50": 0.5},
    "classes": ["单一热斑", "大面积热斑"],
    "split": "val",
    "weight_rel": rel,
}, ensure_ascii=False), encoding="utf-8")
'''

_SLEEP_WORKER = '''
import pathlib, sys, time
run_dir = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1]).resolve()
(run_dir / "started.marker").write_text("1", encoding="utf-8")
time.sleep(120)
'''

_FAIL_WORKER = '''
import sys
sys.exit(3)
'''

_SLOW_SUCCESS_WORKER = '''
import json, pathlib, shutil, sys, time
run_dir = pathlib.Path(sys.argv[sys.argv.index("--run-dir") + 1]).resolve()
time.sleep(1.5)
weight = run_dir / "best.pt"
shutil.copyfile(r"%(template)s", weight)
rel = weight.relative_to(run_dir.parents[1]).as_posix()
(run_dir / "worker.json").write_text(json.dumps({
    "status": "succeeded",
    "metrics": %(metrics)s,
    "classes": ["单一热斑", "大面积热斑"],
    "class_map": {"0": 0, "1": 1},
    "weight_rel": rel,
}, ensure_ascii=False), encoding="utf-8")
''' % {"template": "%(template)s", "metrics": json.dumps(FAKE_METRICS)}


def _make_weight_template(target: Path) -> Path:
    """真实可校验的权重模板：真的 ``DetectionModel`` checkpoint，不是只有 names 的字典。"""
    return write_real_weight(target)


def _make_mm_weight_template(target: Path) -> Path:
    """双模态模板：真的带 YAML 路由注解与 X 模态 metadata 的 YOLOMM 结构。"""
    return write_mm_weight(target)


class TrainingBase(ReviewBase):
    def setUp(self):
        super().setUp()
        self.workers = self.harness.data_dir / "workers"
        self.workers.mkdir(parents=True, exist_ok=True)
        self.weight_template = _make_weight_template(self.workers / "weight_template.pt")
        self.mm_weight_template = _make_mm_weight_template(
            self.workers / "weight_template_mm.pt"
        )
        # vendor 数据解析器校验默认注入轻量实现（避免每个用例加载 torch）；
        # 专项用例用 set_config_validator(None) 走真实 vendor 解析器。
        training_service.set_config_validator(
            lambda path: {"resolved": {"train": str(path), "val": str(path)}}
        )
        self._mount(_SUCCESS_WORKER)

    def tearDown(self):
        training_service.set_worker_command(None)
        training_service.set_config_validator(None)
        training_service.set_device_probe(None)
        super().tearDown()

    # ------------------------------------------------------------------ 夹具

    def _mount(self, source: str, name: str = "worker.py") -> Path:
        if "%(template)s" in source:
            source = source % {
                "template": str(self.weight_template),
                "mm_template": str(self.mm_weight_template),
            }
        path = self.workers / name
        path.write_text(source, encoding="utf-8")
        training_service.set_worker_command([sys.executable, str(path)])
        return path

    def dataset(self, *, samples: int = 2, name: str | None = None, ir: bool = False) -> dict:
        dataset_id = f"dst_{secrets.token_hex(8)}"
        # 默认名带唯一后缀：同一用例多次建数据集不撞 UNIQUE(project_id, name)。
        name = name or f"训练集 {dataset_id[-6:]}"
        base = storage.project_dir(self.root, storage.DATASETS, dataset_id)
        for split in ("train", "val"):
            storage.project_dir(self.root, storage.DATASETS, dataset_id, "images", split)
            storage.project_dir(self.root, storage.DATASETS, dataset_id, "labels", split)
            if ir:
                storage.project_dir(self.root, storage.DATASETS, dataset_id, "images_ir", split)
        for split in ("train", "val"):
            storage.write_bytes(self.root, base / "images" / split / "a.png", png_bytes())
            storage.write_bytes(self.root, base / "labels" / split / "a.txt", b"0 0.5 0.5 0.2 0.2\n")
            if ir:
                storage.write_bytes(self.root, base / "images_ir" / split / "a.png", png_bytes())
        payload = {
            "names": ["单一热斑", "大面积热斑"],
            "nc": 2,
            "path": ".",
            "train": "images/train",
            "val": "images/val",
        }
        if ir:
            payload["modalities"] = {"rgb": "images", "ir": "images_ir"}
            payload["modality_used"] = ["rgb", "ir"]
        storage.write_bytes(
            self.root,
            base / "data.yaml",
            yaml.safe_dump(payload, allow_unicode=True).encode("utf-8"),
        )
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, secondary_modality,"
                " classes, root_rel, source, sample_count, split_counts, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, '{\"train\": 1, \"val\": 1}',"
                " 'ready', ?)",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    name,
                    json.dumps(["rgb", "ir"] if ir else ["rgb"]),
                    "ir" if ir else None,
                    json.dumps(["单一热斑", "大面积热斑"], ensure_ascii=False),
                    storage.relative(self.root, base),
                    samples,
                    "2026-11-05T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"id": dataset_id, "root": base}

    def start(self, dataset_id: str, *, key: str | None = None, **kwargs) -> tuple[dict, bool]:
        conn = self.conn()
        try:
            return training_service.start(
                conn,
                self.alice["id"],
                self.project["id"],
                dataset_id=dataset_id,
                request_key=key or f"train-{secrets.token_hex(4)}",
                **kwargs,
            )
        finally:
            conn.close()

    def wait_run(self, run_id: str, *, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = self.conn()
            try:
                run = training_service.public(
                    training_service.get_owned(conn, self.alice["id"], self.project["id"], run_id)
                )
            finally:
                conn.close()
            if run["status"] in training_service.TERMINAL:
                return run
            time.sleep(0.05)
        raise AssertionError(f"训练未在 {timeout}s 内结束：{run_id}")

    def wait_marker(self, run_id: str, *, timeout: float = 15.0) -> None:
        """等待假 worker 写出 started.marker（确认真进程已启动）。"""
        conn = self.conn()
        try:
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run_id)
            run_dir = self.root / str(row["run_dir_rel"])
        finally:
            conn.close()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (run_dir / "started.marker").is_file():
                return
            time.sleep(0.05)
        raise AssertionError("假 worker 没有启动")

    def lock(self) -> dict:
        conn = self.conn()
        try:
            return training_service.list_for_project(
                conn, self.alice["id"], self.project["id"], limit=1
            )["lock"]
        finally:
            conn.close()


class TrainingLifecycleTest(TrainingBase):
    def test_first_training_creates_model_with_real_metrics_and_releases_lock(self):
        dataset = self.dataset()
        run, created = self.start(dataset["id"], name="首训")
        self.assertTrue(created)
        self.assertIn(run["status"], {"queued", "running"})
        self.assertTrue(self.lock()["held"], "启动后训练槽必须被占用")

        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertFalse(self.lock()["held"], "训练结束后必须释放训练槽")
        self.assertTrue(finished["output_model_id"])

        conn = self.conn()
        try:
            model = model_service.public(
                model_service.get_owned(conn, self.alice["id"], self.project["id"], finished["output_model_id"])
            )
        finally:
            conn.close()
        self.assertEqual(model["source"], "trained")
        self.assertIsNone(model["parent_model_id"])
        self.assertEqual(model["training_run_id"], run["id"])
        self.assertEqual(model["metrics"]["values"]["map50"], FAKE_METRICS["map50"])
        self.assertEqual(model["metrics"]["split"], "val")
        self.assertTrue(model["metrics"]["source"])
        self.assertEqual(model["status"], "imported")
        # 类别契约：{index, name} 对象列表（与导入/检测端一致），不是字符串列表。
        self.assertEqual(
            model["classes"],
            [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}],
        )
        self.assertEqual(model["class_map"], {"0": 0, "1": 1})
        self.assertEqual(model["family"], "yolo")
        self.assertEqual(model["model_mode"], "single")

        # 权重与日志都真实落在项目工作区的 training/<run_id> 里
        root = self.root / finished["run_dir_rel"]
        self.assertTrue((root / "best.pt").is_file())
        self.assertTrue((root / "params.json").is_file())
        self.assertTrue((self.root / finished["log_rel"]).is_file())

    def test_same_request_key_does_not_start_twice(self):
        dataset = self.dataset()
        first, created = self.start(dataset["id"], key="same-key")
        self.assertTrue(created)
        second, created_again = self.start(dataset["id"], key="same-key")
        self.assertFalse(created_again)
        self.assertEqual(second["id"], first["id"])
        self.wait_run(first["id"])
        conn = self.conn()
        try:
            runs = training_service.list_for_project(
                conn, self.alice["id"], self.project["id"]
            )["runs"]
        finally:
            conn.close()
        self.assertEqual([item["id"] for item in runs], [first["id"]])

    def test_second_start_is_busy_while_lock_is_held(self):
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        run, _ = self.start(dataset["id"])
        self.wait_marker(run["id"])
        with self.assertRaises(ApiError) as ctx:
            self.start(dataset["id"], name="抢锁")
        self.assertEqual(ctx.exception.code, "TRAINING_BUSY")
        self.assertEqual(ctx.exception.status, 409)

        conn = self.conn()
        try:
            cancelled = training_service.cancel(
                conn, self.alice["id"], self.project["id"], run["id"]
            )
        finally:
            conn.close()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["terminated"])
        self.assertFalse(self.lock()["held"])

    def test_spawn_failure_releases_lock_and_marks_failed(self):
        dataset = self.dataset()
        # 可执行文件不存在 → Popen 直接抛错
        training_service.set_worker_command([str(self.workers / "no-such-python.exe")])
        with self.assertRaises(ApiError) as ctx:
            self.start(dataset["id"])
        self.assertEqual(ctx.exception.code, "TRAINING_SPAWN_FAILED")
        self.assertFalse(self.lock()["held"], "启动失败必须把锁放回去")

        conn = self.conn()
        try:
            runs = training_service.list_for_project(
                conn, self.alice["id"], self.project["id"]
            )["runs"]
        finally:
            conn.close()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["error"]["code"], "TRAINING_SPAWN_FAILED")

    def test_worker_exit_without_result_is_failed_not_success(self):
        self._mount(_FAIL_WORKER)
        dataset = self.dataset()
        run, _ = self.start(dataset["id"])
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"]["code"], "TRAINING_NO_RESULT")
        self.assertIsNone(finished["output_model_id"])
        self.assertFalse(self.lock()["held"])

    def test_cancel_kills_real_process_and_keeps_latest_state(self):
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        run, _ = self.start(dataset["id"])
        self.wait_marker(run["id"])
        conn = self.conn()
        try:
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
            pid = int(row["pid"])
            self.assertIsNotNone(row["pid_started_at"])
            result = training_service.cancel(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["terminated"])
        self.assertFalse(training_service._alive(pid, None), "取消必须真的结束进程")
        self.assertFalse(self.lock()["held"])

    def test_cancel_refuses_when_pid_does_not_belong_to_run(self):
        """PID 记录与真实进程对不上（被篡改/复用）：不对不认识的进程下手。"""
        dataset = self.dataset()
        self._mount(_SLEEP_WORKER)
        run, _ = self.start(dataset["id"])
        self.wait_marker(run["id"])
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE training_runs SET pid_started_at = 1.0 WHERE id = ?", (run["id"],)
            )
            conn.commit()
            result = training_service.cancel(conn, self.alice["id"], self.project["id"], run["id"])
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
            pid = int(row["pid"])
        finally:
            conn.close()
        # PID 对不上 → 判定“我们的进程已结束”（PID_REUSED），按真实产物收尾；
        # 绝不终止那个还在跑的真实进程（它现在属于“不认识的 PID”）。
        self.assertTrue(result["terminated"])
        self.assertIn("PID_REUSED", result["note"])
        self.assertTrue(psutil_pid_alive(pid), "不能误杀身份对不上的进程")
        self.assertEqual(result["status"], "failed")  # sleep worker 没有 worker.json → 结果未知
        self.assertFalse(self.lock()["held"])
        # 测试自己把假 worker 收掉，别留 120s 的后台进程。
        conn = self.conn()
        try:
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        try:
            os.kill(int(row["pid"]), 9)
        except OSError:
            pass
        training_service._reap(run["id"])


class TrainingRecoveryTest(TrainingBase):
    def test_restart_marks_leftover_run_interrupted_and_frees_lock(self):
        """模拟“进程被外部杀掉 + 服务重启”：库里仍是 running、锁还占着。"""
        dataset = self.dataset()
        run_id = f"trn_{secrets.token_hex(8)}"
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO training_runs (id, project_id, owner_id, job_id, dataset_id,"
                " parent_model_id, base_weight_rel, name, status, params, conditions, run_dir_rel,"
                " log_rel, pid, pid_started_at, request_key, created_at, started_at)"
                " VALUES (?, ?, ?, NULL, ?, NULL, '', '崩溃残留', 'running', '{}', '{}', ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    dataset["id"],
                    f"training/{run_id}",
                    f"training/{run_id}/train.log",
                    999999,
                    1.0,
                    "leftover-key",
                    "2026-11-05T00:00:00Z",
                    "2026-11-05T00:00:01Z",
                ),
            )
            conn.execute(
                "INSERT INTO training_locks (id, project_id, owner_id, run_id, pid, acquired_at, heartbeat_at)"
                " VALUES ('machine', ?, ?, ?, 999999, ?, ?)",
                (self.project["id"], int(self.alice["id"]), run_id, "2026-11-05T00:00:00Z", "2026-11-05T00:00:00Z"),
            )
            conn.commit()
            stats = training_service.recover(conn)
            fresh = training_service.public(
                training_service.get_owned(conn, self.alice["id"], self.project["id"], run_id)
            )
        finally:
            conn.close()
        self.assertGreaterEqual(stats["runs_interrupted"], 1)
        self.assertEqual(fresh["status"], "interrupted")
        self.assertEqual(fresh["error"]["code"], "SERVICE_RESTART")
        self.assertFalse(self.lock()["held"], "重启后残留锁必须被释放，否则永远忙")

    def test_recover_reattaches_live_process_without_starting_a_second_one(self):
        self._mount(_SLOW_SUCCESS_WORKER)
        dataset = self.dataset()
        run, _ = self.start(dataset["id"])
        conn = self.conn()
        try:
            before = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
            stats = training_service.recover(conn)
            after = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        self.assertEqual(str(before["pid"]), str(after["pid"]), "不能重复启动第二个进程")
        self.assertGreaterEqual(stats["runs_reattached"], 1)
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded")
        conn = self.conn()
        try:
            models = model_service.list_for_project(conn, self.alice["id"], self.project["id"])["models"]
        finally:
            conn.close()
        self.assertEqual(len(models), 1, "同一次训练只能产出一个模型")


class TrainingComparisonTest(TrainingBase):
    def _train(self, dataset_id: str, *, parent: str | None = None, epochs: int = 10, name: str) -> dict:
        run, _ = self.start(
            dataset_id, name=name, parent_model_id=parent, params={"epochs": epochs}
        )
        return self.wait_run(run["id"])

    def test_incremental_training_keeps_parent_and_compares_conditions(self):
        dataset = self.dataset()
        first = self._train(dataset["id"], name="第一版")
        self.assertEqual(first["status"], "succeeded")
        second = self._train(dataset["id"], parent=first["output_model_id"], name="第二版")
        self.assertEqual(second["status"], "succeeded")

        conn = self.conn()
        try:
            model = model_service.public(
                model_service.get_owned(conn, self.alice["id"], self.project["id"], second["output_model_id"])
            )
            same = training_service.compare(conn, self.alice["id"], self.project["id"], second["id"])
        finally:
            conn.close()
        self.assertEqual(model["parent_model_id"], first["output_model_id"])
        # 同一数据集、同样的默认参数：条件一致，指标可以直接比较
        self.assertTrue(same["comparable"], same)
        self.assertEqual(same["differences"], [])
        self.assertEqual(same["delta"]["map50"], 0.0)
        self.assertTrue(same["child"]["values"])
        self.assertEqual(same["child"]["split"], "val")

    def test_different_conditions_are_not_reported_as_improvement(self):
        dataset = self.dataset()
        first = self._train(dataset["id"], name="十轮")
        third = self._train(
            dataset["id"], parent=first["output_model_id"], epochs=3, name="三轮"
        )
        self.assertEqual(third["status"], "succeeded")
        conn = self.conn()
        try:
            result = training_service.compare(conn, self.alice["id"], self.project["id"], third["id"])
        finally:
            conn.close()
        self.assertFalse(result["comparable"])
        self.assertIn("epochs", [item["key"] for item in result["differences"]])
        self.assertIn("不能直接说明模型变好", result["note"])

    def test_compare_without_parent_says_it_is_first_training(self):
        dataset = self.dataset()
        run = self._train(dataset["id"], name="首训对比")
        conn = self.conn()
        try:
            result = training_service.compare(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        self.assertIsNone(result["parent"]["model"])
        self.assertFalse(result["comparable"])
        self.assertIn("首次训练", result["note"])

    def test_plan_rejects_bad_params_and_missing_yaml(self):
        dataset = self.dataset()
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as bad:
                training_service.plan(
                    conn, self.alice["id"], self.project["id"],
                    dataset_id=dataset["id"], params={"epochs": 0},
                )
            self.assertEqual(bad.exception.code, "BAD_PARAM")
            with self.assertRaises(ApiError) as device:
                training_service.plan(
                    conn, self.alice["id"], self.project["id"],
                    dataset_id=dataset["id"], params={"device": "tpu"},
                )
            self.assertEqual(device.exception.code, "BAD_PARAM")
            with self.assertRaises(ApiError) as size:
                training_service.plan(
                    conn, self.alice["id"], self.project["id"],
                    dataset_id=dataset["id"], params={"imgsz": 650},
                )
            self.assertEqual(size.exception.code, "BAD_PARAM")
        finally:
            conn.close()

        (dataset["root"] / "data.yaml").unlink()
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as no_yaml:
                training_service.plan(
                    conn, self.alice["id"], self.project["id"], dataset_id=dataset["id"]
                )
            self.assertEqual(no_yaml.exception.code, "DATASET_NOT_TRAINABLE")
        finally:
            conn.close()

    def test_worker_command_is_an_argument_list_even_with_spaces(self):
        """参数列表而不是拼 shell：路径带空格也必须原样传给进程。"""
        spaced = self.workers / "with space"
        spaced.mkdir(parents=True, exist_ok=True)
        script = spaced / "worker script.py"
        script.write_text(
            _SUCCESS_WORKER
            % {
                "template": str(self.weight_template),
                "mm_template": str(self.mm_weight_template),
            },
            encoding="utf-8",
        )
        training_service.set_worker_command([sys.executable, str(script)])
        dataset = self.dataset()
        run, _ = self.start(dataset["id"])
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(len(training_service.worker_command()), 2)


class ModelManagementTest(TrainingBase):
    def _patch(self, model: dict, **fields) -> dict:
        conn = self.conn()
        try:
            return model_service.public(
                model_service.update_model(
                    conn, self.alice["id"], self.project["id"], model["id"], **fields
                )
            )
        finally:
            conn.close()

    def test_publish_reports_replaced_models_of_this_project_only(self):
        dataset = self.dataset()
        first = self.make_model()
        second = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET status = 'imported', class_map = '{\"0\": 0, \"1\": 1}',"
                " dataset_id = ? WHERE id IN (?, ?)",
                (dataset["id"], first["id"], second["id"]),
            )
            conn.commit()
            published = model_service.publish(conn, self.alice["id"], self.project["id"], first["id"])
        finally:
            conn.close()
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["replaced"], [])

        # 另一个项目里的已发布模型不能受本项目发布影响
        other_project = self.harness.create_project(self.alice["id"], "另一个项目")
        self.harness.bind_project(other_project["id"], self.root, "alice")
        conn = self.conn()
        try:
            foreign = self.make_model()
            conn.execute(
                "UPDATE models SET project_id = ?, status = 'published', published_at = ? WHERE id = ?",
                (other_project["id"], "2026-11-05T00:00:00Z", foreign["id"]),
            )
            conn.commit()
            switched = model_service.publish(conn, self.alice["id"], self.project["id"], second["id"])
            foreign_row = conn.execute("SELECT status FROM models WHERE id = ?", (foreign["id"],)).fetchone()
            first_row = conn.execute("SELECT status FROM models WHERE id = ?", (first["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual([item["id"] for item in switched["replaced"]], [first["id"]])
        self.assertEqual(first_row["status"], "superseded")
        self.assertEqual(foreign_row["status"], "published", "不能影响其他项目")

    def test_unpublish_delete_and_restore(self):
        dataset = self.dataset()
        model = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET class_map = '{\"0\": 0, \"1\": 1}', dataset_id = ? WHERE id = ?",
                (dataset["id"], model["id"]),
            )
            conn.commit()
            model_service.publish(conn, self.alice["id"], self.project["id"], model["id"])
            with self.assertRaises(ApiError) as blocked:
                model_service.delete(conn, self.alice["id"], self.project["id"], model["id"])
            self.assertEqual(blocked.exception.code, "MODEL_PUBLISHED")

            model_service.unpublish(conn, self.alice["id"], self.project["id"], model["id"])
            removed = model_service.delete(conn, self.alice["id"], self.project["id"], model["id"])
            self.assertIsNotNone(removed["deleted_at"])
            listed = model_service.list_for_project(conn, self.alice["id"], self.project["id"])["models"]
            self.assertNotIn(model["id"], [item["id"] for item in listed])
            with_trash = model_service.list_for_project(
                conn, self.alice["id"], self.project["id"], include_deleted=True
            )["models"]
            self.assertIn(model["id"], [item["id"] for item in with_trash])

            back = model_service.restore(conn, self.alice["id"], self.project["id"], model["id"])
            self.assertIsNone(back["deleted_at"])
            renamed = model_service.update_model(
                conn, self.alice["id"], self.project["id"], model["id"], notes="复核后保留"
            )
            self.assertEqual(renamed["notes"], "复核后保留")
        finally:
            conn.close()

    def test_model_http_endpoints_are_wired(self):
        model = self.make_model()
        listed = self.client.get(f"/api/projects/{self.project['id']}/models")
        self.assertEqual(listed.status_code, 200)
        patched = self.client.patch(
            f"/api/projects/{self.project['id']}/models/{model['id']}",
            json={"notes": "页面改备注"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(patched.status_code, 200, patched.get_data(as_text=True))
        run_list = self.client.get(f"/api/projects/{self.project['id']}/training")
        self.assertEqual(run_list.status_code, 200, run_list.get_data(as_text=True))
        payload = json.loads(run_list.get_data(as_text=True))["data"]
        self.assertEqual(payload["runs"], [])
        self.assertFalse(payload["lock"]["held"])


class TrainingSummaryTest(TrainingBase):
    def test_job_summary_uses_real_training_facts(self):
        dataset = self.dataset()
        run, _ = self.start(dataset["id"], name="摘要用例")
        finished = self.wait_run(run["id"])
        text = training_service.summary_text(finished)
        self.assertIn("succeeded", text)
        self.assertIn("mAP50", text)
        conn = self.conn()
        try:
            job = job_service.get(conn, str(finished["job_id"]))
        finally:
            conn.close()
        self.assertEqual(str(job["status"]), "done")
        self.assertEqual(json.loads(job["result"])["run_id"], run["id"])


if __name__ == "__main__":
    unittest.main()


class TrainingToolTest(TrainingBase):
    """Agent 工具与网页必须走同一份服务：这里用真实工具处理器验证。"""

    def _ctx(self, conn, args: dict):
        from backend.agent import identity as agent_identity
        from backend.agent.context import Actor
        from backend.agent.tools import ToolContext

        connection = agent_identity.connection_for(conn, self.alice["id"])
        actor = Actor(
            user_id=int(self.alice["id"]),
            username=str(self.alice["username"]),
            project_id=str(self.project["id"]),
            project_name=str(self.project["name"]),
            session_id=str(self.session["id"]),
            turn_id=f"trn_{secrets.token_hex(8)}",
            mode=agent_identity.effective_mode(conn, self.alice["id"]),
            root_path=self.root,
            connection_fingerprint=agent_identity.connection_fingerprint(connection),
            provider=str(connection["provider"]),
            model=agent_identity.effective_model(conn, self.alice["id"], connection),
        )
        return ToolContext(conn=conn, actor=actor, args=args, emit=lambda *_args: None, jobs={})

    def test_tools_use_the_same_training_service(self):
        from backend.agent import tools as agent_tools

        dataset = self.dataset()
        conn = self.conn()
        try:
            planned = agent_tools.TOOLS["hx_training_plan"].handler(
                self._ctx(conn, {"dataset_id": dataset["id"], "params": {"epochs": 2}})
            )
            self.assertEqual(planned["params"]["epochs"], 2)
            self.assertIn("conditions", planned)

            started = agent_tools.TOOLS["hx_training_start"].handler(
                self._ctx(
                    conn,
                    {"dataset_id": dataset["id"], "request_key": "tool-start-1", "name": "工具训练"},
                )
            )
            run_id = started["run"]["id"]
        finally:
            conn.close()

        finished = self.wait_run(run_id)
        self.assertEqual(finished["status"], "succeeded")

        conn = self.conn()
        try:
            status = agent_tools.TOOLS["hx_training_status"].handler(self._ctx(conn, {"run_id": run_id}))
            compared = agent_tools.TOOLS["hx_training_compare"].handler(
                self._ctx(conn, {"run_id": run_id})
            )
        finally:
            conn.close()
        self.assertEqual(status["run"]["status"], "succeeded")
        self.assertIn("log", status)
        self.assertIn("parent", compared)
        self.assertIn("note", compared)

    def test_model_admin_tool_round_trip(self):
        from backend.agent import tools as agent_tools

        dataset = self.dataset()
        model = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET class_map = '{\"0\": 0, \"1\": 1}', dataset_id = ? WHERE id = ?",
                (dataset["id"], model["id"]),
            )
            conn.commit()
            published = agent_tools.TOOLS["hx_publish_model"].handler(
                self._ctx(conn, {"model_id": model["id"]})
            )
            self.assertEqual(published["model"]["status"], "published")
            unpublished = agent_tools.TOOLS["hx_model_admin"].handler(
                self._ctx(conn, {"action": "unpublish", "model_id": model["id"]})
            )
            self.assertEqual(unpublished["model"]["status"], "imported")
            removed = agent_tools.TOOLS["hx_model_admin"].handler(
                self._ctx(conn, {"action": "delete", "model_id": model["id"]})
            )
            self.assertIsNotNone(removed["model"]["deleted_at"])
            restored = agent_tools.TOOLS["hx_model_admin"].handler(
                self._ctx(conn, {"action": "restore", "model_id": model["id"]})
            )
            self.assertIsNone(restored["model"]["deleted_at"])
        finally:
            conn.close()


class TrainingEdgeTest(TrainingBase):
    """协作者独立反例对应的三组真实边缘：产物有效性、启动收尾、进程状态未知。"""

    def test_text_weight_is_not_reported_as_success(self):
        """假文本 best.pt：安全加载读不出类别表 → 登记失败，不显示成功。"""
        self._mount(_TEXT_WEIGHT_WORKER)
        dataset = self.dataset()
        run, _ = self.start(dataset["id"], name="文本权重")
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed", finished)
        self.assertIn("TRAINING_WEIGHT_INVALID", finished["error"]["code"])
        self.assertIsNone(finished["output_model_id"])
        self.assertFalse(self.lock()["held"])

    def test_classes_contract_survives_registration(self):
        """训练产物登记：classes 是 {index,name} 契约 + 恒等映射，发布可用。"""
        dataset = self.dataset()
        run, _ = self.start(dataset["id"], name="契约")
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        conn = self.conn()
        try:
            model = model_service.public(
                model_service.get_owned(
                    conn, self.alice["id"], self.project["id"], finished["output_model_id"]
                )
            )
            self.assertTrue(
                all(isinstance(x, dict) and "index" in x and "name" in x for x in model["classes"])
            )
            published = model_service.publish(
                conn, self.alice["id"], self.project["id"], finished["output_model_id"]
            )
            self.assertEqual(published["status"], "published")
            # 检测端展示口径：labels_for 按数据集类别名输出。
            labels = model_service.labels_for(
                conn,
                model_service.get_owned(
                    conn, self.alice["id"], self.project["id"], finished["output_model_id"]
                ),
            )
            self.assertEqual(labels[0], "单一热斑")
            self.assertEqual(labels[1], "大面积热斑")
        finally:
            conn.close()

    def test_publish_rejects_classless_model(self):
        """无类别表/无映射的模型不能上线（之前空 classes 也能 publish 的反例）。"""
        dataset = self.dataset()
        model = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET classes = '[]', class_map = '{}', dataset_id = ? WHERE id = ?",
                (dataset["id"], model["id"]),
            )
            conn.commit()
            with self.assertRaises(ApiError) as ctx:
                model_service.publish(conn, self.alice["id"], self.project["id"], model["id"])
            self.assertEqual(ctx.exception.code, "MODEL_NO_CLASSES")
            # 恢复类别表、只清空映射 → CLASS_MAPPING_REQUIRED。
            conn.execute(
                "UPDATE models SET classes = ?, class_map = '{}' WHERE id = ?",
                (
                    json.dumps(
                        [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}],
                        ensure_ascii=False,
                    ),
                    model["id"],
                ),
            )
            conn.commit()
            with self.assertRaises(ApiError) as ctx2:
                model_service.publish(conn, self.alice["id"], self.project["id"], model["id"])
            self.assertEqual(ctx2.exception.code, "CLASS_MAPPING_REQUIRED")
        finally:
            conn.close()

    def test_params_write_failure_leaves_no_active_run_or_lock(self):
        """启动链路（写 params/运行时配置/建 job）失败：标 failed + 放本 run 的锁。"""
        dataset = self.dataset()
        from backend.services import storage as storage_module

        with patch.object(
            storage_module,
            "write_bytes",
            side_effect=ApiError(500, "QA_WRITE_FAILED", "测试注入写入失败"),
        ):
            with self.assertRaises(ApiError) as ctx:
                self.start(dataset["id"], key="params-write-failure")
        self.assertEqual(ctx.exception.code, "QA_WRITE_FAILED")
        conn = self.conn()
        try:
            active = conn.execute(
                "SELECT count(*) FROM training_runs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            held = conn.execute("SELECT count(*) FROM training_locks").fetchone()[0]
            rows = conn.execute("SELECT status, error_code FROM training_runs").fetchall()
        finally:
            conn.close()
        self.assertEqual((active, held), (0, 0), "不能留下'训练中'假记录或占用锁")
        self.assertEqual([r["error_code"] for r in rows], ["QA_WRITE_FAILED"])

    def test_job_create_failure_cleans_up(self):
        """建 job 失败（启动链路更早的一步）同样要收尾。"""
        dataset = self.dataset()
        from backend.services import jobs as jobs_module

        with patch.object(
            jobs_module,
            "create",
            side_effect=ApiError(500, "QA_JOB_FAILED", "测试注入 job 失败"),
        ):
            with self.assertRaises(ApiError) as ctx:
                self.start(dataset["id"], key="job-create-failure")
        self.assertEqual(ctx.exception.code, "QA_JOB_FAILED")
        conn = self.conn()
        try:
            active = conn.execute(
                "SELECT count(*) FROM training_runs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            held = conn.execute("SELECT count(*) FROM training_locks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((active, held), (0, 0))

    def test_unknown_process_state_does_not_release_live_worker(self):
        """进程查询返回 None（查询失败）：状态未知 → 不终止、不放锁、不宣布结束。"""
        dataset = self.dataset()
        self._mount(_SLEEP_WORKER)
        with patch.object(training_service, "_watch_async", return_value=None):
            run, _ = self.start(dataset["id"], key="unknown-process-state")
        self.wait_marker(run["id"])
        conn = self.conn()
        try:
            with patch.object(training_service, "process_started_at", return_value=None):
                with self.assertRaises(ApiError) as ctx:
                    training_service.cancel(
                        conn, self.alice["id"], self.project["id"], run["id"]
                    )
            self.assertEqual(ctx.exception.code, "PROCESS_STATE_UNKNOWN")
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
            pid = int(row["pid"])
            held = conn.execute("SELECT count(*) FROM training_locks").fetchone()[0]
            state = training_service.public(row)["status"]
        finally:
            conn.close()
        self.assertTrue(psutil_pid_alive(pid), "查询失败不代表进程死了")
        self.assertEqual(held, 1, "状态未知不能释放训练槽")
        self.assertEqual(state, "running", "状态未知不能宣布结束")
        # 恢复真实查询后正常取消（测试自己清理假 worker）。
        conn = self.conn()
        try:
            result = training_service.cancel(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(psutil_pid_alive(pid))

    def test_finalize_with_unavailable_root_marks_failed_and_releases(self):
        """进程结束但项目根目录不可用（撤权/删除）：结果未知按失败，不假成功、不越权读。"""
        dataset = self.dataset()
        run, _ = self.start(dataset["id"], name="撤权收尾")
        with patch.object(training_service, "_root_for", return_value=None):
            finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed", finished)
        self.assertEqual(finished["error"]["code"], "TRAINING_ROOT_UNAVAILABLE")
        self.assertFalse(self.lock()["held"], "进程已确认结束，槽位跟着释放")


class TrainingDispatchTest(TrainingBase):
    """GPU 设备参数与环境检查、YOLO/YOLOMM 分派、运行时配置与 vendor 解析器验证。"""

    def test_device_gpu_is_a_real_capability_with_environment_check(self):
        """device=gpu/cuda:N 规范化 + 环境探测；本机不假装跑 GPU，只验证分支。"""
        conn = self.conn()
        try:
            # 探测注入为可用：计划接受 GPU 参数并写进 conditions。
            training_service.set_device_probe(lambda device: {"ok": True, "detail": "test"})
            planned = training_service.plan(
                conn,
                self.alice["id"],
                self.project["id"],
                dataset_id=self.dataset()["id"],
                params={"device": "gpu"},
            )
            self.assertEqual(planned["params"]["device"], "cuda")
            self.assertEqual(planned["conditions"]["device"], "cuda")
            self.assertIn("GPU", "".join(planned["warnings"]))

            planned2 = training_service.plan(
                conn,
                self.alice["id"],
                self.project["id"],
                dataset_id=self.dataset()["id"],
                params={"device": "cuda:1"},
            )
            self.assertEqual(planned2["params"]["device"], "cuda:1")

            # 探测注入为不可用：明确报 409，不硬编码拒绝。
            training_service.set_device_probe(lambda device: {"ok": False, "detail": "cuda_available=False"})
            with self.assertRaises(ApiError) as ctx:
                training_service.plan(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    dataset_id=self.dataset()["id"],
                    params={"device": "cuda:1"},
                )
            self.assertEqual(ctx.exception.code, "TRAINING_DEVICE_UNAVAILABLE")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            training_service.set_device_probe(None)
            conn.close()

    def test_gpu_params_reach_worker_spec(self):
        """GPU 分支的命令/配置断言：device 与 family 写进 params.json（不真跑 GPU）。"""
        training_service.set_device_probe(lambda device: {"ok": True, "detail": "test"})
        dataset = self.dataset()
        run, _ = self.start(dataset["id"], name="GPU 配置", params={"device": "cuda:0"})
        spec = json.loads(
            (self.root / run["run_dir_rel"] / "params.json").read_text(encoding="utf-8")
        )
        self.assertEqual(spec["params"]["device"], "cuda:0")
        self.assertEqual(spec["family"], "yolo")
        # 运行时配置是相对项目根的完整路径（worker 在 run 目录里用它，不改原数据集 yaml）。
        expected_runtime = storage.relative(
            self.root, self.root / run["run_dir_rel"] / "data.yaml"
        )
        self.assertEqual(spec["runtime_data_yaml"], expected_runtime)
        self.assertTrue((self.root / run["run_dir_rel"] / "data.yaml").is_file())
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)

    def test_multimodal_dataset_dispatches_yolomm(self):
        """数据集带 IR 模态 → YOLOMM 分派；运行时配置带 modality_used 与 X 目录校验。"""
        dataset = self.dataset(ir=True)
        conn = self.conn()
        try:
            planned = training_service.plan(
                conn, self.alice["id"], self.project["id"], dataset_id=dataset["id"]
            )
            self.assertEqual(planned["family"], "yolomm")
            self.assertEqual(planned["secondary_modality"], "ir")
        finally:
            conn.close()
        run, _ = self.start(dataset["id"], name="多模态")
        self.assertEqual(run["family"], "yolomm")
        self.assertEqual(run["secondary_modality"], "ir")
        runtime = yaml.safe_load(
            (self.root / run["run_dir_rel"] / "data.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(Path(str(runtime["path"])).is_absolute(), "运行时配置必须写绝对 path")
        self.assertEqual(runtime["modality_used"], ["rgb", "ir"])
        self.assertEqual(runtime["modalities"], {"rgb": "images", "ir": "images_ir"})
        # 原数据集 data.yaml 不被改写。
        origin = yaml.safe_load((dataset["root"] / "data.yaml").read_text(encoding="utf-8"))
        self.assertEqual(origin["path"], ".")
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        conn = self.conn()
        try:
            model = model_service.public(
                model_service.get_owned(
                    conn, self.alice["id"], self.project["id"], finished["output_model_id"]
                )
            )
        finally:
            conn.close()
        self.assertEqual(model["family"], "yolomm")
        self.assertEqual(model["model_mode"], "multimodal")
        self.assertEqual(model["secondary_modality"], "ir")

    def test_missing_x_modality_dir_blocks_start(self):
        """YOLOMM 但 X 目录没配图：启动前就拒绝（X_MODALITY_MISSING），不进训练。"""
        dataset = self.dataset(ir=True)
        # 删掉 val 的 IR 配图 → 启动时核验失败。
        for item in (dataset["root"] / "images_ir" / "val").glob("*"):
            item.unlink()
        with self.assertRaises(ApiError) as ctx:
            self.start(dataset["id"], name="缺 X")
        self.assertEqual(ctx.exception.code, "X_MODALITY_MISSING")
        self.assertFalse(self.lock()["held"])

    def test_runtime_config_uses_real_vendor_parser(self):
        """真实 vendor 数据解析器（check_det_dataset）验证运行时配置（不注入）。"""
        dataset = self.dataset()
        conn = self.conn()
        try:
            training_service.set_config_validator(None)  # 走真实 vendor 解析器
            planned = training_service.plan(
                conn, self.alice["id"], self.project["id"], dataset_id=dataset["id"]
            )
            self.assertIn("family", planned)
        finally:
            conn.close()
        run, _ = self.start(dataset["id"], name="vendor 验证")
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished)

    def test_imported_yaml_with_relative_path_runs_from_absolute_runtime(self):
        """协作者反例：源 data.yaml 写 ``path: raw``（vendor 会相对全局目录解析）。

        运行时配置按源 path 拼出真实数据根并写绝对路径，vendor 解析器解析到
        本数据集目录，而不是开发机的全局 DATASETS_DIR。
        """
        dataset = self.dataset()
        # 把目录重排成导入常见形态：<ds>/raw/images/...，data.yaml 写 path: raw。
        raw = dataset["root"] / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        for item in ("images", "labels"):
            source = dataset["root"] / item
            if source.exists():
                shutil_dir(source, raw / item)
        (dataset["root"] / "data.yaml").write_text(
            yaml.safe_dump(
                {"path": "raw", "nc": 2, "names": ["单一热斑", "大面积热斑"],
                 "train": "images/train", "val": "images/val"},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        run_dir = storage.project_dir(self.root, "training", "probe-raw")
        runtime_yaml = training_service.generate_runtime_config(
            run_dir, dataset["root"] / "data.yaml", family="yolo", secondary=None
        )
        payload = yaml.safe_load(runtime_yaml.read_text(encoding="utf-8"))
        self.assertEqual(payload["path"], str((dataset["root"] / "raw").resolve()))
        training_service.set_config_validator(None)
        data = training_service._verify_with_vendor(runtime_yaml)
        self.assertIn(str((dataset["root"] / "raw").resolve()), str(data["resolved"]["train"]))


def shutil_dir(source: Path, target: Path) -> None:
    import shutil as _shutil

    target.parent.mkdir(parents=True, exist_ok=True)
    _shutil.move(str(source), str(target))


class TrainingRealWorkerTest(TrainingBase):
    """用真实 ``backend.training_worker`` 跑一次 1 epoch CPU 微训练（无 GPU、无下载）。

    验证闭环：运行时配置 → vendor 数据解析器 → 真实训练 → best.pt 安全加载 →
    登记 → 发布 → 类别展示（labels_for），全部走生产同一条链路。

    这条用例会**真的启动训练子进程**（CPU、64px、1 epoch，约 1–3 分钟），所以默认
    跳过：普通 ``pytest`` / ``unittest discover`` 不应在无告知的情况下消耗算力。
    需要时显式执行::

        HX17_RUN_REAL_TRAINING_TEST=1 python -m pytest \\
            backend/tests/test_business_wb17_05_training.py -k real_worker

    跳过不代表训练通过；本批与最终非训练回归都把这条列在排除项里。
    """

    @unittest.skipUnless(
        os.environ.get("HX17_RUN_REAL_TRAINING_TEST") == "1",
        "真实 1 epoch CPU 微训练默认不跑；设 HX17_RUN_REAL_TRAINING_TEST=1 显式执行",
    )
    def test_real_worker_end_to_end_registers_publishes_and_resolves_labels(self):
        dataset = self.dataset()
        training_service.set_worker_command(None)  # 生产默认：backend.training_worker
        run, created = self.start(
            dataset["id"], name="真实微训练", params={"epochs": 1, "imgsz": 64, "batch": 2}
        )
        self.assertTrue(created)
        finished = self.wait_run(run["id"], timeout=300)
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        self.assertTrue(finished["output_model_id"])
        self.assertTrue((self.root / run["run_dir_rel"] / "best.pt").is_file())
        conn = self.conn()
        try:
            row = model_service.get_owned(
                conn, self.alice["id"], self.project["id"], finished["output_model_id"]
            )
            model = model_service.public(row)
            self.assertEqual(model["source"], "trained")
            self.assertEqual(
                model["classes"],
                [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}],
            )
            published = model_service.publish(
                conn, self.alice["id"], self.project["id"], finished["output_model_id"]
            )
            self.assertEqual(published["status"], "published")
            labels = model_service.labels_for(conn, row)
            self.assertEqual(labels[0], "单一热斑")
            self.assertEqual(labels[1], "大面积热斑")
        finally:
            conn.close()
        self.assertFalse(self.lock()["held"])


class TrainingStartHandoffTest(TrainingBase):
    """启动交接：准备期取消必须挡住 spawn；交接中取消不能提前宣称成功。

    这里用**另一条数据库连接**执行取消，模拟网页/Agent 与启动请求并发——
    不是同一个 conn 上的顺序调用。
    """

    def _latest_run_id(self) -> str:
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT id FROM training_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "应该已经建出训练记录")
        return str(row["id"])

    def test_cancel_during_preparation_prevents_spawn(self):
        dataset = self.dataset()
        spawned: list[int] = []
        cancelled: list[dict] = []

        def cancel_in_validation(_yaml_path):
            # 准备阶段（vendor 校验里）：另一条连接把这次训练取消掉。
            run_id = self._latest_run_id()
            conn = self.conn()
            try:
                cancelled.append(
                    training_service.cancel(conn, self.alice["id"], self.project["id"], run_id)
                )
            finally:
                conn.close()
            return {"resolved": {"train": "stub", "val": "stub"}}

        def observe_spawn(*_args, **_kwargs):
            spawned.append(1)
            raise AssertionError("准备阶段已取消，不允许再启动进程")

        with patch.object(training_service, "_verify_with_vendor", side_effect=cancel_in_validation):
            with patch.object(training_service, "_spawn", side_effect=observe_spawn):
                with self.assertRaises(ApiError) as ctx:
                    self.start(dataset["id"], key="cancel-during-prepare")
        self.assertEqual(ctx.exception.code, "TRAINING_CANCELLED")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["status"], "cancelled")
        self.assertEqual(spawned, [], "取消之后一个进程都不许起")

        run_id = self._latest_run_id()
        conn = self.conn()
        try:
            row = conn.execute("SELECT * FROM training_runs WHERE id = ?", (run_id,)).fetchone()
            locks = conn.execute("SELECT COUNT(*) FROM training_locks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(row["status"]), "cancelled", "取消的结论不能被启动链路覆盖")
        self.assertEqual(int(locks), 0, "取消后训练槽必须释放")
        self.assertNotIn(run_id, training_service._processes, "没有进程被启动")

    def test_cancel_during_launch_handoff_is_refused_and_process_registers(self):
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        refusals: list[str] = []
        real_spawn = training_service._spawn

        def cancel_then_spawn(*args, **kwargs):
            # 已经认领启动权、还没登记 PID：这时取消不能成功，也不能放锁。
            run_id = self._latest_run_id()
            conn = self.conn()
            try:
                training_service.cancel(conn, self.alice["id"], self.project["id"], run_id)
                refusals.append("NO_ERROR")
            except ApiError as exc:
                refusals.append(exc.code)
            finally:
                conn.close()
            return real_spawn(*args, **kwargs)

        with patch.object(training_service, "_spawn", side_effect=cancel_then_spawn):
            run, created = self.start(dataset["id"], key="cancel-during-launch")
        self.assertTrue(created)
        self.assertEqual(refusals, ["CANCEL_STARTING"])

        self.wait_marker(run["id"])  # 进程确实起来了
        conn = self.conn()
        try:
            row = training_service.get_owned(conn, self.alice["id"], self.project["id"], run["id"])
            locks = conn.execute("SELECT COUNT(*) FROM training_locks").fetchone()[0]
            status, pid = str(row["status"]), int(row["pid"] or 0)
        finally:
            conn.close()
        self.assertEqual(status, "running")
        self.assertGreater(pid, 0, "PID 必须真的落库")
        self.assertEqual(int(locks), 1, "交接中的取消不能释放训练槽")
        self.assertTrue(psutil_pid_alive(pid))

        # 交接完成后再取消：这次必须真的结束进程（证明上面只是“稍后重试”）。
        conn = self.conn()
        try:
            result = training_service.cancel(conn, self.alice["id"], self.project["id"], run["id"])
        finally:
            conn.close()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(psutil_pid_alive(pid))

    def test_cancel_after_pid_recorded_is_not_overwritten_by_running(self):
        """PID 已落库、还没转 running 时被取消：启动链路不能把结论改回 running。"""
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        real_persist = training_service._persist_pid

        def persist_then_cancel(conn, run_id, pid, started_at):
            real_persist(conn, run_id, pid, started_at)
            other = self.conn()  # 另一条连接取消（网页/Agent 的入口）
            try:
                training_service.cancel(other, self.alice["id"], self.project["id"], run_id)
            finally:
                other.close()

        with patch.object(training_service, "_persist_pid", side_effect=persist_then_cancel):
            run, created = self.start(dataset["id"], key="cancel-after-pid")
        self.assertTrue(created)
        self.assertEqual(run["status"], "cancelled", "取消的结论不能被启动链路覆盖")
        self.assertFalse(self.lock()["held"])
        self.assertFalse(psutil_pid_alive(int(run["pid"] or 0)))


class TrainingPidPersistenceTest(TrainingBase):
    """spawn 之后 PID 写库失败：真实进程不能丢，也不能被说成“已结束”。"""

    def _latest_run(self) -> dict:
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM training_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            locks = conn.execute("SELECT COUNT(*) FROM training_locks").fetchone()[0]
        finally:
            conn.close()
        return {**dict(row), "_locks": int(locks)}

    def test_pid_write_failure_terminates_the_real_worker(self):
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        spawned: list = []
        real_spawn = training_service._spawn

        def recording_spawn(*args, **kwargs):
            out = real_spawn(*args, **kwargs)
            spawned.append(out[0])
            return out

        with patch.object(training_service, "_spawn", side_effect=recording_spawn):
            with patch.object(training_service, "_set_pid", side_effect=RuntimeError("disk full")):
                with self.assertRaises(ApiError) as ctx:
                    self.start(dataset["id"], key="pid-write-fail")
        self.assertEqual(ctx.exception.code, "TRAINING_PID_UNRECORDED")
        self.assertEqual(len(spawned), 1, "只允许起一个进程")

        row = self._latest_run()
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(str(row["error_code"]), "TRAINING_PID_UNRECORDED")
        self.assertEqual(row["_locks"], 0, "确认终止后必须释放训练槽")
        self.assertIsNotNone(spawned[0].poll(), "PID 没落库时进程必须被终止并核实")
        self.assertFalse(psutil_pid_alive(int(spawned[0].pid)))

    def test_pid_write_failure_keeps_slot_when_termination_unconfirmed(self):
        self._mount(_FAIL_WORKER)  # 立刻退出，避免测试真的留下进程
        dataset = self.dataset()
        with patch.object(training_service, "_set_pid", side_effect=RuntimeError("disk full")):
            with patch.object(
                training_service, "_terminate_tree", return_value=(True, "KILL_TIMEOUT")
            ):
                with self.assertRaises(ApiError) as ctx:
                    self.start(dataset["id"], key="pid-write-unconfirmed")
        self.assertEqual(ctx.exception.code, "TRAINING_PID_UNRECORDED")

        row = self._latest_run()
        self.assertNotIn(str(row["status"]), training_service.TERMINAL, "不确定结束就不能写终态")
        self.assertEqual(str(row["error_code"]), "TRAINING_PID_UNRECORDED")
        self.assertEqual(row["_locks"], 1, "无法确认结束时保留训练槽")
        self.assertIn("PID", str(row["error_message"] or ""), "可诊断状态里要有真实 PID")
        training_service._processes.pop(str(row["id"]), None)

    def test_recover_keeps_slot_for_unrecorded_launch(self):
        """PID 从未落库、停止未确认 → 重启核对不能当 interrupted 并放锁。"""
        self._mount(_SLEEP_WORKER)
        dataset = self.dataset()
        with patch.object(
            training_service, "_spawn", return_value=(Mock(pid=424242), 424242, 1.0)
        ):
            with patch.object(training_service, "_set_pid", side_effect=RuntimeError("disk full")):
                with patch.object(
                    training_service, "_terminate_tree", return_value=(True, "KILL_TIMEOUT")
                ):
                    with self.assertRaises(ApiError) as ctx:
                        self.start(dataset["id"], key="unrecorded-launch")
        self.assertEqual(ctx.exception.code, "TRAINING_PID_UNRECORDED")
        before = self._latest_run()
        self.assertEqual(before["_locks"], 1, "未确认停止时必须保留训练槽")

        training_service._processes.clear()  # 模拟服务重启：内存里的进程句柄消失
        conn = self.conn()
        try:
            stats = training_service.recover(conn)
            after = conn.execute(
                "SELECT * FROM training_runs WHERE id = ?", (before["id"],)
            ).fetchone()
            locks = conn.execute("SELECT COUNT(*) FROM training_locks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(locks), 1, "未知 worker 不能因为重启就被放锁")
        self.assertEqual(str(after["status"]), "queued", "不能把未知启动交接当成 interrupted")
        self.assertEqual(str(after["error_code"]), "TRAINING_PROCESS_UNKNOWN")
        self.assertEqual(stats["runs_blocked"], 1)
        self.assertEqual(stats["runs_interrupted"], 0, "不得自动收尾、更不得自动重跑")

        # 第二次训练仍被拒绝：未知 worker 占着唯一的训练槽。
        with self.assertRaises(ApiError) as busy:
            self.start(dataset["id"], name="第二训练")
        self.assertEqual(busy.exception.code, "TRAINING_BUSY")
        self.assertEqual(busy.exception.details.get("run_id"), str(before["id"]))


class TrainingTerminateEvidenceTest(unittest.TestCase):
    """``_terminate_tree`` 只认明确证据：查不出来就不许说“已结束”。"""

    PID = 4242
    STARTED = 1.0

    def _call(self, process, *, identity: float | None = STARTED, alive=()):
        fake = _FakePsutil(process, alive=alive)
        with patch.object(training_service, "_psutil", return_value=fake):
            with patch.object(training_service, "process_started_at", return_value=identity):
                return training_service._terminate_tree(self.PID, self.STARTED), fake

    def test_children_enumeration_failure_is_not_reported_as_stopped(self):
        proc = _FakeProcess(children_error=_FakePsutil.AccessDenied("denied"))
        (alive, reason), fake = self._call(proc)
        self.assertTrue(alive, "枚举不了子进程就不能说整棵树结束了")
        self.assertEqual(reason, "CHILDREN_UNKNOWN")
        self.assertFalse(proc.terminated, "枚举失败时不该先发信号再宣称结束")
        self.assertEqual(fake.waited, 0, "没有确认对象就不该等")

    def test_access_denied_on_process_is_unknown_not_gone(self):
        (alive, reason), _fake = self._call(_FakePsutil.AccessDenied("denied"))
        self.assertTrue(alive)
        self.assertEqual(reason, "PROCESS_UNREADABLE")

    def test_terminate_denied_is_not_reported_as_stopped(self):
        proc = _FakeProcess(
            terminate_error=_FakePsutil.AccessDenied("denied"),
            kill_error=_FakePsutil.AccessDenied("denied"),
        )
        (alive, reason), fake = self._call(proc)
        self.assertTrue(alive)
        self.assertEqual(reason, "TERMINATE_DENIED")
        self.assertEqual(fake.waited, 0)

    def test_vanished_root_before_enumeration_is_unknown_tree(self):
        """探测说活着、取对象时却没了：连枚举都没做，不能说“整棵树结束”。"""
        (alive, reason), _fake = self._call(_FakePsutil.NoSuchProcess(4242))
        self.assertTrue(alive)
        self.assertEqual(reason, "PROCESS_VANISHED")

    def test_identity_unavailable_is_not_treated_as_exited(self):
        proc = _FakeProcess()
        (alive, reason), fake = self._call(proc, identity=None)
        self.assertTrue(alive)
        self.assertEqual(reason, "IDENTITY_UNAVAILABLE")
        self.assertEqual(fake.waited, 0)

    def test_root_exit_does_not_end_the_tree_when_child_still_alive(self):
        """根进程 terminate 抛 NoSuchProcess，但已枚举的子进程还活着 → 不能说整棵树结束。"""
        child = _FakeProcess()
        root = _FakeProcess(
            children=[child], terminate_error=_FakePsutil.NoSuchProcess(self.PID)
        )
        (alive, reason), fake = self._call(root, alive=[child])
        self.assertTrue(alive, "根进程退出不等于整棵树结束")
        self.assertEqual(reason, "KILL_TIMEOUT")
        self.assertEqual(child.terminated, 1, "已枚举的子进程必须先收到 terminate")
        self.assertEqual(child.killed, 1, "仍存活时要继续 kill 并再核实")
        self.assertEqual(fake.waited, 2, "等一次不够，kill 之后要再等一次")

    def test_root_exit_after_all_children_exited_is_gone(self):
        child = _FakeProcess()
        root = _FakeProcess(
            children=[child], terminate_error=_FakePsutil.NoSuchProcess(self.PID)
        )
        (alive, reason), _fake = self._call(root)
        self.assertFalse(alive)
        self.assertEqual(reason, "TERMINATED")

    def test_root_exit_without_known_children_is_gone(self):
        root = _FakeProcess(terminate_error=_FakePsutil.NoSuchProcess(self.PID))
        (alive, reason), fake = self._call(root)
        self.assertFalse(alive)
        self.assertEqual(reason, "ALREADY_GONE")
        self.assertEqual(fake.waited, 0, "没有已知进程就不该等")


class TrainingWeightRegistrationTest(TrainingBase):
    """无效权重不能登记成“训练成功”的模型。"""

    def test_names_only_checkpoint_is_not_registered_as_model(self):
        self._mount(_NAMES_ONLY_WORKER)
        dataset = self.dataset()
        run, created = self.start(dataset["id"], key="names-only-weight")
        self.assertTrue(created)
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed", finished)
        self.assertEqual(finished["error"]["code"], "TRAINING_WEIGHT_INVALID")
        self.assertIsNone(finished["output_model_id"], "没有模型对象就不能产出模型记录")
        self.assertFalse(self.lock()["held"], "失败后训练槽要释放")

    def test_publish_rejects_weight_replaced_after_registration(self):
        dataset = self.dataset()
        model = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET status = 'imported', dataset_id = ?, class_map = '{\"0\": 0, \"1\": 1}'"
                " WHERE id = ?",
                (dataset["id"], model["id"]),
            )
            conn.commit()
            (self.root / "weights" / "best.pt").write_bytes(b"not-a-model-anymore")
            with self.assertRaises(ApiError) as ctx:
                model_service.publish(conn, self.alice["id"], self.project["id"], model["id"])
            row = conn.execute("SELECT status FROM models WHERE id = ?", (model["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "MODEL_WEIGHT_INVALID")
        self.assertEqual(str(row["status"]), "imported", "校验失败不能留下发布状态")


class _FakePsutil:
    """最小 psutil 替身：只覆盖 ``_probe`` / ``_terminate_tree`` 用到的接口。"""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, process, *, alive=()):
        self._process = process
        self.alive = list(alive)  # wait_procs 每次都把“仍存活”的这些对象还回来
        self.waited = 0
        self.waited_targets: list[list] = []

    def pid_exists(self, pid) -> bool:  # noqa: ARG002 - 替身签名与 psutil 一致
        return True

    def Process(self, pid):  # noqa: N802 - 对齐 psutil 的接口名
        if isinstance(self._process, BaseException):
            raise self._process
        return self._process

    def wait_procs(self, procs, timeout=None):  # noqa: ARG002
        self.waited += 1
        self.waited_targets.append(list(procs))
        # 只把真正传进来的对象当“存活”返回，模拟 wait_procs 的语义
        alive = [item for item in self.alive if item in procs]
        return [], alive


class _FakeProcess:
    """最小进程替身：默认“活着且身份对得上”，可指定各步抛什么异常。"""

    def __init__(
        self,
        *,
        create_time: float = 1.0,
        children: list | None = None,
        children_error: Exception | None = None,
        terminate_error: Exception | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self._create_time = create_time
        self._children = list(children or [])
        self._children_error = children_error
        self._terminate_error = terminate_error
        self._kill_error = kill_error
        self.terminated = 0
        self.killed = 0

    def create_time(self) -> float:
        return self._create_time

    def children(self, recursive: bool = True):  # noqa: ARG002
        if self._children_error is not None:
            raise self._children_error
        return list(self._children)

    def terminate(self) -> None:
        if self._terminate_error is not None:
            raise self._terminate_error
        self.terminated += 1

    def kill(self) -> None:
        if self._kill_error is not None:
            raise self._kill_error
        self.killed += 1
