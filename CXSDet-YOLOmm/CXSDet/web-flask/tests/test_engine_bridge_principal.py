"""私有桥请求必须把引擎所有者暴露为“当前用户”。

Agent 的工具（如 business_review_ai / business_detection_analyze_defects）通过引擎私有桥
调用业务工具，而复核/模型工厂从 flask.g 取当前用户。桥请求只有引擎令牌、没有站点 JWT，
如果不在请求上下文里写回 g.user_id，就会误报 AGENT_UNAUTHENTICATED（无法确定当前用户）。
"""

from types import SimpleNamespace

import pytest
from flask import g

from agent import bootstrap, hx_engine_runtime
from agent.errors import AgentError, Code
from agent.hx_engine_runtime import HxEngineRuntime, hxengine_bp
from tests.test_inspection_loop import setup


def test_bridge_exposes_engine_owner_as_current_principal(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    runtime.pool.tokens["bridge-test-token"] = SimpleNamespace(
        owner_id=f.actor.user_id, site_session=f.chat["id"]
    )
    f.app.extensions["haixun_engine"] = runtime
    f.app.register_blueprint(hxengine_bp)

    captured = {}
    monkeypatch.setattr(
        "services.personal_review_client.create_personal_review_client",
        lambda actor: captured.setdefault("actor", actor),
    )
    factory = bootstrap._personal_review_factory()

    with f.app.test_request_context(
        "/api/agent/engine/bridge/tools",
        headers={"Authorization": "Bearer bridge-test-token"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        resolved, engine, actor = hx_engine_runtime._bridge_engine()
        assert resolved is runtime
        assert engine.owner_id == f.actor.user_id
        assert actor.user_id == f.actor.user_id
        assert g.user_id == f.actor.user_id

        # 业务工厂在桥请求里也能解析出同一个用户，而不是抛 AGENT_UNAUTHENTICATED。
        factory()
        assert captured["actor"].user_id == f.actor.user_id


def _bridge_app(f, token, owner_id=None):
    runtime = HxEngineRuntime(f.app)
    runtime.pool.tokens[token] = SimpleNamespace(
        owner_id=owner_id or f.actor.user_id, site_session=f.chat["id"]
    )
    f.app.extensions["haixun_engine"] = runtime
    f.app.register_blueprint(hxengine_bp)
    return runtime


def test_bridge_rejects_forged_token_and_cannot_impersonate_another_user(setup, monkeypatch):
    """伪造/失效令牌不能通过请求头伪装成其它用户（例如管理员）。"""
    f = setup
    _bridge_app(f, "valid-token")
    called = []
    monkeypatch.setattr(
        "services.personal_review_client.create_personal_review_client",
        lambda actor: called.append(actor),
    )

    with f.app.test_request_context(
        "/api/agent/engine/bridge/tools",
        headers={
            "Authorization": "Bearer forged-token",
            "X-User-Id": "1",
            "X-Owner-Id": "1",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        # setup fixture 的外层 app context 可能已预置 g.user_id；记录调用前身份，
        # 断言 401 拒绝后没有被伪造令牌或请求头覆盖（含伪装成管理员的 999）。
        before = getattr(g, "user_id", None)
        with pytest.raises(AgentError) as caught:
            hx_engine_runtime._bridge_engine()
        after = getattr(g, "user_id", None)

    assert caught.value.code == Code.UNAUTHENTICATED
    assert caught.value.http_status == 401
    # 认证失败时不能把任何用户身份写进请求上下文，也不能调用本人复核客户端。
    assert after == before
    assert after != 999
    assert called == []


def test_bridge_only_accepts_valid_token_from_local_address(setup):
    f = setup
    _bridge_app(f, "valid-token")

    with f.app.test_request_context(
        "/api/agent/engine/bridge/tools",
        headers={"Authorization": "Bearer valid-token"},
        environ_base={"REMOTE_ADDR": "10.0.0.8"},
    ):
        with pytest.raises(AgentError) as caught:
            hx_engine_runtime._bridge_engine()

    assert caught.value.code == Code.UNAUTHENTICATED


def test_bridge_uses_token_owner_and_ignores_spoofed_user_headers(setup, monkeypatch):
    f = setup
    _bridge_app(f, "valid-token")
    captured = {}
    monkeypatch.setattr(
        "services.personal_review_client.create_personal_review_client",
        lambda actor: captured.setdefault("actor", actor),
    )
    factory = bootstrap._personal_review_factory()

    with f.app.test_request_context(
        "/api/agent/engine/bridge/tools",
        headers={"Authorization": "Bearer valid-token", "X-User-Id": "999", "X-Owner-Id": "999"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        _, engine, actor = hx_engine_runtime._bridge_engine()
        assert g.user_id == f.actor.user_id
        assert g.user_id != 999
        assert actor.user_id == engine.owner_id == f.actor.user_id

        factory()
        assert captured["actor"].user_id == f.actor.user_id