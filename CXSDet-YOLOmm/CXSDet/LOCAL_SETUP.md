# 本机环境说明

本目录是从原始中文路径复制出的英文工作副本，原始项目未删除或修改。

## 已配置环境

- Python 环境：`D:\Anaconda3\envs\py12`
- Python 版本：3.12.15
- Node.js：`C:\Users\A\Desktop\ele-yolomm\.tools\node-v22.2.0-win-x64`
- Node.js 版本：22.2.0
- npm 版本：10.7.0
- 前端依赖：已安装在 `web-vue\node_modules`
- 后端依赖：已安装到 `py12`

API 配置尚未处理，按要求留到最后。

## 启动方式

分别打开两个终端。

后端：

```bat
cd /d C:\Users\A\Desktop\ele-yolomm\photovoltaic-panels-detect-web
start-backend.cmd
```

前端：

```bat
cd /d C:\Users\A\Desktop\ele-yolomm\photovoltaic-panels-detect-web
start-frontend.cmd
```

浏览器访问：`http://localhost:3000/`

后端健康检查：`http://localhost:5020/api/health/health_check`

## 验证说明

- `pip check` 已通过。
- Flask 已在 5020 端口启动并通过健康检查。
- Vite 已在 3000 端口启动，首页与主入口模块均返回 HTTP 200。
- `vite build` 已成功生成 `web-vue\dist`。
- 项目自带的 `npm run build` 会先运行 `vue-tsc -b`，当前会被原始源码中的 TypeScript 类型错误拦截；这不影响教程使用的 `npm run dev`。
- 当前 Torch 为 CPU 版，检测功能可运行，但不会使用 RTX 4060 加速。教程和 `requirements.txt` 未指定 CUDA 版 Torch。

