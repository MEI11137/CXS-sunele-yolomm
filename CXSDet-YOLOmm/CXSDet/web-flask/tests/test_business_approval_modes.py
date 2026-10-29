"""Website OpenCode policy with real business services and isolated model doubles."""
from dataclasses import replace

import pytest

from agent import actions, sessions, tasks
from agent.business_approval import automatic_mode
from agent.llm.provider import ToolCall
from agent.opencode_runtime import OpenCodeRuntime
from agent.runtime import LoopState
from agent.tools.contract import SOURCE_MCP, ToolSpec, RISK_WRITE
from tests.test_inspection_loop import setup, start


def invoke(f, runtime, task, name, arguments):
    return runtime._handle_tool_call(f.actor, sessions.get_session(f.actor,f.chat['id']), f.project,
        tasks.get_task_by_id(task['id']), ToolCall(name=name,arguments=arguments), LoopState(), {})


@pytest.mark.parametrize('mode',['assist','full'])
def test_three_detections_and_one_batch_ai_do_not_require_clicks(setup,mode):
    f=setup;run=start(f)
    sessions.set_permission_mode(f.actor,f.chat['id'],mode)
    runtime=OpenCodeRuntime(f.app)
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'检测这3张图片并批量AI核查')
    before={row['id'] for row in f.db.query('SELECT id FROM detection_records')}
    for i in range(3):
        params={'asset_id':run['state']['assets'][f'rgb-{i}'], 'secondary_asset_id':run['state']['assets'][f'x-{i}'],
                'model_id':run['state']['model_id'],'confidence':.25,'output_project_id':f.project['id']}
        assert invoke(f,runtime,task,'business_detection_run',params) is None
        assert not actions.list_actions(task['id'],[actions.STATUS_PENDING])
    ids=[row['id'] for row in f.db.query('SELECT id FROM detection_records') if row['id'] not in before]
    assert len(ids)==3
    calls_before=len(f.calls)
    assert invoke(f,runtime,task,'business_review_ai',{'record_ids':ids}) is None
    assert len(f.calls)==calls_before+3
    assert len(actions.list_actions(task['id'],[actions.STATUS_SUCCEEDED]))==4
    events=tasks.list_events(f.chat['id'])
    own=[event for event in events if event.get('task_id')==task['id']]
    assert not any(event['type']==tasks.EVENT_ACTION_REQUIRED for event in own)
    assert any(event['type']=='permission.auto_approved' and event['payload']['human_clicked'] is False for event in own)
    assert all(action['preview']['approval']=={'source':'session_policy','mode':mode} for action in actions.list_actions(task['id']))
    # Identical tool request is replayed, not another paid review.
    invoke(f,runtime,task,'business_review_ai',{'record_ids':ids})
    assert len(f.calls)==calls_before+3


def test_request_mode_retains_card_and_model_cannot_claim_full_mode(setup):
    f=setup;run=start(f);runtime=OpenCodeRuntime(f.app)
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'核查一条')
    record=run['state']['items'][0]['record_id']
    result=invoke(f,runtime,task,'business_review_ai',{'record_ids':[record]})
    assert result['status']==tasks.STATUS_AWAITING_ACTION
    assert actions.list_actions(task['id'],[actions.STATUS_PENDING])
    spec=ToolSpec(name='business_review_ai',title='spoof',description='',risk=RISK_WRITE,source=SOURCE_MCP)
    sessions.set_permission_mode(f.actor,f.chat['id'],'full')
    assert automatic_mode(f.actor,f.chat['id'],spec) is None
    assert automatic_mode(f.actor,f.chat['id'],replace(spec,source='business',server_id='external')) is None


def test_full_keeps_delete_and_final_human_decision_cards(setup):
    f=setup;run=start(f);sessions.set_permission_mode(f.actor,f.chat['id'],'full');runtime=OpenCodeRuntime(f.app)
    record=run['state']['items'][0]['record_id']
    for name,params in [
        ('workspace_delete_records',{'project_id':f.project['id'],'records':[{'record_id':record,'expected_version':0}]}),
        ('workspace_review_record',{'project_id':f.project['id'],'record_id':record,'operation':'confirmed','expected_version':0,
            'annotations':[{'class_id':0,'class_name':'热斑','bbox':[.1,.1,.4,.4]}]})]:
        task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'必须保留人工批准')
        result=invoke(f,runtime,task,name,params)
        assert result and result['status']==tasks.STATUS_AWAITING_ACTION, name
    row=f.backend._read_record(record)
    assert row['record_status']!='deleted' and row['human_review_status']=='pending'


def test_old_pending_ai_card_resumes_by_fresh_policy_without_click(setup):
    f=setup;run=start(f);runtime=OpenCodeRuntime(f.app)
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'继续当前核查')
    invoke(f,runtime,task,'business_review_ai',{'record_ids':[run['state']['items'][0]['record_id']]})
    pending=actions.list_actions(task['id'],[actions.STATUS_PENDING])[0]
    sessions.set_permission_mode(f.actor,f.chat['id'],'full')
    calls=len(f.calls)
    assert runtime._resume_automatic_actions(f.actor,sessions.get_session(f.actor,f.chat['id']),f.project,task) is None
    assert len(f.calls)==calls+1
    assert not actions.list_actions(task['id'],[actions.STATUS_PENDING])
    action=actions.get_action(f.actor,pending['id'])
    assert action['status']==actions.STATUS_SUCCEEDED
    assert action['preview']['approval']['source']=='session_policy'


def test_feedback_transfer_is_automatic_but_unconfirmed_samples_stay_blocked(setup):
    f=setup;run=start(f);sessions.set_permission_mode(f.actor,f.chat['id'],'assist');runtime=OpenCodeRuntime(f.app)
    record=run['state']['items'][0]['record_id']
    params={'project_id':f.project['id'],'records':[{'record_id':record,'expected_version':0}]}
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'加入反馈')
    invoke(f,runtime,task,'workspace_add_feedback',params)
    assert f.db.query('SELECT COUNT(*) AS n FROM feedback_samples',fetchone=True)['n']==0
    f.db.execute("UPDATE detection_records SET human_review_status='confirmed',corrected_annotations='[]' WHERE id=?",(record,))
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'已复核，加入反馈')
    assert invoke(f,runtime,task,'workspace_add_feedback',params) is None
    assert f.db.query('SELECT COUNT(*) AS n FROM feedback_samples',fetchone=True)['n']==1
    assert not actions.list_actions(task['id'],[actions.STATUS_PENDING])


def test_automatic_business_refusal_does_not_leave_action_executing(setup,monkeypatch):
    f=setup;run=start(f);sessions.set_permission_mode(f.actor,f.chat['id'],'full');runtime=OpenCodeRuntime(f.app)
    task=tasks.create_task(f.actor,f.chat['id'],f.project['id'],'核查明确记录')
    handler=f.backend._handlers()['review.ai']
    monkeypatch.setattr(handler,'execute',lambda *args,**kwargs: (_ for _ in ()).throw(ValueError('业务状态变化，拒绝执行')))
    assert invoke(f,runtime,task,'business_review_ai',{'record_ids':[run['state']['items'][0]['record_id']]}) is None
    assert not actions.list_actions(task['id'],[actions.STATUS_EXECUTING,actions.STATUS_PENDING])
    assert actions.list_actions(task['id'])[0]['status']==actions.STATUS_FAILED
