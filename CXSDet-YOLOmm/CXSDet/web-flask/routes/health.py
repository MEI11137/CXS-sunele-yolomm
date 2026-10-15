
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