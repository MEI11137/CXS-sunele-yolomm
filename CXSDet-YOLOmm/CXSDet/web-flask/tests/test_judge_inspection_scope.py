import pytest
from agent import identity
from agent.inspection_loop import InspectionLoopTools, START, ROUTE
from agent.errors import AgentError
from tests.test_inspection_loop import setup, start

def ordinary(f):
    f.db.execute('UPDATE user SET role=0 WHERE id=1')
    f.actor = identity.load_actor(1)
    assert not f.actor.is_admin
    return f.actor

def test_ordinary_without_demo_root_has_no_folder_loop_tools(setup):
    f = setup
    names = {s.name for s in InspectionLoopTools().list_tools(ordinary(f))}
    assert START not in names and ROUTE not in names

def test_judge_can_discover_and_start_inside_prebound_demo_project(setup):
    f = setup
    ordinary(f)
    f.app.config['DEMO_WORKSPACE_ROOT'] = str(f.root)
    f.specs = {s.name:s for s in f.provider.list_tools(f.actor)}
    assert {START,ROUTE,'workspace_discover_inputs'} <= set(f.specs)
    result=f.provider.execute(f.actor,f.specs['workspace_discover_inputs'],
        {'project_id':f.project['id']},request_key='judge-discover',context=f.context)
    assert result.outputs['dataset_summaries'][0]['sample_count']==3
    assert start(f)['status']=='awaiting_human'

def test_demo_tool_visibility_does_not_allow_outside_folder(setup):
    f=setup
    ordinary(f)
    other=f.root.parent/'separate';other.mkdir()
    f.app.config['DEMO_WORKSPACE_ROOT']=str(other)
    specs={s.name:s for s in f.provider.list_tools(f.actor)}
    with pytest.raises(AgentError):
        f.provider.execute(f.actor,specs['workspace_discover_inputs'],
            {'project_id':f.project['id']},request_key='outside',context=f.context)
