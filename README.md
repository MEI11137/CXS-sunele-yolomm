# 海巡智检 · 海上风电叶片巡检平台（前端）

> 项目：海巡智检 Agent 化改造 V1 —— 前端工程
> 技术栈：Vue 3 + TypeScript + Vite + Pinia + Vue Router

这是「海巡智检」海上风电叶片巡检平台的**前端独立工程**。负责个人工作区、全局巡检助手（右侧对话栏）、图片巡检、检测计划确认、缺陷人工复核、识别记录、数据集、模型管理、个人大模型连接、资料手册与扩展连接的界面与交互。

## 分工边界

| 人员 | 职责 |
| --- | --- |
| **前端（本仓库）** | 全部前端界面、输入交互、卡片、原页面衔接、前端接口接入 |
| 后端 A（Agent 后端） | 个人大模型连接、工作区元数据、公共权限、任务/消息/确认/记忆/手册 |
| 后端 B（业务服务） | 检测到训练发布的业务服务、文件上传/读取、文档解析、数据集文件操作、MCP 连接与工具 |

> 前端只实现界面与「统一接口适配层」，**不实现后端业务逻辑**。后端未就绪时以 `MockAdapter` 跑通界面（明确标识「模拟」），接入真实接口时只需替换 `src/services/index.ts` 中的 `createAdapter()`。

## 快速开始

```bash
npm install
npm run dev        # 开发：http://127.0.0.1:5173
npm run build      # 类型检查 + 生产构建（产物在 dist/）
npm run preview    # 预览构建产物
npm run typecheck  # 仅类型检查
```

### 演示辅助参数（URL 查询参数，仅开发/演示用）

- `?theme=dark|light` —— 临时覆盖主题（不写回 localStorage）
- `?guide=on|off` —— 控制首次引导是否自动弹出
- `?guideStep=0..4` —— 直达引导第 N 步

## 目录结构

```
src/
  assets/          # 主题变量、全局样式、图片素材常量
  components/      # 布局、导航、助手栏、标注画布、引导等
  views/           # 页面：总览/巡检/记录/复核/数据集/模型/设置/手册/扩展
  stores/          # Pinia：主题/工作区/巡检/会话/引导
  services/        # 统一接口适配层（mock/real 隔离）
  types/           # 与后端约定的基线字段类型
  router/          # 路由
```

## 接口约定（后端队友请看这里）

前端所有数据请求都走 `src/services/` 下的适配层，字段与任务书「共同字段基线」一致：

```ts
// 资源
{ id, kind, project_id, display_name, revision, allowed_actions }
// 任务
{ id, session_id, project_id, status, steps, waiting_for, business_refs }
// 确认
{ action_id, revision, actual_target_projects, parameters_summary, preview, status }
// 事件
{ event_id, sequence, task_id, type, payload }
// 错误
{ code, message, execution_state }   // execution_state: not_started|running|completed|unknown
```

完整类型定义见 `src/types/index.ts`，接口签名见 `src/services/types.ts`（`ApiAdapter` 接口）。

**接入真实后端**：新建 `src/services/real-adapter.ts` 实现 `ApiAdapter` 接口，然后在 `src/services/index.ts` 的 `createAdapter()` 里返回它。注意：

- 真实请求失败时**不要**回退到 mock 成功结果。
- API Key 不进入聊天、URL、日志、截图或 localStorage；后端应提供密钥保存/加密机制。

## 视觉与交互规范（摘要）

- 默认浅色冷灰白，支持深色中性黑灰（非科技蓝大屏），照片保持原色。
- 浅色变量：背景 `#F3F5F7`、面板 `#FFFFFF`、正文 `#17212A`、强调蓝 `#1D5FBF`、警示 `#8A4B0E`/`#FFF5E7`。
- 左导航 216px，右侧巡检助手 336px（可收起），主内容内边距 24px。
- 避免：蓝紫渐变、霓虹发光、玻璃模糊、彩色 KPI 图标方块、营销横幅。
- 首次进入显示五步聚光引导，可随时关闭/跳过，重播入口在「设置」。

## 素材与许可

演示用风机/叶片照片来自 [Unsplash](https://unsplash.com)（免费商用许可，`public/images/`）：

- `turbine-sea.jpg` — 海上风机场景
- `windfarm-aerial.jpg` — 风电场阵列
- `blade-closeup.jpg` — 叶片近景
- `scene-windfarm.jpg` — 风力发电机近景

这些是**演示占位素材**，不冒充真实业务影像；接入真实后端后由实际巡检影像 URL 替换。字体优先 Geist，未捆绑时回退微软雅黑/系统无衬线。

## 开发日志

见 `docs/development-logs/A/`。每次新增/修复/优化需按任务书要求补一条日志。
