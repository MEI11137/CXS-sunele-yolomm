"""Single shared implementation of one training run.

The page routes and the business tool layer both start training through this
module so that:

* one machine runs at most one training at a time (no queue: a second request is
  refused while a lock is held),
* the worker's state is written to the ``models`` row it was created for, which
  makes a restart recoverable by reading the row instead of re-submitting a run,
* the weight, metrics and failure text land in the same columns either way.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
import psutil
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import TEMP_DIR, DB_PATH
from services.dataset_contract import (
    build_runtime_yaml,
    safe_extract_dataset,
    validate_dataset_archive,
)
from services.training_service import MULTIMODAL_BASELINE_YAML, TrainingRun, run_training
from utils.models import MODEL_STATUS


class TrainingBusy(RuntimeError):
    """Another training run currently owns the machine."""

    code = "TRAINING_BUSY"

    def __init__(self, holder: dict | None = None) -> None:
        self.holder = holder or {}
        super().__init__("本机正在训练其他模型，请等待该训练结束后再发起")


def _connect(database_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS training_lock (
            lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1),
            owner TEXT NOT NULL,
            holder_pid INTEGER,
            acquired_at TEXT NOT NULL
        )"""
    )
    if 'holder_started' not in {r[1] for r in conn.execute('PRAGMA table_info(training_lock)')}:
        conn.execute('ALTER TABLE training_lock ADD COLUMN holder_started REAL')
    if 'cancel_targets' not in {r[1] for r in conn.execute('PRAGMA table_info(training_lock)')}:
        conn.execute('ALTER TABLE training_lock ADD COLUMN cancel_targets TEXT')


def _lock_alive(row):
    value = dict(row)
    if _process_alive(value.get('holder_pid'), value.get('holder_started')):
        return True
    return any(_process_alive(item['pid'], item['started'])
               for item in json.loads(value.get('cancel_targets') or '[]'))


def _process_alive(pid: int | None, started: float | None = None) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(int(pid))
        return process.is_running() and (started is None or abs(process.create_time() - started) < .01)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _process_started(pid):
    try:
        return psutil.Process(int(pid)).create_time() if pid else None
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def read_lock(database_path: str | Path = DB_PATH) -> dict | None:
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM training_lock WHERE lock_id=1").fetchone()
    return dict(row) if row and _lock_alive(row) else None


def acquire_training_lock(
    database_path: str | Path, owner: str, holder_pid: int | None = None,
) -> None:
    """Take the machine-wide training slot or refuse with the current holder."""
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM training_lock WHERE lock_id=1").fetchone()
        if row is not None and _lock_alive(row):
            raise TrainingBusy(dict(row))
        conn.execute(
            "INSERT INTO training_lock (lock_id, owner, holder_pid, acquired_at, holder_started) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(lock_id) DO UPDATE SET "
            "owner=excluded.owner, holder_pid=excluded.holder_pid, "
            "acquired_at=excluded.acquired_at, holder_started=excluded.holder_started, cancel_targets=NULL",
            (str(owner), holder_pid, datetime.now().isoformat(), _process_started(holder_pid)),
        )


def set_lock_holder(database_path: str | Path, owner: str, pid: int) -> None:
    """Point the lock at the worker process so a dead worker can be reclaimed."""
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        changed = conn.execute(
            "UPDATE training_lock SET holder_pid=?,holder_started=? WHERE lock_id=1 AND owner=?",
            (int(pid), _process_started(pid), str(owner)),
        ).rowcount
        if not changed:
            raise RuntimeError('训练占用在进程移交前已改变')


def transfer_training_lock(database_path: str | Path, from_owner: str, to_owner: str) -> None:
    """Hand the slot from a reservation to the model row the worker will release."""
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE training_lock SET owner=? WHERE lock_id=1 AND owner=?",
            (str(to_owner), str(from_owner)),
        )


def release_training_lock(database_path: str | Path, owner: str) -> None:
    """Release only if this owner still holds the slot."""
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM training_lock WHERE lock_id=1 AND owner=?', (str(owner),)).fetchone()
        if row and row['cancel_targets'] and _lock_alive(row):
            return  # A cancelled worker must not release a slot with live children.
        conn.execute("DELETE FROM training_lock WHERE lock_id=1 AND owner=?", (str(owner),))


class TrainingCancelError(RuntimeError):
    """Cancellation could not be verified; keep the slot reserved."""


class TrainingLaunchUncertain(RuntimeError):
    """A spawned process could not be stopped; callers must retain its slot."""


def _run_after_handoff(ready, payload):
    # No GPU work or DataLoader children before the parent has recorded this PID.
    if ready.wait(30):
        run_training_job(**payload)


def launch_training_job(payload):
    """Shared by page and Agent; record ownership before allowing real training."""
    import multiprocessing
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=_run_after_handoff, args=(ready, payload))
    process.daemon = False
    try:
        process.start()
        set_lock_holder(payload['database_path'], payload['model_id'], process.pid)
        ready.set()
    except Exception:
        if process.pid:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=8)
                if process.is_alive():
                    raise TrainingLaunchUncertain('训练进程移交失败且未确认退出，保留训练占用，请核实')
            except (OSError, psutil.Error) as exc:
                raise TrainingLaunchUncertain('无法核实新建进程是否退出，保留训练占用，请核实') from exc
        raise
    return process


def cancel_training_job(database_path, model_id, reason, *, timeout=8):
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        row = conn.execute('SELECT * FROM training_lock WHERE lock_id=1').fetchone()
        model = conn.execute('SELECT status FROM models WHERE id=?', (model_id,)).fetchone()
        if not model or model['status'] != 0:
            raise TrainingCancelError('模型已不处于训练状态，请刷新真实结果')
        if not row or row['owner'] != model_id or not row['holder_pid'] or row['holder_started'] is None:
            raise TrainingCancelError('无法核实本模型的训练进程，未标记取消或释放训练槽')
        lock = dict(row)
    if lock['holder_pid'] == os.getpid():
        raise TrainingCancelError('训练进程尚未完成移交，不能终止网站服务，请稍后核实')
    targets = json.loads(lock.get('cancel_targets') or '[]')
    try:
        if _process_alive(lock['holder_pid'], lock['holder_started']):
            parent = psutil.Process(lock['holder_pid'])
            if abs(parent.create_time() - lock['holder_started']) >= .01:
                raise TrainingCancelError('进程身份已变化，未发送终止信号')
            targets += [{'pid': p.pid, 'started': p.create_time()}
                        for p in [parent, *parent.children(recursive=True)]]
        targets = list({(item['pid'], item['started']): item for item in targets}.values())
        if any(item['pid'] == os.getpid() for item in targets):
            raise TrainingCancelError('目标包含网站服务进程，拒绝取消')
        with closing(_connect(database_path)) as conn, conn:
            conn.execute('UPDATE training_lock SET cancel_targets=? WHERE lock_id=1 AND owner=?',
                         (json.dumps(targets), model_id))
        processes = []
        for item in targets:
            if _process_alive(item['pid'], item['started']):
                process = psutil.Process(item['pid'])
                if abs(process.create_time() - item['started']) >= .01:
                    raise TrainingCancelError('目标进程身份已变化')
                processes.append(process)
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=timeout)
        if alive or any(_process_alive(item['pid'], item['started']) for item in targets):
            raise TrainingCancelError('训练进程尚未全部退出，未标记取消，训练槽继续保留')
    except (psutil.Error, OSError) as exc:
        raise TrainingCancelError('终止训练进程失败，未标记取消，训练槽继续保留') from exc
    result = {'canceled_by_user': True, 'reason': str(reason), 'canceled_at': datetime.now().isoformat()}
    with closing(_connect(database_path)) as conn, conn:
        changed = conn.execute('UPDATE models SET status=4,train_result=?,complete_time=? WHERE id=? AND status IN (0,2)',
                               (json.dumps(result, ensure_ascii=False), datetime.now().isoformat(), model_id)).rowcount
    release_training_lock(database_path, model_id)
    if not changed:
        raise TrainingCancelError('进程已退出，但模型已完成或状态改变，请刷新；未覆盖已有结果')
    return {'model_id': model_id, 'canceled': True, 'process_terminated': True,
            'train_result': result, 'status': 'cancelled'}


def reconcile_training_jobs(database_path):
    """A dead worker means interrupted; never restart training automatically."""
    changed = 0
    with closing(_connect(database_path)) as conn, conn:
        _ensure_table(conn)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(models)')}
        if not {'id','status','train_result','complete_time'} <= columns:
            return 0
        lock = conn.execute('SELECT * FROM training_lock WHERE lock_id=1').fetchone()
        alive = lock and _lock_alive(lock)
        for row in conn.execute('SELECT id FROM models WHERE status=0').fetchall():
            if alive and lock['owner'] == row['id']:
                continue
            conn.execute('UPDATE models SET status=4,complete_time=?,train_result=? WHERE id=? AND status=0',
                         (datetime.now().isoformat(), json.dumps({'error':'后端启动核实：未发现对应训练进程，训练已中断；未自动重做'}, ensure_ascii=False), row['id']))
            changed += 1
        if lock and not alive:
            conn.execute('DELETE FROM training_lock WHERE lock_id=1')
    return changed


def run_training_job(
    model_id: str,
    dataset_id: str,
    base_model: str,
    epochs: int,
    batch_size: int,
    parent_model_id: str | None = None,
    database_path: str | Path = DB_PATH,
    file_store_path: str | Path | None = None,
    temp_root: str | Path | None = None,
    project_id: str | None = None,
) -> dict:
    from services.project_storage import resource_scope
    try:
        with resource_scope(database_path, 'models', model_id, project_id):
            return _run_training_job(model_id, dataset_id, base_model, epochs, batch_size,
                                     parent_model_id, database_path, file_store_path, temp_root,
                                     project_id)
    except (OSError, ValueError) as exc:
        from utils.db import Database
        Database(database_path).execute('UPDATE models SET status=?,complete_time=?,train_result=? WHERE id=?',
            (MODEL_STATUS['FAILED'], datetime.now().isoformat(), json.dumps({'error': str(exc)}, ensure_ascii=False), model_id))
        return {'model_id': model_id, 'status': 'failed', 'error': str(exc)}
    finally:
        release_training_lock(database_path, model_id)


def _run_training_job(
    model_id, dataset_id, base_model, epochs, batch_size, parent_model_id,
    database_path, file_store_path, temp_root, project_id=None,
) -> dict:
    """Execute one run for an already registered model row.

    Runs in a worker process, so it opens its own database connection and its
    own storage client. Every outcome is persisted; nothing is retried.

    三个路径都必须与启动 worker 的 app 保持一致：``database_path`` 决定结果写进
    哪个库，``file_store_path`` 决定权重与数据集从哪读，``temp_root`` 决定解压目录。
    用模块默认值会在 app 覆盖了配置（测试/多实例）时把训练结果写到另一份库里。
    """
    from clients.file_client import FileClient
    from utils.db import Database

    database = Database(database_path)
    storage = FileClient(base_path=file_store_path, database_path=database_path)
    from services.project_storage import temp_directory, directory, current_scope
    base_temp = str(temp_directory(temp_root or TEMP_DIR))
    temp_dir = os.path.join(base_temp, f"model_training_{model_id}_{uuid.uuid4().hex[:8]}")
    output_dir = directory('training/' + model_id, os.path.join(temp_dir, 'output'))
    outcome: dict = {"model_id": model_id, "status": "failed"}
    try:
        database.execute(
            "UPDATE models SET status=?, start_time=? WHERE id=?",
            (MODEL_STATUS["TRAINING"], datetime.now().isoformat(), model_id),
        )
        dataset = database.query(
            "SELECT zip_bucket, zip_object_key, dataset_mode, primary_modality, "
            "secondary_modality FROM datasets WHERE id=?",
            (dataset_id,), fetchone=True,
        )
        if not dataset:
            raise RuntimeError("数据集不存在")

        os.makedirs(temp_dir, exist_ok=True)
        dataset_path = os.path.join(temp_dir, "dataset.zip")
        with open(dataset_path, "wb") as handle:
            handle.write(storage.get(dataset["zip_bucket"], dataset["zip_object_key"]))

        model_mode = dataset.get("dataset_mode") or "single"
        validation = validate_dataset_archive(dataset_path, model_mode)
        extracted_root = safe_extract_dataset(dataset_path, os.path.join(temp_dir, "dataset"))
        runtime_yaml = build_runtime_yaml(
            extracted_root, validation, os.path.join(temp_dir, "data.runtime.yaml")
        )

        if parent_model_id:
            parent = database.query(
                "SELECT model_bucket, model_object_key, model_mode, model_yaml "
                "FROM models WHERE id=?", (parent_model_id,), fetchone=True,
            )
            if not parent or not parent.get("model_object_key"):
                raise RuntimeError("父模型权重不存在")
            if (parent.get("model_mode") or "single") != model_mode:
                raise RuntimeError("父模型与反馈数据集模式不一致")
            model_source = os.path.join(temp_dir, "parent_model.pt")
            with open(model_source, "wb") as handle:
                handle.write(storage.get(parent["model_bucket"], parent["model_object_key"]))
            model_yaml = parent.get("model_yaml")
        elif model_mode == "multimodal":
            model_source = str(MULTIMODAL_BASELINE_YAML)
            model_yaml = model_source
        else:
            model_source = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "weights", base_model)
            )
            model_yaml = None
            if not os.path.exists(model_source):
                raise RuntimeError(f"基础模型文件不存在: {model_source}")

        training_result = run_training(TrainingRun(
            model_mode=model_mode,
            model_source=model_source,
            model_yaml=model_yaml,
            runtime_yaml=runtime_yaml,
            epochs=epochs,
            batch_size=batch_size,
            output_dir=output_dir,
            secondary_modality=dataset.get("secondary_modality"),
        ))
        upload = storage.upload("models", training_result.best_weight_path)
        database.execute(
            "UPDATE models SET status=?, complete_time=?, model_bucket=?, "
            "model_object_key=?, train_result=?, metrics_json=? WHERE id=?",
            (
                MODEL_STATUS["COMPLETED"], datetime.now().isoformat(),
                upload["bucket"], upload["objectKey"],
                json.dumps({
                    "final_epoch": epochs,
                    "results_dict": training_result.metrics,
                    "training_metrics": [],
                }, ensure_ascii=False),
                json.dumps(training_result.metrics, ensure_ascii=False),
                model_id,
            ),
        )
        outcome["status"] = "completed"
        outcome["metrics"] = training_result.metrics
        # 训练产出的子模型必须登记到“发起训练的这个项目”。不登记的话，只有下次
        # 启动时的历史资源迁移会把它塞进管理员的历史项目，Agent 的 model.get_state /
        # training.get_state 就查不到（表现为“资源不存在或未登记授权”）。
        try:
            from agent import resources
            resources.ensure("models", model_id, project_id=project_id)
        except Exception:
            pass
        if current_scope():
            (Path(output_dir) / 'metrics.json').write_text(json.dumps(training_result.metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        try:
            database.execute(
                "UPDATE models SET status=?, complete_time=?, train_result=? WHERE id=?",
                (MODEL_STATUS["FAILED"], datetime.now().isoformat(),
                 json.dumps({"error": str(exc)}, ensure_ascii=False), model_id),
            )
        finally:
            outcome["error"] = str(exc)
            if current_scope():
                (Path(output_dir) / 'failure.json').write_text(json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        release_training_lock(database_path, model_id)
    return outcome
