"""Visual-language model configuration; credentials stay on the local machine."""

import json
import os
from pathlib import Path


_local_path = Path(__file__).with_name('config.local.json')
_local = json.loads(_local_path.read_text(encoding='utf-8-sig')) if _local_path.is_file() else {}

VL_MODEL_CONFIGS = {
    'OpenAI-Plus-17634e': {
        'name': 'DeepSeek Flash',
        'api_url': os.environ.get('VL_API_URL', _local.get('api_url', 'https://api.deepseek.com')),
        'api_key': os.environ.get('VL_API_KEY', _local.get('api_key', '')),
        'model': os.environ.get('VL_MODEL', _local.get('model', 'deepseek-flash')),
    }
}

# A stable configuration ID keeps selection valid when the upstream model changes.
DEFAULT_VL_MODEL = 'OpenAI-Plus-17634e'
