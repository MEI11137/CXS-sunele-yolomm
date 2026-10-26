
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
from utils.resource_guard import register_new, resource_required, resources_required, visible_ids
from utils.models import Model, ModelVO, MODEL_STATUS
from config import DB_PATH, TEMP_DIR, YOLO_CLASSES_CONFIG
from services.dataset_contract import (
    build_runtime_yaml,
    safe_extract_dataset,
    validate_dataset_archive,
)
from services.training_service import (
    MULTIMODAL_BASELINE_YAML,
    TrainingRun,
    run_training,
)
from services.training_runner import (
    TrainingBusy,
    acquire_training_lock,
    release_training_lock,
    run_training_job,
    set_lock_holder,
)
from services.model_registry_service import (
    ModelRegistryError,
    compare_models,
    publish_model as publish_registered_model,
    start_incremental_training,
)
from services.model_import_service import (
    ModelImportError,
    import_model_weight as register_imported_model,
)
from services.yolo_runtime import load_detector

model_bp = Blueprint('model', __name__, url_prefix='/api/model')


def _bump_agent_revision(model_id):
    """发布/撤销发布后递增 Agent 索引版本，使旧预览与旧确认失效。"""
    try:
        from agent import resources

        resources.bump('models', model_id)
    except Exception:
        pass

# 全局训练进程字典，用于管理训练进程
training_processes = {}

def run_training_process(
    model_id, dataset_id, base_model, epochs, batch_size, parent_model_id=None
):
    """在后台进程中运行模型训练（实现见 services/training_runner）。"""
    try:
        run_training_job(
            model_id, dataset_id, base_model, epochs, batch_size, parent_model_id,
        )
    finally:
        # 从训练进程字典中移除已完成的进程
        training_processes.pop(model_id, None)


def _lock_db_path():
    """训练占用必须记录在本次请求实际写入的数据库上。

    Agent 侧（``services/business_tools``）与页面侧共用同一张
    ``training_lock`` 表；若这里固定用模块级 ``DB_PATH``，测试或换库部署时
    两侧会各自持锁，"单机单训练"就不再成立。
    """
    return getattr(db, 'db_path', None) or DB_PATH


def _mark_training_taken_slot(lock_path, model_id, process):
    """把占用指向真正的训练进程，便于崩溃后按 pid 回收。

    拿不到 worker pid 时（进程对象未暴露 pid）无法在异常后判断占用是否还活着，
    此时释放占用而不是留下一个回收不了的死锁。
    """
    worker_pid = getattr(process, 'pid', None)
    if worker_pid:
        set_lock_holder(lock_path, model_id, worker_pid)
    else:
        release_training_lock(lock_path, model_id)

@model_bp.route('/create', methods=['POST'])
@token_required
def create_model():
    """创建新模型并开始训练"""
    try:
        data = request.json
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        dataset_id = data.get('dataset_id', '').strip()
        base_model = data.get('base_model', 'yolo11n.pt').strip()
        epochs = int(data.get('epochs', 50))
        batch_size = int(data.get('batch_size', 16))
        
        if not name:
            return error("模型名称不能为空")
        
        if not dataset_id:
            return error("必须选择数据集")
            
        # 检查模型名称是否已存在
        existing_model = db.query("SELECT id FROM models WHERE name = ?", (name,), fetchone=True)
        if existing_model:
            return error("模型名称已存在")
            
        # 检查数据集是否存在
        dataset_data = db.query('''
            SELECT id, dataset_mode, primary_modality, secondary_modality
            FROM datasets WHERE id = ?
        ''', (dataset_id,), fetchone=True)
        if not dataset_data:
            return error("选择的数据集不存在")

        # 训练用的是别人的数据集也算越权：先按公共规则确认可读
        from utils.resource_guard import check_resource

        blocked = check_resource('dataset', dataset_id, 'read')
        if blocked is not None:
            return blocked

        model_mode = dataset_data.get('dataset_mode') or 'single'
        primary_modality = dataset_data.get('primary_modality') or 'rgb'
        secondary_modality = dataset_data.get('secondary_modality')
        model_yaml = str(MULTIMODAL_BASELINE_YAML) if model_mode == 'multimodal' else None
            
        # 创建模型记录
        model_id = str(uuid.uuid4())
        
        affected_rows = db.execute('''
            INSERT INTO models (id, name, description, dataset_id, base_model,
                              epochs, batch_size, status, create_time,
                              model_mode, primary_modality, secondary_modality,
                              model_yaml, version, publish_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            model_id, name, description, dataset_id, base_model,
            epochs, batch_size, MODEL_STATUS['TRAINING'], datetime.now().isoformat(),
            model_mode, primary_modality, secondary_modality, model_yaml, 1, 'draft'
        ))
        
        if affected_rows > 0:
            lock_path = _lock_db_path()
            # 单机单训练占用：忙时明确拒绝，不排队
            try:
                acquire_training_lock(lock_path, model_id, os.getpid())
            except TrainingBusy as exc:
                try:
                    db.execute('DELETE FROM models WHERE id = ?', (model_id,))
                except Exception:
                    pass
                return error(str(exc), 409), 409
            # 创建并启动训练进程
            training_process = multiprocessing.Process(
                target=run_training_process,
                args=(model_id, dataset_id, base_model, epochs, batch_size)
            )
            training_process.daemon = True
            training_processes[model_id] = training_process
            try:
                training_process.start()
            except Exception:
                release_training_lock(lock_path, model_id)
                db.execute('DELETE FROM models WHERE id = ?', (model_id,))
                raise
            _mark_training_taken_slot(lock_path, model_id, training_process)

            register_new('model', model_id, display_name=name)

            return success({
                'id': model_id,
                'name': name,
                'description': description,
                'dataset_id': dataset_id,
                'base_model': base_model,
                'epochs': epochs,
                'batch_size': batch_size,
                'status': MODEL_STATUS['TRAINING'],
                'model_mode': model_mode,
                'primary_modality': primary_modality,
                'secondary_modality': secondary_modality,
                'model_yaml': model_yaml,
                'version': 1,
                'publish_status': 'draft',
                'create_time': datetime.now().isoformat()
            }, "模型创建成功，开始训练")
        else:
            return error("创建模型失败")
            
    except Exception as e:
        return error(f"创建模型失败: {str(e)}")


@model_bp.route('/import', methods=['POST'])
@token_required
def import_model():
    """导入并验证外部训练得到的模型权重。"""
    try:
        imported = register_imported_model(
            request.form.get('name', ''),
            request.form.get('description', ''),
            request.form.get('dataset_id', ''),
            request.files.get('weight'),
            db,
            file_client,
            load_detector,
        )
        register_new(
            'model',
            imported.to_dict().get('id'),
            display_name=request.form.get('name', ''),
        )
        return success(imported.to_dict(), "模型权重导入成功，请确认后发布")
    except ModelImportError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"导入模型权重失败: {str(exc)}", 500), 500

@model_bp.route('/list', methods=['GET'])
@token_required
def list_models():
    """获取模型列表"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        search = request.args.get('search', '').strip()
        
        # 构建查询条件
        conditions = []
        params = []

        # 普通用户只看本人（及系统默认只读）模型；管理员不过滤
        allowed = visible_ids('model')
        if allowed is not None:
            if not allowed:
                return success({'models': [], 'total': 0, 'page': page, 'page_size': page_size})
            conditions.append('m.id IN (%s)' % ','.join('?' for _ in allowed))
            params.extend(sorted(allowed))
        
        if search:
            conditions.append("(m.name LIKE ? OR m.description LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%'])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 获取总数
        count_sql = f"SELECT COUNT(*) as total FROM models m{where_clause}"
        total_result = db.query(count_sql, tuple(params), fetchone=True)
        total = total_result['total'] if total_result else 0
        
        # 获取数据
        offset = (page - 1) * page_size
        data_sql = f'''
            SELECT m.*, d.name as dataset_name
            FROM models m
            LEFT JOIN datasets d ON m.dataset_id = d.id
            {where_clause}
            ORDER BY m.create_time DESC
            LIMIT {page_size} OFFSET {offset}
        '''
        
        model_list = db.query(data_sql, tuple(params))
        
        models = []
        for row in model_list:
            model = Model(row)
            model_vo = ModelVO(model, row.get('dataset_name'))
            models.append(model_vo.to_dict())
        
        return success({
            'models': models,
            'total': total,
            'page': page,
            'page_size': page_size
        })
        
    except Exception as e:
        return error(f"获取模型列表失败: {str(e)}")

@model_bp.route('/<model_id>', methods=['GET'])
@resource_required('model', 'model_id', 'read')
def get_model(model_id):
    """获取模型详情"""
    try:
        model_data = db.query('''
            SELECT m.*, d.name as dataset_name
            FROM models m
            LEFT JOIN datasets d ON m.dataset_id = d.id
            WHERE m.id = ?
        ''', (model_id,), fetchone=True)
        
        if not model_data:
            return error("模型不存在", 404)
            
        model = Model(model_data)
        model_vo = ModelVO(model, model_data.get('dataset_name'))
        
        return success(model_vo.to_dict())
        
    except Exception as e:
        return error(f"获取模型详情失败: {str(e)}")

@model_bp.route('/<model_id>', methods=['PUT'])
@resource_required('model', 'model_id', 'write')
def update_model(model_id):
    """更新模型信息"""
    try:
        data = request.json
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        
        if not name:
            return error("模型名称不能为空")
            
        # 检查模型是否存在
        existing_model = db.query("SELECT id FROM models WHERE id = ?", (model_id,), fetchone=True)
        if not existing_model:
            return error("模型不存在", 404)
            
        # 检查名称是否与其他模型重复
        duplicate_model = db.query("SELECT id FROM models WHERE name = ? AND id != ?", (name, model_id), fetchone=True)
        if duplicate_model:
            return error("模型名称已存在")
            
        # 更新模型信息
        affected_rows = db.execute('''
            UPDATE models SET name = ?, description = ?
            WHERE id = ?
        ''', (name, description, model_id))
        
        if affected_rows > 0:
            return success(None, "模型信息更新成功")
        else:
            return error("更新失败")
        
    except Exception as e:
        return error(f"更新模型信息失败: {str(e)}")

@model_bp.route('/<model_id>', methods=['DELETE'])
@resource_required('model', 'model_id', 'write')
def delete_model(model_id):
    """删除模型"""
    try:
        # 获取模型信息
        model_data = db.query('''
            SELECT model_bucket, model_object_key FROM models 
            WHERE id = ?
        ''', (model_id,), fetchone=True)
        
        if not model_data:
            return error("模型不存在", 404)
        
        # 删除文件服务中的模型文件
        if model_data['model_bucket'] and model_data['model_object_key']:
            try:
                file_client.delete(model_data['model_bucket'], model_data['model_object_key'])
            except Exception as e:
                print(f"删除模型文件失败: {e}")
        
        # 删除模型记录
        affected_rows = db.execute('''
            DELETE FROM models WHERE id = ?
        ''', (model_id,))
        
        if affected_rows > 0:
            return success(None, "模型删除成功")
        else:
            return error("删除失败")
        
    except Exception as e:
        return error(f"删除模型失败: {str(e)}")

@model_bp.route('/<model_id>/publish', methods=['POST'])
@resource_required('model', 'model_id', 'publish')
def publish_model(model_id):
    """原子发布模型，只归档本人同一项目内的同模态旧版本。"""
    try:
        from agent.resources import model_scope_ids

        result = publish_registered_model(
            model_id, g.user_id, db, scope_model_ids=model_scope_ids(model_id),
        )
        _bump_agent_revision(model_id)
        return success(result.to_dict(), "模型发布成功")
    except ModelRegistryError as exc:
        return error(str(exc), 400), 400
    except Exception as e:
        return error(f"发布模型失败: {str(e)}")


@model_bp.route('/<parent_model_id>/incremental', methods=['POST'])
@resource_required('model', 'parent_model_id', 'read')
def create_incremental_model(parent_model_id):
    """创建独立子模型记录，并从父权重启动反馈数据集训练。"""
    try:
        payload = request.get_json(silent=True) or {}
        feedback_dataset_id = str(payload.get('feedback_dataset_id') or '').strip()
        if not feedback_dataset_id:
            return error("feedback_dataset_id 不能为空", 400), 400
        child = start_incremental_training(
            parent_model_id,
            feedback_dataset_id,
            payload,
            db,
        )
        lock_path = _lock_db_path()
        try:
            acquire_training_lock(lock_path, child.model_id, os.getpid())
        except TrainingBusy as exc:
            try:
                db.execute('DELETE FROM models WHERE id = ?', (child.model_id,))
            except Exception:
                pass
            return error(str(exc), 409), 409
        process = multiprocessing.Process(
            target=run_training_process,
            args=(
                child.model_id,
                child.dataset_id,
                '',
                child.epochs,
                child.batch_size,
                parent_model_id,
            ),
        )
        process.daemon = True
        training_processes[child.model_id] = process
        try:
            process.start()
        except Exception:
            release_training_lock(lock_path, child.model_id)
            db.execute('DELETE FROM models WHERE id = ?', (child.model_id,))
            raise
        _mark_training_taken_slot(lock_path, child.model_id, process)
        return success(child.to_dict(), "增量训练已启动")
    except ModelRegistryError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"启动增量训练失败: {str(exc)}")


@model_bp.route('/<parent_model_id>/compare/<child_model_id>', methods=['GET'])
@resources_required([('model','parent_model_id','read'), ('model','child_model_id','read')])
def compare_model_versions(parent_model_id, child_model_id):
    """比较父子模型指标；缺失值保持 unknown。"""
    try:
        result = compare_models(parent_model_id, child_model_id, db)
        return success(result.to_dict())
    except ModelRegistryError as exc:
        return error(str(exc), 400), 400
    except Exception as exc:
        return error(f"模型指标比较失败: {str(exc)}")

@model_bp.route('/<model_id>/unpublish', methods=['POST'])
@resource_required('model', 'model_id', 'publish')
def unpublish_model(model_id):
    """取消发布模型"""
    try:
        # 检查模型是否存在且已发布
        model_data = db.query('''
            SELECT id, status FROM models WHERE id = ?
        ''', (model_id,), fetchone=True)
        
        if not model_data:
            return error("模型不存在", 404)
            
        if model_data['status'] != MODEL_STATUS['PUBLISHED']:
            return error("只能取消发布已发布的模型")
        
        # 更新模型状态为训练完成
        affected_rows = db.execute('''
            UPDATE models SET status = ? WHERE id = ?
        ''', (MODEL_STATUS['COMPLETED'], model_id))
        
        if affected_rows > 0:
            _bump_agent_revision(model_id)
            return success(None, "取消发布成功")
        else:
            return error("取消发布失败")
        
    except Exception as e:
        return error(f"取消发布模型失败: {str(e)}")

@model_bp.route('/base-models', methods=['GET'])
@token_required
def list_base_models():
    """获取可用的基础模型列表"""
    try:
        weights_dir = 'weights'
        base_models = []
        
        if os.path.exists(weights_dir):
            for filename in os.listdir(weights_dir):
                if filename.endswith('.pt'):
                    base_models.append(filename)
        
        return success({
            'base_models': base_models
        })
        
    except Exception as e:
        return error(f"获取基础模型列表失败: {str(e)}")

@model_bp.route('/classes', methods=['GET'])
def get_yolo_classes():
    """获取YOLO类别配置信息"""
    try:
        return success({
            'classes': YOLO_CLASSES_CONFIG
        })
    except Exception as e:
        return error(f"获取类别配置失败: {str(e)}")


"""
版权所有 © 2025 羊羊小栈 (GJQ)

作者：羊羊小栈
时间：2025-09-06 23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需要立即停止侵权行为，并按照【羊羊小栈系统版权声明和保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""
