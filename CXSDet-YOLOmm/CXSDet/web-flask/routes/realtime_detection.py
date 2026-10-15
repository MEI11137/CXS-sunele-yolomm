
import os
import uuid
import base64
import cv2
import numpy as np
import json
from datetime import datetime
from flask import Blueprint, request
from utils.db import db
from utils.response import success, error
from utils.jwt_util import token_required
from utils.security_utils import get_current_user
from utils.models import RealtimeDetectionRecord, RealtimeDetectionRecordVO, PageVO
from clients.file_client import file_client
from config import TEMP_DIR, YOLO_CLASSES_CONFIG

# 创建蓝图
realtime_detection_bp = Blueprint('realtime_detection', __name__, url_prefix='/api/realtime-detection')

# 全局变量用于存储YOLO模型实例和跟踪器
yolo_models = {}
tracker_sessions = {}

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

def get_yolo_model(model_id: str):
    """获取或加载YOLO模型"""
    if model_id not in yolo_models:
        try:
            print(f"开始加载模型: {model_id}")
            
            # 查询模型信息
            model_data = db.query(
                "SELECT * FROM models WHERE id = ? AND status IN (1, 3)", 
                (model_id,), 
                fetchone=True
            )
            if not model_data:
                print(f"模型未找到或状态不可用: {model_id}")
                return None
            
            # 将模型下载到临时缓存目录
            cache_filename = f"model_{model_id}_{model_data['model_object_key']}"
            model_cache_path = os.path.join(TEMP_DIR, 'model_cache', cache_filename)
            
            print(f"模型缓存路径: {model_cache_path}")
            
            # 如果缓存中不存在模型文件，则下载
            if not os.path.exists(model_cache_path):
                try:
                    print(f"缓存中不存在模型文件，开始下载...")
                    # 确保缓存目录存在
                    os.makedirs(os.path.dirname(model_cache_path), exist_ok=True)
                    # 使用file_client下载模型到缓存目录
                    file_client.download(model_data['model_bucket'], model_data['model_object_key'], model_cache_path)
                    print(f"模型下载完成: {model_cache_path}")
                except Exception as e:
                    print(f"下载模型失败 {model_id}: {e}")
                    return None
            else:
                print(f"使用缓存中的模型文件: {model_cache_path}")
            
            # 导入YOLO（这里假设已经安装了ultralytics）
            try:
                from ultralytics import YOLO
                yolo_models[model_id] = YOLO(model_cache_path)
                print(f"YOLO模型加载成功: {model_id}")
            except ImportError:
                print(f"无法导入ultralytics，请确保已安装: pip install ultralytics")
                return None
            except Exception as e:
                print(f"YOLO模型创建失败: {e}")
                return None
            
        except Exception as e:
            print(f"加载模型时发生错误 {model_id}: {e}")
            return None
    else:
        print(f"使用已缓存的模型: {model_id}")
    
    return yolo_models[model_id]

@realtime_detection_bp.route('/detect', methods=['POST'])
@token_required
def realtime_detect():
    """实时检测接口"""
    try:
        data = request.get_json()
        if not data:
            return error("请求数据不能为空", 400)
        
        # 获取参数
        model_id = data.get('model_id')
        session_id = data.get('session_id')
        image_data = data.get('image_data')  # base64编码的图片数据
        confidence_threshold = data.get('confidence_threshold', 0.5)
        
        if not all([model_id, session_id, image_data]):
            return error("缺少必要参数", 400)
        
        # 获取YOLO模型
        model = get_yolo_model(model_id)
        if model is None:
            return error("模型未找到或加载失败", 404)
        
        # 解码base64图片
        try:
            image_bytes = base64.b64decode(image_data.split(',')[1] if ',' in image_data else image_data)
            nparr = np.frombuffer(image_bytes, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if image is None:
                return error("图片解码失败", 400)
        except Exception as e:
            return error(f"图片数据无效: {str(e)}", 400)
        
        # 使用YOLO进行跟踪检测
        results = model.track(
            image,
            conf=confidence_threshold,
            persist=True,
            tracker="bytetrack.yaml"
        )
        
        new_detections = []
        result_image = image.copy()
        
        if results and len(results) > 0:
            result = results[0]
            
            # 处理检测结果
            if result.boxes is not None and len(result.boxes) > 0:
                boxes = result.boxes.cpu().numpy()
                
                for box in boxes:
                    # 获取边界框坐标
                    x1, y1, x2, y2 = box.xyxy[0]
                    confidence = box.conf[0]
                    class_id = int(box.cls[0])
                    class_name = model.names[class_id]
                    
                    # 获取track_id（如果有）
                    track_id = int(box.id[0]) if box.id is not None else None
                    
                    # 获取类别对应的颜色
                    color = get_class_color(class_name)
                    
                    # 为所有检测结果绘制框
                    cv2.rectangle(result_image, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                    
                    # 构建显示文本，将track_id放在最前面
                    if track_id is not None:
                        text = f'ID:{track_id} {class_name} {confidence:.2f}'
                    else:
                        text = f'{class_name} {confidence:.2f}'
                    
                    # 在检测框上方显示文本
                    cv2.putText(result_image, text, 
                              (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                    
                    # 检查是否是新的track_id和class_name组合，如果是新组合则保存到数据库
                    if track_id is not None:
                        existing_detection = db.query(
                            "SELECT COUNT(*) as count FROM realtime_detection_records WHERE session_id = ? AND track_id = ? AND class_name = ?",
                            (session_id, track_id, class_name),
                            fetchone=True
                        )
                        
                        # 如果是新的目标和类别组合，保存检测记录
                        if existing_detection['count'] == 0:
                            
                            # 保存结果图片
                            result_filename = f"realtime_detection_{uuid.uuid4().hex}.jpg"
                            temp_result_path = os.path.join(TEMP_DIR, result_filename)
                            
                            # 确保临时目录存在
                            os.makedirs(TEMP_DIR, exist_ok=True)
                            cv2.imwrite(temp_result_path, result_image)
                            
                            # 上传到文件存储
                            bucket_name = "realtime_detections"
                            upload_result = file_client.upload(bucket_name, temp_result_path)
                            # 从上传结果中获取实际的object_key
                            actual_object_key = upload_result['objectKey']
                            
                            # 删除临时文件
                            os.remove(temp_result_path)
                            
                            # 保存检测记录到数据库
                            detection_id = str(uuid.uuid4())
                            detection_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            
                            # 获取模型名称
                            model_info = db.query(
                                "SELECT name FROM models WHERE id = ?",
                                (model_id,),
                                fetchone=True
                            )
                            model_name = model_info['name'] if model_info else "Unknown Model"
                            
                            bbox_data = json.dumps({
                                'x1': float(x1), 'y1': float(y1),
                                'x2': float(x2), 'y2': float(y2)
                            })
                            
                            detection_result_data = json.dumps({
                                'class_name': class_name,
                                'confidence': float(confidence),
                                'bbox': {'x1': float(x1), 'y1': float(y1), 'x2': float(x2), 'y2': float(y2)},
                                'track_id': track_id
                            })
                            
                            current_user = get_current_user()
                            db.execute('''
                                INSERT INTO realtime_detection_records 
                                (id, user_id, model_id, model_name, session_id, track_id, detection_result, 
                                 confidence_threshold, detection_time, result_image_bucket, result_image_object_key, 
                                 bbox, class_name, confidence, create_time)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                detection_id, current_user.id, model_id, model_name, session_id, track_id,
                                detection_result_data, confidence_threshold, detection_time, bucket_name, actual_object_key,
                                bbox_data, class_name, float(confidence), detection_time
                            ))
                            
                            new_detections.append({
                                'id': detection_id,
                                'track_id': track_id,
                                'class_name': class_name,
                                'confidence': float(confidence),
                                'bbox': {'x1': float(x1), 'y1': float(y1), 'x2': float(x2), 'y2': float(y2)}
                            })
        
        # 将处理后的图片转换为base64返回
        _, buffer = cv2.imencode('.jpg', result_image)
        result_image_base64 = base64.b64encode(buffer).decode('utf-8')
        
        return success({
            'result_image': f'data:image/jpeg;base64,{result_image_base64}',
            'new_detections': new_detections,
            'detection_count': len(new_detections)
        })
        
    except Exception as e:
        print(f"实时检测错误: {e}")
        return error(f"检测失败: {str(e)}", 500)

@realtime_detection_bp.route('/records', methods=['GET'])
@token_required
def get_realtime_records():
    """获取实时检测记录列表"""
    try:
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        session_id = request.args.get('session_id')
        model_id = request.args.get('model_id')
        class_name = request.args.get('class_name')
        
        print(f"接收到的参数: page={page}, page_size={page_size}, session_id={session_id}, model_id={model_id}, class_name={class_name}")
        
        # 构建查询条件
        where_conditions = ["1=1"]
        params = []
        
        current_user = get_current_user()
        # 非管理员用户只能看自己的记录
        if current_user.role != 1:
            where_conditions.append("user_id = ?")
            params.append(current_user.id)
        
        if session_id:
            where_conditions.append("session_id LIKE ?")
            params.append(f"%{session_id}%")
        
        if model_id:
            where_conditions.append("model_id = ?")
            params.append(model_id)
        
        if class_name:
            where_conditions.append("class_name = ?")
            params.append(class_name)
        
        where_clause = " AND ".join(where_conditions)
        print(f"构建的查询条件: {where_clause}")
        print(f"查询参数: {params}")
        
        # 查询总数
        count_sql = f"SELECT COUNT(*) as total FROM realtime_detection_records WHERE {where_clause}"
        total_result = db.query(count_sql, params, fetchone=True)
        total = total_result['total']
        
        # 查询记录
        offset = (page - 1) * page_size
        query_sql = f"""
            SELECT rd.*, u.real_name as user_name 
            FROM realtime_detection_records rd
            LEFT JOIN user u ON rd.user_id = u.id
            WHERE {where_clause}
            ORDER BY rd.detection_time DESC
            LIMIT ? OFFSET ?
        """
        params.extend([page_size, offset])
        
        records_data = db.query(query_sql, params)
        
        # 转换为VO对象
        records = []
        for record_data in records_data:
            record = RealtimeDetectionRecord(record_data)
            # 管理员可以看到用户名
            user_name = record_data.get('user_name') if current_user.role == 1 else None
            record_vo = RealtimeDetectionRecordVO(record, user_name)
            records.append(record_vo.to_dict())
        
        # 构建分页响应
        page_vo = PageVO(records, total, page_size, page)
        
        return success(page_vo.to_dict())
        
    except Exception as e:
        print(f"获取实时检测记录错误: {e}")
        return error(f"获取记录失败: {str(e)}", 500)

@realtime_detection_bp.route('/sessions', methods=['GET'])
@token_required  
def get_detection_sessions():
    """获取检测会话列表"""
    try:
        # 构建查询条件
        where_conditions = ["1=1"]
        params = []
        
        current_user = get_current_user()
        # 非管理员用户只能看自己的会话
        if current_user.role != 1:
            where_conditions.append("user_id = ?")
            params.append(current_user.id)
        
        where_clause = " AND ".join(where_conditions)
        
        # 查询会话统计信息
        query_sql = f"""
            SELECT 
                session_id,
                model_name,
                MIN(detection_time) as start_time,
                MAX(detection_time) as end_time,
                COUNT(*) as detection_count,
                COUNT(DISTINCT class_name) as class_count,
                user_id
            FROM realtime_detection_records
            WHERE {where_clause}
            GROUP BY session_id, model_name, user_id
            ORDER BY start_time DESC
        """
        
        sessions_data = db.query(query_sql, params)
        
        # 如果是管理员，获取用户名信息
        if current_user.role == 1:
            user_ids = list(set([session['user_id'] for session in sessions_data]))
            if user_ids:
                user_query = f"SELECT id, real_name FROM user WHERE id IN ({','.join(['?'] * len(user_ids))})"
                users_data = db.query(user_query, user_ids)
                users_map = {user['id']: user['real_name'] for user in users_data}
                
                for session in sessions_data:
                    session['user_name'] = users_map.get(session['user_id'], 'Unknown')
        
        return success({
            'sessions': sessions_data
        })
        
    except Exception as e:
        print(f"获取检测会话错误: {e}")
        return error(f"获取会话失败: {str(e)}", 500)

@realtime_detection_bp.route('/records/<record_id>', methods=['DELETE'])
@token_required
def delete_realtime_record(record_id):
    """删除实时检测记录"""
    try:
        # 查询记录是否存在
        record_data = db.query(
            "SELECT * FROM realtime_detection_records WHERE id = ?",
            (record_id,),
            fetchone=True
        )
        
        if not record_data:
            return error("记录不存在", 404)
        
        current_user = get_current_user()
        # 非管理员用户只能删除自己的记录
        if current_user.role != 1 and record_data['user_id'] != current_user.id:
            return error("没有权限删除此记录", 403)
        
        # 删除文件
        if record_data['result_image_bucket'] and record_data['result_image_object_key']:
            try:
                file_client.delete(record_data['result_image_bucket'], record_data['result_image_object_key'])
            except Exception as e:
                print(f"删除文件失败: {e}")
        
        # 删除数据库记录
        db.execute("DELETE FROM realtime_detection_records WHERE id = ?", (record_id,))
        
        return success({"message": "记录删除成功"})
        
    except Exception as e:
        print(f"删除实时检测记录错误: {e}")
        return error(f"删除失败: {str(e)}", 500)

@realtime_detection_bp.route('/sessions/<session_id>', methods=['DELETE'])
@token_required
def delete_detection_session(session_id):
    """删除检测会话及其所有相关记录"""
    try:
        current_user = get_current_user()
        
        # 查询会话的所有记录
        where_conditions = ["session_id = ?"]
        params = [session_id]
        
        # 非管理员用户只能删除自己的会话
        if current_user.role != 1:
            where_conditions.append("user_id = ?")
            params.append(current_user.id)
        
        where_clause = " AND ".join(where_conditions)
        
        # 查询所有相关记录
        records_data = db.query(
            f"SELECT * FROM realtime_detection_records WHERE {where_clause}",
            params
        )
        
        if not records_data:
            return error("会话不存在或没有权限删除", 404)
        
        # 删除所有相关的文件
        deleted_files_count = 0
        for record in records_data:
            if record['result_image_bucket'] and record['result_image_object_key']:
                try:
                    file_client.delete(record['result_image_bucket'], record['result_image_object_key'])
                    deleted_files_count += 1
                except Exception as e:
                    print(f"删除文件失败 {record['result_image_object_key']}: {e}")
        
        # 删除数据库中的所有相关记录
        db.execute(
            f"DELETE FROM realtime_detection_records WHERE {where_clause}",
            params
        )
        
        return success({
            "message": "会话删除成功",
            "deleted_records_count": len(records_data),
            "deleted_files_count": deleted_files_count
        })
        
    except Exception as e:
        print(f"删除检测会话错误: {e}")
        return error(f"删除会话失败: {str(e)}", 500)

@realtime_detection_bp.route('/reset-tracker', methods=['POST'])
@token_required
def reset_tracker():
    """重置YOLO追踪器"""
    try:
        data = request.get_json()
        if not data:
            return error("请求数据不能为空", 400)
        
        model_id = data.get('model_id')
        if not model_id:
            return error("缺少模型ID参数", 400)
        
        # 获取模型实例
        model = get_yolo_model(model_id)
        if model is None:
            return error("模型未找到或加载失败", 404)
        
        # 重置追踪器状态
        # 通过调用一次track方法并设置persist=False来重置追踪器
        try:
            # 创建一个1x1的虚拟图像来重置追踪器
            import numpy as np
            dummy_image = np.zeros((1, 1, 3), dtype=np.uint8)
            model.track(dummy_image, persist=False)
            print(f"追踪器已重置: {model_id}")
        except Exception as e:
            print(f"重置追踪器时出错: {e}")
        
        return success({"message": "追踪器重置成功"})
        
    except Exception as e:
        print(f"重置追踪器错误: {e}")
        return error(f"重置追踪器失败: {str(e)}", 500)