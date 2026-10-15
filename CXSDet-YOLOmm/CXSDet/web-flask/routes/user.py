
from flask import Blueprint, request, g, jsonify
from utils.response import success, error
from utils.jwt_util import token_required, admin_required, get_user_id
from services.user_service import user_service
from utils.models import User, UserLoginVO
from utils.security_utils import get_current_user, is_admin

# 用户路由
user_bp = Blueprint('user_bp', __name__, url_prefix='/api/user')

@user_bp.route('/login', methods=['POST'])
def login():
    """用户登录"""
    try:
        data = request.json
        if not data or not data.get('username') or not data.get('password'):
            return error('用户名和密码不能为空', 400)
        
        login_vo = user_service.login(data.get('username'), data.get('password'))
        if not login_vo:
            return error('用户名或密码错误', 400)
        
        return success(login_vo.to_dict(), '登录成功')
    except Exception as e:
        return error(f'登录失败: {str(e)}')

@user_bp.route('/register', methods=['POST'])
def register():
    """用户注册"""
    try:
        data = request.json
        if not data or not data.get('username') or not data.get('password'):
            return error('用户名和密码不能为空', 400)
        
        if user_service.register(
            data.get('username'),
            data.get('password'),
            data.get('realName', ''),
            data.get('phone', ''),
            data.get('email', '')
        ):
            return success(None, '注册成功')
        else:
            return error('用户名已存在', 400)
    except Exception as e:
        return error(f'注册失败: {str(e)}')

@user_bp.route('/password', methods=['POST'])
@token_required
def update_password():
    """修改密码"""
    try:
        data = request.json
        if not data or not data.get('oldPassword') or not data.get('newPassword'):
            return error('旧密码和新密码不能为空', 400)
        
        if user_service.update_password(g.user_id, data.get('oldPassword'), data.get('newPassword')):
            return success(None, '密码修改成功')
        else:
            return error('旧密码错误', 400)
    except Exception as e:
        return error(f'密码修改失败: {str(e)}')

@user_bp.route('', methods=['PUT'])
@token_required
def update_user():
    """更新用户信息"""
    try:
        data = request.json
        if not data:
            return error('更新数据不能为空', 400)
        
        # 从请求数据中获取用户ID
        user_id = data.get('id')
        if not user_id:
            return error('用户ID不能为空', 400)
        
        # 检查是否有权限更新该用户
        # 管理员可以更新所有用户，普通用户只能更新自己的信息
        if not is_admin() and user_id != g.user_id:
            return error('没有权限更新该用户信息', 403)
        
        if user_service.update_user(user_id, data):
            return success(None, '更新成功')
        else:
            return error('更新失败', 400)
    except Exception as e:
        return error(f'更新失败: {str(e)}')

@user_bp.route('/page', methods=['GET'])
@admin_required
def get_user_page():
    """分页查询用户列表(需要管理员权限)"""
    try:
        # 获取查询参数
        query_params = {
            'current': request.args.get('current', 1, type=int),
            'size': request.args.get('size', 10, type=int),
            'username': request.args.get('username'),
            'realName': request.args.get('realName'),
            'phone': request.args.get('phone'),
            'email': request.args.get('email'),
            'role': request.args.get('role', type=int),
            'status': request.args.get('status', type=int)
        }
        
        # 执行查询
        page_vo = user_service.get_user_page(query_params)
        
        return success(page_vo.to_dict())
    except Exception as e:
        return error(f'查询失败: {str(e)}')

@user_bp.route('/current', methods=['GET'])
@token_required
def get_current_user_info():
    """获取当前登录用户信息"""
    try:
        # 使用安全工具类获取当前用户
        user = get_current_user()
        
        # 获取token
        token = request.headers.get('Authorization')
        if token and token.startswith('Bearer '):
            token = token[7:]
        
        # 直接创建UserLoginVO对象，不调用登录方法
        login_vo = UserLoginVO(user, token)
        return success(login_vo.to_dict(), '获取成功')
    except Exception as e:
        return error(f'获取失败: {str(e)}')

@user_bp.route('/logout', methods=['POST'])
@token_required
def logout():
    """退出登录"""
    # 实际上JWT是无状态的，客户端只需要丢弃token即可
    # 这里只是为了兼容前端接口
    return success(None, '退出成功')

@user_bp.route('/<int:id>', methods=['GET'])
@admin_required
def get_user_info(id):
    """获取用户详情(需要管理员权限)"""
    try:
        user = user_service.get_user_by_id(id)
        if not user:
            return error('用户不存在', 404)
        
        return success(user.to_dict(), '获取成功')
    except Exception as e:
        return error(f'获取失败: {str(e)}')

@user_bp.route('', methods=['POST'])
@admin_required
def add_user():
    """新增用户(需要管理员权限)"""
    try:
        data = request.json
        if not data or not data.get('username'):
            return error('用户名不能为空', 400)
        
        if user_service.add_user(data):
            return success(None, '添加成功')
        else:
            return error('用户名已存在', 400)
    except Exception as e:
        return error(f'添加失败: {str(e)}')

@user_bp.route('/<int:id>', methods=['DELETE'])
@admin_required
def delete_user(id):
    """删除用户(需要管理员权限)"""
    try:
        if user_service.delete_user(id):
            return success(None, '删除成功')
        else:
            return error('删除失败', 400)
    except Exception as e:
        return error(f'删除失败: {str(e)}')

@user_bp.route('/<int:id>/reset-password', methods=['PUT'])
@admin_required
def reset_password(id):
    """重置密码(需要管理员权限)"""
    try:
        if user_service.reset_password(id):
            return success(None, '重置成功')
        else:
            return error('重置失败', 400)
    except Exception as e:
        return error(f'重置失败: {str(e)}')

@user_bp.route('/<int:id>/status', methods=['PUT'])
@admin_required
def update_user_status(id):
    """更新用户状态(需要管理员权限)"""
    try:
        data = request.json
        if not data or data.get('status') is None:
            return error('状态不能为空', 400)
        
        if user_service.update_status(id, data.get('status')):
            return success(None, '状态更新成功')
        else:
            return error('状态更新失败', 400)
    except Exception as e:
        return error(f'状态更新失败: {str(e)}') 


"""
版权所有 © 2025 羊羊小栈 (GJQ)

作者：羊羊小栈
时间：2025-09-06 23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需要立即停止侵权行为，并按照【羊羊 小栈系统版权声明和保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""