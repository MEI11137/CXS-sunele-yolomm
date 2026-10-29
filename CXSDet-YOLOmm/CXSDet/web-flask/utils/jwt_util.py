
import jwt
import time
import os
import secrets
from pathlib import Path
from typing import Dict, Optional
from flask import request, g
from functools import wraps
from utils.response import error
from config import FILE_STORE_CONFIG

def _load_jwt_secret() -> str:
    configured = os.environ.get('CXS_JWT_SECRET')
    if configured:
        if len(configured) < 32:
            raise RuntimeError('CXS_JWT_SECRET must contain at least 32 characters')
        return configured
    root = Path(FILE_STORE_CONFIG['base_path'])
    root.mkdir(parents=True, exist_ok=True)
    secret_path = root / '.jwt-secret'
    try:
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as secret_file:
            secret_file.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    if os.name == 'posix':
        os.chmod(secret_path, 0o600)
    value = secret_path.read_text(encoding='ascii').strip()
    if len(value) < 32:
        raise RuntimeError('Local JWT signing key is incomplete')
    return value


JWT_SECRET = _load_jwt_secret()
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
        if request.headers.get('X-Project-Id') and request.path.startswith(('/api/dataset', '/api/model', '/api/detection')):
            from utils.resource_guard import validate_project_request
            blocked = validate_project_request(user_id)
            if blocked is not None:
                return blocked
        from services.project_storage import project_scope, strict_workspace
        if request.path.startswith(('/api/dataset', '/api/model', '/api/detection', '/api/video', '/api/realtime')):
            project_id = request.headers.get('X-Project-Id')
            if project_id:
                try:
                    from agent.identity import load_actor
                    from agent.permissions import get_project
                    from flask import current_app
                    get_project(load_actor(user_id), project_id)
                    with project_scope(current_app.config['DATABASE'], project_id):
                        return f(*args, **kwargs)
                except ValueError as exc:
                    return error(str(exc), 409), 409
            if strict_workspace() and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                return error('请从已绑定本机文件夹的项目中执行此操作', 409), 409
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
