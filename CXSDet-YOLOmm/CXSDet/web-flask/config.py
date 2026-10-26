
import os
import sys

def get_application_root():
    """获取应用程序根目录"""
    if getattr(sys, 'frozen', False):
        # 如果是打包后的可执行文件
        return os.path.dirname(sys.executable)
    else:
        # 如果是开发环境
        return os.path.dirname(__file__)

# 服务配置
HOST = os.environ.get('FLASK_SERVICE_HOST', 'localhost')
PORT = int(os.environ.get('FLASK_SERVICE_PORT', 5020))

# 数据库配置
DB_PATH = os.environ.get('DB_PATH', os.path.join(get_application_root(), 'yyxz_sqlite.db'))

# 临时文件配置
# 1. 首先尝试从环境变量获取
# 2. 如果环境变量未设置，则使用应用程序目录下的temp目录
TEMP_DIR = os.environ.get('FLASK_TEMP_DIR', os.path.join(get_application_root(), 'temp'))
os.makedirs(TEMP_DIR, exist_ok=True)  # 创建临时文件目录

# 文件存储配置
FILE_STORE_CONFIG = {
    'base_path': os.environ.get('FILE_STORE_PATH', os.path.join(get_application_root(), 'file_store')),
    'access_url': os.environ.get('FILE_PUBLIC_URL', f'http://{HOST}:{PORT}/api/file').rstrip('/'),
    'allowed_types': [  # 支持的文件类型
        # 图片类型
        'image/jpeg',            # JPG/JPEG图片 (.jpg, .jpeg)
        'image/png',             # PNG图片 (.png)
        'image/gif',             # GIF图片 (.gif)
        'image/tiff',            # TIFF图片 (.tif, .tiff)
        'image/bmp',             # BMP图片 (.bmp)
        'image/x-ms-bmp',        # BMP图片的另一种MIME类型 (.bmp)
        'image/webp',            # WebP图片 (.webp)
        'image/x-icon',          # ICO图片 (.ico)
        'image/vnd.microsoft.icon', # ICO图片的另一种MIME类型 (.ico)
        'image/svg+xml',         # SVG图片 (.svg)
        'image/x-portable-pixmap', # PPM图片 (.ppm)
        'image/x-portable-graymap', # PGM图片 (.pgm)
        'image/x-portable-bitmap',  # PBM图片 (.pbm)
        'image/x-portable-anymap',  # PNM图片 (.pnm)
        'image/x-rgb',           # RGB图片 (.rgb)

        # 音频类型
        'audio/mpeg',            # MP3音频 (.mp3)
        'audio/ogg',             # OGG音频 (.ogg)
        'audio/wav',             # WAV音频 (.wav)
        'audio/x-wav',           # WAV音频的另一种MIME类型 (.wav)
        'audio/aac',             # AAC音频 (.aac)
        'audio/flac',            # FLAC音频 (.flac)
        'audio/mp4',             # MP4音频 (.mp4)
        'audio/midi',            # MIDI音频 (.mid,.midi)
        'audio/x-midi',          # MIDI音频的另一种MIME类型 (.mid,.midi)
        'audio/x-mp3',           # MP3音频的另一种MIME类型 (.mp3)
        'audio/x-mp4',           # MP4音频的另一种MIME类型 (.mp4)
        'audio/x-flac',          # FLAC音频的另一种MIME类型 (.flac)
        'audio/x-midi',          # MIDI音频的另一种MIME类型 (.mid,.midi)

        # 文档类型
        'application/pdf',       # PDF文档 (.pdf)
        'application/msword',    # Word文档 (.doc)
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # Word文档 (.docx)
        'text/plain',           # 纯文本文件 (.txt)
        'text/markdown',        # Markdown文档 (.md)
        'text/x-markdown',      # Markdown文档的另一种MIME类型 (.md)
        'application/json',     # JSON文件 (.json)
        'text/csv',             # CSV文件 (.csv)
        'application/vnd.ms-excel', # Excel文件 (.xls)
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', # Excel文件 (.xlsx)
        'application/vnd.ms-excel.sheet.macroenabled.12', # Excel文件 (.xlsm)
        'application/vnd.ms-excel.template.macroenabled.12', # Excel文件 (.xltm)
        'application/vnd.ms-excel.addin.macroenabled.12', # Excel文件 (.xlam)
        
        # 压缩包类型
        'application/zip',              # ZIP压缩包 (.zip)
        'application/x-zip-compressed', # ZIP压缩包的另一种MIME类型 (.zip)
        'application/x-gzip',    # GZIP文件 (.gz)
        'application/x-tar',     # TAR文件 (.tar)
        'application/x-bzip2',   # BZIP2文件 (.bz2)
        'application/x-rar-compressed', # RAR压缩包 (.rar)
        'application/x-7z-compressed', # 7Z压缩包 (.7z)
        
        # 视频类型
        'video/mp4',            # MP4视频 (.mp4)
        'video/mpeg',           # MPEG视频 (.mpeg, .mpg)
        'video/quicktime',      # QuickTime视频 (.mov)
        'video/x-msvideo',      # AVI视频 (.avi)
        'video/x-ms-wmv',       # Windows Media视频 (.wmv)
        'video/x-flv',          # Flash视频 (.flv)
        'video/webm',           # WebM视频 (.webm)
        'video/3gpp',           # 3GPP视频 (.3gp)
        'video/x-matroska',     # Matroska视频 (.mkv)
        
        # 深度学习模型权重文件类型
        'application/x-hdf5',           # HDF5文件 (.h5, .hdf5) - TensorFlow/Keras权重文件
        'application/octet-stream',     # 二进制文件 - 通用类型，支持.weights.h5, .pt, .pth, .ckpt等
        'application/x-tensorflow',      # TensorFlow模型文件 (.pb, .ckpt)
        'application/x-pytorch',         # PyTorch模型文件 (.pt, .pth)
        'application/x-onnx',           # ONNX模型文件 (.onnx)
        'application/x-keras',          # Keras模型文件 (.keras)
        'application/x-savedmodel'      # TensorFlow SavedModel格式
    ],
    'max_size': 1000 * 1024 * 1024  # 最大文件大小：1000MB
}

# YOLO类别配置
YOLO_CLASSES_CONFIG = {
    'bird_shit': {
        'chinese_name': '鸟粪',
        'color': '#FF8C00',  # 深橙色 - 更醒目
        'description': '光伏板表面的鸟类粪便污染'
    },
    'crack': {
        'chinese_name': '裂纹',
        'color': '#FF0000',  # 亮红色 - 更醒目
        'description': '光伏板表面的裂纹损伤'
    },
    'dirt': {
        'chinese_name': '污垢',
        'color': '#00BFFF',  # 深天蓝色 - 醒目且舒适
        'description': '光伏板表面的灰尘、污渍等污染物'
    },
    'panel': {
        'chinese_name': '面板',
        'color': '#00FF00',  # 亮绿色 - 更醒目
        'description': '光伏板面板区域'
    }
}

