

import sqlite3
from pathlib import Path
from config import DB_PATH
from utils.schema_migrations import run_schema_migrations

class Database:
    def __init__(self, db_path: str | Path | None = None, *, seed_demo: bool = False):
        # 允许显式指定库路径：worker 进程必须和当前 app 用同一个库，
        # 否则训练结果会写进默认库，页面查不到。
        self.db_path = str(db_path) if db_path else DB_PATH
        self.seed_demo = seed_demo
        self.initialize_db()

    def initialize_db(self):
        """初始化数据库，创建表结构"""
        # 显式指定的库路径可能所在目录还不存在；先建目录再连
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # 正式安装默认为空业务库；仅隔离演示/测试显式选择种子，重启不补回删除项。
        seed_demo = self.seed_demo and cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasets'").fetchone() is None
        
        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) NOT NULL UNIQUE,
            password VARCHAR(100) NOT NULL,
            real_name VARCHAR(50),
            phone VARCHAR(20),
            email VARCHAR(100),
            avatar_bucket VARCHAR(50),
            avatar_object_key VARCHAR(255),
            role INTEGER NOT NULL DEFAULT 0,
            status INTEGER NOT NULL DEFAULT 1,
            create_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建数据集表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            zip_bucket TEXT NOT NULL,
            zip_object_key TEXT NOT NULL,
            structure_info TEXT,
            upload_time TEXT NOT NULL,
            file_size INTEGER,
            status INTEGER DEFAULT 0
        )
        ''')
        
        # 创建模型表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            dataset_id TEXT NOT NULL,
            base_model TEXT NOT NULL,
            epochs INTEGER NOT NULL DEFAULT 50,
            batch_size INTEGER NOT NULL DEFAULT 16,
            status SMALLINT DEFAULT 0,
            model_bucket TEXT,
            model_object_key TEXT,
            train_result TEXT,
            create_time TEXT NOT NULL,
            start_time TEXT,
            complete_time TEXT,
            FOREIGN KEY (dataset_id) REFERENCES datasets (id)
        )
        ''')
        
        # 创建图片识别记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS detection_records (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            original_image_bucket TEXT NOT NULL,
            original_image_object_key TEXT NOT NULL,
            result_image_bucket TEXT,
            result_image_object_key TEXT,
            detection_result TEXT,
            confidence_threshold REAL DEFAULT 0.5,
            create_time TEXT NOT NULL,
            title TEXT,
            description TEXT,
            enhanced_image_bucket TEXT,
            enhanced_image_object_key TEXT,
            analysis_result TEXT,
            suggestions TEXT,
            FOREIGN KEY (user_id) REFERENCES user (id),
            FOREIGN KEY (model_id) REFERENCES models (id)
        )
        ''')
        
        # 创建视频识别记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS video_detection_records (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            original_video_bucket TEXT NOT NULL,
            original_video_object_key TEXT NOT NULL,
            result_video_bucket TEXT,
            result_video_object_key TEXT,
            detection_result TEXT,
            confidence_threshold REAL DEFAULT 0.5,
            frame_rate INTEGER DEFAULT 30,
            total_frames INTEGER,
            processed_frames INTEGER,
            target_frames INTEGER,
            actual_processed_frames INTEGER DEFAULT 0,
            create_time TEXT NOT NULL,
            start_time TEXT,
            complete_time TEXT,
            status INTEGER DEFAULT 0,
            title TEXT,
            description TEXT,
            FOREIGN KEY (user_id) REFERENCES user (id),
            FOREIGN KEY (model_id) REFERENCES models (id)
        )
        ''')
        
        # 创建实时检测记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS realtime_detection_records (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            track_id INTEGER,
            detection_result TEXT,
            confidence_threshold REAL DEFAULT 0.5,
            detection_time TEXT NOT NULL,
            result_image_bucket TEXT,
            result_image_object_key TEXT,
            bbox TEXT,
            class_name TEXT,
            confidence REAL,
            create_time TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES user (id),
            FOREIGN KEY (model_id) REFERENCES models (id)
        )
        ''')
        
        # 检查是否有管理员用户，如果没有则创建
        cursor.execute("SELECT COUNT(*) FROM user WHERE username = 'admin'")
        if cursor.fetchone()[0] == 0:
            # 添加管理员用户(初始密码：123456)
            cursor.execute('''
            INSERT INTO user (username, password, real_name, phone, email, role, status)
            VALUES ('admin', 'e10adc3949ba59abbe56e057f20f883e', '系统管理员', '12345678901', 'admin@example.com', 1, 1)
            ''')
            
            # 添加测试用户(初始密码：123456)
            cursor.execute('''
            INSERT INTO user (username, password, real_name, phone, email, role, status)
            VALUES ('test', 'e10adc3949ba59abbe56e057f20f883e', '测试用户', '12345678902', 'test@example.com', 0, 1)
            ''')

        # 检查是否有默认数据集，如果没有则创建
        cursor.execute("SELECT COUNT(*) FROM datasets WHERE id = 'default-dataset-001'")
        if cursor.fetchone()[0] == 0 and seed_demo:
            # 添加默认数据集
            cursor.execute('''
            INSERT INTO datasets (id, name, description, zip_bucket, zip_object_key, structure_info, upload_time, file_size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                'default-dataset-001',
                '光伏板缺陷检测数据集',
                '完整的光伏板缺陷检测数据集，包含4类缺陷：鸟粪(bird_shit)、裂纹(crack)、污垢(dirt)、面板(panel)。训练集2620张，验证集328张，测试集328张。',
                'datasets',
                'default_dataset.zip',
                '{"total_files": 3276, "train_images": 2620, "train_labels": 2620, "valid_images": 328, "valid_labels": 328, "test_images": 328, "test_labels": 328}',
                '2025-09-10 00:00:00',
                329604813,
                1  # 已验证通过
            ))
            
        # 检查是否有默认模型，如果没有则创建
        cursor.execute("SELECT COUNT(*) FROM models WHERE id = 'default-model-001'")
        if cursor.fetchone()[0] == 0 and seed_demo:
            # 添加默认模型
            cursor.execute('''
            INSERT INTO models (id, name, description, dataset_id, base_model, epochs, batch_size, status, model_bucket, model_object_key, train_result, create_time, start_time, complete_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                'default-model-001',
                '光伏板缺陷检测模型 v1.0',
                '基于YOLO12n训练的光伏板缺陷检测模型，在验证集上达到mAP50: 0.539，precision: 0.565，recall: 0.579的性能，可以有效检测光伏板表面的鸟粪、裂纹、污垢等缺陷。',
                'default-dataset-001',
                'yolo12n',
                300,
                16,
                3,  # 已发布
                'models',
                'default_model.pt',
                '{"final_epoch":300,"results_dict":{"metrics/precision(B)":0.67521,"metrics/recall(B)":0.5641,"metrics/mAP50(B)":0.55452,"metrics/mAP50-95(B)":0.37769,"fitness":0.37769},"training_metrics":[{"epoch":10.0,"time":289.806,"train/box_loss":1.25534,"train/cls_loss":1.56063,"train/dfl_loss":1.51378,"metrics/precision(B)":0.40457,"metrics/recall(B)":0.4127,"metrics/mAP50(B)":0.38246,"metrics/mAP50-95(B)":0.23155,"val/box_loss":1.37388,"val/cls_loss":1.7376,"val/dfl_loss":1.65947,"lr/pg0":0.009703,"lr/pg1":0.009703,"lr/pg2":0.009703},{"epoch":20.0,"time":571.402,"train/box_loss":1.1341,"train/cls_loss":1.32151,"train/dfl_loss":1.43701,"metrics/precision(B)":0.44713,"metrics/recall(B)":0.45708,"metrics/mAP50(B)":0.44099,"metrics/mAP50-95(B)":0.27694,"val/box_loss":1.27525,"val/cls_loss":1.59785,"val/dfl_loss":1.54164,"lr/pg0":0.009373,"lr/pg1":0.009373,"lr/pg2":0.009373},{"epoch":30.0,"time":852.899,"train/box_loss":1.08557,"train/cls_loss":1.21582,"train/dfl_loss":1.39407,"metrics/precision(B)":0.52178,"metrics/recall(B)":0.45994,"metrics/mAP50(B)":0.48277,"metrics/mAP50-95(B)":0.30265,"val/box_loss":1.25907,"val/cls_loss":1.45986,"val/dfl_loss":1.55436,"lr/pg0":0.009043,"lr/pg1":0.009043,"lr/pg2":0.009043},{"epoch":40.0,"time":1134.08,"train/box_loss":1.04421,"train/cls_loss":1.13233,"train/dfl_loss":1.35975,"metrics/precision(B)":0.58373,"metrics/recall(B)":0.46701,"metrics/mAP50(B)":0.48885,"metrics/mAP50-95(B)":0.31385,"val/box_loss":1.28431,"val/cls_loss":1.44088,"val/dfl_loss":1.55458,"lr/pg0":0.008713,"lr/pg1":0.008713,"lr/pg2":0.008713},{"epoch":50.0,"time":1415.67,"train/box_loss":1.01423,"train/cls_loss":1.07141,"train/dfl_loss":1.34244,"metrics/precision(B)":0.49709,"metrics/recall(B)":0.51277,"metrics/mAP50(B)":0.48357,"metrics/mAP50-95(B)":0.31563,"val/box_loss":1.2229,"val/cls_loss":1.40872,"val/dfl_loss":1.50652,"lr/pg0":0.008383,"lr/pg1":0.008383,"lr/pg2":0.008383},{"epoch":60.0,"time":1696.52,"train/box_loss":0.9979,"train/cls_loss":1.0166,"train/dfl_loss":1.31528,"metrics/precision(B)":0.59913,"metrics/recall(B)":0.46722,"metrics/mAP50(B)":0.50263,"metrics/mAP50-95(B)":0.32666,"val/box_loss":1.23061,"val/cls_loss":1.41299,"val/dfl_loss":1.52593,"lr/pg0":0.008053,"lr/pg1":0.008053,"lr/pg2":0.008053},{"epoch":70.0,"time":1977.04,"train/box_loss":0.98449,"train/cls_loss":0.99262,"train/dfl_loss":1.30701,"metrics/precision(B)":0.54374,"metrics/recall(B)":0.53627,"metrics/mAP50(B)":0.53281,"metrics/mAP50-95(B)":0.34343,"val/box_loss":1.21786,"val/cls_loss":1.34404,"val/dfl_loss":1.48796,"lr/pg0":0.007723,"lr/pg1":0.007723,"lr/pg2":0.007723},{"epoch":80.0,"time":2257.92,"train/box_loss":0.95454,"train/cls_loss":0.93854,"train/dfl_loss":1.28956,"metrics/precision(B)":0.53706,"metrics/recall(B)":0.54411,"metrics/mAP50(B)":0.53258,"metrics/mAP50-95(B)":0.34128,"val/box_loss":1.26975,"val/cls_loss":1.38016,"val/dfl_loss":1.57009,"lr/pg0":0.007393,"lr/pg1":0.007393,"lr/pg2":0.007393},{"epoch":90.0,"time":2539.07,"train/box_loss":0.93328,"train/cls_loss":0.89558,"train/dfl_loss":1.27422,"metrics/precision(B)":0.56853,"metrics/recall(B)":0.50948,"metrics/mAP50(B)":0.53246,"metrics/mAP50-95(B)":0.34863,"val/box_loss":1.2292,"val/cls_loss":1.37635,"val/dfl_loss":1.52892,"lr/pg0":0.007063,"lr/pg1":0.007063,"lr/pg2":0.007063},{"epoch":100.0,"time":2819.45,"train/box_loss":0.92647,"train/cls_loss":0.88284,"train/dfl_loss":1.26085,"metrics/precision(B)":0.54188,"metrics/recall(B)":0.54641,"metrics/mAP50(B)":0.53875,"metrics/mAP50-95(B)":0.35831,"val/box_loss":1.18349,"val/cls_loss":1.33758,"val/dfl_loss":1.48259,"lr/pg0":0.006733,"lr/pg1":0.006733,"lr/pg2":0.006733},{"epoch":110.0,"time":3100.1,"train/box_loss":0.92378,"train/cls_loss":0.84903,"train/dfl_loss":1.25318,"metrics/precision(B)":0.59093,"metrics/recall(B)":0.54963,"metrics/mAP50(B)":0.54225,"metrics/mAP50-95(B)":0.36125,"val/box_loss":1.21841,"val/cls_loss":1.35387,"val/dfl_loss":1.50863,"lr/pg0":0.006403,"lr/pg1":0.006403,"lr/pg2":0.006403},{"epoch":120.0,"time":3381.68,"train/box_loss":0.90317,"train/cls_loss":0.82824,"train/dfl_loss":1.24695,"metrics/precision(B)":0.55202,"metrics/recall(B)":0.57374,"metrics/mAP50(B)":0.533,"metrics/mAP50-95(B)":0.3559,"val/box_loss":1.17498,"val/cls_loss":1.31948,"val/dfl_loss":1.47417,"lr/pg0":0.006073,"lr/pg1":0.006073,"lr/pg2":0.006073},{"epoch":130.0,"time":3661.96,"train/box_loss":0.8813,"train/cls_loss":0.81647,"train/dfl_loss":1.23205,"metrics/precision(B)":0.61066,"metrics/recall(B)":0.54154,"metrics/mAP50(B)":0.54835,"metrics/mAP50-95(B)":0.37083,"val/box_loss":1.21999,"val/cls_loss":1.37167,"val/dfl_loss":1.49828,"lr/pg0":0.005743,"lr/pg1":0.005743,"lr/pg2":0.005743},{"epoch":140.0,"time":3941.95,"train/box_loss":0.86392,"train/cls_loss":0.76944,"train/dfl_loss":1.22656,"metrics/precision(B)":0.5864,"metrics/recall(B)":0.58005,"metrics/mAP50(B)":0.54411,"metrics/mAP50-95(B)":0.36489,"val/box_loss":1.21703,"val/cls_loss":1.41741,"val/dfl_loss":1.5263,"lr/pg0":0.005413,"lr/pg1":0.005413,"lr/pg2":0.005413},{"epoch":150.0,"time":4221.66,"train/box_loss":0.84934,"train/cls_loss":0.74722,"train/dfl_loss":1.20823,"metrics/precision(B)":0.5859,"metrics/recall(B)":0.57388,"metrics/mAP50(B)":0.55988,"metrics/mAP50-95(B)":0.37847,"val/box_loss":1.19978,"val/cls_loss":1.38423,"val/dfl_loss":1.49401,"lr/pg0":0.005083,"lr/pg1":0.005083,"lr/pg2":0.005083},{"epoch":160.0,"time":4502.19,"train/box_loss":0.84123,"train/cls_loss":0.73987,"train/dfl_loss":1.20926,"metrics/precision(B)":0.63495,"metrics/recall(B)":0.56932,"metrics/mAP50(B)":0.56235,"metrics/mAP50-95(B)":0.37545,"val/box_loss":1.21263,"val/cls_loss":1.36662,"val/dfl_loss":1.50221,"lr/pg0":0.004753,"lr/pg1":0.004753,"lr/pg2":0.004753},{"epoch":170.0,"time":4781.83,"train/box_loss":0.82741,"train/cls_loss":0.71036,"train/dfl_loss":1.18547,"metrics/precision(B)":0.58194,"metrics/recall(B)":0.59642,"metrics/mAP50(B)":0.56114,"metrics/mAP50-95(B)":0.38039,"val/box_loss":1.20419,"val/cls_loss":1.35323,"val/dfl_loss":1.50864,"lr/pg0":0.004423,"lr/pg1":0.004423,"lr/pg2":0.004423},{"epoch":180.0,"time":5061.23,"train/box_loss":0.82226,"train/cls_loss":0.68235,"train/dfl_loss":1.17967,"metrics/precision(B)":0.63092,"metrics/recall(B)":0.56625,"metrics/mAP50(B)":0.54974,"metrics/mAP50-95(B)":0.37849,"val/box_loss":1.20048,"val/cls_loss":1.36617,"val/dfl_loss":1.50131,"lr/pg0":0.004093,"lr/pg1":0.004093,"lr/pg2":0.004093},{"epoch":190.0,"time":5341.37,"train/box_loss":0.80693,"train/cls_loss":0.67407,"train/dfl_loss":1.17929,"metrics/precision(B)":0.62519,"metrics/recall(B)":0.55218,"metrics/mAP50(B)":0.54059,"metrics/mAP50-95(B)":0.37299,"val/box_loss":1.22142,"val/cls_loss":1.3745,"val/dfl_loss":1.51668,"lr/pg0":0.003763,"lr/pg1":0.003763,"lr/pg2":0.003763},{"epoch":200.0,"time":5620.47,"train/box_loss":0.78226,"train/cls_loss":0.64851,"train/dfl_loss":1.16215,"metrics/precision(B)":0.65187,"metrics/recall(B)":0.57518,"metrics/mAP50(B)":0.5748,"metrics/mAP50-95(B)":0.39012,"val/box_loss":1.20254,"val/cls_loss":1.3065,"val/dfl_loss":1.51642,"lr/pg0":0.003433,"lr/pg1":0.003433,"lr/pg2":0.003433},{"epoch":210.0,"time":5900.69,"train/box_loss":0.77295,"train/cls_loss":0.63712,"train/dfl_loss":1.15311,"metrics/precision(B)":0.63467,"metrics/recall(B)":0.55949,"metrics/mAP50(B)":0.55524,"metrics/mAP50-95(B)":0.37912,"val/box_loss":1.22143,"val/cls_loss":1.34552,"val/dfl_loss":1.51701,"lr/pg0":0.003103,"lr/pg1":0.003103,"lr/pg2":0.003103},{"epoch":220.0,"time":6180.59,"train/box_loss":0.76601,"train/cls_loss":0.62307,"train/dfl_loss":1.14798,"metrics/precision(B)":0.62824,"metrics/recall(B)":0.56374,"metrics/mAP50(B)":0.55452,"metrics/mAP50-95(B)":0.38421,"val/box_loss":1.22937,"val/cls_loss":1.37644,"val/dfl_loss":1.52328,"lr/pg0":0.002773,"lr/pg1":0.002773,"lr/pg2":0.002773},{"epoch":230.0,"time":6460.62,"train/box_loss":0.77008,"train/cls_loss":0.61093,"train/dfl_loss":1.13747,"metrics/precision(B)":0.65477,"metrics/recall(B)":0.56296,"metrics/mAP50(B)":0.55539,"metrics/mAP50-95(B)":0.38113,"val/box_loss":1.22215,"val/cls_loss":1.39604,"val/dfl_loss":1.54809,"lr/pg0":0.002443,"lr/pg1":0.002443,"lr/pg2":0.002443},{"epoch":240.0,"time":6741.07,"train/box_loss":0.73948,"train/cls_loss":0.5796,"train/dfl_loss":1.13673,"metrics/precision(B)":0.62696,"metrics/recall(B)":0.5856,"metrics/mAP50(B)":0.56974,"metrics/mAP50-95(B)":0.39027,"val/box_loss":1.22445,"val/cls_loss":1.40797,"val/dfl_loss":1.53462,"lr/pg0":0.002113,"lr/pg1":0.002113,"lr/pg2":0.002113},{"epoch":250.0,"time":7021.35,"train/box_loss":0.73625,"train/cls_loss":0.57065,"train/dfl_loss":1.12076,"metrics/precision(B)":0.6849,"metrics/recall(B)":0.55912,"metrics/mAP50(B)":0.56435,"metrics/mAP50-95(B)":0.38884,"val/box_loss":1.22815,"val/cls_loss":1.40383,"val/dfl_loss":1.54512,"lr/pg0":0.001783,"lr/pg1":0.001783,"lr/pg2":0.001783},{"epoch":260.0,"time":7300.9,"train/box_loss":0.71956,"train/cls_loss":0.5552,"train/dfl_loss":1.11123,"metrics/precision(B)":0.68772,"metrics/recall(B)":0.56527,"metrics/mAP50(B)":0.56132,"metrics/mAP50-95(B)":0.38356,"val/box_loss":1.23642,"val/cls_loss":1.39013,"val/dfl_loss":1.56196,"lr/pg0":0.001453,"lr/pg1":0.001453,"lr/pg2":0.001453},{"epoch":270.0,"time":7580.86,"train/box_loss":0.69912,"train/cls_loss":0.54425,"train/dfl_loss":1.10895,"metrics/precision(B)":0.64638,"metrics/recall(B)":0.59,"metrics/mAP50(B)":0.56446,"metrics/mAP50-95(B)":0.3868,"val/box_loss":1.25184,"val/cls_loss":1.38825,"val/dfl_loss":1.5737,"lr/pg0":0.001123,"lr/pg1":0.001123,"lr/pg2":0.001123},{"epoch":280.0,"time":7858.83,"train/box_loss":0.70012,"train/cls_loss":0.53664,"train/dfl_loss":1.1032,"metrics/precision(B)":0.65591,"metrics/recall(B)":0.58448,"metrics/mAP50(B)":0.56648,"metrics/mAP50-95(B)":0.38392,"val/box_loss":1.24389,"val/cls_loss":1.40882,"val/dfl_loss":1.56979,"lr/pg0":0.000793,"lr/pg1":0.000793,"lr/pg2":0.000793},{"epoch":290.0,"time":8135.81,"train/box_loss":0.67963,"train/cls_loss":0.51643,"train/dfl_loss":1.08969,"metrics/precision(B)":0.65979,"metrics/recall(B)":0.57017,"metrics/mAP50(B)":0.56242,"metrics/mAP50-95(B)":0.38296,"val/box_loss":1.25031,"val/cls_loss":1.43912,"val/dfl_loss":1.58245,"lr/pg0":0.000463,"lr/pg1":0.000463,"lr/pg2":0.000463},{"epoch":300.0,"time":8416.48,"train/box_loss":0.70386,"train/cls_loss":0.46029,"train/dfl_loss":1.09061,"metrics/precision(B)":0.67521,"metrics/recall(B)":0.5641,"metrics/mAP50(B)":0.55452,"metrics/mAP50-95(B)":0.37769,"val/box_loss":1.26734,"val/cls_loss":1.44087,"val/dfl_loss":1.60548,"lr/pg0":0.000133,"lr/pg1":0.000133,"lr/pg2":0.000133}]}',
                '2025-09-10 00:00:00',
                '2025-09-10 00:00:00',
                '2025-09-10 02:27:00'
            ))
        
        run_schema_migrations(conn)
        conn.commit()
        conn.close()

    def get_connection(self):
        """获取数据库连接"""
        return sqlite3.connect(self.db_path)

    def query(self, sql, params=(), fetchone=False):
        """执行查询SQL并返回结果"""
        conn = self.get_connection()
        # 使用行工厂获取结果为字典
        conn.row_factory = self._dict_factory
        cursor = conn.cursor()
        cursor.execute(sql, params)
        if fetchone:
            result = cursor.fetchone()
        else:
            result = cursor.fetchall()
        conn.close()
        return result

    def execute(self, sql, params=()):
        """执行非查询SQL"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        # 获取受影响的行数
        rowcount = cursor.rowcount
        conn.commit()
        conn.close()
        return rowcount

    def execute_many(self, sql, params_list):
        """批量执行SQL"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.executemany(sql, params_list)
        conn.commit()
        conn.close()

    def _dict_factory(self, cursor, row):
        """将查询结果转换为字典格式"""
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

# 创建数据库单例
db = Database() 
