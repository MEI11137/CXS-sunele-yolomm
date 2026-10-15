# MD5: 453ac8c353226b997ec5f942874fe561
"""
版权所有 ©2025 羊羊小栈(GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""

from flask import Blueprint
from utils.response import success

# url前缀统一需要添加/api，这里的url已经和其他端对齐，禁止修改
health_bp = Blueprint('health_bp', __name__, url_prefix='/api/health')

@health_bp.route('/health_check', methods=['GET'])
def health_check():
    """健康检查接口"""
    data = {
        'status': 'ok',
        'service': 'algorithm-service',
        'version': '1.0.0'
    }
    return success(data, msg='服务状态检查成功') 