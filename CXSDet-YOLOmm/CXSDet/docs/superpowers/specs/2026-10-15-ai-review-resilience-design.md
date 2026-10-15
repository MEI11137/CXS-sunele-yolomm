# AI 复核结构化输出与失败可见性设计

## 背景

AI 复核通过 OpenAI 兼容的 Chat Completions 视觉接口请求结构化结果。当前代码只在提示词中要求 JSON；当模型返回空内容、截断内容或非 JSON 文本时，服务会静默降级为 `needs_human_review`，路由仍返回“AI辅助复核完成”。这使真实调用失败在前端表现为功能失灵，同时没有足够的脱敏诊断信息。

## 目标

- 使用 Chat Completions JSON Mode，要求模型返回合法 JSON 对象。
- 显式检测空响应、拒绝、截断和内容过滤等不完整响应。
- 仅在 JSON 解析失败时自动重试一次，避免对正常结果或明确 API 错误重复计费。
- 连续解析失败或上游 API 异常时，将记录安全地降级为 `needs_human_review`，同时让单条和批量接口明确报告失败。
- 日志仅包含记录 ID、模型名、异常类型、状态码、响应长度和响应摘要哈希；禁止记录 API Key、图片内容、完整提示词或模型原文。

## 非目标

- 不更换当前模型、API 地址或凭据。
- 不迁移到 Responses API。
- 不引入新的运行时依赖。
- 不修改人工复核、检测推理或前端页面布局。

## 方案

### 1. 视觉模型客户端

在 `algo/llm/llm_vl_client.py` 中为结构化复核请求增加：

```python
response_format={"type": "json_object"}
```

提示词继续明确包含“JSON”要求，以满足 JSON Mode 的调用约束。客户端在返回内容前检查：

- 响应至少包含一个 choice；
- `finish_reason` 不是 `length` 或 `content_filter`；
- `message.refusal` 为空；
- `message.content` 是非空字符串。

上述检查失败时抛出新的 `LLMResponseError`，异常消息只描述安全原因，不携带响应正文或凭据。保留 `review_detection_structured(image_bytes, detection_results) -> str` 接口，避免影响调用方。

### 2. 服务层解析与重试

在 `services/ai_review_service.py` 中新增：

- `AIReviewError`：路由可识别的复核失败基类；
- `AIReviewParseError`：返回内容不能转换成有效复核对象；
- `AIReviewUpstreamError`：模型客户端或网络调用失败。

解析函数在无法找到 JSON 对象、JSON 解码失败或顶层不是对象时抛出 `AIReviewParseError`。合法对象仍沿用现有字段归一化规则。

`review_detection` 的流程为：

1. 读取记录、图片和 YOLO 结果；
2. 调用模型并解析；
3. 首次发生 `AIReviewParseError` 时，使用相同客户端重试一次；
4. 第二次解析仍失败时，保存 `needs_human_review` 降级记录并抛出 `AIReviewParseError`；
5. 模型调用抛出其他异常时，不额外重试，保存降级记录并抛出 `AIReviewUpstreamError`；
6. 成功时保存结构化结果并正常返回。

降级记录的摘要分别使用“AI返回格式异常，需要人工复核”和“AI服务调用失败，需要人工复核”，不暴露底层异常文本。

### 3. 路由与批量行为

单条复核路由捕获 `AIReviewError`，返回统一响应体 `code=502`，并设置真实 HTTP 状态码为 502。错误文案区分“返回格式异常”和“服务调用失败”，但不暴露第三方响应正文。

批量复核现有编排器已经逐条捕获异常并继续处理；服务层开始抛出类型化异常后，该行为自然变为失败项，不中断其他记录。批量项继续只返回异常类型对应的安全错误信息。

### 4. 脱敏诊断

使用标准库 `logging` 和 `hashlib.sha256`。解析失败日志包含：

- `record_id`；
- 配置模型名；
- 尝试次数；
- 原始内容类型；
- 字符长度；
- 内容 SHA-256 的前 12 位。

上游异常日志包含异常类型、公开状态码和公开错误码。任何日志字段都不得包含 API Key、图片字节、完整提示词、完整响应或数据库中的检测结果。

## 测试设计

### 客户端契约

- 结构化复核请求包含 `response_format={"type": "json_object"}`。
- 空 choices、截断、内容过滤、拒绝和空内容分别抛出 `LLMResponseError`。
- 正常响应继续返回字符串内容。

### 服务行为

- 合法 fenced JSON 正常解析并更新同一记录。
- 首次非 JSON、第二次合法 JSON 时调用两次并保存成功结果。
- 连续两次非 JSON 时保存降级结果并抛出 `AIReviewParseError`。
- 上游客户端异常时只调用一次、保存降级结果并抛出 `AIReviewUpstreamError`。
- 日志断言不包含测试密钥、图片字节和完整模型原文。

### 路由与回归

- 单条成功仍返回 HTTP 200。
- `AIReviewError` 返回 HTTP 502，响应体 `code` 也是 502。
- 批量调用将类型化异常计为单条失败并继续处理其余记录。
- 运行 AI 复核相关测试后，再运行后端完整测试集与前端构建。

## 成功标准

- 模型生成的结构化复核结果可稳定解析。
- 一次偶发格式异常会自动恢复；连续异常不会再伪装为成功。
- 用户能从 HTTP 状态和批量结果看出调用失败。
- 数据库始终保留安全的人工复核降级状态。
- 日志足以定位错误类别且不泄露敏感内容。
