# MD5: a19281258768cb6c02bd836a706e7963
"""
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""

"""
安全工具类
"""
from flask import g, current_app
from services.user_service import user_service
from typing import Optional
from utils.models import User

def get_current_user() -> User:
    """
    获取当前登录用户
    
    Returns:
        User: 当前登录用户
    
    Raises:
        Exception: 用户不存在或未登录
    """
    user_id = get_user_id()
    user = user_service.get_user_by_id(user_id)
    if not user:
        raise Exception("用户不存在")
    return user

def get_user_id() -> int:
    """
    获取当前登录用户ID
    
    Returns:
        int: 用户ID
    
    Raises:
        Exception: 用户未登录
    """
    try:
        user_id = getattr(g, 'user_id', None)
        if user_id is None:
            raise Exception("用户未登录")
        return user_id
    except:
        raise Exception("用户未登录")

def is_admin() -> bool:
    """
    判断当前用户是否为管理员
    
    Returns:
        bool: 是否为管理员
    """
    try:
        user = get_current_user()
        return user is not None and user.role == 1
    except:
        return False 