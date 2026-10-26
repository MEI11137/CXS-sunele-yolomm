# C-03 数据集导入：预检、登记与权重导入约束

## 交付物

| 文件 | 作用 |
| --- | --- |
| `services/dataset_import_service.py` | 归档预检（问题清单）与正式登记 |
| `services/business_tools.py` | 新工具 `dataset.import`，参数按 JSON Schema 校验 |
| `services/file_access.py` | 上传文件与业务资源的绑定、`LocalFileAssetGateway` |
| `routes/dataset.py` | 原上传页面改为复用同一登记服务 |

原页面 `POST /api/dataset/upload` 的入参、响应字段、错误文案保持不变，只是内部改调共享服务，因此页面与 Agent 不会对“什么算可导入的数据集”产生两套判定。

## 预检：`inspect_dataset_archive`

```python
from services.dataset_import_service import inspect_dataset_archive

preview = inspect_dataset_archive(
    zip_path, "multimodal", secondary_modality="edge", class_names=["crack"],
)
preview.to_dict()
```

返回字段：`dataset_mode`、`primary_modality`、`secondary_modality`、`class_names`、
`class_count`、`split_counts`、`total_samples`、`generated_data_yaml`、
`archive_root`、`runtime_mapping`、`data_yaml_member`、`issues`、
`issue_counts`、`ready_to_register`。

预检一次收集全部问题而不是抛出第一个错误，`issues` 每项含 `code`、`severity`
（`blocking` / `warning`）、`message`、`path`、`line`。清单最多保留 200 条，
`issue_counts.total` 与 `issue_counts.truncated` 给出真实总数。

稳定 code：

| code | 级别 | 含义 |
| --- | --- | --- |
| `BAD_ZIP` | blocking | 不是有效 ZIP |
| `UNSAFE_ENTRY` | blocking | 绝对路径、`..` 跳转或符号链接 |
| `MISSING_DATA_YAML` | blocking | 单模态缺 `data.yaml` |
| `DUPLICATE_DATA_YAML` | blocking | 一个包内多个 `data.yaml` |
| `INVALID_DATA_YAML` | blocking | YAML 无法解析或顶层不是映射 |
| `MISSING_X_MODALITY` / `INVALID_X_MODALITY` | blocking | 缺 `data.yaml` 时未指定本次 X 模态，或与 RGB 相同 |
| `MISSING_CLASS_NAMES` | blocking | 缺 `data.yaml` 时未给类别名 |
| `ARCHIVE_ROOT_UNRESOLVED` | blocking | 无法唯一定位 `images/train` |
| `MODE_MISMATCH` | blocking | 选择模态与 `data.yaml` 声明不一致 |
| `MODALITY_CONFIG` | blocking | `modality_used` 不是 `[rgb, X]` 或模态不在 `modalities` |
| `INVALID_CLASS_NAMES` | blocking | 类别为空、非字符串或结构非法 |
| `DUPLICATE_CLASS_NAMES` | blocking | 类别重名，标签与名称无法一一映射 |
| `MISSING_SPLIT` | blocking | 缺 `train` 或 `val` 图片 |
| `MISSING_LABEL` | blocking | 图片缺标注 |
| `MISSING_SECONDARY` | blocking | RGB 样本缺 X 配对图片 |
| `ORPHAN_SECONDARY` | blocking | X 图片没有对应 RGB 样本 |
| `ORPHAN_LABEL` | warning | 标注没有对应图片，会被忽略 |
| `DUPLICATE_SAMPLE` | warning | 同名不同扩展名，只会保留一个 |
| `LABEL_ENCODING` | blocking | 标注不是 UTF-8 |
| `LABEL_FIELD_COUNT` | blocking | 标注不是 5 个字段 |
| `LABEL_NOT_NUMERIC` | blocking | 标注含非数字 |
| `LABEL_CLASS_RANGE` | blocking | 类别编号超出实际类别范围 |
| `LABEL_COORDINATE` | blocking | 归一化坐标越界或框超出图片 |

判定权限不变：`services.dataset_contract.validate_dataset_archive` 仍是唯一权威。
预检只负责把问题说清楚，`import_dataset_archive` 会再跑一次权威校验，
因此旧预览不会被用来登记失效归档。测试里有一组参数化用例逐条比对
“预检通过 ⇔ 权威校验通过”，防止两套判定漂移。

## 登记：`import_dataset_archive`

```python
record = import_dataset_archive(
    zip_path=temp_copy,           # 调用方自己的临时副本
    name="sea-v1", description="", dataset_mode="single",
    database=db, storage_client=file_client,
    project_id="project-1", owner_id=7,
    on_registered=None,           # 可选：同一事务内绑定来源上传
)
```

- 缺 `data.yaml` 的多模态归档会在临时副本中生成配置，再存入 `datasets` 桶，
  因此库里保存的归档始终自带 `data.yaml`；`project_uploads` 里的原件保留为来源。
- 名称唯一性在登记前和事务内各查一次，`NAME_CONFLICT` 不会留下空对象。
- `on_registered(conn, dataset_id)` 在登记事务内执行，让“上传文件 → 数据集”的
  绑定与数据集记录同生同灭。
- 失败码：`NAME_REQUIRED`、`DATASET_INVALID`、`NAME_CONFLICT`、`STORAGE_FAILED`、
  `REGISTER_FAILED`；失败时删除刚上传的对象，不留孤儿文件。

## 工具：`dataset.import`

描述符见 `list_tools`，参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `asset_id` | 是 | C-02 项目上传返回的 `file_id`，`kind` 必须为 `dataset_archive` |
| `name` | 是 | 数据集名称，必须未被占用 |
| `dataset_mode` | 是 | `single` / `multimodal` |
| `description` | 否 | 描述 |
| `secondary_modality` | 否 | 仅用于缺 `data.yaml` 的多模态归档 |
| `class_names` | 否 | 仅用于缺 `data.yaml` 的多模态归档，按标签编号顺序 |

资源范围是 **项目**，不是文件：适配器先由 `file_assets` 解析出 `project_id`，
再向第二人的 `resolve_resource(principal, "project", project_id)` 要快照，并校验
`file:read` 与 `dataset:create`。身份、项目归属都来自受信任回调，不接受请求入参。

预览内容：`inspection`（上表的预检结果）、`asset`（`sha256`/`object_key`/大小）、
`proposed_registration`（将登记的类别、模态、样本数、来源文件）、
`blocked` 与 `blocked_reasons`、`ignored_arguments`。

`ignored_arguments` 用于避免误解：归档里已有 `data.yaml` 时，传入的
`class_names` / `secondary_modality` 不会生效，会在这里明确列出。

执行行为：

- 重新解析文件资产并比对 `sha256` 与 `object_key`，准备后被替换的上传会得到
  `PREVIEW_STALE`，不会把别的文件登记成预览里的数据集。
- 同一 `request_key` 返回原操作；换新键重试时，若该上传已绑定数据集，
  直接返回 `reused: true` 与原数据集，不产生第二份。
- 预检阻断的归档允许先 prepare（用户看得到完整问题清单），execute 时以
  `failed` + `DATASET_INVALID` + `item_results`（问题清单）结束，不写任何记录。
- 登记事务提交后无法确认的异常记为 `unknown`，不自动重提。

## 文件绑定

`file_assets` 新增 `business_type`、`business_id`、`updated_at` 三列（启动时幂等补列）。
上传状态从 `uploaded` 推进为 `registered`，`registered` 与 `uploaded` 一样可被
所有者或项目成员读取；已绑定的上传不能通过 HTTP 删除入口移除，清理归业务流程。

一个上传只能对应一个业务资源：重复绑定同一对是幂等空操作，绑定到不同资源会拒绝，
因此重试不会从同一份压缩包造出两个数据集。

## 权重导入（本轮未做）

`model.import_weight` 仍未接入适配层，现有 `/api/model/import` 与
`services/model_import_service.import_model_weight` 行为未改。C-03 要求
“PT 权重导入明确来源、模态、类别及关联数据集，条件不明时要求补充”，
下一轮在此基础上加预检与工具封装：来源说明、声明的模态/类别必须与所选数据集
实际映射一致，缺失则要求补充而不是猜。

## 未完成与限制

- 第二人的 `resolve_resource` 需支持 `kind="project"`；未接入时适配器默认拒绝，
  不提供放行替身。
- 检测与人工复核的类别已按数据集 `structure_info.names` 解析
  （`utils.dataset_classes`），但 `/api/model/classes` 仍返回固定配置供旧页面筛选，
  改这一处会影响第一人的筛选控件，留到 C-04/C-11 一起处理。
- 预检会读整包标签文本，大包（数十万张）耗时明显；当前没有分片或超时上限，
  后续可加样本数上限与进度状态。
