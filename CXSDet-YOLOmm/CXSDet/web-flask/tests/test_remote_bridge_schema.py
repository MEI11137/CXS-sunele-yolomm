"""Workspace tools with task-bound context must not require an absent schema field."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent.hx_engine_runtime import HxEngineRuntime,hxengine_bp
from tests.test_inspection_loop import setup


def test_bridge_handles_both_implicit_and_explicit_project_schemas(setup):
    f=setup;runtime=HxEngineRuntime(f.app)
    f.app.register_blueprint(hxengine_bp)
    engine=SimpleNamespace(lock=threading.RLock(),busy=True,task_id=f.task['id'],site_session=f.chat['id'],project_id=f.project['id'],redact=lambda value:value)
    with patch('agent.hx_engine_runtime._bridge_engine',return_value=(runtime,engine,f.actor)):
        client=f.app.test_client()
        response=client.post('/api/agent/engine/bridge/call',json={'name':'workspace_projects','arguments':{}})
        assert response.status_code==200 and response.json['status']=='returned'
        for args in [{},{'project_id':'foreign'}]:
            response=client.post('/api/agent/engine/bridge/call',json={'name':'workspace_list_resources','arguments':args})
            assert response.status_code==403
