"""INT-01：同一个 Flask app 内的真实业务服务装配。

设计要点（对应整合文档 INT-01 的目标修改）：

1. 所有初始化都在 ``with app.app_context():`` 内完成，用的是这个 app 自己的
   ``DATABASE``，不再回退到进程默认库。
2. 真实构造 ``FileClient`` / ``LocalFileAssetGateway`` / ``DirectoryStore`` /
   ``McpStore`` / ``BusinessToolAdapter``，按各自真实签名传参。
3. 注入公共权限与资源解析（:mod:`agent.policy_bridge`）、业务变更通知、
   工作目录与候选输出目录、个人模型客户端工厂。
4. 设置 ``PROJECT_FILE_ACCESS``，并把业务后端注册给 Agent 运行时。
5. 实例保存在 ``app.extensions``，B 的 backend/registry 取用当前 app 的实例，
   避免多个 app（或测试库）错误共享同一个业务实例。
6. 缺依赖时**如实报告未就绪**并让对应工具不可用，不用全权限策略、假客户端或
   空成功响应兜底。
7. 默认数据库、文件存储、密钥都指向本仓库 ``web-flask/`` 自己的运行目录。

本模块不做业务逻辑，也不改 C 的 handlers。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from agent.config import settings

EXTENSION_KEY = "haixun_agent"


def _report_not_ready(items: Dict[str, Any], name: str, reason: str) -> None:
    items[name] = f"not_ready: {reason}"


def _candidate_root(app) -> str:
    configured = app.config.get("AGENT_CANDIDATE_ROOT") or os.environ.get(
        "AGENT_CANDIDATE_ROOT"
    )
    if configured:
        return str(configured)
    return str(Path(app.config.get("TEMP_DIR") or Path.cwd()) / "agent_candidates")


def _current_principal() -> Optional[int]:
    """从当前请求上下文取真实用户，取不到就返回 None（绝不猜）。"""
    try:
        from flask import g

        user_id = getattr(g, "user_id", None)
        return int(user_id) if user_id else None
    except Exception:  # noqa: BLE001 - 无应用上下文
        return None


def _model_scope_resolver(model_id: str) -> list:
    """发布/归档的合法范围：同一真实 owner + project 下的模型 ID。"""
    from agent import resources

    return resources.model_scope_ids(model_id)


def _personal_review_factory(principal_getter=None):
    """零参数复核客户端工厂：本人连接 + 视觉能力校验，不做全局回退。"""

    def factory():
        from agent.errors import AgentError, Code
        from agent.identity import load_actor
        from services.personal_review_client import create_personal_review_client

        principal = principal_getter() if principal_getter else _current_principal()
        if not principal:
            raise AgentError(
                Code.UNAUTHENTICATED,
                "无法确定当前用户，不能使用本人模型连接做复核",
            )
        return create_personal_review_client(load_actor(int(principal)))

    return factory


def _personal_model_client_factory(principal_getter=None):
    """零参数工厂：每次调用都按当前用户构造客户端，不持有共享 Key。"""
    return _personal_review_factory(principal_getter)


def configure_business_backend(app, *, overrides: Optional[Dict[str, Any]] = None,
                                force: bool = False) -> Dict[str, Any]:
    """装配真实业务后端，返回可写入日志的装配报告。"""
    existing = app.extensions.get(EXTENSION_KEY)
    if existing and existing.get("business_backend") is not None and not force:
        return existing["report"]

    overrides = dict(overrides or {})
    report: Dict[str, Any] = {"database": None, "dependencies": {}, "notes": []}

    with app.app_context():
        from agent.policy_bridge import AgentPolicyBridge, BusinessNotifier
        from agent.tools import business as business_tools
        from clients.file_client import FileClient
        from services.authorized_directories import DirectoryStore
        from services.business_tools import BusinessToolAdapter
        from services.file_access import LocalFileAssetGateway
        from services.mcp_registry import McpStore

        db_path = str(app.config.get("DATABASE") or "")
        if not db_path:
            raise RuntimeError("app.config['DATABASE'] 未配置，不能装配业务后端")

        base_path = app.config.get("FILE_STORE_PATH")
        work_dir = app.config.get("TEMP_DIR")
        candidate_root = overrides.get("candidate_root") or _candidate_root(app)

        policy = AgentPolicyBridge()
        notifier = BusinessNotifier()
        storage = overrides.get("storage_client") or FileClient(base_path=base_path)
        gateway = overrides.get("file_assets") or LocalFileAssetGateway(
            db_path,
            storage,
            project_access=policy.project_file_access,
            work_dir=work_dir,
        )
        directory_store = overrides.get("directory_store") or DirectoryStore(db_path)
        mcp_store = overrides.get("mcp_store")
        if mcp_store is None:
            from services.mcp_transport import transport_factory, PersonalMcpSecrets
            mcp_store = McpStore(db_path, transport_factory=transport_factory, secret_store=PersonalMcpSecrets())

        # 文件访问策略：C 的上传/预览/下载都读这一项，缺失时上传会直接被拒。
        app.config["PROJECT_FILE_ACCESS"] = policy.project_file_access

        deps: Dict[str, Any] = {}
        if gateway is not None:
            deps["file_assets"] = "ready"
        else:
            _report_not_ready(deps, "file_assets", "项目文件网关未构造")
        if storage is not None:
            deps["storage_client"] = "ready"
        else:
            _report_not_ready(deps, "storage_client", "文件存储客户端未构造")
        if directory_store is not None:
            deps["directory_store"] = "ready"
        else:
            _report_not_ready(deps, "directory_store", "授权目录存储未构造")
        if mcp_store is not None and getattr(mcp_store, "transport_factory", None) is not None:
            deps["mcp_store"] = "ready"
        else:
            # 没有真实 transport 时不把 MCP 工具交给模型，避免"能看见但一定失败"。
            mcp_store = None
            _report_not_ready(deps, "mcp_store", "MCP 真实传输尚未接入（INT-10），MCP 工具已隐藏")

        adapter = BusinessToolAdapter(
            db_path,
            require_access=overrides.get("require_access") or policy.require_access,
            resolve_resource=overrides.get("resolve_resource") or policy.resolve_resource,
            storage_exists=getattr(storage, "exists", None),
            file_assets=gateway,
            storage_client=storage,
            model_client_factory=overrides.get("model_client_factory")
            or _personal_model_client_factory(),
            # 复核走同一份本人连接，但先过视觉能力校验（认证成功≠能看图）
            review_client_factory=overrides.get("review_client_factory")
            or _personal_review_factory(),
            detector_loader=overrides.get("detector_loader"),
            notify=overrides.get("notify") or notifier,
            work_dir=work_dir,
            directory_store=directory_store,
            mcp_store=mcp_store,
            candidate_root=candidate_root,
            training_launcher=overrides.get("training_launcher"),
            # 发布/归档范围按真实 owner + project 计算，不按整表同模态
            model_scope_resolver=overrides.get("model_scope_resolver")
            or _model_scope_resolver,
        )
        deps["model_client_factory"] = (
            "ready: per-user personal connection with structured vision capability checks"
        )
        deps["training_launcher"] = (
            "ready: multiprocessing worker" if overrides.get("training_launcher") is None
            else "ready: injected launcher"
        )

        # 历史数据的归属迁移（幂等）：已确认的默认资源按共享只读登记，其余按原
        # 提交者/管理员登记为私有；判定不出归属的不公开。
        from agent import legacy

        try:
            migrated = legacy.migrate_legacy_resources()
            report["legacy_migration"] = migrated
        except Exception as exc:  # noqa: BLE001 - 迁移失败要如实报告，不静默跳过
            report["legacy_migration"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            report["system_default_manifest"] = legacy.system_default_manifest()
        except Exception as exc:  # noqa: BLE001
            report["system_default_manifest"] = {"error": f"{type(exc).__name__}: {exc}"}

        app.extensions[EXTENSION_KEY] = {
            "business_backend": adapter,
            "policy": policy,
            "notifier": notifier,
            "storage": storage,
            "gateway": gateway,
            "directory_store": directory_store,
            "mcp_store": mcp_store,
            "report": report,
        }
        # 兼容 B 原有的模块级取用方式（脚本/无上下文场景），app 内取用优先 extensions。
        business_tools.set_backend(adapter)

        report.update(
            {
                "database": db_path,
                "file_store": str(base_path),
                "work_dir": str(work_dir),
                "candidate_root": candidate_root,
                "project_file_access": "installed",
                "dependencies": deps,
                # 替身是否可用要能在启动报告里被核实，不能只看环境变量
                "allow_stub_tools": settings().allow_stub_tools,
                "notes": [
                    "新数据集/模型/记录/草稿在业务成功后按真实归属登记，状态变化递增版本（INT-03）",
                    "历史数据按原提交者/管理员登记，未判定归属的不公开；清单见 system_default_manifest",
                    "工具清单是否可见取决于本次请求的真实 principal",
                    "工具替身默认关闭，需显式 AGENT_ALLOW_STUB=1；正式工具故障不会按同名回落（INT-04 R3）",
                ],
            }
        )
    return report


def get_business_backend(app):
    """取当前 app 装配好的业务后端；未装配返回 None。"""
    entry = app.extensions.get(EXTENSION_KEY) or {}
    return entry.get("business_backend")


def get_policy(app):
    entry = app.extensions.get(EXTENSION_KEY) or {}
    return entry.get("policy")


def reset_business_backend(app) -> None:
    """测试切换 app/数据库时使用。"""
    from agent.tools import business as business_tools

    app.extensions.pop(EXTENSION_KEY, None)
    business_tools.set_backend(None)
