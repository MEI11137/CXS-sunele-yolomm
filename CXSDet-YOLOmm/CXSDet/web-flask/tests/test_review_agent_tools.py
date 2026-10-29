"""复核对话工具复用真实服务；点击确认是人工决定的唯一对话授权来源。"""
from pathlib import Path

import pytest

from agent import actions, permissions, tasks
from agent.errors import AgentError
from agent.review_tools import ReviewTools, STATE, VIEW, WRITE, validate_page_context
from agent.tools.grants import needs_confirmation
from services.review_service import submit_human_review, ReviewValidationError
from tests.test_inspection_loop import setup, start


def tools(f):
    provider = ReviewTools()
    specs = {spec.name: spec for spec in provider.list_tools(f.actor)}
    return provider, specs


def card(f, provider, spec, arguments):
    prepared = provider.prepare(f.actor, spec, arguments, f.context)
    action = actions.create_action(f.actor, task_id=f.task['id'], tool_name=spec.name,
        tool_version=spec.version, tool_title=spec.title, parameters=arguments,
        preview=prepared['preview'], prepared_id=prepared['prepared_id'], target_projects=[f.project['id']])
    return prepared, action


def execute(f, provider, spec, arguments, prepared, action):
    actions.confirm(f.actor, action['id'], expected_revision=1)
    return provider.execute(f.actor, spec, arguments, request_key=action['request_key'], prepared_id=prepared['prepared_id'],
        context={**f.context, 'authorization': {'confirmed': True, 'action_id': action['id']}})


def test_user_confirmation_submits_corrected_annotations_once_with_audit(setup):
    f = setup; run = start(f); record = run['state']['items'][0]['record_id']
    provider, specs = tools(f)
    arguments = {'project_id': f.project['id'], 'record_id': record, 'operation': 'confirmed', 'expected_version': 0,
                 'annotations': [{'class_id': 0, 'class_name': '热斑', 'bbox': [.1, .2, .4, .6], 'severity': 'high'}]}
    prepared, action = card(f, provider, specs[WRITE], arguments)
    assert needs_confirmation(f.actor, specs[WRITE], project_id=f.project['id'])
    assert f.backend._read_record(record)['human_review_status'] == 'pending'
    with pytest.raises(AgentError):
        provider.execute(f.actor, specs[WRITE], arguments, request_key=action['request_key'],
            prepared_id=prepared['prepared_id'], context=f.context)  # 仅伪造 confirmed=True 不够。
    result = execute(f, provider, specs[WRITE], arguments, prepared, action)
    assert result.outputs['review_version'] == 1
    assert result.outputs['review_source'] == 'user_confirmed_conversation'
    assert result.outputs['annotations'][0]['severity'] == 'high'
    assert Path(result.outputs['audit_file']).is_relative_to(f.root)
    repeat = provider.execute(f.actor, specs[WRITE], arguments, request_key=action['request_key'], prepared_id=prepared['prepared_id'],
        context={**f.context, 'authorization': {'confirmed': True, 'action_id': action['id']}})
    assert repeat.outputs == result.outputs
    assert f.backend._read_record(record)['review_version'] == 1
    assert f.db.query('SELECT count(*) AS n FROM detection_record_reviews WHERE record_id=?', (record,), fetchone=True)['n'] == 1


def test_stale_review_card_cannot_replace_new_manual_submission(setup):
    f = setup; record = start(f)['state']['items'][0]['record_id']; provider, specs = tools(f)
    arguments = {'project_id': f.project['id'], 'record_id': record, 'operation': 'no_defect', 'expected_version': 0}
    prepared, action = card(f, provider, specs[WRITE], arguments)
    submit_human_review(record, 1, 0, [], 'no_defect', f.db)
    with pytest.raises(AgentError):
        execute(f, provider, specs[WRITE], arguments, prepared, action)
    assert f.backend._read_record(record)['review_version'] == 1


def test_draft_and_view_events_do_not_confirm_any_record(setup):
    f = setup; record = start(f)['state']['items'][0]['record_id']; provider, specs = tools(f)
    arguments = {'project_id': f.project['id'], 'record_id': record, 'operation': 'draft', 'expected_version': 0,
                 'annotations': [], 'editor_revision': 42}
    prepared, action = card(f, provider, specs[WRITE], arguments)
    result = execute(f, provider, specs[WRITE], arguments, prepared, action)
    assert result.outputs['draft_requested'] is True
    provider.execute(f.actor, specs[VIEW], {'project_id': f.project['id'], 'record_id': record, 'command': 'mode', 'value': 'pan'},
        request_key='view', context=f.context)
    row = f.backend._read_record(record)
    assert row['review_version'] == 0 and row['human_review_status'] == 'pending'
    events = tasks.list_events(f.chat['id'])
    assert any(event['type'] == 'review.draft' and event['payload']['editor_revision'] == 42 for event in events)
    assert any(event['type'] == 'review.view' and event['payload']['value'] == 'pan' for event in events)


def test_delete_and_restore_keep_files_and_filter_batch_queue(setup, monkeypatch):
    f = setup; run = start(f); record = run['state']['items'][0]['record_id']; provider, specs = tools(f)
    monkeypatch.setattr(f.backend.storage_client, 'delete', lambda *args: pytest.fail('must preserve files'))
    arguments = {'project_id': f.project['id'], 'record_id': record, 'operation': 'delete', 'expected_version': 0}
    prepared, action = card(f, provider, specs[WRITE], arguments)
    result = execute(f, provider, specs[WRITE], arguments, prepared, action)
    assert result.outputs['record_status'] == 'deleted' and result.outputs['files_preserved']
    from agent.inspection_loop import _review_queue
    assert record not in _review_queue(run)
    with pytest.raises(ReviewValidationError):
        submit_human_review(record, 1, 1, [], 'no_defect', f.db)
    arguments = {**arguments, 'operation': 'restore', 'expected_version': 1}
    prepared, action = card(f, provider, specs[WRITE], arguments)
    execute(f, provider, specs[WRITE], arguments, prepared, action)
    assert record in _review_queue(run)


def test_page_context_is_only_a_checked_draft_not_authorization(setup):
    f = setup; record = start(f)['state']['items'][0]['record_id']; provider, specs = tools(f)
    context = {'project_id': f.project['id'], 'record_id': record, 'expected_version': 0,
               'annotations': [], 'draft_revision': 1793000000000, 'zoom': 1.5, 'mode': 'pan', 'approved': True}
    checked = validate_page_context(f.actor, f.chat['id'], context)
    assert checked['draft_revision'] == 1793000000000 and 'approved' not in checked
    assert f.backend._read_record(record)['human_review_status'] == 'pending'
    with pytest.raises(AgentError):
        validate_page_context(f.actor, f.chat['id'], {**context, 'project_id': 'another'})
    with pytest.raises(AgentError):
        provider.execute(f.actor, specs[STATE], {'project_id': f.project['id'], 'record_id': record}, request_key='other',
                         context={**f.context, 'project_id': 'another'})


def test_rejecting_last_card_waits_for_input_instead_of_faking_running(setup):
    f = setup; record = start(f)['state']['items'][0]['record_id']; provider, specs = tools(f)
    arguments = {'project_id': f.project['id'], 'record_id': record, 'operation': 'delete', 'expected_version': 0}
    _, action = card(f, provider, specs[WRITE], arguments)
    tasks.set_status(f.task['id'], tasks.STATUS_AWAITING_ACTION, force=True)
    actions.reject(f.actor, action['id'], '验收取消，不删除')
    assert tasks.get_task(f.actor, f.task['id'])['status'] == tasks.STATUS_AWAITING_INPUT
    assert f.backend._read_record(record)['record_status'] != 'deleted'
    assert f.backend._read_record(record)['review_version'] == 0


def test_another_account_cannot_prepare_a_review_or_delete_card(setup):
    f = setup; record = start(f)['state']['items'][0]['record_id']; provider, specs = tools(f)
    from agent.identity import load_actor
    with pytest.raises(AgentError):
        provider.prepare(load_actor(2), specs[WRITE], {'project_id': f.project['id'], 'record_id': record,
            'operation': 'delete', 'expected_version': 0}, f.context)
    assert f.backend._read_record(record)['record_status'] != 'deleted'
