
import os
import uuid
import json
import cv2
import numpy as np
from datetime import datetime
from PIL import Image
from flask import Blueprint, request, g
from clients.file_client import file_client
from utils.response import success, error
from utils.db import db
from utils.jwt_util import admin_required, token_required
from utils.models import DetectionRecord, DetectionRecordVO, DETECTION_STATUS
from utils.security_utils import is_admin
from config import TEMP_DIR, YOLO_CLASSES_CONFIG
from services.detection_service import execute_detection
from services.yolo_runtime import MissingModalityError
from services.ai_review_service import AIReviewError, check_vl_health, review_detection
from services.review_service import (
    ReviewConflictError,
    ReviewValidationError,
    submit_human_review,
)
from services.feedback_dataset_service import (
    FeedbackValidationError,
    add_confirmed_record,
)
from services.batch_review_service import (
    BatchValidationError,
    add_batch_confirmed_records,
    run_batch_ai_reviews,
)
from algo.llm.llm_vl_client import LLMVLClient

detection_bp = Blueprint('detection', __name__, url_prefix='/api/detection')

def hex_to_bgr(hex_color):
    """将十六进制颜色转换为OpenCV的BGR格式"""
    try:
        # 去掉#号
        hex_color = hex_color.lstrip('#')
        # 转换为RGB
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        # OpenCV使用BGR格式
        return (b, g, r)
    except:
        # 如果转换失败，返回默认颜色（绿色）
        return (0, 255, 0)

def get_class_color(class_name):
    """获取类别对应的BGR颜色"""
    if class_name in YOLO_CLASSES_CONFIG:
        hex_color = YOLO_CLASSES_CONFIG[class_name]['color']
        return hex_to_bgr(hex_color)
    else:
        # 默认颜色（绿色）
        return (0, 255, 0)

@detection_bp.route('/models/published', methods=['GET'])
@token_required
def get_published_models():
    """获取已发布的模型列表"""
    try:
        models = db.query('''
            SELECT id, name, description, model_mode, primary_modality,
                   secondary_modality, version
            FROM models
            WHERE status = 3 
            ORDER BY create_time DESC
        ''')
        
        return success({
            'models': models
        })
        
    except Exception as e:
        return error(f"获取模型列表失败: {str(e)}")

@detection_bp.route('/detect', methods=['POST'])
@token_required
def detect_image():
    """图片识别接口"""
    return _detect_image_v2()


def _detect_image_v2():
    """统一单/多模态推理，并在成功响应前自动保存识别记录。"""
    temp_dir = None
    try:
        model_id = request.form.get('model_id')
        confidence_threshold = float(request.form.get('confidence_threshold', 0.5))
        if not model_id:
            return error("请选择识别模型", 400)
        if not 0 <= confidence_threshold <= 1:
            return error("置信度阈值必须在0到1之间", 400)
        if 'image' not in request.files or not request.files['image'].filename:
            return error("请上传图片", 400)

        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
        image_file = request.files['image']
        extension = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
        if extension not in allowed_extensions:
            return error("请上传支持的图片格式（PNG、JPG、JPEG、GIF、BMP、WEBP）", 400)

        model_data = db.query('''
            SELECT id, name, model_bucket, model_object_key, model_mode,
                   primary_modality, secondary_modality, model_yaml
            FROM models WHERE id = ? AND status = 3
        ''', (model_id,), fetchone=True)
        if not model_data:
            return error("选择的模型不存在或未发布", 404)

        temp_dir = os.path.join(TEMP_DIR, f"detection_{uuid.uuid4().hex[:8]}")
        os.makedirs(temp_dir, exist_ok=True)
        rgb_path = os.path.join(temp_dir, 'rgb.jpg')
        with Image.open(image_file.stream) as image:
            image.convert('RGB').save(rgb_path, 'JPEG', quality=95)

        x_path = None
        x_file = request.files.get('x_image')
        if x_file and x_file.filename:
            x_extension = x_file.filename.rsplit('.', 1)[-1].lower() if '.' in x_file.filename else ''
            if x_extension not in allowed_extensions:
                return error("X模态图片格式不受支持", 400)
            x_path = os.path.join(temp_dir, 'x.jpg')
            with Image.open(x_file.stream) as image:
                image.convert('RGB').save(x_path, 'JPEG', quality=95)

        result = execute_detection(
            user_id=g.user_id,
            model_meta=model_data,
            rgb_path=rgb_path,
            x_path=x_path,
            confidence=confidence_threshold,
            work_dir=os.path.join(temp_dir, 'runtime'),
            storage_client=file_client,
            database=db,
            request_key=request.form.get('request_key') or None,
        )
        original = result.storage['original']
        secondary = result.storage['secondary']
        result_file = result.storage['result']
        return success({
            'record_id': result.record_id,
            'record_saved': True,
            'original_image_url': result.original_image_url,
            'secondary_image_url': result.secondary_image_url,
            'secondary_image_origin': result.secondary_image_origin,
            'result_image_url': result.result_image_url,
            'detections': result.detections,
            'model_id': result.model_id,
            'model_name': result.model_name,
            'model_mode': model_data.get('model_mode') or 'single',
            'secondary_modality': model_data.get('secondary_modality'),
            'confidence_threshold': result.confidence_threshold,
            'detection_count': result.detection_count,
            'save_data': {
                'record_id': result.record_id,
                'model_id': result.model_id,
                'model_name': result.model_name,
                'original_image_bucket': original['bucket'],
                'original_image_object_key': original['objectKey'],
                'secondary_image_bucket': secondary['bucket'] if secondary else None,
                'secondary_image_object_key': secondary['objectKey'] if secondary else None,
                'secondary_image_origin': result.secondary_image_origin,
                'result_image_bucket': result_file['bucket'],
                'result_image_object_key': result_file['objectKey'],
                'detection_result': json.dumps(result.detections, ensure_ascii=False),
                'confidence_threshold': result.confidence_threshold
            },
            'enhancement_data': {
                'result_image_bucket': result_file['bucket'],
                'result_image_object_key': result_file['objectKey'],
                'detections': result.detections
            }
        }, "图片识别完成，记录已自动保存")
    except MissingModalityError as exc:
        return error(str(exc), 400)
    except Exception as exc:
        return error(f"图片识别失败: {exc}")
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    # 下面保留旧实现作为不可达兼容参考，实际请求均由_v2处理。
    try:
        # 获取请求参数
        model_id = request.form.get('model_id')
        confidence_threshold = float(request.form.get('confidence_threshold', 0.5))
        
        if not model_id:
            return error("请选择识别模型")
        
        # 检查是否上传了图片
        if 'image' not in request.files:
            return error("请上传图片")
        
        image_file = request.files['image']
        if image_file.filename == '':
            return error("请选择图片文件")
        
        # 验证文件类型
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
        if not ('.' in image_file.filename and 
                image_file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
            return error("请上传支持的图片格式（PNG、JPG、JPEG、GIF、BMP、WEBP）")
        
        # 检查模型是否存在且已发布
        model_data = db.query('''
            SELECT id, name, model_bucket, model_object_key FROM models 
            WHERE id = ? AND status = 3
        ''', (model_id,), fetchone=True)
        
        if not model_data:
            return error("选择的模型不存在或未发布")
        
        # 创建临时目录
        temp_id = uuid.uuid4().hex[:8]
        temp_dir = os.path.join(TEMP_DIR, f"detection_{temp_id}")
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # 保存上传的图片
            image_filename = f"input_{uuid.uuid4().hex[:8]}.jpg"
            temp_image_path = os.path.join(temp_dir, image_filename)
            
            # 转换并保存为JPG格式
            with Image.open(image_file.stream) as img:
                # 如果是RGBA模式，转换为RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = rgb_img
                img.save(temp_image_path, 'JPEG', quality=95)
            
            # 上传原图到文件服务
            original_upload_result = file_client.upload('detection_original', temp_image_path)
            
            # 下载模型文件
            if not model_data['model_bucket'] or not model_data['model_object_key']:
                return error("模型文件不存在")
                
            model_content = file_client.get(model_data['model_bucket'], model_data['model_object_key'])
            model_path = os.path.join(temp_dir, 'model.pt')
            with open(model_path, 'wb') as f:
                f.write(model_content)
            
            # 执行图片识别
            from ultralytics import YOLO
            
            model = YOLO(model_path)
            results = model.predict(
                source=temp_image_path,
                conf=confidence_threshold,
                save=False,  # 不自动保存，我们手动绘制
                project=temp_dir,
                name='results',
                exist_ok=True
            )
            
            # 获取识别结果
            detection_result = []
            result_image_path = None
            
            if results and len(results) > 0:
                result = results[0]
                
                # 读取原始图片
                image = cv2.imread(temp_image_path)
                result_image = image.copy()
                
                # 处理检测结果
                if result.boxes is not None and len(result.boxes) > 0:
                    for box in result.boxes:
                        # 获取边界框坐标
                        x1, y1, x2, y2 = box.xyxy[0]
                        confidence = box.conf[0]
                        class_id = int(box.cls[0])
                        class_name = model.names[class_id]
                        
                        # 获取类别对应的颜色
                        color = get_class_color(class_name)
                        
                        # 绘制检测框
                        cv2.rectangle(result_image, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                        
                        # 构建显示文本
                        text = f'{class_name} {confidence:.2f}'
                        
                        # 在检测框上方显示文本
                        cv2.putText(result_image, text, 
                                  (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                        
                        # 添加到检测结果
                        detection_result.append({
                            'bbox': box.xyxy[0].tolist() if hasattr(box.xyxy, 'tolist') else box.xyxy[0],
                            'confidence': float(box.conf[0]) if hasattr(box.conf, '__getitem__') else float(box.conf),
                            'class': int(box.cls[0]) if hasattr(box.cls, '__getitem__') else int(box.cls),
                            'class_name': model.names[int(box.cls[0])] if hasattr(box.cls, '__getitem__') else model.names[int(box.cls)]
                        })
                
                # 保存结果图片
                result_dir = os.path.join(temp_dir, 'results')
                os.makedirs(result_dir, exist_ok=True)
                result_image_path = os.path.join(result_dir, 'result.jpg')
                cv2.imwrite(result_image_path, result_image)
            
            # 上传结果图片（如果存在）
            result_upload_result = None
            if result_image_path and os.path.exists(result_image_path):
                result_upload_result = file_client.upload('detection_results', result_image_path)
            
            # 构建返回结果
            result_data = {
                'original_image_url': file_client.get_file_url(
                    original_upload_result['bucket'], 
                    original_upload_result['objectKey']
                ),
                'result_image_url': file_client.get_file_url(
                    result_upload_result['bucket'], 
                    result_upload_result['objectKey']
                ) if result_upload_result else None,
                'detections': detection_result,
                'model_id': model_id,
                'model_name': model_data['name'],
                'confidence_threshold': confidence_threshold,
                'detection_count': len(detection_result),
                # 保存记录需要的数据
                'save_data': {
                    'model_id': model_id,
                    'model_name': model_data['name'],
                    'original_image_bucket': original_upload_result['bucket'],
                    'original_image_object_key': original_upload_result['objectKey'],
                    'result_image_bucket': result_upload_result['bucket'] if result_upload_result else None,
                    'result_image_object_key': result_upload_result['objectKey'] if result_upload_result else None,
                    'detection_result': json.dumps(detection_result),
                    'confidence_threshold': confidence_threshold
                },
                # 用于图像增强和分析的数据
                'enhancement_data': {
                    'result_image_bucket': result_upload_result['bucket'] if result_upload_result else None,
                    'result_image_object_key': result_upload_result['objectKey'] if result_upload_result else None,
                    'detections': detection_result
                }
            }
            
            return success(result_data, "图片识别完成")
            
        finally:
            # 清理临时目录
            import shutil
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
        
    except Exception as e:
        return error(f"图片识别失败: {str(e)}")

@detection_bp.route('/save', methods=['POST'])
@token_required
def save_detection():
    """保存识别结果"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        save_data = data.get('save_data', {})
        
        if not title:
            return error("请输入标题")
        
        if not save_data:
            return error("保存数据不能为空")

        existing_record_id = save_data.get('record_id')
        if existing_record_id:
            existing = db.query(
                "SELECT id, user_id FROM detection_records WHERE id = ?",
                (existing_record_id,),
                fetchone=True
            )
            if not existing or (existing.get('user_id') != g.user_id and not is_admin()):
                return error("识别记录不存在或无权访问", 404)
            affected_rows = db.execute('''
                UPDATE detection_records
                SET title = ?, description = ?,
                    enhanced_image_bucket = COALESCE(?, enhanced_image_bucket),
                    enhanced_image_object_key = COALESCE(?, enhanced_image_object_key),
                    analysis_result = COALESCE(?, analysis_result),
                    suggestions = COALESCE(?, suggestions)
                WHERE id = ?
            ''', (
                title, description,
                save_data.get('enhanced_image_bucket'),
                save_data.get('enhanced_image_object_key'),
                save_data.get('analysis_result'),
                save_data.get('suggestions'),
                existing_record_id
            ))
            if affected_rows > 0:
                return success({'id': existing_record_id}, "识别记录已更新")
            return error("更新识别记录失败")
        
        # 创建识别记录
        record_id = str(uuid.uuid4())
        
        affected_rows = db.execute('''
            INSERT INTO detection_records (
                id, user_id, model_id, model_name, 
                original_image_bucket, original_image_object_key,
                result_image_bucket, result_image_object_key,
                detection_result, confidence_threshold,
                create_time, title, description,
                enhanced_image_bucket, enhanced_image_object_key,
                analysis_result, suggestions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            record_id, g.user_id, save_data.get('model_id'), save_data.get('model_name'),
            save_data.get('original_image_bucket'), save_data.get('original_image_object_key'),
            save_data.get('result_image_bucket'), save_data.get('result_image_object_key'),
            save_data.get('detection_result'), save_data.get('confidence_threshold'),
            datetime.now().isoformat(), title, description,
            save_data.get('enhanced_image_bucket'), save_data.get('enhanced_image_object_key'),
            save_data.get('analysis_result'), save_data.get('suggestions')
        ))
        
        if affected_rows > 0:
            return success({'id': record_id}, "识别结果保存成功")
        else:
            return error("保存失败")
        
    except Exception as e:
        return error(f"保存识别结果失败: {str(e)}")

@detection_bp.route('/records', methods=['GET'])
@token_required
def get_detection_records():
    """获取图片识别记录列表"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        search = request.args.get('search', '').strip()
        
        # 构建查询条件
        conditions = []
        params = []
        
        # 普通用户只能看到自己的记录，管理员可以看到所有记录
        if not is_admin():
            conditions.append("dr.user_id = ?")
            params.append(g.user_id)
        
        if search:
            conditions.append("(dr.title LIKE ? OR dr.description LIKE ? OR dr.model_name LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 获取总数
        count_sql = f"SELECT COUNT(*) as total FROM detection_records dr{where_clause}"
        total_result = db.query(count_sql, tuple(params), fetchone=True)
        total = total_result['total'] if total_result else 0
        
        # 获取数据
        offset = (page - 1) * page_size
        data_sql = f'''
            SELECT dr.*, u.real_name as user_name,
                   m.dataset_id as source_dataset_id,
                   d.name as source_dataset_name
            FROM detection_records dr
            LEFT JOIN user u ON dr.user_id = u.id
            LEFT JOIN models m ON dr.model_id = m.id
            LEFT JOIN datasets d ON m.dataset_id = d.id
            {where_clause}
            ORDER BY dr.create_time DESC
            LIMIT {page_size} OFFSET {offset}
        '''
        
        record_list = db.query(data_sql, tuple(params))
        
        records = []
        for row in record_list:
            record = DetectionRecord(row)
            # 管理员可以看到用户真实姓名
            user_name = row.get('user_name') if is_admin() else None
            record_vo = DetectionRecordVO(record, user_name)
            records.append(record_vo.to_dict())
        
        return success({
            'records': records,
            'total': total,
            'page': page,
            'page_size': page_size
        })
        
    except Exception as e:
        return error(f"获取识别记录失败: {str(e)}")


@detection_bp.route('/records/<record_id>/ai-review', methods=['POST'])
@token_required
def review_detection_record(record_id):
    """对已保存的YOLO记录运行结构化视觉大模型复核。"""
    try:
        record = db.query(
            "SELECT id, user_id FROM detection_records WHERE id = ?",
            (record_id,),
            fetchone=True
        )
        if not record or (record.get('user_id') != g.user_id and not is_admin()):
            return error("识别记录不存在或无权访问", 404)
        review = review_detection(record_id, LLMVLClient(), db, file_client)
        return success(review.to_dict(), "AI辅助复核完成，请人工确认")
    except AIReviewError as exc:
        return error(exc.public_message, 502), 502
    except Exception as exc:
        return error(f"AI辅助复核失败: {type(exc).__name__}")


@detection_bp.route('/records/ai-review/batch', methods=['POST'])
@token_required
def batch_review_detection_records():
    """逐条运行 AI 辅助复核，并保留每条记录的成功或失败结果。"""
    try:
        payload = request.get_json(silent=True) or {}
        result = run_batch_ai_reviews(
            payload.get('record_ids'),
            user_id=g.user_id,
            is_admin_user=is_admin(),
            database=db,
            storage_client=file_client,
            review_runner=review_detection,
            client_factory=LLMVLClient,
        )
        return success(result, "批量AI辅助复核已完成，请逐条人工确认")
    except BatchValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"批量AI辅助复核失败: {type(exc).__name__}")


@detection_bp.route('/llm/health', methods=['GET'])
@admin_required
def llm_health():
    """验证当前用户配置的大模型和模型可见性，不返回凭据。"""
    try:
        return success(check_vl_health(LLMVLClient()))
    except Exception as exc:
        return error(f"大模型连接检查失败: {type(exc).__name__}")


def _record_is_accessible(record_id):
    if is_admin():
        return True
    record = db.query(
        "SELECT id FROM detection_records WHERE id = ? AND user_id = ?",
        (record_id, g.user_id),
        fetchone=True,
    )
    return bool(record)


@detection_bp.route('/records/<record_id>/human-review', methods=['POST'])
@token_required
def human_review_detection_record(record_id):
    """保存归一化人工标注，并以乐观锁保护审计链。"""
    try:
        if not _record_is_accessible(record_id):
            return error("识别记录不存在或无权访问", 404), 404
        payload = request.get_json(silent=True) or {}
        result = submit_human_review(
            record_id,
            g.user_id,
            payload.get('expected_version'),
            payload.get('annotations'),
            payload.get('decision'),
            db,
        )
        return success(result.to_dict(), "人工复核已保存")
    except ReviewConflictError as exc:
        return error(str(exc), 409), 409
    except ReviewValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"保存人工复核失败: {type(exc).__name__}")


@detection_bp.route('/records/<record_id>/feedback', methods=['POST'])
@token_required
def add_detection_record_to_feedback(record_id):
    """将已人工确认的记录幂等加入指定反馈数据集。"""
    try:
        if not _record_is_accessible(record_id):
            return error("识别记录不存在或无权访问", 404), 404
        payload = request.get_json(silent=True) or {}
        dataset_id = str(payload.get('dataset_id') or '').strip()
        if not dataset_id:
            return error("dataset_id 不能为空", 400), 400
        result = add_confirmed_record(record_id, dataset_id, db)
        return success(result.to_dict(), "已加入反馈数据集")
    except FeedbackValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"加入反馈数据集失败: {type(exc).__name__}")


@detection_bp.route('/records/feedback/batch', methods=['POST'])
@token_required
def add_detection_records_to_feedback():
    """按每条记录所属模型自动回流到原训练数据集的反馈样本池。"""
    try:
        payload = request.get_json(silent=True) or {}
        result = add_batch_confirmed_records(
            payload.get('record_ids'),
            user_id=g.user_id,
            is_admin_user=is_admin(),
            database=db,
        )
        return success(result, "批量反馈处理完成")
    except BatchValidationError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"批量加入反馈数据集失败: {type(exc).__name__}")

@detection_bp.route('/records/<record_id>', methods=['GET'])
@token_required
def get_detection_record(record_id):
    """获取图片识别记录详情"""
    try:
        # 构建查询条件
        conditions = ["dr.id = ?"]
        params = [record_id]
        
        # 普通用户只能看到自己的记录
        if not is_admin():
            conditions.append("dr.user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        record_data = db.query(f'''
            SELECT dr.*, u.real_name as user_name,
                   m.dataset_id as source_dataset_id,
                   d.name as source_dataset_name
            FROM detection_records dr
            LEFT JOIN user u ON dr.user_id = u.id
            LEFT JOIN models m ON dr.model_id = m.id
            LEFT JOIN datasets d ON m.dataset_id = d.id
            {where_clause}
        ''', tuple(params), fetchone=True)
        
        if not record_data:
            return error("识别记录不存在或无权访问", 404)
        
        record = DetectionRecord(record_data)
        # 管理员可以看到用户真实姓名
        user_name = record_data.get('user_name') if is_admin() else None
        record_vo = DetectionRecordVO(record, user_name)
        
        return success(record_vo.to_dict())
        
    except Exception as e:
        return error(f"获取识别记录详情失败: {str(e)}")

@detection_bp.route('/records/<record_id>', methods=['PUT'])
@token_required
def update_detection_record(record_id):
    """更新图片识别记录"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        
        if not title:
            return error("标题不能为空")
        
        # 检查记录是否存在以及用户权限
        conditions = ["id = ?"]
        params = [record_id]
        
        if not is_admin():
            conditions.append("user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        existing_record = db.query(f"SELECT id FROM detection_records{where_clause}", tuple(params), fetchone=True)
        if not existing_record:
            return error("识别记录不存在或无权访问", 404)
        
        # 更新记录信息
        affected_rows = db.execute('''
            UPDATE detection_records SET title = ?, description = ?
            WHERE id = ?
        ''', (title, description, record_id))
        
        if affected_rows > 0:
            return success(None, "记录更新成功")
        else:
            return error("更新失败")
        
    except Exception as e:
        return error(f"更新识别记录失败: {str(e)}")

@detection_bp.route('/records/<record_id>', methods=['DELETE'])
@token_required
def delete_detection_record(record_id):
    """删除图片识别记录"""
    try:
        # 检查记录是否存在以及用户权限
        conditions = ["id = ?"]
        params = [record_id]
        
        if not is_admin():
            conditions.append("user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        record_data = db.query(f'''
            SELECT original_image_bucket, original_image_object_key,
                   result_image_bucket, result_image_object_key,
                   enhanced_image_bucket, enhanced_image_object_key
            FROM detection_records{where_clause}
        ''', tuple(params), fetchone=True)
        
        if not record_data:
            return error("识别记录不存在或无权访问", 404)
        
        # 删除文件服务中的图片文件
        try:
            # 删除原始图片
            if record_data['original_image_bucket'] and record_data['original_image_object_key']:
                file_client.delete(record_data['original_image_bucket'], record_data['original_image_object_key'])
            
            # 删除结果图片
            if record_data['result_image_bucket'] and record_data['result_image_object_key']:
                file_client.delete(record_data['result_image_bucket'], record_data['result_image_object_key'])
            
            # 删除增强图片
            if record_data['enhanced_image_bucket'] and record_data['enhanced_image_object_key']:
                file_client.delete(record_data['enhanced_image_bucket'], record_data['enhanced_image_object_key'])
                
        except Exception as e:
            print(f"删除图片文件失败: {e}")
        
        # 删除记录
        affected_rows = db.execute('''
            DELETE FROM detection_records WHERE id = ?
        ''', (record_id,))
        
        if affected_rows > 0:
            return success(None, "记录删除成功")
        else:
            return error("删除失败")
        
    except Exception as e:
        return error(f"删除识别记录失败: {str(e)}")

@detection_bp.route('/enhance-image', methods=['POST'])
@token_required
def enhance_image():
    """图像增强接口"""
    try:
        data = request.get_json()
        bucket = data.get('bucket')
        object_key = data.get('object_key')
        
        if not bucket or not object_key:
            return error("缺少图像文件信息")
        
        # 获取原始图像
        image_data = file_client.get(bucket, object_key)
        
        # 使用OpenCV进行对比度增强
        nparr = np.frombuffer(image_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if image is None:
            return error("图像解码失败")
        
        # 应用CLAHE (对比度限制自适应直方图均衡化)
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        enhanced_image = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        
        # 进一步调整对比度和亮度
        enhanced_image = cv2.convertScaleAbs(enhanced_image, alpha=1.2, beta=10)
        
        # 创建临时文件
        temp_id = uuid.uuid4().hex[:8]
        enhanced_filename = f"enhanced_{temp_id}.jpg"
        temp_enhanced_path = os.path.join(TEMP_DIR, enhanced_filename)
        
        # 确保临时目录存在
        os.makedirs(TEMP_DIR, exist_ok=True)
        
        # 保存增强后的图像
        cv2.imwrite(temp_enhanced_path, enhanced_image)
        
        try:
            # 上传增强后的图像
            enhanced_upload_result = file_client.upload('detection_enhanced', temp_enhanced_path)
            
            return success({
                'enhanced_image_url': file_client.get_file_url(
                    enhanced_upload_result['bucket'],
                    enhanced_upload_result['objectKey']
                ),
                'enhanced_image_bucket': enhanced_upload_result['bucket'],
                'enhanced_image_object_key': enhanced_upload_result['objectKey']
            }, "图像增强完成")
            
        finally:
            # 清理临时文件
            try:
                os.remove(temp_enhanced_path)
            except Exception:
                pass
        
    except Exception as e:
        return error(f"图像增强失败: {str(e)}")

@detection_bp.route('/analyze-defects', methods=['POST'])
@token_required  
def analyze_defects():
    """多模态大模型缺陷分析接口"""
    try:
        data = request.get_json()
        bucket = data.get('bucket')
        object_key = data.get('object_key')
        detections = data.get('detections', [])
        
        if not bucket or not object_key:
            return error("缺少图像文件信息")
        
        # 获取图像数据
        image_data = file_client.get(bucket, object_key)
        
        # 调用多模态大模型进行分析
        from algo.llm.llm_vl_client import LLMVLClient
        
        llm_client = LLMVLClient()
        analysis_result = llm_client.analyze_defects(image_data, detections)
        
        return success({
            'analysis_result': analysis_result
        }, "缺陷分析完成")
        
    except Exception as e:
        return error(f"缺陷分析失败: {str(e)}")

@detection_bp.route('/generate-suggestions', methods=['POST'])
@token_required
def generate_suggestions():
    """生成改进建议接口"""
    try:
        data = request.get_json()
        analysis_result = data.get('analysis_result', '')
        
        if not analysis_result:
            return error("缺少分析结果")
        
        # 调用多模态大模型生成建议
        from algo.llm.llm_vl_client import LLMVLClient
        
        llm_client = LLMVLClient()
        suggestions = llm_client.generate_improvement_suggestions(analysis_result)
        
        return success({
            'suggestions': suggestions
        }, "改进建议生成完成")
        
    except Exception as e:
        return error(f"生成改进建议失败: {str(e)}")
