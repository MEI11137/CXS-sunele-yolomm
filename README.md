# 海巡智检 · CXS-sunele-yolomm

基于 Vue 3、Flask 和 YOLO 的巡检系统，支持图片、视频和实时检测，以及视觉大模型辅助核查。

## 本次更新

- 更新巡检前端、工作台和数据集及模型管理页面。
- 图片检测结果支持 AI 辅助核查、人工修改标注和人工确认。
- 人工复核类别按当前记录所用模型关联的数据集读取，保持 YOLO 类别编号顺序。
- 已确认记录可进入反馈池、生成数据集版本，并用于后续增量训练和模型对比。
- 对接 DeepSeek 官方视觉模型，并明确提示 API 认证失败。

## 项目目录

```text
CXSDet-YOLOmm/CXSDet/
├─ web-flask/    # Flask 后端、YOLO 运行时和训练及复核服务
└─ web-vue/      # Vue 3 前端
```

## 启动

后端依赖见 `web-flask/requirements.txt`，前端依赖由 `web-vue/package-lock.json` 锁定。后端依赖文件对应 Python 3.12 和 CUDA 12.1 环境，请按实际设备配置运行环境。

1. 在 `web-flask` 中安装后端依赖：`python -m pip install -r requirements.txt`。
2. 将 `web-flask/algo/llm/config.local.example.json` 复制为同目录的 `config.local.json`，填写自己的 API Key。
3. 在 `web-flask` 中运行 `python app.py`，默认端口为 `5020`。
4. 将 `web-vue/.env.example` 复制为 `.env.local`，按后端地址调整 `VITE_API_URL`。
5. 在 `web-vue` 中运行 `npm ci`，然后运行 `npm run dev`。
6. 浏览器打开 `http://localhost:3000/`。首次初始化的管理员账号为 `admin / 123456`，正式使用时请修改密码。

也可通过 `VL_API_KEY`、`VL_API_URL`、`VL_MODEL` 环境变量覆盖大模型配置。修改配置后需重启后端。

## 数据与模型

运行数据库、上传图片、检测记录、训练数据、模型权重、依赖目录和本地密钥不随源码上传。部署后需自行上传数据集、导入或训练匹配的模型并发布，再开始检测。

使用图片巡检时，类别来自所选模型关联的数据集；导入权重时应选择与权重类别编号一致的数据集。AI 仅提供辅助建议，训练反馈标签由人工确认。

## 原项目说明

保留原项目作者及第三方代码中的版权和使用声明；使用范围以原有授权和相关组件许可为准。
