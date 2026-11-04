# 第三方组件与来源声明

本工程为全新开发的 v1.7。下列组件为开源依赖或需要单独声明的第三方来源；
后续引入新组件时必须同步更新本文件，并保留上游许可文本。

## 运行时依赖（npm，见 frontend/package.json）

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| Vue 3 | 前端框架 | MIT |
| Vue Router | 路由 | MIT |
| Pinia | 状态管理 | MIT |
| marked | 助手回复的 Markdown 解析 | MIT |
| DOMPurify | 净化 Markdown 渲染结果（模型输出按不可信内容处理） | Apache-2.0 OR MPL-2.0 |
| Vite（含 @vitejs/plugin-vue、vue-tsc、TypeScript） | 构建与类型检查（开发依赖） | MIT / Apache-2.0 |

说明：前端 `package-lock.json` 复用自本机已验证的 v1.6 前端构建基础（同一套锁定版本），
仅用于锁定依赖版本，没有复制 v1.6 的业务源码。

## 运行时依赖（Python，见 backend/requirements.txt）

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| Flask | HTTP 框架 | BSD-3-Clause |
| itsdangerous | 会话令牌的签名与篡改检测（Flask 已依赖，显式声明） | BSD-3-Clause |
| cryptography | 个人连接 API Key 的 AES-256-GCM 加密 | Apache-2.0 / BSD-3-Clause |
| requests | “检查连接”与引擎/网关的本地调用（超时受限、无重试） | Apache-2.0 |
| mcp | 私有 stdio 工具桥的服务端 SDK；检查脚本另用其客户端 SDK 做真实握手 | MIT |
| anyio | mcp 的运行时依赖（显式声明，避免离线环境漏装） | MIT |

## 外部程序（不入库，按许可单独引入）

### OpenCode 1.19.4 — MIT

- 用途：本工程的 Agent 执行层（模型循环、规划、工具调用）。**不修改其源码、不整目录复制**，
  以外部可执行文件方式调用。
- 来源：`https://github.com/sst/opencode`；本机路径 `E:/OpenCode/bin/opencode.exe`，
  可用 `HX17_ENGINE_EXE` 覆盖。版本校验默认严格 1.19.4（不匹配即拒绝启动），
  可用 `HX17_ENGINE_VERSION` 与 `HX17_ENGINE_STRICT_VERSION` 调整。
- 许可：MIT；上游 LICENSE 随其发布物分发，本仓库不复制其源码。
- 调用方式：以 `serve`（headless HTTP）子进程运行，隐藏窗口、只绑 `127.0.0.1` 随机端口、
  随机 `OPENCODE_SERVER_PASSWORD`；每个「用户 + 会话 + 连接指纹」一个隔离运行目录
  （自带 XDG / HOME / TEMP），换连接后旧引擎被销毁。
- **保留引擎原始变量名，未做重命名**：`OPENCODE_SERVER_PASSWORD`、`OPENCODE_CONFIG_CONTENT`、
  `OPENCODE_DISABLE_AUTOUPDATE`、`OPENCODE_DISABLE_MODELS_FETCH`、`OPENCODE_DISABLE_LSP_DOWNLOAD`、
  `OPENCODE_DISABLE_DEFAULT_PLUGINS`、`OPENCODE_DISABLE_EXTERNAL_SKILLS`、
  `OPENCODE_DISABLE_CLAUDE_CODE`、`OPENCODE_DISABLE_EMBEDDED_WEB_UI`、
  `OPENCODE_DISABLE_PROJECT_CONFIG`。
  本工程自有的注入变量统一 `HX17_` 前缀（如 `HX17_GATEWAY_URL`、`HX17_TOOL_TOKEN`），与引擎变量不混用。
- 边界：不读本机全局 `~/.config/opencode` 的认证与配置，不使用全局凭据，只注入当前用户本人的个人连接。

## 计划引入（尚未加入本工程）

| 组件 | 用途 | 状态 |
| --- | --- | --- |
| vendor 定制 YOLO（ultralytics 派生） | 检测与训练 | 后续阶段以第三方组件方式明确来源与许可 |

## 未包含内容

本仓库不包含：旧版数据库、账号、API Key、模型权重、原始数据集、训练产物、
个人连接配置、node_modules 与 Python 虚拟环境。

## 参考来源（非代码来源）

功能与交互契约参考海巡智检 v1.6（保留其原有版权与许可声明）；v1.7 的业务代码按本工程
结构重新实现，不整目录复制旧站源码。
