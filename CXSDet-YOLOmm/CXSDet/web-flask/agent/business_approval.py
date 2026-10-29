"""Trusted built-in business transitions eligible for the user's session mode.

Not a blanket write exemption: native file changes, human decisions, deletion,
unknown tools and external MCP retain their independent approval policies.
"""
from agent.tools.contract import SOURCE_BUSINESS

ROUTINE_TOOLS = frozenset({
    'workspace_export_records_csv',
    'workspace_attach_file', 'workspace_add_feedback',
    'workspace_start_inspection_loop', 'workspace_route_reviewed_samples',
    'business_detection_run', 'business_review_ai', 'business_feedback_add_confirmed',
    'business_dataset_import', 'business_dataset_transform', 'business_dataset_split',
    'business_dataset_register_candidate', 'business_dataset_feedback_revision',
    'business_draft_create_from_feedback', 'business_draft_assign_splits', 'business_draft_finalize',
    'business_model_import_weight', 'business_model_publish', 'business_model_unpublish',
    'business_training_start', 'business_directory_transform', 'business_document_parse',
})


def automatic_mode(actor, session_id, spec, source=None):
    from agent import sessions
    if ((source or spec.source) != SOURCE_BUSINESS or spec.source != SOURCE_BUSINESS
            or spec.server_id or spec.name not in ROUTINE_TOOLS):
        return None
    mode = sessions.get_session(actor, session_id).get('permission_mode', 'request')
    return mode if mode in {'assist', 'full'} else None
