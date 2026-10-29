"""仅扩展 OpenCode 的网站工具：打开项目页面、查资源、登记项目内本地文件。"""
import hashlib
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import current_app
from agent import permissions, tasks
from agent.errors import AgentError, Code
from agent.project_folders import checked_project_root
from agent.tools.contract import ToolProvider, ToolSpec, OperationResult, RISK_READ, RISK_WRITE

PAGES = {"inspection": "图片巡检", "records": "识别记录", "review": "人工复核", "datasets": "数据集", "models": "模型管理", "manual": "资料与手册", "extensions": "扩展与连接"}


class ProjectTools(ToolProvider):
    def list_tools(self, actor):
        base = {"project_id": {"type": "string", "description": "当前会话的项目 ID"}}
        def spec(name, title, props, required, risk=RISK_READ):
            return ToolSpec(name=name, title=title, description=title,
                parameters={"type": "object", "properties": {**base, **props}, "required": ["project_id", *required], "additionalProperties": False},
                risk=risk, supports_preview=risk == RISK_WRITE, supports_status=False)
        result = [spec("workspace_open_page", "为用户打开当前项目的业务页面；只展示界面，不提交人工审核或执行业务写入", {
            "page": {"type": "string", "enum": list(PAGES)},
            "record_id": {"type": "string", "description": "可选，人工复核时指定当前项目的真实检测记录 ID"},
            "draft_id": {"type": "string", "description": "数据集页面打开指定真实草稿"},
            "tab": {"type": "string", "enum": ['images', 'feedback', 'drafts']},
            "status": {"type": "string", "enum": ['all', 'pending', 'done', 'rejected'], "description": "识别记录页面状态筛选"}}, ["page"]),
            spec("workspace_list_resources", "列出当前项目的数据集、模型、识别记录、上传文件和已授权目录，返回真实 ID", {}, [])]
        if actor.is_admin:
            result.append(spec("workspace_attach_file", "把当前项目目录中的 ZIP、PT、图片或资料登记成上传附件；确认后复制保存，原文件不改。随后调用已有导入/检测工具处理附件 ID", {
                "path": {"type": "string", "description": "项目目录内的相对路径或绝对文件路径"}}, ["path"], RISK_WRITE))
        return result

    def _source(self, actor, arguments):
        project = permissions.get_project(actor, arguments["project_id"])
        root = checked_project_root(actor, project)
        if root is None:
            raise AgentError(Code.VALIDATION, "请先为此项目绑定本机文件夹，或在网页上传文件")
        try:
            candidate = Path(arguments["path"])
            source = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError()
        except (OSError, ValueError):
            raise AgentError(Code.VALIDATION, "只能上传当前项目目录内已存在的文件") from None
        from routes.file import _PROJECT_TYPES, _validate_project_file
        from config import FILE_STORE_CONFIG
        if source.suffix.lower() not in _PROJECT_TYPES or source.stat().st_size > FILE_STORE_CONFIG["max_size"]:
            raise AgentError(Code.VALIDATION, "不支持此文件类型或文件超过大小限制")
        try:
            kind, mime = _validate_project_file(source, source.suffix.lower())
        except ValueError as exc:
            raise AgentError(Code.VALIDATION, str(exc)) from exc
        with source.open("rb") as input_file:
            digest = hashlib.file_digest(input_file, "sha256").hexdigest()
        return source, kind, mime, digest

    def prepare(self, actor, spec, arguments, context):
        source, kind, mime, digest = self._source(actor, arguments)
        return {"prepared_id": digest, "preview": {"project_id": arguments["project_id"], "file": str(source), "size": source.stat().st_size,
                "impact": "复制并登记为当前项目附件；不会修改原文件，也不会自动导入数据集、加载权重或开始训练"}}

    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        project_id = context["project_id"]
        if arguments["project_id"] != project_id:
            raise AgentError(Code.FORBIDDEN, "页面与附件工具只能作用于当前会话项目", http_status=403)
        permissions.get_project(actor, project_id)
        if spec.name == "workspace_list_resources":
            from services.authorized_directories import DirectoryStore
            from agent import resources as resource_store
            resources = permissions.list_project_resources(actor, project_id)
            resources = [resource for resource in resources if resource.get('source_table') != 'detection_records'
                or (resource_store.business_row('detection_records', resource.get('source_id')) or {}).get('record_status') != 'deleted']
            return OperationResult(outputs={"project_id": project_id, "resources": resources,
                "directories": DirectoryStore(current_app.config["DATABASE"]).list(project_id)})
        if spec.name == "workspace_open_page":
            page = arguments["page"]
            path = f"/projects/{quote(project_id, safe='')}/{page}"
            record_id = arguments.get("record_id")
            if record_id:
                if page != "review":
                    raise AgentError(Code.VALIDATION, "记录 ID 仅用于人工复核页面")
                resource = permissions.resolve_resource(actor, {"source_table": "detection_records", "source_id": record_id})
                if resource["project_id"] != project_id:
                    raise AgentError(Code.FORBIDDEN, "检测记录不属于当前项目", http_status=403)
                path += "/" + quote(resource.get("source_id") or record_id, safe="")
            query = {}
            if arguments.get('draft_id'):
                draft = permissions.resolve_resource(actor, {'kind': 'draft', 'id': arguments['draft_id']})
                if page != 'datasets' or draft['project_id'] != project_id:
                    raise AgentError(Code.FORBIDDEN, '草稿不属于目标项目数据集页面', http_status=403)
                query.update(draft=arguments['draft_id'], tab='drafts')
            if arguments.get('tab'):
                if page != 'datasets': raise AgentError(Code.VALIDATION, 'tab 仅用于数据集页面')
                query['tab'] = arguments['tab']
            if arguments.get('status'):
                if page != 'records': raise AgentError(Code.VALIDATION, 'status 仅用于识别记录页面')
                query['status'] = arguments['status']
            if query: path += '?' + urlencode(query)
            task = tasks.get_task(actor, context["task_id"])
            navigation = {"project_id": project_id, "path": path, "title": PAGES[page]}
            tasks.append_event(task["session_id"], task["id"], "workspace.navigate", navigation)
            return OperationResult(outputs={"navigation_requested": navigation, "message": "已向当前项目界面发送打开请求；人工复核仍由用户提交"})
        source, kind, mime, digest = self._source(actor, arguments)
        if not prepared_id or digest != prepared_id:
            raise AgentError(Code.ACTION_STALE, "文件已改变，请重新预览确认后上传", http_status=409)
        return register_local_asset(actor, project_id, source, kind, mime, digest)


def register_local_asset(actor, project_id, source, kind, mime, digest):
    from services.project_storage import project_scope
    with project_scope(current_app.config['DATABASE'], project_id):
        return _register_local_asset(actor, project_id, source, kind, mime, digest)


def _register_local_asset(actor, project_id, source, kind, mime, digest):
        """共享登记实现；调用者必须先验证来源（项目文件或已批准任务的私有副本）。"""
        permissions.get_project(actor, project_id)
        from routes.file import _storage, _access
        storage, access = _storage(), _access()
        saved = storage.upload("project_uploads", str(source))
        file_id = None
        try:
            file_id = access.register_upload(actor.user_id, saved["bucket"], saved["objectKey"], kind, mime,
                source.stat().st_size, project_id, original_name=source.name, sha256=digest, status="uploaded")
            resource_kind = "dataset" if source.suffix.lower() == ".zip" else "model" if source.suffix.lower() == ".pt" else "image" if kind == "image" else "manual"
            resource = permissions.register_resource(actor, project_id=project_id, kind=resource_kind,
                display_name=source.name, metadata={"file_id": file_id, "content_type": mime})
        except Exception:
            storage.delete(saved["bucket"], saved["objectKey"])
            if file_id:
                from agent.store import connect
                conn = connect()
                try:
                    conn.execute("DELETE FROM file_assets WHERE id=? AND owner_id=?", (file_id, actor.user_id))
                    conn.commit()
                finally:
                    conn.close()
            raise
        return OperationResult(outputs={"asset_id": file_id, "resource_id": resource["id"], "project_id": project_id, "file": source.name, "status": "uploaded", "next": "附件已登记，业务导入/检测仍调用已有工具"})
