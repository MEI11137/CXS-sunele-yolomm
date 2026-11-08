# 权重

放单模态（`family=yolo`）或双模态（`family=yolomm`）的 `.pt` 权重文件。

- 导入路径：页面「模型管理 → 导入权重」，可以选本目录下的相对路径，也可以直接上传 `.pt`。
- 家族必须与权重真实结构一致：单模态权重按 `yolomm` 登记会被后端拒绝（`MODEL_FAMILY_MISMATCH`）。
  不确定时先问助手（`hx_inspect_weight` 会安全读取结构），不要按文件名里的 `mm` 猜。
- 权重走 `backend/vendor/ultralytics` + 安全加载白名单，不联网下载、不执行旧式 pickle。
- 本目录内容不进 Git（`.gitignore` 已忽略 `*.pt`/`*.pth` 与本目录其余文件）。
