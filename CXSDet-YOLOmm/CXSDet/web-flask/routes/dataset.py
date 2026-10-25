
import os
import uuid
import tempfile
import json
import re
import multiprocessing
import shutil
import mimetypes
import math
from io import BytesIO
from pathlib import Path
from datetime import datetime
from flask import Blueprint, g, request, send_file
from werkzeug.utils import secure_filename
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
    create_feedback_dataset_draft,
)
from services.dataset_draft_service import (
    DraftValidationError,
    DRAFT_STATES,
    assign_draft_samples,
    get_draft,
    get_draft_sample,
    list_draft_samples,
    repair_draft_sample_secondary,
    reset_failed_draft_for_retry,
    mark_draft_failed,
)
from services.dataset_finalization_service import finalize_draft
from services.video_dataset_service import VideoDatasetConfig, generate_video_dataset

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
        try:
            page = int(request.args.get('page', 1))
            page_size = int(request.args.get('page_size', 10))
        except (TypeError, ValueError):
            return error("分页参数无效", 400)
        search = request.args.get('search', '').strip()
        record_kind = request.args.get('record_kind', 'formal').strip().lower()
        workflow_status = request.args.get('workflow_status', '').strip().lower()
        if record_kind not in {'formal', 'draft', 'all'}:
            return error("数据集记录类型无效", 400)
        if workflow_status and workflow_status not in DRAFT_STATES:
            return error("数据集工作流状态无效", 400)
        if page < 1 or page_size < 1 or page_size > 200:
            return error("分页参数无效", 400)

        records = []
        search_clause = "(name LIKE ? OR description LIKE ?)" if search else None
        search_params = [f'%{search}%', f'%{search}%'] if search else []

        if record_kind in {'formal', 'all'} and not workflow_status:
            conditions = [search_clause] if search_clause else []
            params = list(search_params)
            where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
            formal_rows = db.query(
                f"SELECT * FROM datasets{where_clause} ORDER BY upload_time DESC",
                tuple(params),
            )
            for row in formal_rows:
                dataset = Dataset(row)
                try:
                    structure_info = json.loads(dataset.structure_info) if dataset.structure_info else {}
                except (TypeError, json.JSONDecodeError):
                    structure_info = {}
                records.append({
                    'id': dataset.id,
                    'name': dataset.name,
                    'description': dataset.description,
                    'structure_info': structure_info,
                    'upload_time': dataset.upload_time,
                    'file_size': dataset.file_size,
                    'status': dataset.status,
                    'dataset_mode': row.get('dataset_mode', 'single'),
                    'primary_modality': row.get('primary_modality', 'rgb'),
                    'secondary_modality': row.get('secondary_modality'),
                    'record_kind': 'formal',
                    'workflow_status': None,
                })

        if record_kind in {'draft', 'all'}:
            conditions = ["workflow_status != 'finalized'"]
            params = []
            if workflow_status:
                conditions.append("workflow_status = ?")
                params.append(workflow_status)
            if search_clause:
                conditions.append(search_clause)
                params.extend(search_params)
            where_clause = " AND ".join(conditions)
            draft_rows = db.query(f'''
                SELECT d.*, COUNT(s.id) AS sample_count,
                       SUM(CASE WHEN s.split = 'unassigned' THEN 1 ELSE 0 END) AS unassigned_count,
                       SUM(CASE WHEN s.split = 'train' THEN 1 ELSE 0 END) AS train_count,
                       SUM(CASE WHEN s.split = 'val' THEN 1 ELSE 0 END) AS val_count,
                       SUM(CASE WHEN s.split = 'test' THEN 1 ELSE 0 END) AS test_count,
                       SUM(CASE WHEN s.split = 'excluded' THEN 1 ELSE 0 END) AS excluded_count
                FROM dataset_drafts d
                LEFT JOIN dataset_draft_samples s ON s.draft_id = d.id
                WHERE {where_clause}
                GROUP BY d.id ORDER BY d.create_time DESC
            ''', tuple(params))
            for row in draft_rows:
                try:
                    metadata = json.loads(row.get('metadata_json') or '{}')
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                records.append({
                    'id': row['id'],
                    'name': row['name'],
                    'description': row.get('description'),
                    'upload_time': row.get('create_time'),
                    'file_size': None,
                    'status': None,
                    'structure_info': {},
                    'dataset_mode': row.get('dataset_mode', 'single'),
                    'primary_modality': row.get('primary_modality', 'rgb'),
                    'secondary_modality': row.get('secondary_modality'),
                    'record_kind': 'draft',
                    'workflow_status': row.get('workflow_status'),
                    'error_message': row.get('error_message'),
                    'metadata': {key: value for key, value in metadata.items() if key != 'secondary_video_ref'},
                    'sample_count': row.get('sample_count') or 0,
                    'split_counts': {
                        'unassigned': row.get('unassigned_count') or 0,
                        'train': row.get('train_count') or 0,
                        'val': row.get('val_count') or 0,
                        'test': row.get('test_count') or 0,
                        'excluded': row.get('excluded_count') or 0,
                    },
                })

        records.sort(key=lambda item: str(item.get('upload_time') or ''), reverse=True)
        total = len(records)
        offset = (page - 1) * page_size
        records = records[offset:offset + page_size]

        return success({
            'datasets': records,
            'total': total,
            'page': page,
            'page_size': page_size,
            'record_kind': record_kind,
            'workflow_status': workflow_status or None,
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


@dataset_bp.route('/drafts/<draft_id>/samples', methods=['GET'])
@admin_required
def get_dataset_draft_samples(draft_id):
    try:
        draft = get_draft(draft_id, db)
        if not draft:
            return error("待划分数据集不存在", 404)
        result = list_draft_samples(
            draft_id,
            db,
            page=request.args.get('page', 1),
            page_size=request.args.get('page_size', 50),
            split=request.args.get('split') or None,
            pairing_state=request.args.get('pairing_state') or None,
        )
        safe_samples = []
        for sample in result['samples']:
            safe = dict(sample)
            safe['rgb_preview_url'] = f"/api/dataset/drafts/{draft_id}/samples/{sample['id']}/preview/rgb"
            safe['secondary_preview_url'] = (
                f"/api/dataset/drafts/{draft_id}/samples/{sample['id']}/preview/secondary"
                if sample.get('secondary_image_object_key') else None
            )
            safe['label_preview_url'] = f"/api/dataset/drafts/{draft_id}/samples/{sample['id']}/preview/label"
            for key in (
                'rgb_bucket', 'rgb_object_key', 'secondary_image_bucket', 'secondary_image_object_key',
                'label_bucket', 'label_object_key',
            ):
                safe.pop(key, None)
            safe_samples.append(safe)
        result['samples'] = safe_samples
        draft_metadata = dict(draft.get('metadata') or {})
        draft_metadata.pop('secondary_video_ref', None)
        result['draft'] = {**draft, 'metadata': draft_metadata}
        return success(result)
    except (DraftValidationError, ValueError) as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"获取待划分数据集样本失败: {str(exc)}")


@dataset_bp.route('/drafts/<draft_id>/samples/<sample_id>/assign', methods=['POST'])
@admin_required
def assign_dataset_draft_sample(draft_id, sample_id):
    data = request.get_json(silent=True) or {}
    split = data.get('split')
    try:
        result = assign_draft_samples(draft_id, [sample_id], split, db)
        return success(result, "样本划分已更新")
    except DraftValidationError as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"更新样本划分失败: {str(exc)}")


@dataset_bp.route('/drafts/<draft_id>/assign', methods=['POST'])
@admin_required
def assign_dataset_draft_samples(draft_id):
    data = request.get_json(silent=True) or {}
    try:
        result = assign_draft_samples(draft_id, data.get('sample_ids'), data.get('split'), db)
        return success(result, "样本划分已更新")
    except DraftValidationError as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"更新样本划分失败: {str(exc)}")


@dataset_bp.route('/drafts/<draft_id>/samples/<sample_id>/preview/<kind>', methods=['GET'])
@admin_required
def preview_dataset_draft_sample(draft_id, sample_id, kind):
    if kind not in {'rgb', 'secondary', 'label'}:
        return error("预览类型无效", 400)
    try:
        sample = get_draft_sample(draft_id, sample_id, db)
        if not sample:
            return error("样本不存在", 404)
        fields = {
            'rgb': (sample.rgb_bucket, sample.rgb_object_key),
            'secondary': (sample.secondary_image_bucket, sample.secondary_image_object_key),
            'label': (sample.label_bucket, sample.label_object_key),
        }
        bucket, object_key = fields[kind]
        if not bucket or not object_key:
            return error("样本文件不存在", 404)
        content = file_client.get(bucket, object_key)
        filename = Path(str(object_key)).name
        safe_name = secure_filename(filename) or f"{secure_filename(sample.sample_key) or 'sample'}.bin"
        mime_type = 'text/plain; charset=utf-8' if kind == 'label' else (
            mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
        )
        return send_file(
            BytesIO(content),
            mimetype=mime_type,
            download_name=safe_name,
            as_attachment=False,
            max_age=0,
        )
    except Exception as exc:
        return error(f"读取样本预览失败: {str(exc)}")


@dataset_bp.route('/drafts/<draft_id>/samples/<sample_id>/secondary', methods=['POST'])
@admin_required
def upload_dataset_draft_secondary(draft_id, sample_id):
    image_file = request.files.get('file')
    if not image_file or not image_file.filename:
        return error("请选择第二模态图片", 400)
    extension = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
    if extension not in {'png', 'jpg', 'jpeg', 'bmp', 'tif', 'tiff', 'webp'}:
        return error("第二模态图片格式不受支持", 400)
    frame_index = request.form.get('frame_index')
    timestamp = request.form.get('timestamp')
    try:
        frame_index = int(frame_index) if frame_index not in (None, '') else None
        timestamp = float(timestamp) if timestamp not in (None, '') else None
        if (frame_index is not None and frame_index < 0) or (
            timestamp is not None and (timestamp < 0 or not math.isfinite(timestamp))
        ):
            raise ValueError("时间戳必须为有效非负数字")
    except ValueError as exc:
        return error(str(exc) or "帧号或时间戳格式无效", 400)

    old_sample = get_draft_sample(draft_id, sample_id, db)
    if not old_sample:
        return error("样本不存在或不属于该数据集", 404)
    os.makedirs(TEMP_DIR, exist_ok=True)
    uploaded = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{extension}', dir=TEMP_DIR) as temporary:
            image_file.save(temporary.name)
            temp_path = temporary.name
        try:
            uploaded = file_client.upload('dataset_images', temp_path)
        finally:
            os.unlink(temp_path)
        result = repair_draft_sample_secondary(
            draft_id, sample_id, uploaded, db, frame_index=frame_index, timestamp=timestamp
        )
        if old_sample.secondary_image_bucket and old_sample.secondary_image_object_key:
            try:
                file_client.delete(old_sample.secondary_image_bucket, old_sample.secondary_image_object_key)
            except Exception:
                pass
        return success(result, "第二模态图片已补充")
    except DraftValidationError as exc:
        if uploaded:
            try:
                file_client.delete(uploaded['bucket'], uploaded['objectKey'])
            except Exception:
                pass
        return error(str(exc), 400)
    except Exception as exc:
        if uploaded:
            try:
                file_client.delete(uploaded['bucket'], uploaded['objectKey'])
            except Exception:
                pass
        return error(f"补充第二模态图片失败: {str(exc)}")


@dataset_bp.route('/drafts/<draft_id>/finalize', methods=['POST'])
@admin_required
def finalize_dataset_draft(draft_id):
    os.makedirs(TEMP_DIR, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=f'draft_finalize_{secure_filename(draft_id)}_', dir=TEMP_DIR) as work_dir:
            result = finalize_draft(draft_id, work_dir, db, file_client)
        return success(result.to_dict(), "数据集已标记为可训练")
    except DraftValidationError as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"定稿待划分数据集失败: {str(exc)}")


def _retry_video_draft_worker(draft_id):
    """Regenerate a failed video draft from its persisted source objects."""
    from utils.db import Database

    process_db = Database()
    work_dir = tempfile.mkdtemp(prefix=f'video_draft_retry_{draft_id}_', dir=TEMP_DIR)
    try:
        draft = get_draft(draft_id, process_db)
        metadata = draft['metadata']
        record_id = draft['video_detection_record_id']
        source = process_db.query(
            "SELECT original_video_bucket, original_video_object_key FROM video_detection_records WHERE id = ?",
            (record_id,), fetchone=True,
        )
        if not source:
            raise DraftValidationError("原视频记录不存在")
        model_id = metadata.get('annotation_model_id')
        model_meta = process_db.query('''
            SELECT id, name, model_bucket, model_object_key, model_mode,
                   primary_modality, secondary_modality, model_yaml
            FROM models WHERE id = ? AND status = 3
        ''', (model_id,), fetchone=True)
        if not model_meta:
            raise DraftValidationError("原标注模型已不存在或未发布，无法重试")
        primary_path = os.path.join(work_dir, 'primary.mp4')
        with open(primary_path, 'wb') as primary_file:
            primary_file.write(file_client.get(source['original_video_bucket'], source['original_video_object_key']))
        secondary_path = None
        secondary_ref = metadata.get('secondary_video_ref')
        if draft['dataset_mode'] == 'multimodal':
            if not secondary_ref or not secondary_ref.get('bucket') or not secondary_ref.get('objectKey'):
                raise DraftValidationError("原第二路视频未保存，无法重试")
            secondary_path = os.path.join(work_dir, 'secondary.mp4')
            with open(secondary_path, 'wb') as secondary_file:
                secondary_file.write(file_client.get(secondary_ref['bucket'], secondary_ref['objectKey']))
        config = VideoDatasetConfig(
            enabled=True,
            name=draft['name'],
            description=draft['description'] or '',
            mode=draft['dataset_mode'],
            frame_interval=int(metadata.get('frame_interval', 1)),
            annotation_model_id=model_id,
            confidence=float(metadata.get('annotation_confidence', 0.5)),
            secondary_modality=draft['secondary_modality'],
            secondary_time_offset=float(metadata.get('secondary_time_offset', 0.0)),
        )
        stats = generate_video_dataset(
            draft_id, config,
            {'primary_video': primary_path, 'secondary_video': secondary_path,
             'work_dir': work_dir, 'model_meta': model_meta},
            process_db, file_client,
        )
        process_db.execute(
            "UPDATE video_detection_records SET dataset_generation_status = ?, dataset_statistics = ? WHERE id = ?",
            ('completed', json.dumps(stats, ensure_ascii=False), record_id),
        )
    except Exception as exc:
        try:
            mark_draft_failed(draft_id, str(exc), process_db)
            process_db.execute(
                "UPDATE video_detection_records SET dataset_generation_status = ?, dataset_statistics = ? WHERE id = ?",
                ('failed', json.dumps({'error': str(exc)}, ensure_ascii=False), record_id),
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@dataset_bp.route('/drafts/<draft_id>/retry', methods=['POST'])
@admin_required
def retry_dataset_draft(draft_id):
    draft = get_draft(draft_id, db)
    if not draft:
        return error("待划分数据集不存在", 404)
    if draft['workflow_status'] != 'failed':
        return error("只有生成失败的数据集可以重试", 400)
    try:
        metadata = draft.get('metadata') or {}
        record_id = draft.get('video_detection_record_id')
        source = db.query(
            "SELECT original_video_bucket, original_video_object_key FROM video_detection_records WHERE id = ?",
            (record_id,), fetchone=True,
        )
        if not source:
            return error("原视频记录不存在，无法重试", 400)
        if draft['dataset_mode'] == 'multimodal' and not metadata.get('secondary_video_ref'):
            return error("原第二路视频未保存，无法重试", 400)
        model_meta = db.query(
            "SELECT id, model_mode, secondary_modality FROM models WHERE id = ? AND status = 3",
            (metadata.get('annotation_model_id'),), fetchone=True,
        )
        if not model_meta or model_meta.get('model_mode', 'single') != draft['dataset_mode']:
            return error("原标注模型不可用，无法重试", 400)
        if draft['dataset_mode'] == 'multimodal' and (
            (model_meta.get('secondary_modality') or '').lower()
            != (draft.get('secondary_modality') or '').lower()
        ):
            return error("原标注模型的第二模态配置已变化，无法重试", 400)
        reset_failed_draft_for_retry(draft_id, db, file_client)
        db.execute(
            "UPDATE video_detection_records SET dataset_generation_status = ?, dataset_statistics = NULL WHERE id = ?",
            ('generating', record_id),
        )
        os.makedirs(TEMP_DIR, exist_ok=True)
        process = multiprocessing.Process(target=_retry_video_draft_worker, args=(draft_id,))
        process.daemon = True
        try:
            process.start()
        except Exception as start_error:
            mark_draft_failed(draft_id, str(start_error), db)
            db.execute(
                "UPDATE video_detection_records SET dataset_generation_status = ? WHERE id = ?",
                ('failed', record_id),
            )
            raise
        return success({'id': draft_id, 'workflow_status': 'generating'}, "已重新开始生成待划分数据集")
    except DraftValidationError as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"重试待划分数据集失败: {str(exc)}")


@dataset_bp.route('/<dataset_id>/feedback-version', methods=['POST'])
@admin_required
def build_feedback_dataset_version(dataset_id):
    """把人工确认样本保存为等待人工划分的数据集草稿。"""
    os.makedirs(TEMP_DIR, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='feedback_draft_', dir=TEMP_DIR) as work_dir:
            result = create_feedback_dataset_draft(
                dataset_id, work_dir, g.user_id, db, file_client
            )
        return success(result.to_dict(), "反馈样本已生成待划分数据集")
    except FeedbackValidationError as exc:
        return error(str(exc), 400), 400
    except DraftValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"生成反馈待划分数据集失败: {str(exc)}")


