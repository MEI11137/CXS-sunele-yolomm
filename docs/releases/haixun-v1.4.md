# 海巡智检 v1.4

版本号：1.4.0。日期：2026-11-03。

源码分支：release/haixun-v1.4.0。基于已上传的修复提交 67f276f；保留原 v1.3 与协作分支历史。本次汇总并统一版本名称，没有另外替换 Agent 执行框架。

## 下载

- [浏览 v1.4 源码](https://github.com/MEI11137/CXS-sunele-yolomm/tree/release/haixun-v1.4.0)
- [下载 v1.4 源码 ZIP](https://github.com/MEI11137/CXS-sunele-yolomm/archive/refs/heads/release/haixun-v1.4.0.zip)

```powershell
git clone --branch release/haixun-v1.4.0 --single-branch https://github.com/MEI11137/CXS-sunele-yolomm.git 海巡智检v1.4
```

克隆或解压后先阅读根目录“先读我_另一台电脑部署.md”。已有部署应保留并备份服务器自己的运行库、密钥及演示配置，核对未提交修改后更新。

## 包含的修复与实测

- 最终回答收尾及前端消息轮询、训练取消状态和占用锁、初始训练失败遗留记录、数据集导出路径、图片增强登记和个人多模态连接修正。
- 中文 PT 文件名保留后缀，普通演示账号在预绑定工作目录内获得闭环工具，回流后自动打开项目数据集页。
- 服务商错误使用白名单中文提示；迁移只处理未登记的旧检测记录，避免为新项目额外生成空历史项目。
- 普通角色通过真实网页与内嵌 Agent 完成三组 RGB/IR 检测、AI核查、页面人工提交、回原修订和另建数据集；实际核对工作目录文件、修正标签和导出哈希。详见 INT-49、INT-50 的公开摘要。

## 仍需另一台电脑验收

- 模型上游繁忙时出现过 503 和超时，部分请求重新发起才成功；不能称为对话始终稳定。
- GPU 实际训练、训练后效果对比、外地公网入口与全部客户端操作尚未逐项真实验收。
- 本机实测不等于服务器环境已配置完成。

## 源码交付边界

GitHub 源码包含前后端、部署脚本、测试与说明，不带 dist、node_modules、运行数据库、API Key、加密密钥、个人原始数据、权重、Python/Node 环境或引擎二进制。

前端执行 npm ci 和 npm run build。按本机实际环境配置 HAIXUN_PYTHON、HAIXUN_NODE 与 HX_ENGINE_BIN，再使用检查环境.cmd / 启动海巡智检.cmd。引擎版本仍为已验证的 1.19.4，与网站版本 1.4.0 分开管理。原始作者与第三方许可声明继续保留。
