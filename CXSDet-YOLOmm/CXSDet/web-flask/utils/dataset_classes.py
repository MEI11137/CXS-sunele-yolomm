"""Resolve YOLO class order from the source dataset, including legacy datasets."""

import json

from config import YOLO_CLASSES_CONFIG


def dataset_class_names(structure_info):
    info = json.loads(structure_info) if isinstance(structure_info, str) else structure_info
    names = (info or {}).get("names")
    # Seeded datasets predate structure_info.names and use the original class order.
    if names is None:
        return list(YOLO_CLASSES_CONFIG)
    if isinstance(names, dict):
        indexed = {int(key): value for key, value in names.items()}
        names = [indexed[index] for index in range(len(indexed))]
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not name.strip() for name in names
    ) or len(set(names)) != len(names):
        raise ValueError("数据集类别名称无效，请检查 data.yaml 中的 names")
    return names
