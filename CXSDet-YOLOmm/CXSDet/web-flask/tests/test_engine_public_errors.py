import pytest

from agent.hx_engine_runtime import _model_failure_message


@pytest.mark.parametrize('status,expected', [(401, '认证'), (403, '访问'),
    (429, '限流'), (503, '暂时不可用'), (400, '请求失败')])
def test_provider_status_is_actionable_without_raw_payload(status, expected):
    result = _model_failure_message({'data': {'statusCode': status,
        'message': 'private-body', 'responseHeaders': {'Authorization': 'private-token'}}})
    assert str(status) in result and expected in result
    assert 'private' not in result and 'responseHeaders' not in result


@pytest.mark.parametrize('error', ['private-error', None, {'data': {'statusCode': '503', 'message': 'private'}}])
def test_unknown_provider_payload_never_echoed(error):
    result = _model_failure_message(error)
    assert '调用失败' in result and 'private' not in result
