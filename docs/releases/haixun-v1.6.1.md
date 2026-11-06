# 海巡智检 v1.6.1

发布日期：2026-11-06。基线 `v1.6.0`，本版只做运行期修复与现场物料，不改变业务行为。

## 变更
1. `CXSDet-YOLOmm/CXSDet/web-flask/requirements.txt` 增加 `dill==0.4.1`。
   自带权重若用 dill 序列化，导入时会报「权重加载验证失败: No module named 'dill'」（`services/model_import_service.py:132`）。
   演示网关实际用 `D:\miniconda\envs\yolo_rgb` 的 python 运行，所以该环境同样需要装。
2. 版本号 `1.6.0` → `1.6.1`：`VERSION`、`web-haixun/package.json`、`web-haixun/package-lock.json`、`start.ps1` 的三处提示文案。

## 新增物料
- `docs/judge-logs/2026-11-06/`：评委端测试日志（8 会话 / 266 消息 / 53 任务 / 1348 事件，含引擎侧消息、后端日志、错误汇总与原始 JSON）。
  摘要里记录 6 个现场问题，可直接对照复现。

## 单模态验证结论（供后续开发参考）
- RGB 单模态、20 epoch、imgsz 640：val mAP50 **0.0022**，学不到东西（热斑在可见光里几乎不可见）。
- 红外单模态、60 epoch、imgsz 1024：val mAP50 **0.988**、mAP50-95 **0.842**、precision 0.896、recall 0.983。
- 结论：热斑信号在热成像通道。单图检测要么喂 IR 图，要么继续走 RGB+IR 配对的 multimodal 路径。

## 已知问题（本版未修）
1. 单图检测对 multimodal 模型强制要求 `secondary_asset_id`，否则 `MODALITY_PAIR_REQUIRED`。
2. 同一任务里第二次写操作会触发 `AGENT_PLAN_REQUIRED_FOR_MULTI_WRITE`。
3. `permission.demo_rejected`（操作目标不在演示文件夹内）后引擎提前结束回合，需要靠兜底文案补一句。
4. 任务标 `completed` 但 `summary` 仍写「等待用户补充信息」，状态不一致。
5. 数据集可重复导入（两份 `file_size` 完全一致）。
