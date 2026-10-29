
import os
import uuid
import json
import multiprocessing
from datetime import datetime
from flask import Blueprint, request, g
from clients.file_client import file_client
from utils.response import success, error
from utils.db import db
from utils.jwt_util import token_required
from utils.models import VideoDetectionRecord, VideoDetectionRecordVO, VIDEO_DETECTION_STATUS
from utils.security_utils import is_admin
from services.dataset_draft_service import (
    DraftValidationError,
    create_draft,
    ensure_dataset_name_available,
    mark_draft_failed,
)
from services.video_dataset_service import VideoDatasetConfig, generate_video_dataset
from utils.tracking_counter import UniqueTrackCounter
from utils.video_writer import create_browser_video_writer
from config import TEMP_DIR, YOLO_CLASSES_CONFIG
from services.project_storage import ProjectTempPath
TEMP_DIR = ProjectTempPath(TEMP_DIR)

video_detection_bp = Blueprint('video_detection', __name__, url_prefix='/api/video-detection')

# 全局视频处理进程字典，用于管理处理进程
video_processing = {}

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

def run_video_detection_process(
    record_id, user_id, model_id, model_name, video_path, confidence_threshold, frame_rate,
    dataset_config=None, dataset_model_meta=None, secondary_video_path=None, project_id=None,
):
    from config import DB_PATH
    from services.project_storage import resource_scope
    with resource_scope(DB_PATH, 'video_detection_records', record_id, project_id):
        return _run_video_detection_process(record_id, user_id, model_id, model_name, video_path,
            confidence_threshold, frame_rate, dataset_config, dataset_model_meta, secondary_video_path)


def _run_video_detection_process(
    record_id, user_id, model_id, model_name, video_path, confidence_threshold, frame_rate,
    dataset_config=None, dataset_model_meta=None, secondary_video_path=None,
):
    """在后台进程中运行视频识别"""
    # 预先定义temp_dir，确保在finally块中能够访问
    temp_dir = os.path.join(TEMP_DIR, f"video_detection_{record_id}_{uuid.uuid4().hex[:8]}")
    
    try:
        # 在子进程中重新创建数据库连接
        from utils.db import Database
        process_db = Database()
        
        # 更新状态为处理中
        process_db.execute('''
            UPDATE video_detection_records 
            SET status = ?, start_time = ?, dataset_generation_status = ?
            WHERE id = ?
        ''', (
            VIDEO_DETECTION_STATUS['PROCESSING'], datetime.now().isoformat(),
            'generating' if dataset_config and dataset_config.get('enabled') else 'not_requested',
            record_id,
        ))
        
        # 获取模型信息
        model_data = process_db.query('''
            SELECT model_bucket, model_object_key FROM models 
            WHERE id = ? AND status = 3
        ''', (model_id,), fetchone=True)
        
        if not model_data or not model_data['model_bucket'] or not model_data['model_object_key']:
            raise Exception("模型文件不存在")
        
        # 创建临时目录
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # 检查视频文件是否存在
            if not os.path.exists(video_path):
                raise Exception(f"视频文件不存在: {video_path}")
            
            # 下载模型文件
            model_content = file_client.get(model_data['model_bucket'], model_data['model_object_key'])
            model_path = os.path.join(temp_dir, 'model.pt')
            with open(model_path, 'wb') as f:
                f.write(model_content)
            
            # 获取视频信息
            import cv2
            print(f"尝试打开视频文件: {video_path}")
            print(f"文件是否存在: {os.path.exists(video_path)}")
            if os.path.exists(video_path):
                print(f"文件大小: {os.path.getsize(video_path)} bytes")
            
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise Exception(f"无法打开视频文件: {video_path}")
                
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            original_fps = cap.get(cv2.CAP_PROP_FPS)
            
            print(f"视频信息 - 总帧数: {total_frames}, FPS: {original_fps}")
            
            if total_frames <= 0:
                cap.release()
                raise Exception("无效的视频文件：无法获取帧数")
            
            cap.release()
            
            # 更新总帧数
            process_db.execute('''
                UPDATE video_detection_records 
                SET total_frames = ?
                WHERE id = ?
            ''', (total_frames, record_id))
            
            # 执行视频识别 - 逐帧处理以提供实时进度更新
            from ultralytics import YOLO
            import numpy as np
            
            model = YOLO(model_path)
            
            # 设置输出目录
            output_dir = os.path.join(temp_dir, 'results')
            os.makedirs(output_dir, exist_ok=True)
            
            # 初始化视频读取和写入
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise Exception("无法重新打开视频文件进行处理")
            
            # 获取视频属性
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            
            # 确保FPS是有效值
            if fps <= 0 or fps > 120:  # 修正无效的FPS值
                fps = 30.0  # 使用默认帧率
                print(f"修正无效的FPS值，使用默认值: {fps}")
            
            # 确保分辨率是有效的偶数值（某些编码器要求）
            if width % 2 != 0:
                width -= 1
            if height % 2 != 0:
                height -= 1
                
            print(f"视频参数 - 分辨率: {width}x{height}, FPS: {fps}, 总帧数: {total_frames}")
            
            # 仅选择浏览器可直接播放的编码：H.264/MP4，或 VP8/VP9 WebM。
            # 不再回退到 mp4v/FMP4，因为 Chrome/Edge 无法在 HTML5 video 中解码它。
            output_fps = min(frame_rate, fps)
            out, result_video_path, selected_codec = create_browser_video_writer(
                output_dir=output_dir,
                filename_stem=f'result_{uuid.uuid4().hex[:8]}',
                fps=output_fps,
                frame_size=(width, height),
            )
            print(f"浏览器兼容视频输出配置 - 路径: {result_video_path}, 编码器: {selected_codec}, 输出FPS: {output_fps}, 分辨率: {width}x{height}")
            
            # 处理识别结果
            detection_summary = {
                'total_frames': total_frames,
                'processed_frames': 0,
                'detections_per_frame': [],
                'summary': {
                    'total_detections': 0,
                    'unique_detections': 0,
                    'unique_detections_by_class': {},
                    'avg_detections_per_frame': 0,
                    'classes_detected': set()
                }
            }
            
            total_detections = 0
            unique_track_counter = UniqueTrackCounter()
            frame_idx = 0
            processed_frame_count = 0  # 实际处理的帧数
            
            # 计算帧采样间隔
            original_fps = fps
            target_fps = frame_rate  # 用户设定的目标帧率
            
            # 如果目标帧率大于等于原始帧率，处理所有帧
            if target_fps >= original_fps:
                frame_interval = 1
                target_process_frames = total_frames
                print(f"目标帧率({target_fps}) >= 原始帧率({original_fps})，处理所有帧")
            else:
                # 计算采样间隔：每隔多少帧处理一帧
                frame_interval = max(1, int(original_fps / target_fps))
                # 计算需要处理的总帧数
                target_process_frames = sum(1 for i in range(total_frames) if i % frame_interval == 0)
                print(f"帧率采样 - 原始FPS: {original_fps}, 目标FPS: {target_fps}, 采样间隔: 每{frame_interval}帧处理1帧")
            
            print(f"开始逐帧处理视频，总帧数: {total_frames}, 需要处理: {target_process_frames}帧, 采样间隔: {frame_interval}")
            
            # 更新目标处理帧数到数据库
            process_db.execute('''
                UPDATE video_detection_records 
                SET target_frames = ?
                WHERE id = ?
            ''', (target_process_frames, record_id))
            
            # 创建初始的处理信息，存储到数据库
            initial_processing_info = {
                'frame_sampling': {
                    'original_fps': original_fps,
                    'target_fps': target_fps,
                    'frame_interval': frame_interval,
                    'total_frames': total_frames,
                    'target_process_frames': target_process_frames,
                    'processed_frames': 0
                },
                'status': 'processing_started',
                'start_time': datetime.now().isoformat()
            }
            
            # 更新数据库，存储初始处理信息
            process_db.execute('''
                UPDATE video_detection_records 
                SET detection_result = ?
                WHERE id = ?
            ''', (json.dumps(initial_processing_info), record_id))
            
            try:
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        break
                    
                    frame_idx += 1
                    
                    # 根据帧率设置决定是否处理当前帧
                    should_process_frame = (frame_idx - 1) % frame_interval == 0
                    
                    if should_process_frame:
                        processed_frame_count += 1
                        
                        # 使用与实时检测一致的ByteTrack进行持久跟踪
                        results = model.track(
                            frame,
                            conf=confidence_threshold,
                            persist=True,
                            tracker="bytetrack.yaml",
                            verbose=False
                        )
                        
                        frame_detections = []
                        annotated_frame = frame.copy()
                        
                        # 处理检测结果
                        if results and len(results) > 0:
                            result = results[0]
                            if result.boxes is not None and len(result.boxes) > 0:
                                # 手动绘制检测框，使用自定义颜色
                                annotated_frame = frame.copy()
                                
                                # 提取检测数据并绘制
                                for box in result.boxes:
                                    # 获取边界框坐标
                                    x1, y1, x2, y2 = box.xyxy[0]
                                    confidence = float(box.conf[0].cpu().item()) if hasattr(box.conf[0], 'cpu') else float(box.conf[0])
                                    class_id = int(box.cls[0].cpu().item()) if hasattr(box.cls[0], 'cpu') else int(box.cls[0])
                                    class_name = model.names[class_id]
                                    track_id = int(box.id[0].cpu().item()) if box.id is not None and hasattr(box.id[0], 'cpu') else (
                                        int(box.id[0]) if box.id is not None else None
                                    )
                                    
                                    # 获取类别对应的颜色
                                    color = get_class_color(class_name)
                                    
                                    # 绘制检测框
                                    cv2.rectangle(annotated_frame, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                                    
                                    # 构建显示文本
                                    if track_id is not None:
                                        text = f'ID:{track_id} {class_name} {confidence:.2f}'
                                    else:
                                        text = f'{class_name} {confidence:.2f}'
                                    
                                    # 在检测框上方显示文本
                                    cv2.putText(annotated_frame, text, 
                                              (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                                    
                                    # 添加到检测数据
                                    detection = {
                                        'bbox': box.xyxy[0].cpu().numpy().tolist() if hasattr(box.xyxy[0], 'cpu') else box.xyxy[0].tolist(),
                                        'confidence': confidence,
                                        'class': class_id,
                                        'class_name': class_name,
                                        'track_id': track_id
                                    }
                                    frame_detections.append(detection)
                                    detection_summary['summary']['classes_detected'].add(detection['class_name'])
                                    unique_track_counter.register(track_id, class_name)
                                    total_detections += 1
                        
                        # 确保帧的尺寸正确
                        if annotated_frame.shape[:2] != (height, width):
                            annotated_frame = cv2.resize(annotated_frame, (width, height))
                        
                        # 写入输出视频
                        if not out.write(annotated_frame):
                            print(f"警告: 第{processed_frame_count}帧写入失败")
                        
                        # 记录当前帧的检测结果
                        detection_summary['detections_per_frame'].append({
                            'frame': frame_idx - 1,
                            'detections': frame_detections,
                            'count': len(frame_detections)
                        })
                    else:
                        # 跳过的帧仍然记录，但不进行检测
                        detection_summary['detections_per_frame'].append({
                            'frame': frame_idx - 1,
                            'detections': [],
                            'count': 0,
                            'skipped': True
                        })
                    
                    # 更新处理进度 - 改进的进度更新策略
                    should_update = (
                        frame_idx % 10 == 0 or  # 每10帧更新一次
                        frame_idx == total_frames or  # 最后一帧
                        frame_idx == 1  # 第一帧
                    )
                    
                    if should_update:
                        try:
                            # 使用事务确保更新的原子性
                            conn = process_db.get_connection()
                            cursor = conn.cursor()
                            
                            # 更新进度：更新读取帧数和实际处理帧数
                            cursor.execute('''
                                UPDATE video_detection_records 
                                SET processed_frames = ?, actual_processed_frames = ?,
                                    target_frames = ?
                                WHERE id = ?
                            ''', (frame_idx, processed_frame_count, target_process_frames, record_id))
                            
                            # 同时更新检测结果中的处理进度信息
                            progress_info = {
                                'frame_sampling': {
                                    'original_fps': original_fps,
                                    'target_fps': target_fps,
                                    'frame_interval': frame_interval,
                                    'total_frames': total_frames,
                                    'target_process_frames': target_process_frames,
                                    'processed_frames': processed_frame_count,
                                    'read_frames': frame_idx
                                },
                                'status': 'processing',
                                'update_time': datetime.now().isoformat()
                            }
                            
                            cursor.execute('''
                                UPDATE video_detection_records 
                                SET detection_result = ?
                                WHERE id = ?
                            ''', (json.dumps(progress_info), record_id))
                            
                            affected = cursor.rowcount
                            conn.commit()
                            conn.close()
                            
                            if affected > 0:
                                # 进度基于实际读取的帧数，而不是处理的帧数
                                progress_percent = (frame_idx / total_frames) * 100
                                process_percent = (processed_frame_count / target_process_frames) * 100 if target_process_frames > 0 else 0
                                print(f"进度更新: 读取{frame_idx}/{total_frames}帧 ({progress_percent:.1f}%), 处理{processed_frame_count}/{target_process_frames}帧 ({process_percent:.1f}%)")
                                
                                # 额外日志记录，帮助调试
                                if frame_idx % 50 == 0 or frame_idx == total_frames:
                                    print(f"详细进度 - 任务ID: {record_id}, 读取进度: {progress_percent:.1f}%, 处理进度: {process_percent:.1f}%, 时间戳: {datetime.now()}")
                                    
                                # 确保数据库更新完成后稍作等待
                                import time
                                time.sleep(0.001)  # 1ms延迟，确保事务提交
                            else:
                                print(f"警告: 进度更新失败，记录ID: {record_id}, 帧索引: {frame_idx}")
                                
                        except Exception as update_e:
                            print(f"进度更新出错: {update_e}")
                            # 在出错时也记录更详细的信息
                            print(f"  错误详情 - 任务ID: {record_id}, 帧索引: {frame_idx}, 总帧数: {total_frames}")
                    
                    # 避免处理过程占用过多CPU，但频率降低
                    if frame_idx % 50 == 0:
                        import time
                        time.sleep(0.01)  # 短暂休息
                        
            finally:
                cap.release()
                if out is not None:
                    # 确保所有帧都被写入
                    try:
                        # 强制刷新缓冲区
                        import time
                        time.sleep(0.1)  # 短暂等待确保写入完成
                    except:
                        pass
                    out.release()
                    print("视频写入器已释放")
                    
            # 验证生成的MP4视频文件
            if os.path.exists(result_video_path):
                file_size = os.path.getsize(result_video_path)
                print(f"生成的MP4视频文件大小: {file_size} bytes")
                
                if file_size > 0:
                    # 验证视频文件是否可读
                    test_cap = cv2.VideoCapture(result_video_path)
                    if test_cap.isOpened():
                        test_frame_count = int(test_cap.get(cv2.CAP_PROP_FRAME_COUNT))
                        test_fps = test_cap.get(cv2.CAP_PROP_FPS)
                        test_width = int(test_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        test_height = int(test_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        print(f"MP4视频文件验证成功 - 帧数: {test_frame_count}, FPS: {test_fps}, 分辨率: {test_width}x{test_height}")
                        
                        extension = os.path.splitext(result_video_path)[1].lower()
                        print(f"✓ 确认浏览器兼容输出格式: {extension}")
                            
                        test_cap.release()
                    else:
                        print("错误: 生成的MP4视频文件无法正常打开")
                        test_cap.release()
                        # 如果视频文件有问题，设置为None以避免上传损坏的文件
                        result_video_path = None
                else:
                    print("错误: 生成的MP4视频文件大小为0")
                    result_video_path = None
            else:
                print("错误: 未找到生成的MP4视频文件")
                result_video_path = None
                
            detection_summary['processed_frames'] = processed_frame_count  # 实际处理的帧数
            detection_summary['total_read_frames'] = frame_idx  # 读取的总帧数
            print(f"视频处理完成 - 读取帧数: {frame_idx}, 实际处理帧数: {processed_frame_count}")
            
            detection_summary['summary']['total_detections'] = total_detections
            detection_summary['summary'].update(unique_track_counter.to_summary())
            detection_summary['summary']['avg_detections_per_frame'] = total_detections / max(processed_frame_count, 1)
            detection_summary['summary']['classes_detected'] = list(detection_summary['summary']['classes_detected'])
            detection_summary['summary']['frame_sampling'] = {
                'original_fps': original_fps,
                'target_fps': target_fps,
                'frame_interval': frame_interval,
                'total_frames': frame_idx,
                'processed_frames': processed_frame_count
            }
            
            # 上传浏览器兼容的结果视频（如果存在且有效）
            result_upload_result = None
            if result_video_path and os.path.exists(result_video_path):
                if os.path.splitext(result_video_path)[1].lower() in ('.mp4', '.webm'):
                    result_upload_result = file_client.upload('video_detection_results', result_video_path)
                    print(f"结果视频上传成功: {result_upload_result}")
                else:
                    print(f"警告: 结果视频格式不受浏览器支持，跳过上传: {result_video_path}")
            else:
                print("警告: 没有有效的结果视频可上传")
            
            # 更新识别记录为完成状态
            # processed_frames 存储读取的帧数
            # target_frames 存储需要处理的总帧数  
            # actual_processed_frames 存储实际处理的帧数
            process_db.execute('''
                UPDATE video_detection_records 
                SET status = ?, complete_time = ?, processed_frames = ?, 
                    total_frames = ?, target_frames = ?, actual_processed_frames = ?,
                    result_video_bucket = ?, result_video_object_key = ?,
                    detection_result = ?
                WHERE id = ?
            ''', (
                VIDEO_DETECTION_STATUS['COMPLETED'],
                datetime.now().isoformat(),
                frame_idx,  # 读取的总帧数
                frame_idx,  # 视频总帧数
                target_process_frames,  # 需要处理的帧数
                processed_frame_count,  # 实际处理的帧数
                result_upload_result['bucket'] if result_upload_result else None,
                result_upload_result['objectKey'] if result_upload_result else None,
                json.dumps(detection_summary),
                record_id
            ))
            
            print(f"视频处理任务完成，记录ID: {record_id}")

            # Dataset generation is deliberately isolated: a dataset failure must not
            # overwrite a successful video-detection status/result.
            if dataset_config and dataset_config.get('enabled'):
                try:
                    dataset_summary = generate_video_dataset(
                        dataset_config['draft_id'],
                        VideoDatasetConfig(**dataset_config['config']),
                        {
                            'primary_video': video_path,
                            'secondary_video': secondary_video_path,
                            'work_dir': os.path.join(temp_dir, 'dataset'),
                            'model_meta': dataset_model_meta,
                        },
                        process_db,
                        file_client,
                    )
                    process_db.execute('''
                        UPDATE video_detection_records
                        SET dataset_generation_status = ?, dataset_statistics = ?
                        WHERE id = ?
                    ''', ('completed', json.dumps(dataset_summary, ensure_ascii=False), record_id))
                except Exception as dataset_error:
                    print(f"视频检测已完成，但数据集生成失败: {dataset_error}")
                    try:
                        mark_draft_failed(dataset_config['draft_id'], str(dataset_error), process_db)
                    except Exception as mark_error:
                        print(f"记录数据集失败状态时出错: {mark_error}")
                    process_db.execute('''
                        UPDATE video_detection_records
                        SET dataset_generation_status = ?, dataset_statistics = ?
                        WHERE id = ?
                    ''', ('failed', json.dumps({'error': str(dataset_error)}, ensure_ascii=False), record_id))
            
        finally:
            # 清理临时目录
            import shutil
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            
            # 清理上传的视频文件
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
                # 也清理上传目录（如果为空）
                upload_dir = os.path.dirname(video_path)
                if os.path.exists(upload_dir) and not os.listdir(upload_dir):
                    os.rmdir(upload_dir)
            except Exception:
                pass
            
    except Exception as e:
        # 在子进程中重新创建数据库连接（如果之前没有创建）
        if 'process_db' not in locals():
            from utils.db import Database
            process_db = Database()
        
        # 记录错误信息
        print(f"视频处理出现错误: {str(e)}")
        print(f"视频路径: {video_path}")
        
        # 更新状态为失败
        error_result = {'error': str(e)}
        process_db.execute('''
            UPDATE video_detection_records 
            SET status = ?, complete_time = ?, detection_result = ?
            WHERE id = ?
        ''', (
            VIDEO_DETECTION_STATUS['FAILED'], 
            datetime.now().isoformat(),
            json.dumps(error_result),
            record_id
        ))
        if dataset_config and dataset_config.get('enabled'):
            try:
                mark_draft_failed(dataset_config['draft_id'], str(e), process_db)
            except Exception as mark_error:
                print(f"记录数据集失败状态时出错: {mark_error}")
            process_db.execute('''
                UPDATE video_detection_records
                SET dataset_generation_status = ?, dataset_statistics = ?
                WHERE id = ?
            ''', ('failed', json.dumps({'error': str(e)}, ensure_ascii=False), record_id))
        
        # 清理临时文件
        import shutil
        try:
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
            
        # 清理上传的视频文件
        try:
            for path in (video_path, secondary_video_path):
                if path and os.path.exists(path):
                    os.remove(path)
            upload_dir = os.path.dirname(video_path)
            if os.path.exists(upload_dir) and not os.listdir(upload_dir):
                os.rmdir(upload_dir)
        except Exception:
            pass
    finally:
        # 从处理进程字典中移除已完成的进程
        if record_id in video_processing:
            del video_processing[record_id]
        
        # 最终清理：确保所有临时文件都被删除
        try:
            for path in (locals().get('video_path'), locals().get('secondary_video_path')):
                if path and os.path.exists(path):
                    os.remove(path)
            # 清理上传目录
            if 'video_path' in locals():
                upload_dir = os.path.dirname(video_path)
                if os.path.exists(upload_dir) and not os.listdir(upload_dir):
                    os.rmdir(upload_dir)
        except Exception:
            pass

@video_detection_bp.route('/detect', methods=['POST'])
@token_required
def detect_video():
    """视频识别接口"""
    try:
        # 获取请求参数
        model_id = request.form.get('model_id')
        confidence_threshold = float(request.form.get('confidence_threshold', 0.5))
        frame_rate = int(request.form.get('frame_rate', 30))
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        dataset_config = VideoDatasetConfig.from_form(request.form)

        if dataset_config.enabled and not is_admin():
            return error("仅管理员可以同时生成待划分数据集", 403)
        
        if not model_id:
            return error("请选择识别模型")
        
        if not title:
            return error("请输入标题")
        
        # 检查是否上传了视频
        if 'video' not in request.files:
            return error("请上传视频")
        
        video_file = request.files['video']
        if video_file.filename == '':
            return error("请选择视频文件")
        
        # 验证文件类型
        allowed_extensions = {'mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv'}
        if not ('.' in video_file.filename and 
                video_file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
            return error("请上传支持的视频格式（MP4、AVI、MOV、MKV、FLV、WMV）")
        
        # 检查模型是否存在且已发布
        model_data = db.query('''
            SELECT id, name, model_mode, secondary_modality FROM models
            WHERE id = ? AND status = 3
        ''', (model_id,), fetchone=True)
        
        if not model_data:
            return error("选择的模型不存在或未发布")

        annotation_model = None
        draft_id = None
        secondary_file = request.files.get('secondary_video')
        if dataset_config.enabled:
            try:
                ensure_dataset_name_available(dataset_config.name, db)
            except DraftValidationError as exc:
                return error(str(exc), 400)

            requested_mode = dataset_config.mode
            if requested_mode == 'single' and secondary_file and secondary_file.filename:
                return error("单模态数据集不能上传第二路视频", 400)
            if requested_mode == 'multimodal':
                if not secondary_file or not secondary_file.filename:
                    return error("多模态数据集必须上传第二路视频", 400)
                if not dataset_config.secondary_modality:
                    return error("多模态数据集必须选择第二模态", 400)

            requested_model_id = dataset_config.annotation_model_id
            if not requested_model_id:
                if (model_data.get('model_mode') or 'single') == requested_mode and (
                    requested_mode == 'single'
                    or (model_data.get('secondary_modality') or '').lower() == dataset_config.secondary_modality
                ):
                    requested_model_id = model_id
                else:
                    return error("当前检测模型与数据集模式不兼容，请选择标注模型", 400)
            annotation_model = db.query('''
                SELECT id, name, model_bucket, model_object_key, model_mode,
                       primary_modality, secondary_modality, model_yaml
                FROM models WHERE id = ? AND status = 3
            ''', (requested_model_id,), fetchone=True)
            if not annotation_model:
                return error("标注模型不存在或未发布", 400)
            annotation_mode = annotation_model.get('model_mode') or 'single'
            if annotation_mode != requested_mode or (
                requested_mode == 'multimodal'
                and (annotation_model.get('secondary_modality') or '').lower() != dataset_config.secondary_modality
            ):
                return error("标注模型与所选数据集模式或模态不兼容", 400)
            dataset_config = VideoDatasetConfig(
                **{**dataset_config.__dict__, 'annotation_model_id': requested_model_id}
            )
        
        # 创建临时目录保存上传的视频
        temp_id = uuid.uuid4().hex[:8]
        temp_dir = os.path.join(TEMP_DIR, f"video_upload_{temp_id}")
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # 保存上传的视频，保持原始扩展名
            original_filename = video_file.filename
            file_extension = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'mp4'
            video_filename = f"input_{uuid.uuid4().hex[:8]}.{file_extension}"
            temp_video_path = os.path.join(temp_dir, video_filename)
            
            print(f"保存视频文件到: {temp_video_path}")
            video_file.save(temp_video_path)
            
            # 验证保存的文件
            if not os.path.exists(temp_video_path):
                raise Exception("视频文件保存失败")
            
            file_size = os.path.getsize(temp_video_path)
            print(f"保存的文件大小: {file_size} bytes")
            
            if file_size == 0:
                raise Exception("上传的视频文件为空")

            import cv2
            secondary_video_path = None
            secondary_video_upload = None
            if dataset_config.enabled and dataset_config.mode == 'multimodal':
                secondary_extension = secondary_file.filename.rsplit('.', 1)[1].lower() if '.' in secondary_file.filename else ''
                if secondary_extension not in allowed_extensions:
                    raise Exception("第二路视频格式不受支持")
                secondary_video_path = os.path.join(temp_dir, f"secondary_{uuid.uuid4().hex[:8]}.{secondary_extension}")
                secondary_file.save(secondary_video_path)
                secondary_cap = cv2.VideoCapture(secondary_video_path)
                if not secondary_cap.isOpened():
                    secondary_cap.release()
                    raise Exception("第二路上传文件不是有效的视频格式")
                secondary_cap.release()
            
            # 初步验证视频文件
            test_cap = cv2.VideoCapture(temp_video_path)
            if not test_cap.isOpened():
                test_cap.release()
                raise Exception("上传的文件不是有效的视频格式")
            test_cap.release()

            if secondary_video_path:
                secondary_video_upload = file_client.upload('video_detection_original', secondary_video_path)
            
            # 上传原视频到文件服务
            original_upload_result = file_client.upload('video_detection_original', temp_video_path)
            
            # 创建视频识别记录
            record_id = str(uuid.uuid4())

            affected_rows = db.execute('''
                INSERT INTO video_detection_records (
                    id, user_id, model_id, model_name,
                    original_video_bucket, original_video_object_key,
                    confidence_threshold, frame_rate, create_time, title, description, status,
                    dataset_generation_status, dataset_draft_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record_id, g.user_id, model_id, model_data['name'],
                original_upload_result['bucket'], original_upload_result['objectKey'],
                confidence_threshold, frame_rate, datetime.now().isoformat(), 
                title, description, VIDEO_DETECTION_STATUS['PENDING'],
                'pending' if dataset_config.enabled else 'not_requested', None,
            ))

            if affected_rows > 0 and dataset_config.enabled:
                try:
                    draft = create_draft({
                        'id': str(uuid.uuid4()),
                        'name': dataset_config.name,
                        'description': dataset_config.description,
                        'creator_id': g.user_id,
                        'source_type': 'video',
                        'video_detection_record_id': record_id,
                        'dataset_mode': dataset_config.mode,
                        'primary_modality': 'rgb',
                        'secondary_modality': dataset_config.secondary_modality,
                        'metadata': {
                            'annotation_model_id': dataset_config.annotation_model_id,
                            'annotation_model_name': annotation_model.get('name'),
                            'annotation_confidence': dataset_config.confidence,
                            'frame_interval': dataset_config.frame_interval,
                            'dataset_mode': dataset_config.mode,
                            'secondary_modality': dataset_config.secondary_modality,
                            'secondary_time_offset': dataset_config.secondary_time_offset,
                            'secondary_video_ref': (
                                {
                                    'bucket': secondary_video_upload['bucket'],
                                    'objectKey': secondary_video_upload['objectKey'],
                                } if secondary_video_upload else None
                            ),
                        },
                    }, db)
                except Exception:
                    db.execute("DELETE FROM video_detection_records WHERE id = ?", (record_id,))
                    for uploaded in (original_upload_result, secondary_video_upload):
                        if uploaded:
                            try:
                                file_client.delete(uploaded['bucket'], uploaded['objectKey'])
                            except Exception:
                                pass
                    raise
                draft_id = draft.id
                db.execute(
                    'UPDATE video_detection_records SET dataset_draft_id = ? WHERE id = ?',
                    (draft_id, record_id),
                )
            
            if affected_rows > 0:
                # 创建并启动视频处理进程
                from services.project_storage import current_scope
                scope = current_scope()
                processing_process = multiprocessing.Process(
                    target=run_video_detection_process,
                    args=(record_id, g.user_id, model_id, model_data['name'], 
                          temp_video_path, confidence_threshold, frame_rate,
                          ({'enabled': True, 'draft_id': draft_id, 'config': dataset_config.__dict__}
                           if dataset_config.enabled else None),
                          annotation_model if dataset_config.enabled else None,
                          secondary_video_path, *([scope[1]] if scope else [])),
                )
                processing_process.daemon = True
                video_processing[record_id] = processing_process
                try:
                    processing_process.start()
                except Exception as start_error:
                    video_processing.pop(record_id, None)
                    if draft_id:
                        mark_draft_failed(draft_id, str(start_error), db)
                    db.execute('''
                        UPDATE video_detection_records
                        SET status = ?, complete_time = ?, dataset_generation_status = ?
                        WHERE id = ?
                    ''', (
                        VIDEO_DETECTION_STATUS['FAILED'], datetime.now().isoformat(),
                        'failed' if draft_id else 'not_requested', record_id,
                    ))
                    raise
                
                return success({
                    'id': record_id,
                    'dataset_draft_id': draft_id,
                    'message': '视频上传成功，开始处理识别任务'
                }, "视频识别任务创建成功")
            else:
                # 如果创建任务失败，清理上传的文件
                try:
                    for uploaded in (original_upload_result, secondary_video_upload):
                        if uploaded:
                            try:
                                file_client.delete(uploaded['bucket'], uploaded['objectKey'])
                            except Exception:
                                pass
                    if os.path.exists(temp_video_path):
                        os.remove(temp_video_path)
                    if os.path.exists(temp_dir) and not os.listdir(temp_dir):
                        os.rmdir(temp_dir)
                except Exception:
                    pass
                return error("创建识别任务失败")
            
        except Exception as inner_e:
            # 如果在处理过程中出现异常，清理临时文件
            try:
                if os.path.exists(temp_video_path):
                    os.remove(temp_video_path)
                if os.path.exists(temp_dir) and not os.listdir(temp_dir):
                    os.rmdir(temp_dir)
            except Exception:
                pass
            raise inner_e
        
    except Exception as e:
        return error(f"视频识别失败: {str(e)}")

@video_detection_bp.route('/progress/<record_id>', methods=['GET'])
@token_required
def get_progress(record_id):
    """获取视频处理进度"""
    try:
        # 检查记录是否存在以及用户权限
        conditions = ["id = ?"]
        params = [record_id]
        
        if not is_admin():
            conditions.append("user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        record_data = db.query(f'''
            SELECT status, total_frames, processed_frames, target_frames, actual_processed_frames,
                   start_time, complete_time, detection_result, frame_rate,
                   dataset_generation_status, dataset_draft_id, dataset_statistics
            FROM video_detection_records{where_clause}
        ''', tuple(params), fetchone=True)
        
        if not record_data:
            return error("识别记录不存在或无权访问", 404)
        
        # 从数据库直接获取详细统计信息
        total_frames = record_data['total_frames'] or 0
        processed_frames = record_data['processed_frames'] or 0
        target_frames = record_data['target_frames'] or total_frames
        actual_processed_frames = record_data['actual_processed_frames'] or 0
        frame_rate = record_data['frame_rate'] or 30
        
        # 对于旧记录的兼容性处理（如果数据库字段为空，从detection_result中提取）
        if not target_frames and record_data['detection_result']:
            try:
                import json
                detection_result = json.loads(record_data['detection_result'])
                frame_sampling = detection_result.get('summary', {}).get('frame_sampling', {})
                if frame_sampling:
                    target_frames = frame_sampling.get('target_process_frames', total_frames)
                    actual_processed_frames = frame_sampling.get('processed_frames', 0)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"解析旧记录检测结果失败: {e}")
                target_frames = total_frames
        
        # 确保target_frames有默认值
        if target_frames == 0:
            target_frames = total_frames
        
        # 计算总体进度百分比（基于读取的帧数）
        overall_progress = 0
        if total_frames > 0:
            overall_progress = min(100, (processed_frames / total_frames) * 100)
        
        # 计算处理进度百分比（基于需要处理的帧数）
        processing_progress = 0
        if target_frames > 0:
            processing_progress = min(100, (actual_processed_frames / target_frames) * 100)
        
        # 添加调试日志
        print(f"进度查询 - 记录ID: {record_id}, 状态: {record_data['status']}, "
              f"总帧数: {total_frames}, 读取: {processed_frames}, "
              f"目标处理: {target_frames}, 已处理: {actual_processed_frames}, "
              f"总体进度: {overall_progress:.1f}%, 处理进度: {processing_progress:.1f}%")
        
        result = {
            'status': record_data['status'],
            'total_frames': total_frames,           # 视频总帧数
            'processed_frames': processed_frames,   # 当前读取的帧数
            'target_frames': target_frames,         # 需要处理的总帧数
            'actual_processed_frames': actual_processed_frames,  # 实际已处理的帧数
            'progress': round(overall_progress, 2), # 总体进度（基于读取）
            'processing_progress': round(processing_progress, 2), # 处理进度（基于处理）
            'frame_rate': frame_rate,               # 设置的帧率
            'start_time': record_data['start_time'],
            'complete_time': record_data['complete_time']
            , 'dataset_generation_status': record_data.get('dataset_generation_status', 'not_requested')
            , 'dataset_draft_id': record_data.get('dataset_draft_id')
            , 'dataset_statistics': json.loads(record_data.get('dataset_statistics') or '{}')
        }
        
        return success(result)
        
    except Exception as e:
        print(f"进度查询失败 - 记录ID: {record_id}, 错误: {str(e)}")
        return error(f"获取处理进度失败: {str(e)}")

@video_detection_bp.route('/records', methods=['GET'])
@token_required
def get_video_detection_records():
    """获取视频识别记录列表"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        search = request.args.get('search', '').strip()
        
        # 构建查询条件
        conditions = []
        params = []
        
        # 普通用户只能看到自己的记录，管理员可以看到所有记录
        if not is_admin():
            conditions.append("vdr.user_id = ?")
            params.append(g.user_id)
        
        if search:
            conditions.append("(vdr.title LIKE ? OR vdr.description LIKE ? OR vdr.model_name LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 获取总数
        count_sql = f"SELECT COUNT(*) as total FROM video_detection_records vdr{where_clause}"
        total_result = db.query(count_sql, tuple(params), fetchone=True)
        total = total_result['total'] if total_result else 0
        
        # 获取数据
        offset = (page - 1) * page_size
        data_sql = f'''
            SELECT vdr.*, u.real_name as user_name
            FROM video_detection_records vdr
            LEFT JOIN user u ON vdr.user_id = u.id
            {where_clause}
            ORDER BY vdr.create_time DESC
            LIMIT {page_size} OFFSET {offset}
        '''
        
        record_list = db.query(data_sql, tuple(params))
        
        records = []
        for row in record_list:
            record = VideoDetectionRecord(row)
            # 管理员可以看到用户真实姓名
            user_name = row.get('user_name') if is_admin() else None
            record_vo = VideoDetectionRecordVO(record, user_name)
            records.append(record_vo.to_dict())
        
        return success({
            'records': records,
            'total': total,
            'page': page,
            'page_size': page_size
        })
        
    except Exception as e:
        return error(f"获取识别记录失败: {str(e)}")

@video_detection_bp.route('/records/<record_id>', methods=['GET'])
@token_required
def get_video_detection_record(record_id):
    """获取视频识别记录详情"""
    try:
        # 构建查询条件
        conditions = ["vdr.id = ?"]
        params = [record_id]
        
        # 普通用户只能看到自己的记录
        if not is_admin():
            conditions.append("vdr.user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        record_data = db.query(f'''
            SELECT vdr.*, u.real_name as user_name
            FROM video_detection_records vdr
            LEFT JOIN user u ON vdr.user_id = u.id
            {where_clause}
        ''', tuple(params), fetchone=True)
        
        if not record_data:
            return error("识别记录不存在或无权访问", 404)
        
        record = VideoDetectionRecord(record_data)
        # 管理员可以看到用户真实姓名
        user_name = record_data.get('user_name') if is_admin() else None
        record_vo = VideoDetectionRecordVO(record, user_name)
        
        return success(record_vo.to_dict())
        
    except Exception as e:
        return error(f"获取识别记录详情失败: {str(e)}")

@video_detection_bp.route('/records/<record_id>', methods=['PUT'])
@token_required
def update_video_detection_record(record_id):
    """更新视频识别记录"""
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
        
        existing_record = db.query(f"SELECT id FROM video_detection_records{where_clause}", tuple(params), fetchone=True)
        if not existing_record:
            return error("识别记录不存在或无权访问", 404)
        
        # 更新记录信息
        affected_rows = db.execute('''
            UPDATE video_detection_records SET title = ?, description = ?
            WHERE id = ?
        ''', (title, description, record_id))
        
        if affected_rows > 0:
            return success(None, "记录更新成功")
        else:
            return error("更新失败")
        
    except Exception as e:
        return error(f"更新识别记录失败: {str(e)}")

@video_detection_bp.route('/records/<record_id>', methods=['DELETE'])
@token_required
def delete_video_detection_record(record_id):
    """删除视频识别记录"""
    try:
        # 检查记录是否存在以及用户权限
        conditions = ["id = ?"]
        params = [record_id]
        
        if not is_admin():
            conditions.append("user_id = ?")
            params.append(g.user_id)
        
        where_clause = " WHERE " + " AND ".join(conditions)
        
        record_data = db.query(f'''
            SELECT original_video_bucket, original_video_object_key,
                   result_video_bucket, result_video_object_key
            FROM video_detection_records{where_clause}
        ''', tuple(params), fetchone=True)
        
        if not record_data:
            return error("识别记录不存在或无权访问", 404)
        
        # 删除文件服务中的视频文件
        try:
            if record_data['original_video_bucket'] and record_data['original_video_object_key']:
                file_client.delete(record_data['original_video_bucket'], record_data['original_video_object_key'])
            
            if record_data['result_video_bucket'] and record_data['result_video_object_key']:
                file_client.delete(record_data['result_video_bucket'], record_data['result_video_object_key'])
        except Exception as e:
            print(f"删除视频文件失败: {e}")
        
        # 删除记录
        affected_rows = db.execute('''
            DELETE FROM video_detection_records WHERE id = ?
        ''', (record_id,))
        
        if affected_rows > 0:
            return success(None, "记录删除成功")
        else:
            return error("删除失败")
        
    except Exception as e:
        return error(f"删除识别记录失败: {str(e)}")
