# 海巡智检 v1 整合来源登记

- 日期：2026-10-26，Asia/Shanghai
- 仓库：https://github.com/MEI11137/CXS-sunele-yolomm
- 整合分支：`integration/haixun-v1`（本地分支，未推送）

## 固定提交

| 范围 | 分支 | 固定提交 | 合入方式 |
| --- | --- | --- | --- |
| 集成基线 | `main` | `080e7da00577e456d51056317d369436821ffa0a` | 分支起点 |
| A：新前端 | `feature/haixun-web-frontend` | `4da77011e89afdbc5a731a1d0637d9e1da502120` | 独立根历史，按子树迁入 |
| B：Agent/公共后端 | `feature/agent-backend` | `e7dc4784557f97ff3a555b5ad7ba555249445acf` | `git merge --no-ff` |
| C：业务/文件/文档/MCP | `feature/business-tools-c01-c11` | `443dc905aa7bc87b3200072f456244f8f8177b49` | `git merge --no-ff` |

合入前已核对 `origin` 上四个分支的 SHA 与《下载版本清单.json》完全一致，未被后续提交替换。

## A 前端迁入方式

A 分支是独立根历史，禁止与后端根目录合并（不使用 `--allow-unrelated-histories`）。
本次用 `git archive <A-SHA> | tar -x -C CXSDet-YOLOmm/CXSDet/web-haixun` 按固定提交导出，
保证内容取自 Git 对象而非工作区文件。

核对结果：迁入 50 个文件，逐文件 blob 哈希与 `4da77011` 的原树 **全部一致**。

- 目标目录：`CXSDet-YOLOmm/CXSDet/web-haixun/`
- 后端仓库根 `package.json` 未被覆盖（后端根原本没有该文件，A 的 `package.json` 只进入 `web-haixun/`）
- `CXSDet-YOLOmm/CXSDet/web-vue/` 原样保留，作为过渡兼容入口
- 未复制 `node_modules`，两套前端各自使用自己的依赖锁

## 本轮范围声明

本轮只做来源整合与目录迁入。功能**尚未**接通：C 的 `BusinessToolAdapter` 未在 `app.py` 装配，
B 的业务后端未注入，新前端仍固定使用 MockAdapter。这些属于 INT-01 及之后的任务。

本轮未启动网站、未调用真实模型、未执行训练、未运行测试。
