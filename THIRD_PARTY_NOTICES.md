# 第三方组件与来源声明

本工程为全新开发的 v1.7。下列组件为开源依赖或需要单独声明的第三方来源；
后续引入新组件时必须同步更新本文件，并保留上游许可文本。

## 运行时依赖（npm，见 frontend/package.json）

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| Vue 3 | 前端框架 | MIT |
| Vue Router | 路由 | MIT |
| Pinia | 状态管理 | MIT |
| Vite（含 @vitejs/plugin-vue、vue-tsc、TypeScript） | 构建与类型检查（开发依赖） | MIT / Apache-2.0 |

说明：前端 `package-lock.json` 复用自本机已验证的 v1.6 前端构建基础（同一套锁定版本），
仅用于锁定依赖版本，没有复制 v1.6 的业务源码。

## 运行时依赖（Python，见 backend/requirements.txt）

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| Flask | HTTP 框架 | BSD-3-Clause |
| itsdangerous | 会话令牌的签名与篡改检测（Flask 已依赖，显式声明） | BSD-3-Clause |
| cryptography | 个人连接 API Key 的 AES-256-GCM 加密 | Apache-2.0 / BSD-3-Clause |
| requests | “检查连接”时的单次 HTTP 探测（超时受限、无重试） | Apache-2.0 |

## 计划引入（尚未加入本工程）

| 组件 | 用途 | 状态 |
| --- | --- | --- |
| OpenCode（内部 Agent 执行层） | 模型循环与工具调用 | WB17-02，按上游许可与版本核对后引入并补声明 |
| vendor 定制 YOLO（ultralytics 派生） | 检测与训练 | 后续阶段以第三方组件方式明确来源与许可 |

## 未包含内容

本仓库不包含：旧版数据库、账号、API Key、模型权重、原始数据集、训练产物、
个人连接配置、node_modules 与 Python 虚拟环境。

## 参考来源（非代码来源）

功能与交互契约参考海巡智检 v1.6（保留其原有版权与许可声明）；v1.7 的业务代码按本工程
结构重新实现，不整目录复制旧站源码。
