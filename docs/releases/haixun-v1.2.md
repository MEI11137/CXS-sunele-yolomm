# 海巡智检 v1.2 版本说明

发布日期：2026-11-01　｜　对应 `VERSION`：`1.2.0`

## 这一版做了什么

1. **对外包壳统一（去品牌痕迹）**
   运行时目录改为 `.runtime/hxengine/`，Python 模块为 `agent/hx_engine.py`、`agent/hx_engine_runtime.py`、`agent/hx_engine_mcp.py`，前端与接口统一走 `/api/agent/engine`，状态事件里的 `engine` 字段为 `hxengine`，界面显示“海巡引擎”。底层执行框架来源和版权保留在 THIRD_PARTY_NOTICES.md，界面使用本站集成名称。

2. **公网演示权限收敛**
   演示账号只能操作初始化时绑定的那一个演示文件夹（`.demo-state/server.local.json` 的 `demo_workspace_root`）。该目录内的读取、写入、新建、改名、删除自动批准；目录外访问、目录浏览、工作区根目录改写一律仅管理员。管理员每台机器首次使用仍需在“本机文件夹”里授权一次。

3. **模型白名单**
   源码版通过 deploy/serve.py 的 --llm-models 或 HX_LLM_MODELS 配置允许的模型；白名单存在时 `/api/agent/connection/models` 只返回名单内的项，切换走 `POST /api/agent/connection/model`，复用已存 Key 与地址。

4. **上传策略**
   网关放行 `jpg/jpeg/png/webp/tif/tiff/bmp/gif/zip/pdf/docx/md/pt`，单包上限 256MB；服务端 900 秒通道超时；ZIP 解压加条目数、解压体积与压缩比三重校验。

5. **一键启动**
   本源码目录没有单独的“部署工具/一键启动演示.cmd”。使用根目录“启动海巡智检.cmd”本机启动，或按 deploy/serve.py 初始化后运行“启动远程演示.cmd”。外部通道单独配置；不会因为双击本机启动就自动获得公网网址。

## 已知边界

- 内置引擎可执行文件（约 210MB）不随 GitHub 源码仓分发，需按部署教程放入 `.runtime/hxengine/`。
- Python 依赖需按 `docs` 中部署教程在目标机重建虚拟环境；`.venv`、`node_modules`、演示数据库不入包。
- 未配置真实模型 Key 时，对话可发起但模型调用会失败并回显鉴权错误。

## 发布前回归检查

检查结果、修正和未实测边界见 docs/development-logs/Integration/2026-11-01_INT-46_01.md。
