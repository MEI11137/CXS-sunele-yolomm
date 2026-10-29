# 海巡智检 · CXS-sunele-yolomm

基于 Vue 3、Flask 和 YOLO 的巡检系统，支持图片、视频和实时检测，以及视觉大模型辅助核查。

## 海巡智检 v1.1 源码版

当前整体版本为 `1.1.0`，在 `feature/opencode-agent` 分支协作审查。网站内部使用 OpenCode 1.19.4，保留原人工操作入口。源码不包含个人API配置、数据库、测试记录、模型权重、原始数据或本机依赖；新安装从空业务数据开始。真实模型能力和GPU训练效果需要按各人的连接、数据与硬件验收。

- [v1.1 版本说明](docs/releases/haixun-v1.1.md)
- [Agent功能覆盖与边界](docs/integration/Agent功能覆盖复核_2026-10-29.md)
- [版本日志](CHANGELOG.md)

历史 v1.0 整合分支及 `haixun-v1.0` 标签保持原样：

- [v1.0 版本说明](docs/releases/haixun-v1.0.md)
- [启动与交接说明](docs/integration/接续说明_2026-10-26.md)
- [完整检查结果](docs/development-logs/Integration/2026-10-26_INT-12_01.md)

## 本次更新

- 项目内会话、全局助手对话与业务页面联动；项目绑定本人电脑的文件夹。
- 内部Agent读取工作区素材，调用检测、AI核查和反馈等业务工具，人工仍负责最终复核标注。
- 项目/会话管理、CSV导出、资料原文、任务手册和明确的界面命令可通过对话调用。
- 图片检测结果支持 AI 辅助核查、人工修改标注和人工确认。
- 人工复核类别按当前记录所用模型关联的数据集读取，保持 YOLO 类别编号顺序。
- 已确认记录可进入反馈池、生成数据集版本，并用于后续增量训练和模型对比。
- 支持个人兼容接口连接，并明确提示认证、视觉或工具能力不满足要求的环节；认证成功不代表支持看图。

## 项目目录

```text
CXSDet-YOLOmm/CXSDet/
├─ web-flask/    # Flask 后端、YOLO 运行时和训练及复核服务
├─ web-haixun/   # 当前Vue前端，默认开发端口5173
└─ web-vue/      # 保留的旧前端
```

## 启动

先准备适配硬件的Python/Torch环境、Node.js，以及OpenCode 1.19.4的Windows可执行程序。Python依赖原有锁定环境涉及Python 3.12/CUDA 12.1，实际运行环境须与本机设备匹配；不要直接复制队友的虚拟环境。

1. 在 `CXSDet-YOLOmm/CXSDet/web-flask` 安装 `requirements.txt` 和 `requirements-agent.txt`。
2. 在 `CXSDet-YOLOmm/CXSDet/web-haixun` 运行 `npm ci`；使用这套前端的 `package-lock.json`。
3. 在PowerShell设置 `HAIXUN_PYTHON` 为所用python.exe绝对路径，`OPENCODE_BIN` 为opencode.exe绝对路径；Node在PATH中，或通过 `HAIXUN_NODE` 指定node.exe。也可将Node和OpenCode分别放入仓库 `.runtime/node/node.exe`、`.runtime/opencode/opencode.exe`。
4. 在仓库根目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Check` 检查依赖，然后双击 **启动海巡智检.cmd** 启动前后端。**停止服务.cmd** 只停止同路径的本版本服务；运行中的训练/任务请先处理完毕。
5. 打开 `http://127.0.0.1:5173/`，后端默认端口5020。首次初始化的管理员为 `admin / 123456`，正式使用时修改密码。
6. 在“设置 → 大模型连接”配置自己的Key、地址和模型。创建项目并选择本机文件夹，再导入数据集/权重；聊天与网页AI功能使用相同个人连接。

启动器不会替你安装依赖或下载模型。依赖检查通过后会在后台启动服务，错误与日志见仓库 `.run/`。新安装没有密钥文件是正常的：服务在首次需要时生成本机密钥，不要复制或上传其他人的密钥。已有整合包也可继续使用包根目录原启动入口。

使用自定义端口可运行 `.\start.ps1 -BackendPort 5031 -FrontendPort 5181 -NoBrowser`；前端代理与Agent业务桥同步使用所选后端端口。旧 `web-vue/3000` 保留作参考，本入口不启动它。

## 数据与模型

运行数据库、上传图片、检测记录、训练数据、模型权重、依赖目录和本地密钥不随源码上传。部署后需自行上传数据集、导入或训练匹配的模型并发布，再开始检测。

使用图片巡检时，类别来自所选模型关联的数据集；导入权重时应选择与权重类别编号一致的数据集。AI 仅提供辅助建议，训练反馈标签由人工确认。

## 原项目说明

保留原项目作者及第三方代码中的版权和使用声明；使用范围以原有授权和相关组件许可为准。

## OpenCode 接入开发分支

当前 Agent 默认由本机 OpenCode **1.19.4** 执行，复用网站的业务工具、权限和人工确认。独立体验版与网站会话分开，网站只使用本人设置的模型连接。启动要求、原生文件/命令边界与回滚见 [接入说明](docs/integration/OpenCode接入与参赛说明.md)，开源许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。此前 `haixun-v1.0` 标签保持不变。
