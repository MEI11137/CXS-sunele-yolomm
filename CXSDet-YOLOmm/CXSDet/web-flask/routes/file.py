
from flask import Blueprint, request, send_file, jsonify
import os
import uuid
from utils.response import success, error
from config import FILE_STORE_CONFIG

# url前缀统一需要添加/api，这里的url已经和其他端对齐
file_bp = Blueprint('file_bp', __name__, url_prefix='/api/file')

def ensure_directory(bucket_name):
    """确保存储目录存在"""
    directory = os.path.join(FILE_STORE_CONFIG['base_path'], bucket_name)
    if not os.path.exists(directory):
        os.makedirs(directory)
    return directory

@file_bp.route('/upload/<bucket>', methods=['POST'])
def upload_file(bucket):
    """上传文件"""
    if 'file' not in request.files:
        return error('没有文件', 400)
    
    file = request.files['file']
    if file.filename == '':
        return error('没有选择文件', 400)
    
    # 验证文件类型
    if file.content_type not in FILE_STORE_CONFIG['allowed_types']:
        return error('不支持的文件类型', 400)
    
    # 一次性读取文件内容
    content = file.read()
    
    # 验证文件大小
    if len(content) > FILE_STORE_CONFIG['max_size']:
        return error('文件大小超过限制', 400)
    
    # 检查是否是缓存文件
    is_cache = request.form.get('is_cache') == 'true'

    # 生成文件名并保存
    # 获取扩展名
    if file.filename.endswith('.nii.gz'):
        extension = '.nii.gz'  # 对于.nii.gz文件使用完整扩展名
    else:
        extension = os.path.splitext(file.filename)[1]  # 直接从原始文件名获取扩展名, 已经带点，例如.pdf
    if is_cache:
        # 如果是缓存文件，直接使用原文件名
        filename = file.filename
    else:
        # 否则，使用uuid作为文件名
        filename = str(uuid.uuid4()) + extension
    
    directory = ensure_directory(bucket)
    file_path = os.path.join(directory, filename)
    
    # 使用已读取的内容写入文件
    with open(file_path, 'wb') as f:
        f.write(content)
    
    # 返回文件访问URL
    url = f"{FILE_STORE_CONFIG['access_url']}/{bucket}/{filename}"
    return success({
        'url': url,
        'bucket': bucket,
        'objectKey': filename
    }, '上传成功')

@file_bp.route('/<bucket>/<object_key>', methods=['GET'])
def get_file(bucket, object_key):
    """获取文件"""
    file_path = os.path.join(FILE_STORE_CONFIG['base_path'], bucket, object_key)
    if not os.path.exists(file_path):
        return error('文件不存在', 404)
    return send_file(file_path)

@file_bp.route('/<bucket>/<object_key>', methods=['DELETE'])
def delete_file(bucket, object_key):
    """删除文件"""
    file_path = os.path.join(FILE_STORE_CONFIG['base_path'], bucket, object_key)
    if os.path.exists(file_path):
        os.remove(file_path)
    return success(None, '删除成功') 