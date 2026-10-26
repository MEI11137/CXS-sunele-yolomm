"""Independent review of the WorkBuddy backend snapshot, 2026-10-25.

These are known, reproducible review findings, NOT passed acceptance checks.
expectedFailure keeps the existing baseline runnable while exposing each gap.
After fixing a finding, remove its decorator and require the check to pass.
All cases use temporary databases, fake credentials and scripted model output.
No production data, network requests or real training are involved.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests import test_agent_backend as baseline
from agent import permissions
from agent.config import settings
from agent.errors import AgentError
from agent.runtime import AgentRuntime
from agent.tools import business, grants
from agent.tools.contract import RISK_CRITICAL, RISK_READ, SOURCE_MCP, ToolSpec
from agent.tools.registry import ToolRegistry


class AgentReviewBoundaries(unittest.TestCase):
    def setUp(self):
        self.fixture = baseline.AgentBackendTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_same_source_must_not_disclose_another_users_private_resource(self):
        f = self.fixture
        private = permissions.register_resource(
            f.a1, project_id=f.project["id"], kind="model",
            display_name="private-review-example", source_table="models",
            source_id="private-review-model",
        )
        other_project = permissions.ensure_default_project(f.a2)
        client, _ = f._client()
        from utils.jwt_util import generate_token

        response = client.post(
            f"/api/agent/projects/{other_project['id']}/resources",
            json={"kind": "model", "display_name": "claim",
                  "source_table": "models", "source_id": private["source_id"]},
            headers={"Authorization": f"Bearer {generate_token(2)}"},
        )
        self.assertIn(response.status_code, (403, 404))

    def test_http_must_not_bind_an_unverified_business_source(self):
        f = self.fixture
        client, token = f._client()
        response = client.post(
            f"/api/agent/projects/{f.project['id']}/resources",
            json={"kind": "model", "display_name": "unverified-example",
                  "source_table": "models", "source_id": "unverified-source-id"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn(response.status_code, (400, 403, 404))

    @unittest.expectedFailure
    def test_default_configuration_must_disable_stub_tools(self):
        with patch.dict(os.environ):
            os.environ.pop("AGENT_ALLOW_STUB", None)
            self.assertFalse(settings().allow_stub_tools)

    @unittest.expectedFailure
    def test_external_prepare_must_wait_for_user_approval(self):
        f = self.fixture
        spec = ToolSpec(
            name="review_external_reader", title="Review external reader",
            description="Review-only fake external reader",
            parameters={"type": "object", "properties": {},
                        "additionalProperties": False},
            source=SOURCE_MCP, server_id="review-server", risk=RISK_READ,
            supports_preview=True,
        )

        class Provider:
            prepare_calls = 0

            def list_tools(self, actor):
                return [spec]

            def prepare(self, *args, **kwargs):
                self.prepare_calls += 1
                return {"preview": {"review_only": True}}

            def execute(self, *args, **kwargs):
                raise AssertionError("Execution must not occur before approval")

        provider = Provider()
        registry = ToolRegistry()
        registry.add_provider(provider, source=SOURCE_MCP)
        gateway = baseline.ScriptedGateway(baseline.tool_turn(spec.name))
        runtime = AgentRuntime(gateway_factory=lambda actor: gateway, registry=registry)
        session = f._session("External approval review")
        result = runtime.handle_message(f.a1, session["id"], "Read external data")
        self.assertEqual(result["status"], "awaiting_action")
        self.assertEqual(provider.prepare_calls, 0)

    @unittest.expectedFailure
    def test_critical_tool_cannot_disable_confirmation_via_spec_flag(self):
        spec = ToolSpec(
            name="review_publish", title="Review publish", description="Review only",
            risk=RISK_CRITICAL, requires_confirmation=False,
        )
        self.assertTrue(grants.needs_confirmation(self.fixture.a1, spec))

    @unittest.expectedFailure
    def test_cross_project_confirmation_must_name_actual_target_project(self):
        f = self.fixture
        target = permissions.create_project(f.a1, "Different review target")
        session = baseline.sessions.create_session(
            f.a1, f.project["id"], "Current chat project"
        )
        self.assertNotEqual(session["project_id"], target["id"])
        gateway = baseline.ScriptedGateway(baseline.tool_turn(
            "run_detection", project_id=target["id"],
            model_id="review-model", image_refs=["review-image.jpg"],
        ))
        result = f._runtime(gateway).handle_message(
            f.a1, session["id"], "Detect in the explicitly referenced project"
        )
        self.assertEqual(result["action"]["parameters"]["project_id"], target["id"])
        self.assertEqual(result["action"]["target_projects"], [target["id"]])

    @unittest.expectedFailure
    def test_empty_business_response_must_not_be_treated_as_success(self):
        with self.assertRaises(AgentError):
            business._as_result({})


if __name__ == "__main__":
    unittest.main(verbosity=2)
