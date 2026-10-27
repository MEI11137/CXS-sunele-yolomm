"""INT-09：计划步骤推进、逐项失败穿透、等待步骤完成。

这些用例不需要数据库：被测函数只根据"真实执行结果"决定是否推进步骤，
所以用打桩的 task_store 就能覆盖全部规则，也便于说明每条规则的边界。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agent import recovery  # noqa: E402
from agent.business_bridge import _effective_status  # noqa: E402
from agent.runtime import AgentRuntime  # noqa: E402


def _task(steps):
    return {"id": "t1", "steps": steps}


def _step(step_id, status="pending", **extra):
    return {"id": step_id, "title": step_id, "status": status, **extra}


class EffectiveStatusTest(unittest.TestCase):
    """外层 succeeded 不能掩盖逐项失败。"""

    def test_partial_items_override_outer_success(self) -> None:
        self.assertEqual(
            _effective_status("succeeded", {"status": "partial_failed", "failed": 2}),
            "partial_failed",
        )
        self.assertEqual(_effective_status("succeeded", {"status": "failed"}), "failed")

    def test_all_success_keeps_outer_status(self) -> None:
        self.assertEqual(_effective_status("succeeded", {"status": "succeeded"}), "succeeded")
        # 逐项状态比外层"更好"时不能反过来升级
        self.assertEqual(_effective_status("failed", {"status": "succeeded"}), "failed")

    def test_missing_or_unknown_item_status_is_ignored(self) -> None:
        self.assertEqual(_effective_status("succeeded", {}), "succeeded")
        self.assertEqual(_effective_status("succeeded", {"status": "whatever"}), "succeeded")

    def test_training_submission_is_running_not_completed(self):
        from agent.business_bridge import BusinessBackendBridge
        from agent.tools.contract import ToolSpec
        result = BusinessBackendBridge(None)._to_result({'status':'succeeded', 'operation_id':'c-op', 'outputs':{'model_id':'child','status':0,'state':'training'}, 'business_refs':[{'type':'model','id':'child','project_id':'actual-project'}]}, ToolSpec(name='business_training_start', title='训练', description=''))
        self.assertEqual(result.status, 'running')
        self.assertEqual(result.execution_state, 'executed')
        self.assertIsNone(result.error)
        self.assertEqual(result.business_refs[0]['project_id'], 'actual-project')


class PlanStepAdvanceTest(unittest.TestCase):
    """步骤推进必须跟着真实执行结果，不能靠模型一句话。"""

    def _advance(self, steps, *, name="detection.run", risk="write", status="succeeded"):
        task = _task(steps)
        spec = SimpleNamespace(name=name, risk=risk)
        result = SimpleNamespace(status=status)
        with patch("agent.runtime.task_store.get_task_by_id", return_value=task), patch(
            "agent.runtime.task_store.mark_step"
        ) as mark:
            AgentRuntime._advance_plan_steps("t1", spec, result)
        return mark

    def test_bound_step_is_marked_done(self) -> None:
        mark = self._advance(
            [_step("s1"), _step("s2", tool="detection.run"), _step("s3")],
        )
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[:3], ("t1", "s2", "done"))

    def test_readonly_tool_does_not_advance_unbound_step(self) -> None:
        """只读查询只是取证，不能把计划推到底。"""
        mark = self._advance([_step("s1"), _step("s2")], name="detection.get_state", risk="read")
        mark.assert_not_called()

    def test_unbound_write_cannot_prove_unrelated_step_completed(self) -> None:
        mark = self._advance(
            [
                _step("s1", needs_human=True),
                _step("s2"),
                _step("s3"),
            ]
        )
        mark.assert_not_called()

    def test_partial_and_human_results_cannot_finish_step(self) -> None:
        self._advance([_step("s1", tool="detection.run")], status="partial").assert_not_called()
        self._advance([_step("s1", tool="detection.run", needs_human=True)]).assert_not_called()

    def test_failed_execution_advances_nothing(self) -> None:
        mark = self._advance([_step("s1")], status="failed")
        mark.assert_not_called()

    def test_unknown_execution_advances_nothing(self) -> None:
        mark = self._advance([_step("s1")], status="unknown")
        mark.assert_not_called()

    def test_already_finished_steps_are_not_reopened(self) -> None:
        mark = self._advance([_step("s1", status="done"), _step("s2", status="failed")])
        mark.assert_not_called()

    def test_no_plan_means_no_step_writes(self) -> None:
        mark = self._advance([])
        mark.assert_not_called()


class WaitingStepCompletionTest(unittest.TestCase):
    """业务/人工状态满足后，对应的等待步骤才算完成。"""

    def _complete(self, steps, kind):
        task = _task(steps)
        task["waiting_for"] = {"refs": [{"kind": "model", "id": "m1"}]}
        with patch("agent.recovery.task_store.get_task_by_id", return_value=task), patch(
            "agent.recovery.task_store.mark_step"
        ) as mark:
            recovery._complete_waiting_steps("t1", kind)
        return mark

    def test_human_waiting_step_is_completed(self) -> None:
        mark = self._complete(
            [_step("s1", status="done"), _step("s2", needs_human=True, business_ref={"kind": "model", "id": "m1"}), _step("s3")],
            "human",
        )
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[1], "s2")

    def test_business_waiting_step_is_completed(self) -> None:
        mark = self._complete(
            [_step("s1", business_ref={"kind": "model", "id": "m1"})],
            "business",
        )
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[1], "s1")

    def test_step_without_waiting_marker_is_left_alone(self) -> None:
        mark = self._complete([_step("s1"), _step("s2")], "business")
        mark.assert_not_called()

    def test_other_object_and_other_wait_kind_are_not_completed(self) -> None:
        self._complete([_step("s1", business_ref={"kind": "model", "id": "m2"})], "business").assert_not_called()
        self._complete([_step("s1", needs_human=True)], "business").assert_not_called()


if __name__ == "__main__":
    unittest.main()
