
from typing import Dict, List, Any
import hashlib
from clients.file_client import file_client

# 用户模型
class User:
    def __init__(self, db_user: Dict):
        self.id = db_user.get('id')
        self.username = db_user.get('username')
        self.password = db_user.get('password')
        self.real_name = db_user.get('real_name')
        self.phone = db_user.get('phone')
        self.email = db_user.get('email')
        self.avatar_bucket = db_user.get('avatar_bucket')
        self.avatar_object_key = db_user.get('avatar_object_key')
        self.role = db_user.get('role', 0)
        self.status = db_user.get('status', 1)
        self.create_time = db_user.get('create_time')
        self.update_time = db_user.get('update_time')
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'username': self.username,
            'password': self.password,
            'realName': self.real_name,
            'phone': self.phone,
            'email': self.email,
            'avatarBucket': self.avatar_bucket,
            'avatarObjectKey': self.avatar_object_key,
            'role': self.role,
            'status': self.status,
            'createTime': self.create_time,
            'updateTime': self.update_time
        }

# 用户信息VO
class UserInfo:
    def __init__(self, user: User):
        self.id = user.id
        self.username = user.username
        self.real_name = user.real_name
        self.phone = user.phone
        self.email = user.email
        self.avatar_bucket = user.avatar_bucket
        self.avatar_object_key = user.avatar_object_key
        # 构建头像URL
        self.avatar_url = None
        if user.avatar_bucket and user.avatar_object_key:
            self.avatar_url = file_client.get_file_url(user.avatar_bucket, user.avatar_object_key)
        self.role = user.role
        self.status = user.status
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'username': self.username,
            'realName': self.real_name,
            'phone': self.phone,
            'email': self.email,
            'avatarBucket': self.avatar_bucket,
            'avatarObjectKey': self.avatar_object_key,
            'avatarUrl': self.avatar_url,
            'role': self.role,
            'status': self.status
        }

# 用户登录VO
class UserLoginVO:
    def __init__(self, user: User, token: str):
        self.user_info = UserInfo(user)
        self.token = token
    
    def to_dict(self) -> Dict:
        return {
            'userInfo': self.user_info.to_dict(),
            'token': self.token
        }

# 分页数据
class PageVO:
    def __init__(self, records: List[Any], total: int, size: int, current: int):
        self.records = records
        self.total = total
        self.size = size
        self.current = current
        self.pages = (total + size - 1) // size if size else 0
    
    def to_dict(self) -> Dict:
        return {
            'records': self.records,
            'total': self.total,
            'size': self.size,
            'current': self.current,
            'pages': self.pages
        }

# 数据集状态常量
DATASET_STATUS = {
    'PENDING': 0,      # 待验证
    'APPROVED': 1,     # 验证通过  
    'REJECTED': 2      # 验证失败
}

# 数据集模型
class Dataset:
    def __init__(self, db_dataset: Dict):
        self.id = db_dataset.get('id')
        self.name = db_dataset.get('name')
        self.description = db_dataset.get('description')
        self.zip_bucket = db_dataset.get('zip_bucket')
        self.zip_object_key = db_dataset.get('zip_object_key')
        self.structure_info = db_dataset.get('structure_info')
        self.upload_time = db_dataset.get('upload_time')
        self.file_size = db_dataset.get('file_size')
        self.status = db_dataset.get('status', 0)  # 默认待验证
        
        # 构建文件URL
        self.zip_file_url = None
        if self.zip_bucket and self.zip_object_key:
            self.zip_file_url = file_client.get_file_url(self.zip_bucket, self.zip_object_key)
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'zip_bucket': self.zip_bucket,
            'zip_object_key': self.zip_object_key,
            'zip_file_url': self.zip_file_url,
            'structure_info': self.structure_info,
            'upload_time': self.upload_time,
            'file_size': self.file_size,
            'status': self.status
        }

# 数据集信息VO
class DatasetVO:
    def __init__(self, dataset: Dataset):
        self.id = dataset.id
        self.name = dataset.name
        self.description = dataset.description
        self.zip_file_url = dataset.zip_file_url
        self.structure_info = dataset.structure_info
        self.upload_time = dataset.upload_time
        self.file_size = dataset.file_size
        self.status = dataset.status
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'zip_file_url': self.zip_file_url,
            'structure_info': self.structure_info,
            'upload_time': self.upload_time,
            'file_size': self.file_size,
            'status': self.status
        }

# 模型状态常量
MODEL_STATUS = {
    'TRAINING': 0,      # 训练中
    'COMPLETED': 1,     # 训练完成
    'FAILED': 2,        # 训练失败
    'PUBLISHED': 3,     # 已发布
    'TERMINATED': 4     # 训练终止
}

# 模型模型
class Model:
    def __init__(self, db_model: Dict):
        self.id = db_model.get('id')
        self.name = db_model.get('name')
        self.description = db_model.get('description')
        self.dataset_id = db_model.get('dataset_id')
        self.base_model = db_model.get('base_model')
        self.epochs = db_model.get('epochs', 50)
        self.batch_size = db_model.get('batch_size', 16)
        self.status = db_model.get('status', 0)
        self.model_bucket = db_model.get('model_bucket')
        self.model_object_key = db_model.get('model_object_key')
        self.train_result = db_model.get('train_result')
        self.create_time = db_model.get('create_time')
        self.start_time = db_model.get('start_time')
        self.complete_time = db_model.get('complete_time')
        self.model_mode = db_model.get('model_mode', 'single')
        self.primary_modality = db_model.get('primary_modality', 'rgb')
        self.secondary_modality = db_model.get('secondary_modality')
        self.model_yaml = db_model.get('model_yaml')
        self.parent_model_id = db_model.get('parent_model_id')
        self.version = db_model.get('version', 1)
        self.metrics_json = db_model.get('metrics_json')
        self.publish_status = db_model.get('publish_status', 'draft')
        
        # 构建模型文件URL
        self.model_file_url = None
        if self.model_bucket and self.model_object_key:
            self.model_file_url = file_client.get_file_url(self.model_bucket, self.model_object_key)
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'dataset_id': self.dataset_id,
            'base_model': self.base_model,
            'epochs': self.epochs,
            'batch_size': self.batch_size,
            'status': self.status,
            'model_bucket': self.model_bucket,
            'model_object_key': self.model_object_key,
            'model_file_url': self.model_file_url,
            'train_result': self.train_result,
            'create_time': self.create_time,
            'start_time': self.start_time,
            'complete_time': self.complete_time
            ,'model_mode': self.model_mode
            ,'primary_modality': self.primary_modality
            ,'secondary_modality': self.secondary_modality
            ,'model_yaml': self.model_yaml
            ,'parent_model_id': self.parent_model_id
            ,'version': self.version
            ,'metrics_json': self.metrics_json
            ,'publish_status': self.publish_status
        }

# 模型信息VO
class ModelVO:
    def __init__(self, model: Model, dataset_name: str = None):
        self.id = model.id
        self.name = model.name
        self.description = model.description
        self.dataset_id = model.dataset_id
        self.dataset_name = dataset_name
        self.base_model = model.base_model
        self.epochs = model.epochs
        self.batch_size = model.batch_size
        self.status = model.status
        self.model_file_url = model.model_file_url
        self.train_result = model.train_result
        self.create_time = model.create_time
        self.start_time = model.start_time
        self.complete_time = model.complete_time
        self.model_mode = model.model_mode
        self.primary_modality = model.primary_modality
        self.secondary_modality = model.secondary_modality
        self.model_yaml = model.model_yaml
        self.parent_model_id = model.parent_model_id
        self.version = model.version
        self.metrics_json = model.metrics_json
        self.publish_status = model.publish_status
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'dataset_id': self.dataset_id,
            'dataset_name': self.dataset_name,
            'base_model': self.base_model,
            'epochs': self.epochs,
            'batch_size': self.batch_size,
            'status': self.status,
            'model_file_url': self.model_file_url,
            'train_result': self.train_result,
            'create_time': self.create_time,
            'start_time': self.start_time,
            'complete_time': self.complete_time
            ,'model_mode': self.model_mode
            ,'primary_modality': self.primary_modality
            ,'secondary_modality': self.secondary_modality
            ,'model_yaml': self.model_yaml
            ,'parent_model_id': self.parent_model_id
            ,'version': self.version
            ,'metrics_json': self.metrics_json
            ,'publish_status': self.publish_status
        }

# 图片识别记录状态常量
DETECTION_STATUS = {
    'PROCESSING': 0,  # 识别中
    'COMPLETED': 1,   # 识别完成
    'FAILED': 2       # 识别失败
}

# 图片识别记录模型
class DetectionRecord:
    def __init__(self, db_record: Dict):
        self.id = db_record.get('id')
        self.user_id = db_record.get('user_id')
        self.model_id = db_record.get('model_id')
        self.model_name = db_record.get('model_name')
        self.original_image_bucket = db_record.get('original_image_bucket')
        self.original_image_object_key = db_record.get('original_image_object_key')
        self.result_image_bucket = db_record.get('result_image_bucket')
        self.result_image_object_key = db_record.get('result_image_object_key')
        self.detection_result = db_record.get('detection_result')
        self.confidence_threshold = db_record.get('confidence_threshold', 0.5)
        self.create_time = db_record.get('create_time')
        self.title = db_record.get('title')
        self.description = db_record.get('description')
        self.enhanced_image_bucket = db_record.get('enhanced_image_bucket')
        self.enhanced_image_object_key = db_record.get('enhanced_image_object_key')
        self.analysis_result = db_record.get('analysis_result')
        self.suggestions = db_record.get('suggestions')
        self.secondary_image_bucket = db_record.get('secondary_image_bucket')
        self.secondary_image_object_key = db_record.get('secondary_image_object_key')
        self.secondary_image_origin = db_record.get('secondary_image_origin')
        self.record_status = db_record.get('record_status', 'completed')
        self.ai_review_status = db_record.get('ai_review_status', 'not_requested')
        self.ai_review_result = db_record.get('ai_review_result')
        self.human_review_status = db_record.get('human_review_status', 'pending')
        self.corrected_annotations = db_record.get('corrected_annotations')
        self.reviewed_by = db_record.get('reviewed_by')
        self.reviewed_at = db_record.get('reviewed_at')
        self.feedback_dataset_id = db_record.get('feedback_dataset_id')
        self.review_version = db_record.get('review_version', 0)
        self.source_dataset_id = db_record.get('source_dataset_id')
        self.source_dataset_name = db_record.get('source_dataset_name')
        
        # 构建图片URL
        self.original_image_url = None
        if self.original_image_bucket and self.original_image_object_key:
            self.original_image_url = file_client.get_file_url(
                self.original_image_bucket, 
                self.original_image_object_key
            )
        
        self.result_image_url = None
        if self.result_image_bucket and self.result_image_object_key:
            self.result_image_url = file_client.get_file_url(
                self.result_image_bucket, 
                self.result_image_object_key
            )

        self.secondary_image_url = None
        if self.secondary_image_bucket and self.secondary_image_object_key:
            self.secondary_image_url = file_client.get_file_url(
                self.secondary_image_bucket,
                self.secondary_image_object_key
            )
        
        # 构建增强图片URL
        self.enhanced_image_url = None
        if self.enhanced_image_bucket and self.enhanced_image_object_key:
            self.enhanced_image_url = file_client.get_file_url(
                self.enhanced_image_bucket, 
                self.enhanced_image_object_key
            )
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'original_image_bucket': self.original_image_bucket,
            'original_image_object_key': self.original_image_object_key,
            'original_image_url': self.original_image_url,
            'result_image_bucket': self.result_image_bucket,
            'result_image_object_key': self.result_image_object_key,
            'result_image_url': self.result_image_url,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'create_time': self.create_time,
            'title': self.title,
            'description': self.description,
            'enhanced_image_bucket': self.enhanced_image_bucket,
            'enhanced_image_object_key': self.enhanced_image_object_key,
            'enhanced_image_url': self.enhanced_image_url,
            'analysis_result': self.analysis_result,
            'suggestions': self.suggestions
            ,'secondary_image_bucket': self.secondary_image_bucket
            ,'secondary_image_object_key': self.secondary_image_object_key
            ,'secondary_image_url': self.secondary_image_url
            ,'secondary_image_origin': self.secondary_image_origin
            ,'record_status': self.record_status
            ,'ai_review_status': self.ai_review_status
            ,'ai_review_result': self.ai_review_result
            ,'human_review_status': self.human_review_status
            ,'corrected_annotations': self.corrected_annotations
            ,'reviewed_by': self.reviewed_by
            ,'reviewed_at': self.reviewed_at
            ,'feedback_dataset_id': self.feedback_dataset_id
            ,'review_version': self.review_version
            ,'source_dataset_id': self.source_dataset_id
            ,'source_dataset_name': self.source_dataset_name
        }

# 图片识别记录信息VO
class DetectionRecordVO:
    def __init__(self, record: DetectionRecord, user_name: str = None):
        self.id = record.id
        self.user_id = record.user_id
        self.user_name = user_name  # 管理员可以看到用户真实姓名
        self.model_id = record.model_id
        self.model_name = record.model_name
        self.original_image_url = record.original_image_url
        self.result_image_url = record.result_image_url
        self.detection_result = record.detection_result
        self.confidence_threshold = record.confidence_threshold
        self.create_time = record.create_time
        self.title = record.title
        self.description = record.description
        self.enhanced_image_url = record.enhanced_image_url
        self.analysis_result = record.analysis_result
        self.suggestions = record.suggestions
        self.secondary_image_url = record.secondary_image_url
        self.secondary_image_origin = record.secondary_image_origin
        self.record_status = record.record_status
        self.ai_review_status = record.ai_review_status
        self.ai_review_result = record.ai_review_result
        self.human_review_status = record.human_review_status
        self.corrected_annotations = record.corrected_annotations
        self.reviewed_by = record.reviewed_by
        self.reviewed_at = record.reviewed_at
        self.feedback_dataset_id = record.feedback_dataset_id
        self.review_version = record.review_version
        self.source_dataset_id = record.source_dataset_id
        self.source_dataset_name = record.source_dataset_name
    
    def to_dict(self) -> Dict:
        result = {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'original_image_url': self.original_image_url,
            'result_image_url': self.result_image_url,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'create_time': self.create_time,
            'title': self.title,
            'description': self.description,
            'enhanced_image_url': self.enhanced_image_url,
            'analysis_result': self.analysis_result,
            'suggestions': self.suggestions
            ,'secondary_image_url': self.secondary_image_url
            ,'secondary_image_origin': self.secondary_image_origin
            ,'record_status': self.record_status
            ,'ai_review_status': self.ai_review_status
            ,'ai_review_result': self.ai_review_result
            ,'human_review_status': self.human_review_status
            ,'corrected_annotations': self.corrected_annotations
            ,'reviewed_by': self.reviewed_by
            ,'reviewed_at': self.reviewed_at
            ,'feedback_dataset_id': self.feedback_dataset_id
            ,'review_version': self.review_version
            ,'source_dataset_id': self.source_dataset_id
            ,'source_dataset_name': self.source_dataset_name
        }
        
        # 只有管理员才能看到用户真实姓名
        if self.user_name:
            result['user_name'] = self.user_name
            
        return result

# 视频识别记录状态常量
VIDEO_DETECTION_STATUS = {
    'PENDING': 0,     # 等待处理
    'PROCESSING': 1,  # 处理中
    'COMPLETED': 2,   # 处理完成
    'FAILED': 3       # 处理失败
}

# 视频识别记录模型
class VideoDetectionRecord:
    def __init__(self, db_record: Dict):
        self.id = db_record.get('id')
        self.user_id = db_record.get('user_id')
        self.model_id = db_record.get('model_id')
        self.model_name = db_record.get('model_name')
        self.original_video_bucket = db_record.get('original_video_bucket')
        self.original_video_object_key = db_record.get('original_video_object_key')
        self.result_video_bucket = db_record.get('result_video_bucket')
        self.result_video_object_key = db_record.get('result_video_object_key')
        self.detection_result = db_record.get('detection_result')
        self.confidence_threshold = db_record.get('confidence_threshold', 0.5)
        self.frame_rate = db_record.get('frame_rate', 30)
        self.total_frames = db_record.get('total_frames')
        self.processed_frames = db_record.get('processed_frames', 0)
        self.target_frames = db_record.get('target_frames')
        self.actual_processed_frames = db_record.get('actual_processed_frames', 0)
        self.create_time = db_record.get('create_time')
        self.start_time = db_record.get('start_time')
        self.complete_time = db_record.get('complete_time')
        self.status = db_record.get('status', 0)
        self.title = db_record.get('title')
        self.description = db_record.get('description')
        
        # 构建视频URL
        self.original_video_url = None
        if self.original_video_bucket and self.original_video_object_key:
            self.original_video_url = file_client.get_file_url(
                self.original_video_bucket, 
                self.original_video_object_key
            )
        
        self.result_video_url = None
        if self.result_video_bucket and self.result_video_object_key:
            self.result_video_url = file_client.get_file_url(
                self.result_video_bucket, 
                self.result_video_object_key
            )
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'original_video_bucket': self.original_video_bucket,
            'original_video_object_key': self.original_video_object_key,
            'original_video_url': self.original_video_url,
            'result_video_bucket': self.result_video_bucket,
            'result_video_object_key': self.result_video_object_key,
            'result_video_url': self.result_video_url,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'frame_rate': self.frame_rate,
            'total_frames': self.total_frames,
            'processed_frames': self.processed_frames,
            'target_frames': self.target_frames,
            'actual_processed_frames': self.actual_processed_frames,
            'create_time': self.create_time,
            'start_time': self.start_time,
            'complete_time': self.complete_time,
            'status': self.status,
            'title': self.title,
            'description': self.description
        }

# 视频识别记录信息VO
class VideoDetectionRecordVO:
    def __init__(self, record: VideoDetectionRecord, user_name: str = None):
        self.id = record.id
        self.user_id = record.user_id
        self.user_name = user_name  # 管理员可以看到用户真实姓名
        self.model_id = record.model_id
        self.model_name = record.model_name
        self.original_video_url = record.original_video_url
        self.result_video_url = record.result_video_url
        self.detection_result = record.detection_result
        self.confidence_threshold = record.confidence_threshold
        self.frame_rate = record.frame_rate
        self.total_frames = record.total_frames
        self.processed_frames = record.processed_frames
        self.target_frames = record.target_frames
        self.actual_processed_frames = record.actual_processed_frames
        self.create_time = record.create_time
        self.start_time = record.start_time
        self.complete_time = record.complete_time
        self.status = record.status
        self.title = record.title
        self.description = record.description
    
    def to_dict(self) -> Dict:
        result = {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'original_video_url': self.original_video_url,
            'result_video_url': self.result_video_url,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'frame_rate': self.frame_rate,
            'total_frames': self.total_frames,
            'processed_frames': self.processed_frames,
            'target_frames': self.target_frames,
            'actual_processed_frames': self.actual_processed_frames,
            'create_time': self.create_time,
            'start_time': self.start_time,
            'complete_time': self.complete_time,
            'status': self.status,
            'title': self.title,
            'description': self.description
        }
        
        # 只有管理员才能看到用户真实姓名
        if self.user_name:
            result['user_name'] = self.user_name
            
        return result

# 实时检测记录模型
class RealtimeDetectionRecord:
    def __init__(self, db_record: Dict):
        self.id = db_record.get('id')
        self.user_id = db_record.get('user_id')
        self.model_id = db_record.get('model_id')
        self.model_name = db_record.get('model_name')
        self.session_id = db_record.get('session_id')
        self.track_id = db_record.get('track_id')
        self.detection_result = db_record.get('detection_result')
        self.confidence_threshold = db_record.get('confidence_threshold', 0.5)
        self.detection_time = db_record.get('detection_time')
        self.result_image_bucket = db_record.get('result_image_bucket')
        self.result_image_object_key = db_record.get('result_image_object_key')
        self.bbox = db_record.get('bbox')
        self.class_name = db_record.get('class_name')
        self.confidence = db_record.get('confidence')
        self.create_time = db_record.get('create_time')
        
        # 构建结果图片URL
        self.result_image_url = None
        if self.result_image_bucket and self.result_image_object_key:
            self.result_image_url = file_client.get_file_url(
                self.result_image_bucket, 
                self.result_image_object_key
            )
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'session_id': self.session_id,
            'track_id': self.track_id,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'detection_time': self.detection_time,
            'result_image_bucket': self.result_image_bucket,
            'result_image_object_key': self.result_image_object_key,
            'result_image_url': self.result_image_url,
            'bbox': self.bbox,
            'class_name': self.class_name,
            'confidence': self.confidence,
            'create_time': self.create_time
        }

# 实时检测记录信息VO
class RealtimeDetectionRecordVO:
    def __init__(self, record: RealtimeDetectionRecord, user_name: str = None):
        self.id = record.id
        self.user_id = record.user_id
        self.user_name = user_name
        self.model_id = record.model_id
        self.model_name = record.model_name
        self.session_id = record.session_id
        self.track_id = record.track_id
        self.detection_result = record.detection_result
        self.confidence_threshold = record.confidence_threshold
        self.detection_time = record.detection_time
        self.result_image_url = record.result_image_url
        self.bbox = record.bbox
        self.class_name = record.class_name
        self.confidence = record.confidence
        self.create_time = record.create_time
    
    def to_dict(self) -> Dict:
        result = {
            'id': self.id,
            'user_id': self.user_id,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'session_id': self.session_id,
            'track_id': self.track_id,
            'detection_result': self.detection_result,
            'confidence_threshold': self.confidence_threshold,
            'detection_time': self.detection_time,
            'result_image_url': self.result_image_url,
            'bbox': self.bbox,
            'class_name': self.class_name,
            'confidence': self.confidence,
            'create_time': self.create_time
        }
        
        # 只有管理员才能看到用户真实姓名
        if self.user_name:
            result['user_name'] = self.user_name
            
        return result

# 密码加密
def encrypt_password(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest() 
