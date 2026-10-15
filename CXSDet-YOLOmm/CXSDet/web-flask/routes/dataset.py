# MD5: 8b4ca80ab7f7bb11b5a9a7b332f6d926
"""
版权所有 © 2025羊羊小栈 （GJQ）

作者: 羊羊小栈
时间: 2025-09-06 23:43:10

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
"""

"""
数据集管理路由
"""
import os
import uuid
import tempfile
import json
import re
from datetime import datetime
from flask import Blueprint, request, send_file
from clients.file_client import file_client
from utils.response import success, error
from utils.db import db
from utils.jwt_util import admin_required
from utils.models import Dataset, DATASET_STATUS
from config import TEMP_DIR
from services.dataset_contract import (
    DatasetValidationError,
    inject_generated_data_yaml,
    validate_dataset_archive,
)
from services.feedback_dataset_service import (
    FeedbackValidationError,
    create_feedback_dataset_version,
)

dataset_bp = Blueprint('dataset', __name__, url_prefix='/api/dataset')

@dataset_bp.route('/upload', methods=['POST'])
@admin_required
def upload_dataset():
    """上传数据集"""
    try:
        # 检查请求中是否有文件
        if 'file' not in request.files:
            return error("没有选择文件")
            
        file = request.files['file']
        if file.filename == '':
            return error("没有选择文件")
            
        # 检查文件类型
        if not file.filename.lower().endswith('.zip'):
            return error("只支持上传zip格式的数据集")
            
        # 获取表单数据
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        dataset_mode = request.form.get('dataset_mode', '').strip().lower()
        secondary_modality = request.form.get('secondary_modality', '').strip().lower()
        class_names = [
            value.strip()
            for value in re.split(r'[,，;；\r\n]+', request.form.get('class_names', ''))
            if value.strip()
        ]
        
        if not name:
            return error("数据集名称不能为空")
        if dataset_mode not in ('single', 'multimodal'):
            return error("请选择有效的数据集类型：单模态或RGB+X多模态", 400)
            
        # 检查数据集名称是否已存在（所有状态的数据集名称都不能重复）
        existing_dataset = db.query("SELECT id FROM datasets WHERE name = ?", (name,), fetchone=True)
        if existing_dataset:
            return error("数据集名称已存在")
            
        # 保存临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as temp_file:
            file.save(temp_file.name)
            temp_file_path = temp_file.name
            
        try:
            try:
                validation = validate_dataset_archive(
                    temp_file_path,
                    dataset_mode,
                    secondary_modality=secondary_modality or None,
                    class_names=class_names or None,
                )
                inject_generated_data_yaml(temp_file_path, validation)
            except DatasetValidationError as exc:
                return error(f"数据集格式验证失败: {exc}", 400)
            structure_result = validation.to_dict()
            
            # 上传到文件服务
            upload_result = file_client.upload('datasets', temp_file_path)
            
            # 保存到数据库
            dataset_id = str(uuid.uuid4())
            file_size = os.path.getsize(temp_file_path)
            
            affected_rows = db.execute('''
                INSERT INTO datasets (id, name, description, zip_bucket, zip_object_key,
                                    structure_info, upload_time, file_size, status,
                                    dataset_mode, primary_modality, secondary_modality,
                                    runtime_mapping)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                dataset_id, name, description, upload_result['bucket'], upload_result['objectKey'],
                json.dumps(structure_result), datetime.now().isoformat(), file_size, 
                DATASET_STATUS['APPROVED'], dataset_mode, validation.primary_modality,
                validation.secondary_modality, json.dumps(validation.runtime_mapping, ensure_ascii=False)
            ))
            
            if affected_rows > 0:
                return success({
                    'id': dataset_id,
                    'name': name,
                    'description': description,
                    'structure_info': structure_result,
                    'dataset_mode': dataset_mode,
                    'primary_modality': validation.primary_modality,
                    'secondary_modality': validation.secondary_modality,
                    'file_size': file_size,
                    'upload_time': datetime.now().isoformat()
                }, "数据集上传成功")
            else:
                return error("保存数据集信息失败")
            
        finally:
            # 清理临时文件
            os.unlink(temp_file_path)
            
    except Exception as e:
        return error(f"上传数据集失败: {str(e)}")

@dataset_bp.route('/list', methods=['GET'])
@admin_required
def list_datasets():
    """获取数据集列表"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        search = request.args.get('search', '').strip()
        
        # 构建查询条件
        conditions = []
        params = []
        
        if search:
            conditions.append("(name LIKE ? OR description LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%'])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 获取总数
        count_sql = f"SELECT COUNT(*) as total FROM datasets{where_clause}"
        total_result = db.query(count_sql, tuple(params), fetchone=True)
        total = total_result['total'] if total_result else 0
        
        # 获取数据
        offset = (page - 1) * page_size
        data_sql = f'''
            SELECT *
            FROM datasets{where_clause}
            ORDER BY upload_time DESC
            LIMIT {page_size} OFFSET {offset}
        '''
        
        dataset_list = db.query(data_sql, tuple(params))
        
        datasets = []
        for row in dataset_list:
            dataset = Dataset(row)
            structure_info = json.loads(dataset.structure_info) if dataset.structure_info else {}
            datasets.append({
                'id': dataset.id,
                'name': dataset.name,
                'description': dataset.description,
                'structure_info': structure_info,
                'upload_time': dataset.upload_time,
                'file_size': dataset.file_size,
                'status': dataset.status
                ,'dataset_mode': row.get('dataset_mode', 'single')
                ,'primary_modality': row.get('primary_modality', 'rgb')
                ,'secondary_modality': row.get('secondary_modality')
            })
        
        return success({
            'datasets': datasets,
            'total': total,
            'page': page,
            'page_size': page_size
        })
        
    except Exception as e:
        return error(f"获取数据集列表失败: {str(e)}")

@dataset_bp.route('/<dataset_id>', methods=['GET'])
@admin_required
def get_dataset(dataset_id):
    """获取数据集详情"""
    try:
        dataset_data = db.query('''
            SELECT * FROM datasets WHERE id = ?
        ''', (dataset_id,), fetchone=True)
        
        if not dataset_data:
            return error("数据集不存在", 404)
            
        dataset = Dataset(dataset_data)
        structure_info = json.loads(dataset.structure_info) if dataset.structure_info else {}
        
        result = {
            'id': dataset.id,
            'name': dataset.name,
            'description': dataset.description,
            'zip_file_url': dataset.zip_file_url,
            'structure_info': structure_info,
            'upload_time': dataset.upload_time,
            'file_size': dataset.file_size,
            'status': dataset.status
            ,'dataset_mode': dataset_data.get('dataset_mode', 'single')
            ,'primary_modality': dataset_data.get('primary_modality', 'rgb')
            ,'secondary_modality': dataset_data.get('secondary_modality')
        }
        
        return success(result)
        
    except Exception as e:
        return error(f"获取数据集详情失败: {str(e)}")

@dataset_bp.route('/<dataset_id>', methods=['PUT'])
@admin_required
def update_dataset(dataset_id):
    """更新数据集信息"""
    try:
        data = request.json
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        
        if not name:
            return error("数据集名称不能为空")
            
        # 检查数据集是否存在
        existing_dataset = db.query("SELECT id FROM datasets WHERE id = ?", (dataset_id,), fetchone=True)
        if not existing_dataset:
            return error("数据集不存在", 404)
            
        # 检查名称是否与其他数据集重复
        duplicate_dataset = db.query("SELECT id FROM datasets WHERE name = ? AND id != ?", (name, dataset_id), fetchone=True)
        if duplicate_dataset:
            return error("数据集名称已存在")
            
        # 更新数据集信息
        affected_rows = db.execute('''
            UPDATE datasets SET name = ?, description = ?
            WHERE id = ?
        ''', (name, description, dataset_id))
        
        if affected_rows > 0:
            return success(None, "数据集信息更新成功")
        else:
            return error("更新失败")
        
    except Exception as e:
        return error(f"更新数据集信息失败: {str(e)}")

@dataset_bp.route('/<dataset_id>', methods=['DELETE'])
@admin_required
def delete_dataset(dataset_id):
    """删除数据集"""
    try:
        # 获取数据集信息
        dataset_data = db.query('''
            SELECT zip_bucket, zip_object_key FROM datasets 
            WHERE id = ?
        ''', (dataset_id,), fetchone=True)
        
        if not dataset_data:
            return error("数据集不存在", 404)
            
        bucket = dataset_data['zip_bucket']
        object_key = dataset_data['zip_object_key']
        
        # 删除文件服务中的文件
        try:
            file_client.delete(bucket, object_key)
        except Exception as e:
            print(f"删除文件服务中的文件失败: {e}")
            
        # 直接删除数据集记录
        affected_rows = db.execute('''
            DELETE FROM datasets WHERE id = ?
        ''', (dataset_id,))
        
        if affected_rows > 0:
            return success(None, "数据集删除成功")
        else:
            return error("删除失败")
        
    except Exception as e:
        return error(f"删除数据集失败: {str(e)}")

@dataset_bp.route('/<dataset_id>/download', methods=['GET'])
@admin_required
def download_dataset(dataset_id):
    """下载数据集"""
    try:
        dataset_data = db.query('''
            SELECT name, zip_bucket, zip_object_key FROM datasets 
            WHERE id = ?
        ''', (dataset_id,), fetchone=True)
        
        if not dataset_data:
            return error("数据集不存在", 404)
            
        name = dataset_data['name']
        bucket = dataset_data['zip_bucket']
        object_key = dataset_data['zip_object_key']
        
        # 从文件服务下载文件
        file_content = file_client.get(bucket, object_key)
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip', dir=TEMP_DIR) as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name
            
        # 返回文件下载
        return send_file(
            temp_file_path,
            as_attachment=True,
            download_name=f"{name}.zip",
            mimetype='application/zip'
        )
        
    except Exception as e:
        return error(f"下载数据集失败: {str(e)}")


@dataset_bp.route('/<dataset_id>/feedback-version', methods=['POST'])
@admin_required
def build_feedback_dataset_version(dataset_id):
    """把人工确认样本导出为新的、不可变且可直接训练的数据集ZIP。"""
    os.makedirs(TEMP_DIR, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='feedback_version_', dir=TEMP_DIR) as work_dir:
            result = create_feedback_dataset_version(
                dataset_id, work_dir, db, file_client
            )
        return success(result.to_dict(), "反馈数据集版本已生成")
    except FeedbackValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"生成反馈数据集版本失败: {str(exc)}")


