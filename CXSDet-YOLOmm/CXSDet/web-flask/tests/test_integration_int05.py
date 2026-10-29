"""INT-05：人工入口、个人发布边界与跨项目输出归属。

每条用例对应关键词里的一句要求，断言的是"应该怎样"，而不是当前实现细节。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.business_tool_specs import TOOL_SPECS
from services.business_tools import (
    ENTRY_AGENT,
    ENTRY_HUMAN_PAGE,
    BusinessToolAdapter,
    ResourceSnapshot,
)
from services.tool_common import ToolConflict
from tests.test_business_tool_chain import Fixture

HUMAN_ONLY = ("review.submit_human", "mcp.save_connection")


# --------------------------------------------------------------------------- #
# 10.1 人工复核不能成为模型自行执行的工具
# --------------------------------------------------------------------------- #
def test_human_only_tools_are_declared_as_human_page_entries():
    for name in HUMAN_ONLY:
        spec = TOOL_SPECS[name]
        assert spec.agent_executable is False, name
        assert spec.descriptor["audience"] == "human_page", name


def test_all_other_tools_stay_agent_executable():
    for name, spec in TOOL_SPECS.items():
        if name in HUMAN_ONLY:
            continue
        assert spec.agent_executable is True, name
        assert spec.descriptor["audience"] == "agent", name


def _adapter(tmp_path: Path) -> BusinessToolAdapter:
    path = tmp_path / "ops.sqlite3"
    return BusinessToolAdapter(
        path,
        require_access=lambda principal, action, resource: True,
        resolve_resource=lambda principal, kind, resource_id: ResourceSnapshot(
            resource_id, "project-1", 7, "v1"
        ),
        storage_exists=lambda bucket, key: True,
    )


def test_agent_entry_cannot_submit_a_human_decision(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "review.submit_human", {
            "record_id": "r1", "expected_version": 0, "decision": "no_defect",
        })
    # 缺省就是 Agent 入口，不能靠默认参数混过去
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "review.submit_human", {
            "record_id": "r1", "expected_version": 0, "decision": "no_defect",
        }, entry=ENTRY_AGENT)


def test_human_page_entry_is_a_separate_server_side_argument(tmp_path):
    adapter = _adapter(tmp_path)
    # INT-29：用户要求划分可由 Agent 按明确指令完成，人工标签决定仍受保护。
    assert TOOL_SPECS['draft.assign_splits'].agent_executable is True
    assert TOOL_SPECS['draft.assign_splits'].descriptor['effect'] == 'write'
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "mcp.save_connection", {
            "name": "x", "transport": "http",
        })


def test_agent_never_sees_human_only_tools_in_its_list(tmp_path):
    adapter = _adapter(tmp_path)
    names = {item["name"] for item in adapter.list_tools(7)}
    assert not (set(HUMAN_ONLY) & names)


def test_execute_also_refuses_a_human_only_operation_from_the_agent_entry(tmp_path):
    """即使有人手工造了一条 prepared 记录，Agent 入口也不能把它执行掉。"""
    import uuid

    adapter = _adapter(tmp_path)
    operation_id = str(uuid.uuid4())
    snapshot = ResourceSnapshot("r1", "project-1", 7, "1")
    with sqlite3.connect(tmp_path / "ops.sqlite3") as conn:
        conn.execute(
            "INSERT INTO business_tool_operations (operation_id, principal_id, "
            "tool_name, arguments_json, resource_json, fingerprint, status, "
            "preview_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id, "7", "review.submit_human",
                json.dumps({"record_id": "r1", "expected_version": 0,
                            "decision": "no_defect"}),
                json.dumps({
                    "id": snapshot.id, "project_id": snapshot.project_id,
                    "owner_id": snapshot.owner_id, "version": snapshot.version,
                }),
                "fp", "prepared", "{}", "2026-01-01T00:00:00", "2026-01-01T00:00:00",
            ),
        )
    with pytest.raises(PermissionError):
        adapter.execute_tool(7, operation_id, "review-agent")
    with pytest.raises(PermissionError):
        adapter.execute_tool(7, operation_id, "review-agent", entry=ENTRY_AGENT)


# --------------------------------------------------------------------------- #
# 10.2 发布必须限定本人和实际项目
# --------------------------------------------------------------------------- #
def _publish_db(path: Path, rows) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE datasets (id TEXT PRIMARY KEY, dataset_mode TEXT, "
            "secondary_modality TEXT)"
        )
        conn.execute("INSERT INTO datasets VALUES ('d1', 'single', NULL)")
        conn.execute(
            "CREATE TABLE models (id TEXT PRIMARY KEY, status INTEGER, model_bucket TEXT, "
            "model_object_key TEXT, dataset_id TEXT, model_mode TEXT, primary_modality TEXT, "
            "secondary_modality TEXT, create_time TEXT, publish_status TEXT)"
        )
        conn.executemany(
            "INSERT INTO models VALUES (?, ?, 'models', ?, 'd1', 'single', 'rgb', NULL, ?, ?)",
            rows,
        )
        conn.execute(
            "CREATE TABLE model_publish_audit (id TEXT PRIMARY KEY, model_id TEXT, "
            "operator_id INTEGER, previous_model_id TEXT, create_time TEXT)"
        )
    conn.close()


def _publish_adapter(path: Path, scope) -> BusinessToolAdapter:
    return BusinessToolAdapter(
        path,
        require_access=lambda principal, action, resource: True,
        resolve_resource=lambda principal, kind, resource_id: ResourceSnapshot(
            resource_id, "project-1", 7, "v1"
        ),
        storage_exists=lambda bucket, key: True,
        model_scope_resolver=scope,
    )


def test_publish_archives_only_models_inside_owner_project(tmp_path):
    path = tmp_path / "publish.sqlite3"
    _publish_db(path, [
        ("mine-old", 3, "a.pt", "2026-01-01", "published"),
        ("theirs", 3, "b.pt", "2026-01-02", "published"),
        ("mine-new", 1, "c.pt", "2026-01-03", None),
    ])
    adapter = _publish_adapter(path, lambda model_id: ["mine-old", "mine-new"])
    prepared = adapter.prepare_tool(7, "model.publish", {"model_id": "mine-new"})
    preview = prepared["preview"]
    assert preview["archived_model_ids"] == ["mine-old"]
    assert preview["scope_model_ids"] == ["mine-new", "mine-old"]
    adapter.execute_tool(7, prepared["operation_id"], "publish-scope")
    with sqlite3.connect(path) as conn:
        states = dict(conn.execute("SELECT id, status FROM models").fetchall())
    assert states["theirs"] == 3, "别人的已发布模型不能被归档"


def test_publish_preview_lists_exact_ids_and_rechecks_them(tmp_path):
    path = tmp_path / "publish2.sqlite3"
    _publish_db(path, [
        ("mine-old", 3, "a.pt", "2026-01-01", "published"),
        ("mine-new", 1, "c.pt", "2026-01-03", None),
    ])
    adapter = _publish_adapter(path, lambda model_id: ["mine-old", "mine-new"])
    prepared = adapter.prepare_tool(7, "model.publish", {"model_id": "mine-new"})
    assert prepared["preview"]["archived_model_ids"] == ["mine-old"]
    # 预览之后归档集合发生变化（待归档的那条已被别人改回未发布）→ 必须重新确认
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE models SET status=1 WHERE id='mine-old'")
    with pytest.raises(ToolConflict):
        adapter.execute_tool(7, prepared["operation_id"], "publish-stale")


def test_publish_refuses_when_one_model_in_scope_is_not_authorised(tmp_path):
    path = tmp_path / "publish3.sqlite3"
    _publish_db(path, [
        ("mine-old", 3, "a.pt", "2026-01-01", "published"),
        ("mine-new", 1, "c.pt", "2026-01-03", None),
    ])
    adapter = BusinessToolAdapter(
        path,
        require_access=lambda principal, action, resource: resource.id == "mine-new",
        resolve_resource=lambda principal, kind, resource_id: ResourceSnapshot(
            resource_id, "project-1", 7, "v1"
        ),
        storage_exists=lambda bucket, key: True,
        model_scope_resolver=lambda model_id: ["mine-old", "mine-new"],
    )
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "model.publish", {"model_id": "mine-new"})


# --------------------------------------------------------------------------- #
# 10.3 检测与引用：模型可只读引用，输出归显式项目
# --------------------------------------------------------------------------- #
@pytest.fixture
def chain(tmp_path):
    return Fixture(tmp_path)


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (4, 4), (10, 20, 30)).save(path)
    return path


def test_detection_preview_names_output_project_and_model_project(chain):
    dataset_id = chain.dataset()
    model_id = chain.model(dataset_id, status=3)
    image = chain.upload(_png(chain.root / "a.png"), "image", "image/png")
    adapter = chain.adapter()

    preview = adapter.prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id,
    })["preview"]
    assert preview["blocked"] is False
    assert preview["output_project_id"] == "project-1"
    assert preview["model_project_id"] == "project-1"
    assert preview["output_project_source"] == "asset_project"


def test_detection_refuses_an_output_project_the_user_cannot_write(chain):
    dataset_id = chain.dataset()
    model_id = chain.model(dataset_id, status=3)
    image = chain.upload(_png(chain.root / "b.png"), "image", "image/png")

    adapter = chain.adapter(
        # 项目资源按 ID 解析；写权限只给 project-1
        resolve_resource=lambda principal, kind, resource_id: ResourceSnapshot(
            resource_id, "project-1" if kind != "project" else resource_id, 7, "v1"
        ),
        require_access=lambda principal, action, resource: not (
            getattr(resource, "project_id", "") == "project-other"
        ),
    )
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "detection.run", {
            "asset_id": image, "model_id": model_id,
            "output_project_id": "project-other",
        })


def test_detection_record_belongs_to_the_output_project_not_the_model(chain):
    dataset_id = chain.dataset()
    model_id = chain.model(dataset_id, status=3)
    image = chain.upload(_png(chain.root / "c.png"), "image", "image/png")
    adapter = chain.adapter(
        detector_predictor=lambda detector, resolved, confidence: SimpleNamespace(
            detections=[{"class_id": 0, "confidence": 0.8, "bbox": [1, 2, 3, 4]}]
        ),
    )
    prepared = adapter.prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id,
    })
    result = adapter.execute_tool(7, prepared["operation_id"], "detect-output")
    assert result["status"] == "succeeded"
    assert result["outputs"]["output_project_id"] == "project-1"
    # 业务引用带着输出项目，登记后记录归输出项目，模型归属不变
    assert result["business_refs"][0]["project_id"] == "project-1"
    rows = chain.sql("SELECT status FROM models WHERE id=?", (model_id,))
    assert rows and rows[0][0] == 3, "检测不修改模型状态与归属"


def test_detection_preview_marks_cross_project_model_as_readonly_reference(chain):
    """跨项目只读引用模型不再阻断；模型归属也不因此改变。"""
    dataset_id = chain.dataset()
    model_id = chain.model(dataset_id, status=3)
    image = chain.upload(_png(chain.root / "d.png"), "image", "image/png")

    def resolve(principal, kind, resource_id):
        project = "project-2" if resource_id == model_id else "project-1"
        return ResourceSnapshot(resource_id, project, 7, "v1")

    adapter = chain.adapter(resolve_resource=resolve)
    preview = adapter.prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id,
    })["preview"]
    assert preview["blocked"] is False
    assert preview["model_source"] == "cross_project_readonly"
    assert preview["output_project_id"] == "project-1"


def test_detection_still_requires_the_paired_x_image(chain):
    dataset_id = chain.dataset(mode="multimodal", secondary="infrared")
    model_id = chain.model(dataset_id, mode="multimodal", secondary="infrared")
    image = chain.upload(_png(chain.root / "e.png"), "image", "image/png")
    preview = chain.adapter().prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id,
    })["preview"]
    assert preview["blocked"] is True
    assert preview["blocked_reasons"][0]["code"] == "MODALITY_PAIR_REQUIRED"


# --------------------------------------------------------------------------- #
# 10.1 复核提交后的业务变更通知：核实版本才接力
# --------------------------------------------------------------------------- #
class _NotifyFixture:
    """最小可用的 Agent 库 + 一个等待中的应用任务。"""

    def __init__(self, tmp_path: Path) -> None:
        import os

        self._env = {
            key: os.environ.get(key)
            for key in ("AGENT_SECRET_PATH", "AGENT_CANDIDATE_ROOT")
        }
        os.environ["AGENT_SECRET_PATH"] = str(tmp_path / "secret.key")
        os.environ["AGENT_CANDIDATE_ROOT"] = str(tmp_path / "candidates")

        from flask import Flask

        from agent.store import reset_schema_cache
        from utils.db import Database

        reset_schema_cache()
        self.db_path = str(tmp_path / "notify.sqlite3")
        database = Database()
        database.db_path = self.db_path
        database.initialize_db()
        # agent.store 通过 current_app.config["DATABASE"] 选库；没有 app 上下文
        # 就会写进仓库里的开发库——测试必须显式指定临时库
        self.app = Flask(__name__)
        self.app.config.update(DATABASE=self.db_path, TESTING=True,
                               TEMP_DIR=str(tmp_path / "temp"))
        self.ctx = self.app.app_context()
        self.ctx.push()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user (id,username,password,role,status) "
                "VALUES (2,'owner','x',0,1)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO user (id,username,password,role,status) "
                "VALUES (3,'other','x',0,1)"
            )

        from agent.identity import ActorContext
        from agent import permissions, sessions, tasks

        self.actor = ActorContext(user_id=2, roles=("user",), source="system")
        self.project = permissions.ensure_default_project(self.actor)
        self.session = sessions.create_session(
            self.actor, self.project["id"], "复核接力"
        )
        self.task = tasks.create_task(
            self.actor, self.session["id"], self.project["id"], "跑完检测再复核"
        )
        tasks.set_status(self.task["id"], tasks.STATUS_AWAITING_HUMAN,
                         summary="等待人工复核")
        tasks.set_waiting(
            self.task["id"], kind="human_review", reason="等待人工复核",
            refs=[{"kind": "record", "id": "rec-1", "version": "1"}],
        )
        # 接力依据是任务上的业务引用（不是 waiting_for 里的展示用 refs）
        tasks.add_business_ref(
            self.task["id"], {"kind": "record", "id": "rec-1", "version": "1"}
        )

    def cleanup(self) -> None:
        import os

        self.ctx.pop()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_notifier_relays_only_after_the_real_version_advances(tmp_path):
    from agent import resources
    from agent.policy_bridge import BusinessNotifier
    from agent import tasks as tasks_module

    fx = _NotifyFixture(tmp_path)
    try:
        # 业务行存在 → 可登记；登记后版本为 1
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute(
                "INSERT INTO detection_records (id, user_id, model_id, model_name, "
                "original_image_bucket, original_image_object_key, "
                "result_image_bucket, result_image_object_key, detection_result, "
                "confidence_threshold, create_time) VALUES ('rec-1', 2, 'm1', 'model', "
                "'b', 'o', 'b', 'r', '{}', 0.25, '2026-01-01T00:00:00')"
            )
        resources.ensure("detection_records", "rec-1")

        notifier = BusinessNotifier()
        event = {
            "tool": "review.submit_human",
            "business_refs": [{"type": "record", "id": "rec-1", "version": "1"}],
        }
        # 版本没动 → 不接力，任务继续等待
        notifier(2, event)
        assert notifier.matched_tasks == 0
        assert notifier.unchanged == 1
        events = tasks_module.list_events(fx.session["id"], 0)
        assert not any(item["type"] == "business.changed" for item in events)

        # 真实版本前进（人工页面提交后递增）→ 这次才接力
        resources.bump("detection_records", "rec-1")
        notifier(2, event)
        assert notifier.matched_tasks == 1
        events = tasks_module.list_events(fx.session["id"], 0)
        changed = [item for item in events if item["type"] == "business.changed"]
        assert changed and changed[0]["payload"]["refs"][0]["version"] == 2
    finally:
        fx.cleanup()


def test_notifier_ignores_another_users_task_and_unverifiable_refs(tmp_path):
    from agent.policy_bridge import BusinessNotifier

    fx = _NotifyFixture(tmp_path)
    try:
        notifier = BusinessNotifier()
        # 索引里根本没有这条记录 → 无法核实，不能当成已变更
        notifier(2, {"tool": "review.submit_human",
                     "business_refs": [{"type": "record", "id": "rec-1"}]})
        assert notifier.matched_tasks == 0
        # 换个用户发同样的通知 → 不碰别人任务
        notifier(3, {"tool": "review.submit_human",
                     "business_refs": [{"type": "record", "id": "rec-1"}]})
        assert notifier.matched_tasks == 0
    finally:
        fx.cleanup()
