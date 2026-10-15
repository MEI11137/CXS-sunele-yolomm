# MD5: 4ee205ed1c0ce729b7180d30a09644c5
"""
版权所有©2025羊羊小栈（GJQ）

作者：羊羊小栈
时间：2025-09-06 23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""

from typing import Dict, List, Optional, Union
from utils.db import db
from utils.models import User, UserInfo, UserLoginVO, PageVO, encrypt_password
from utils.jwt_util import generate_token
from clients.file_client import file_client
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class UserService:
    @staticmethod
    def login(username: str, password: str) -> Optional[UserLoginVO]:
        """
        用户登录
        """
        # 查询用户
        user_data = db.query("SELECT * FROM user WHERE username = ?", (username,), fetchone=True)
        if not user_data:
            return None
        
        user = User(user_data)
        
        # 密码验证
        if user.password != encrypt_password(password):
            return None
        
        # 检查用户状态
        if user.status == 0:
            raise Exception("账号已被禁用")
        
        # 生成token
        token = generate_token(user.id)
        
        # 返回登录信息
        return UserLoginVO(user, token)

    @staticmethod
    def register(username: str, password: str, real_name: str, phone: str, email: str) -> bool:
        """
        用户注册
        返回值: 注册成功返回True，失败返回False
        """
        # 检查用户名是否已存在
        existing_user = db.query("SELECT id FROM user WHERE username = ?", (username,), fetchone=True)
        if existing_user:
            return False
        
        # 密码加密
        encrypted_password = encrypt_password(password)
        
        # 插入用户，返回值为影响的行数
        affected_rows = db.execute(
            "INSERT INTO user (username, password, real_name, phone, email, role, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, encrypted_password, real_name, phone, email, 0, 1)
        )
        
        return affected_rows > 0

    @staticmethod
    def update_password(user_id: int, old_password: str, new_password: str) -> bool:
        """
        修改密码
        """
        # 查询用户
        user_data = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_data:
            return False
        
        user = User(user_data)
        
        # 验证旧密码
        if user.password != encrypt_password(old_password):
            return False
        
        # 更新密码
        affected_rows = db.execute(
            "UPDATE user SET password = ? WHERE id = ?",
            (encrypt_password(new_password), user_id)
        )
        
        return affected_rows > 0

    @staticmethod
    def update_user(user_id: int, user_data: Dict) -> bool:
        """
        更新用户信息
        """
        # 查询用户是否存在
        user_result = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_result:
            return False
        
        user = User(user_result)
        
        # 如果更新了头像，需要删除旧头像
        old_bucket = user.avatar_bucket
        old_object_key = user.avatar_object_key
        new_bucket = user_data.get('avatarBucket')
        new_object_key = user_data.get('avatarObjectKey')
        
        if ((new_bucket is not None and new_bucket != old_bucket) or 
            (new_object_key is not None and new_object_key != old_object_key)):
            # 如果旧头像存在，尝试删除
            if old_bucket and old_object_key:
                try:
                    # 使用文件客户端删除旧头像
                    file_client.delete(old_bucket, old_object_key)
                except Exception as e:
                    # 删除失败不影响更新
                    logger.error(f"删除旧头像失败: {str(e)}")
        
        # 构建更新SQL
        fields = []
        params = []
        
        if 'realName' in user_data:
            fields.append("real_name = ?")
            params.append(user_data['realName'])

        if 'role' in user_data:
            fields.append("role = ?")
            params.append(user_data['role'])
        
        if 'phone' in user_data:
            fields.append("phone = ?")
            params.append(user_data['phone'])
        
        if 'email' in user_data:
            fields.append("email = ?")
            params.append(user_data['email'])
        
        if 'avatarBucket' in user_data:
            fields.append("avatar_bucket = ?")
            params.append(user_data['avatarBucket'])
        
        if 'avatarObjectKey' in user_data:
            fields.append("avatar_object_key = ?")
            params.append(user_data['avatarObjectKey'])
        
        if not fields:
            return True  # 没有更新内容
        
        # 执行更新
        sql = f"UPDATE user SET {', '.join(fields)} WHERE id = ?"
        params.append(user_id)
        
        affected_rows = db.execute(sql, tuple(params))
        return affected_rows > 0

    @staticmethod
    def get_user_page(query_params: Dict) -> PageVO:
        """
        分页查询用户列表
        """
        # 基础SQL
        sql = "SELECT * FROM user"
        count_sql = "SELECT COUNT(*) as total FROM user"
        
        # 条件
        conditions = []
        params = []
        
        if query_params.get('username'):
            conditions.append("username LIKE ?")
            params.append(f"%{query_params['username']}%")
        
        if query_params.get('realName'):
            conditions.append("real_name LIKE ?")
            params.append(f"%{query_params['realName']}%")
        
        if query_params.get('phone'):
            conditions.append("phone LIKE ?")
            params.append(f"%{query_params['phone']}%")
        
        if query_params.get('email'):
            conditions.append("email LIKE ?")
            params.append(f"%{query_params['email']}%")
        
        if query_params.get('role') is not None:
            conditions.append("role = ?")
            params.append(query_params['role'])
        
        if query_params.get('status') is not None:
            conditions.append("status = ?")
            params.append(query_params['status'])
        
        # 添加条件
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)
            sql += where_clause
            count_sql += where_clause
        
        # 获取总数
        total_result = db.query(count_sql, tuple(params), fetchone=True)
        total = total_result['total'] if total_result else 0
        
        # 分页
        current = int(query_params.get('current', 1))
        size = int(query_params.get('size', 10))
        offset = (current - 1) * size
        
        sql += f" ORDER BY id DESC LIMIT {size} OFFSET {offset}"
        
        # 执行查询
        user_list = db.query(sql, tuple(params))
        
        # 转换结果
        records = [User(user).to_dict() for user in user_list]
        
        return PageVO(records, total, size, current)

    @staticmethod
    def get_user_by_id(user_id: int) -> Optional[User]:
        """
        根据ID获取用户
        """
        user_data = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_data:
            return None
        
        return User(user_data)

    @staticmethod
    def add_user(user_data: Dict) -> bool:
        """
        新增用户
        返回值: 添加成功返回True，失败返回False
        """
        # 检查用户名是否已存在
        existing_user = db.query("SELECT id FROM user WHERE username = ?", 
                                (user_data['username'],), fetchone=True)
        if existing_user:
            return False
        
        # 默认密码
        password = user_data.get('password', '123456')
        encrypted_password = encrypt_password(password)
        
        # 插入用户，返回值为影响的行数
        affected_rows = db.execute(
            """INSERT INTO user 
               (username, password, real_name, phone, email, role, status) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_data['username'],
                encrypted_password,
                user_data.get('realName', ''),
                user_data.get('phone', ''),
                user_data.get('email', ''),
                user_data.get('role', 0),
                user_data.get('status', 1)
            )
        )
        
        return affected_rows > 0

    @staticmethod
    def delete_user(user_id: int) -> bool:
        """
        删除用户
        """
        # 查询用户
        user_data = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_data:
            return False
        
        user = User(user_data)
        
        # 不能删除管理员
        if user.role == 1:
            raise Exception("不能删除管理员")
        
        # 删除用户头像
        if user.avatar_bucket and user.avatar_object_key:
            try:
                # 使用文件客户端删除头像
                file_client.delete(user.avatar_bucket, user.avatar_object_key)
            except Exception as e:
                logger.error(f"删除用户头像失败: {str(e)}")
        
        # 删除用户，返回影响的行数
        affected_rows = db.execute("DELETE FROM user WHERE id = ?", (user_id,))
        return affected_rows > 0

    @staticmethod
    def reset_password(user_id: int) -> bool:
        """
        重置密码
        """
        # 检查用户是否存在
        user_data = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_data:
            return False
        
        # 默认密码: 123456
        default_password = encrypt_password('123456')
        
        # 执行密码重置，返回影响的行数
        affected_rows = db.execute(
            "UPDATE user SET password = ? WHERE id = ?",
            (default_password, user_id)
        )
        
        return affected_rows > 0

    @staticmethod
    def update_status(user_id: int, status: int) -> bool:
        """
        更新用户状态
        """
        # 查询用户
        user_data = db.query("SELECT * FROM user WHERE id = ?", (user_id,), fetchone=True)
        if not user_data:
            return False
        
        user = User(user_data)
        
        # 不能修改管理员状态
        if user.role == 1:
            raise Exception("不能修改管理员状态")
        
        # 执行更新，返回影响的行数
        affected_rows = db.execute(
            "UPDATE user SET status = ? WHERE id = ?",
            (status, user_id)
        )
        
        return affected_rows > 0

# 创建用户服务实例
user_service = UserService() 