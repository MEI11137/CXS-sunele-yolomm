# MD5: 263c356aba92b45953b171ce1b39e550
"""
版权所有 © 2025  羊羊小栈 （GJQ）

作者：羊羊小栈 | 时间：2025-09-06  23:43:10

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
"""

import os
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from config import DB_PATH, PORT, TEMP_DIR, FILE_STORE_CONFIG
from services.yolo_runtime import activate_vendored_ultralytics

activate_vendored_ultralytics()

from routes.health import health_bp
from routes.file import file_bp
from routes.user import user_bp
from routes.dataset import dataset_bp
from routes.model import model_bp
from routes.detection import detection_bp
from routes.video_detection import video_detection_bp
from routes.realtime_detection import realtime_detection_bp

def create_app(config_overrides=None):
    """创建 Flask 应用，并允许测试安全地覆盖运行目录。"""
    flask_app = Flask(__name__)
    flask_app.config.from_mapping(
        DATABASE=DB_PATH,
        TEMP_DIR=TEMP_DIR,
        FILE_STORE_PATH=FILE_STORE_CONFIG['base_path'],
    )
    if config_overrides:
        flask_app.config.update(config_overrides)

    CORS(flask_app)

    # 注册路由(注意：url前缀已经在路由文件中设置，无需重复设置)
    flask_app.register_blueprint(health_bp)
    flask_app.register_blueprint(file_bp)
    flask_app.register_blueprint(user_bp)
    flask_app.register_blueprint(dataset_bp)
    flask_app.register_blueprint(model_bp)
    flask_app.register_blueprint(detection_bp)
    flask_app.register_blueprint(video_detection_bp)
    flask_app.register_blueprint(realtime_detection_bp)
    return flask_app


app = create_app()

if __name__ == '__main__':
    # 确保基础存储目录存在
    if not os.path.exists(FILE_STORE_CONFIG['base_path']):
        os.makedirs(FILE_STORE_CONFIG['base_path'])
    app.run(host='0.0.0.0', port=PORT) 
