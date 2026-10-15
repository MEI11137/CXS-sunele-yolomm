
from flask import Response, json
from typing import Any

def api_response(
    data: Any = None,
    code: int = 200,
    msg: str = "success"
) -> Response:
    """
    统一的API响应格式
    
    Args:
        data: 响应数据
        code: 状态码
        msg: 提示信息
    
    Returns:
        包含统一格式的响应对象
    """
    response = {
        'code': code,
        'msg': msg,
        'data': data
    }
    return Response(
        json.dumps(response, ensure_ascii=False),
        content_type='application/json; charset=utf-8'
    )

def success(data=None, msg='操作成功'):
    """
    成功响应
    :param data: 响应数据
    :param msg: 提示信息
    :return: JSON响应
    """
    return api_response(data=data, code=200, msg=msg)

def error(msg='操作失败', code=500, data=None):
    """
    错误响应
    :param msg: 错误信息
    :param code: 错误码
    :param data: 响应数据
    :return: JSON响应
    """
    return api_response(data=data, code=code, msg=msg) 