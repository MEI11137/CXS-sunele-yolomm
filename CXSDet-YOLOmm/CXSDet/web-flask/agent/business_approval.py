"""Trusted built-in business transitions eligible for the user's session mode.

Not a blanket write exemption: human annotations, unknown tools and external MCP
retain their independent approval policies. ``full`` (完全访问) treats every
authorized built-in SOURCE_BUSINESS tool as routine, except capabilities that
would extend the account's local authorization scope (binding a new directory)
or turn an AI suggestion into a human decision (final review result). Those are
refused with an explanation instead of falling back to a clickable card.
"""
from agent.tools.contract import SOURCE_BUSINESS

ROUTINE_TOOLS = frozenset({
    'workspace_export_records_csv',
    'workspace_attach_file', 'workspace_add_feedback', 'business_file_register_local',
    'workspace_start_inspection_loop', 'workspace_route_reviewed_samples',
    'business_detection_run', 'business_review_ai', 'business_feedback_add_confirmed',
    'business_dataset_import', 'business_dataset_transform', 'business_dataset_split',
    'business_dataset_register_candidate', 'business_dataset_feedback_revision',
    'business_draft_create_from_feedback', 'business_draft_assign_splits', 'business_draft_finalize',
    'business_model_import_weight', 'business_model_publish', 'business_model_unpublish',
    'business_training_start', 'business_directory_transform', 'business_document_parse',
})

# 外部 MCP 代理/新授权类调用不属于“内置纯业务”，full 不得自动执行，也不能借它取得新授权。
EXTERNAL_PROXY_TOOLS = frozenset({'mcp.execute', 'business_mcp_execute'})

# full 仍必须由用户本人完成的操作：最终人工结论，以及扩大本机目录授权。
HUMAN_ONLY_OPERATIONS = {
    'workspace_review_record': frozenset({'confirmed', 'no_defect', 'rejected'}),
    'workspace_manage_project': frozenset({'bind'}),
}

HUMAN_ONLY_REASONS = {
    'workspace_review_record': ('最终人工结论（确认/无缺陷/驳回）只能由你本人在复核页提交；'
                                '完全访问模式不会为这一步生成确认卡片。'),
    'workspace_manage_project': ('绑定新目录属于扩大本机授权范围，完全访问模式不会自动执行；'
                                 '请在项目设置里由你本人完成绑定。'),
}


def is_builtin_business_scope(spec, source=None):
    """只有平台内置、无外部服务器、且不是代理外壳的业务工具属于受信范围。"""
    return bool(
        (source or spec.source) == SOURCE_BUSINESS
        and spec.source == SOURCE_BUSINESS
        and not spec.server_id
        and spec.name not in EXTERNAL_PROXY_TOOLS
    )


def full_denial(spec, arguments=None):
    """full 下仍不能自动执行的业务操作及原因；返回原因即表示明确拒绝/引导。"""
    if spec.name in EXTERNAL_PROXY_TOOLS:
        return ('外部 MCP 代理或新授权类调用不在内置业务自动执行范围内，'
                '完全访问模式不会为它生成确认卡片；请改用平台内置工具，或由你本人单独授权。')
    arguments = arguments or {}
    operation = arguments.get('operation')
    if operation in HUMAN_ONLY_OPERATIONS.get(spec.name, ()):
        return HUMAN_ONLY_REASONS.get(spec.name)
    if (spec.name == 'workspace_manage_project' and operation == 'create'
            and str(arguments.get('root_directory') or '').strip()):
        return HUMAN_ONLY_REASONS['workspace_manage_project']
    return None


def automatic_mode(actor, session_id, spec, source=None, arguments=None):
    """返回可自动执行的模式；``None`` 表示不属于自动范围（full 下会被明确拒绝）。"""
    from agent import sessions
    if not is_builtin_business_scope(spec, source):
        return None
    mode = sessions.get_session(actor, session_id).get('permission_mode', 'request')
    if mode == 'assist':
        return mode if spec.name in ROUTINE_TOOLS else None
    if mode != 'full':
        return None
    if full_denial(spec, arguments):
        return None
    return mode