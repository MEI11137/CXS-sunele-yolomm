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

## WorkBuddy 云端入口（本工程自写，不引入第三方 npm 依赖）

| 文件 | 作用 | 依赖 |
| --- | --- | --- |
| `deploy/workbuddy/server.js` | 云端单端口网关：静态托管 + `/api` 反向隧道 + `/api/service` 403 | 仅 Node 标准库（`http`/`https`/`fs`/`path`/`crypto`/`zlib`/`os`） |
| `deploy/workbuddy/connector.js` | 本机连接器：主动外连云端、转发到 `127.0.0.1:8080` | 仅 Node 标准库 |
| `deploy/workbuddy/prepare.mjs`、`preflight.mjs` | 生成 `web/`、启动前自检 | 仅 Node 标准库 |

- `package.json` 里没有 `dependencies`/`devDependencies`，部署机不需要 `npm install`；
  本文件因此不为它新增许可条目。
- 来源：`server.js`、`connector.js` 是本工程在 WorkBuddy 发布阶段自行编写的同源网关与本机连接器，
  2026-11-07 的公网浏览器验收使用的就是同一份实现；整合进本仓库时按字节复制
  （`server.js` sha256 `065a06ccda7a…`），这一轮没有改动它们的逻辑，也没有引入第三方反向代理项目或第二套实现。
  `prepare.mjs`、`preflight.mjs` 与 `scripts/workbuddy_gateway_check.mjs` 是这次整合新写的脚本。
- 隧道协议（NDJSON 帧、`queued`/`inflight` 状态机、`Origin` 精确白名单）是本工程自定义的，
  只在这两个文件之间使用。
- WorkBuddy 平台（`*.app.workbuddy.host` 的托管与边缘）是**腾讯提供的服务**，
  不随本仓库分发；发布材料只运行在用户自己账号下的站点里。

## 运行时依赖（Python 基础，见 backend/requirements.txt）

| 组件 | 用途 | 许可（本机安装包元数据原文） |
| --- | --- | --- |
| Flask | HTTP 框架 | BSD-3-Clause |
| itsdangerous | 会话令牌的签名与篡改检测（**项目实际鉴权方式**；未使用 PyJWT） | BSD-3-Clause |
| cryptography | 个人连接 API Key 的 AES-256-GCM 加密 | Apache-2.0 / BSD-3-Clause |
| requests | “检查连接”与引擎/网关的本地调用（超时受限、无重试） | Apache-2.0 |
| mcp | 私有 stdio 工具桥的服务端 SDK；检查脚本另用其客户端 SDK 做真实握手 | MIT |
| anyio | mcp 的运行时依赖（显式声明，避免离线环境漏装） | MIT |
| Pillow | 资产按真实内容判定（`Image.open` + `verify()`），导入期即依赖 | MIT-CMU |
| PyYAML | `data.yaml` 的解析与生成 | MIT |
| pypdf | 项目资料库的 PDF 解析（逐页取文本，不渲染） | BSD-3-Clause |
| python-docx | 项目资料库的 Word 解析（段落与表格） | MIT |
| waitress | 对外示范用的 WSGI 服务器（`deploy/serve.py` 起单端口，不用 Flask 开发服务器） | ZPL 2.1（Zope Public License） |

以上都是**第三方开源组件**（本工程只声明依赖并按其许可使用，不属于本项目自行开发的部分）；
许可文本位置：pip 安装后位于对应 `*.dist-info/` 下（例如 Python 3.12+ 的 PEP 639 布局是
`pyyaml-6.0.3.dist-info/licenses/LICENSE`，waitress 3.0.2 是
`waitress-3.0.2.dist-info/LICENSE.txt`）；本仓库不复制这些文本，部署时随包分发。

## 运行时依赖（Python 视觉，**可选**，见 backend/requirements-vision.txt）

这一组只在跑真实推理/权重检查时需要，**不在基础依赖里、不静默安装、不自动下载**。

| 组件 | 用途 | 许可（本机安装包元数据原文） |
| --- | --- | --- |
| torch | 张量运行时与权重反序列化 | `Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT`（多许可组合，逐项以包内 `LICENSE` 为准） |
| torchvision | vendor YOLO 的图像算子依赖 | BSD |
| numpy | 数值计算 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| opencv-python | 图像解码/编码（中文路径按字节读取后 `imdecode`） | Apache 2.0（OpenCV 本体；opencv-python 打包脚本为 MIT） |
| matplotlib | vendor ultralytics 的绘图依赖 | Matplotlib License（PSF 风格，见包内 `LICENSE`） |
| pandas | vendor ultralytics 的数据依赖 | BSD-3-Clause |
| scipy | vendor ultralytics 的数值依赖 | BSD-3-Clause |
| tqdm | 进度条 | `MPL-2.0 AND MIT` |
| psutil | 进程/内存信息 | BSD-3-Clause |
| einops | vendor 中 mamba 模块依赖 | MIT |
| timm | vendor 中主干网络依赖 | Apache-2.0 |
| ultralytics-thop | vendor 统计参数量/FLOPs | **AGPL-3.0** |

Pillow、PyYAML、requests 在视觉清单里也出现，但用的是与基础依赖同一份包（上表已列），
不重复声明；torch 的下限是 **2.6**（`safe_weight.py` 需要
`torch.serialization.get_unsafe_globals_in_checkpoint`），高于这个下限的组合由部署方按驱动自选。

## 外部程序（不入库，按许可单独引入）

### OpenCode 1.19.4 — MIT

- 用途：本工程的 Agent 执行层（模型循环、规划、工具调用）。**不修改其源码、不整目录复制**，
  以外部可执行文件方式调用。
- 来源：`https://github.com/sst/opencode`（**外部程序，不随本仓库分发**）。本工作机的
  开发环境用 `HX17_ENGINE_EXE` 指向本机的一份 1.19.4；没有配置时按 PATH 里的
  `opencode` 查找，**不写死任何盘符或安装路径**（换机后路径不同是正常的）。
  版本校验默认严格 1.19.4（不匹配即拒绝启动），可用 `HX17_ENGINE_VERSION` 与
  `HX17_ENGINE_STRICT_VERSION` 调整。
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

### vendor 定制 YOLO（ultralytics 派生，AGPL-3.0）— 已引入

- 位置：`backend/vendor/ultralytics`（`__version__ = "8.3.163"`，见该目录 `__init__.py`）。
  交付内容为 **1301 个源文件**（不含 `__pycache__`，约 9.8MB）；本机运行后会在
  `backend/vendor/ultralytics/` 下生成 `__pycache__`，并在 `backend/vendor/logs/` 下生成
  `error_report_*.txt` 错误报告——两者都是运行期产物，不属于交付内容，已加入 `.gitignore`
  （报告本身保留在磁盘上供排查，不删除、也不改 vendor 源码）。
- 来源：原工程 CXSDet-YOLOmm 的 `web-flask/vendor/ultralytics`（合作方提供），**源码逐字节复制、未做任何修改**；
  交付时的 `best.pt` 是用这套代码训练并保存的，反序列化依赖它的类定义。
- 本工程对该目录的**唯一新增**是补齐上游许可证文本 `backend/vendor/ultralytics/LICENSE`
  （AGPL-3.0 原文，来源 `https://www.gnu.org/licenses/agpl-3.0.txt`，
  SHA-256 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`，34523 字节，未做任何改动）。
  合作方提供的原始副本里没有随附 LICENSE 文件，这里补齐是为了让许可文本随代码实际分发，而不是只在文档里转述。
- 用途：真实推理（`YOLO` 单模态 / `YOLOMM` 双模态）。v1.7 通过 `backend/vision/runtime.py`
  把该目录放在 `sys.path` 首位后导入，**禁止回落到 pip 安装的 ultralytics**：
  两者类定义与算子不同，混用会静默加载出与训练时不一致的权重行为。机器上没有 pip 版
  `ultralytics` 也能运行（实测本机即如此）。
- 许可：**AGPL-3.0**。这是强传染性许可：若本工程对外提供服务，需按 AGPL 的要求提供对应源码；
  内部使用则需遵守其分发条款。本工程不修改上游源码，仅作为独立目录引用。
- 权重反序列化：`backend/vision/safe_weight.py` 只放行已知安全类型（torch/ultralytics 的
  `nn.Module` 子类、张量重建函数、纯数据类型），并给 vendor 内部的 `torch.load(weights_only=False)`
  挂同一道闸门；详见 `docs/workbuddy-development/WB17-03.md`。
- 同批依赖见上表「Python 视觉（可选）」，由部署方按 `backend/requirements-vision.txt` 安装，
  本仓库不打包这些二进制。

## 未包含内容

本仓库不包含：旧版数据库、账号、API Key、模型权重、原始数据集、训练产物、
个人连接配置、node_modules 与 Python 虚拟环境。

## 参考来源（非代码来源）

功能与交互契约参考海巡智检 v1.6（保留其原有版权与许可声明）；v1.7 的业务代码按本工程
结构重新实现，不整目录复制旧站源码。

界面与交互设计参考 DeepSeek Harness（DSH）官方产品界面：
`https://www.deepseek.com/en/harness/`（开源仓库 `https://github.com/deepseek-ai/deepseek-harness`）。
本工程只参考其信息层级与交互方式，前端为自写 Vue 3 组件，**未复制其源码、样式表或静态资源**，
也不随本仓库分发其任何文件。
