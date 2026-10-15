
import jwt
import time
from typing import Dict, Optional
from flask import request, g
from functools import wraps
from utils.response import error

# JWT 密钥
JWT_SECRET = 'gjq-web-flask-secret'
# Token过期时间(秒)
JWT_EXPIRATION = 86400  # 24小时

def generate_token(user_id: int) -> str:
    """
    生成JWT Token
    
    Args:
        user_id: 用户ID
    
    Returns:
        JWT Token
    """
    payload = {
        'user_id': user_id,
        'exp': int(time.time()) + JWT_EXPIRATION
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    return token

def get_user_id(token: str) -> Optional[int]:
    """
    从Token中获取用户ID
    
    Args:
        token: JWT Token
    
    Returns:
        用户ID
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload.get('user_id')
    except:
        return None

def validate_token(token: str) -> bool:
    """
    验证Token是否有效
    
    Args:
        token: JWT Token
    
    Returns:
        是否有效
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return True
    except:
        return False

def token_required(f):
    """
    需要Token的装饰器
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        # 检查是否提供了token
        if not token:
            return error('缺少认证Token', 401)
        
        # 去掉Bearer前缀
        if token.startswith('Bearer '):
            token = token[7:]
        
        # 验证token
        user_id = get_user_id(token)
        if not user_id:
            return error('无效的Token', 401)
        
        # 将用户ID保存到全局对象中，供视图函数使用
        g.user_id = user_id
        
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """
    需要管理员权限的装饰器
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        # 检查是否提供了token
        if not token:
            return error('缺少认证Token', 401)
        
        # 去掉Bearer前缀
        if token.startswith('Bearer '):
            token = token[7:]
        
        # 验证token
        user_id = get_user_id(token)
        if not user_id:
            return error('无效的Token', 401)
        
        # 将用户ID保存到全局对象中，供视图函数使用
        g.user_id = user_id
        
        # 检查用户是否为管理员
        from utils.security_utils import is_admin
        if not is_admin():
            return error('需要管理员权限', 403)
        
        return f(*args, **kwargs)
    return decorated 