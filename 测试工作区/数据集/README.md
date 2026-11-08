# 数据集

放评委实际要用的 YOLO 数据集目录或待导入图片，例如：

```
数据集/
├── 2026-11-巡检A/
│   ├── images/train/…  images/val/…
│   ├── labels/train/…  labels/val/…
│   └── data.yaml
└── 单模态-IR示例/
```

- 双模态数据集需要成对的辅图（IR/边缘/深度）放在 `images_ir/`（或 `images_rgb/` 反向布局），
  标签列 `labels_ir/` 与参考模态 `labels/` 各自对应本图坐标。
- 从页面「数据集 → 导入数据集 → 工作区文件夹」选择本目录下的**子目录**导入；
  也可以直接上传 ZIP。
- 本目录内容不进 Git（`.gitignore` 只保留 `.gitkeep` 与本说明）。
