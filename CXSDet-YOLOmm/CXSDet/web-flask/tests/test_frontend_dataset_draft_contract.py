from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_draft_split_route_is_admin_only_and_uses_named_route():
    router = _read("web-vue/src/router/index.ts")

    assert "DatasetDraftSplit" in router
    assert "path: 'datasets/:draftId/split'" in router
    route = router.split("name: 'DatasetDraftSplit'", 1)[1].split("}", 1)[0]
    assert "requiresAuth: true" in route
    assert "requiresAdmin: true" in route


def test_dataset_list_separates_drafts_from_formal_dataset_actions():
    view = _read("web-vue/src/views/admin/DatasetManagement.vue")

    assert "record_kind" in view
    assert "人工划分" in view
    assert "retryDraft" in view
    assert "生成待划分数据集" in view
    assert "workflow_status" in view


def test_split_view_shows_counts_and_blocks_incomplete_finalization():
    view = _read("web-vue/src/views/admin/DatasetDraftSplit.vue")

    for split in ("unassigned", "train", "val", "test", "excluded"):
        assert split in view
    assert "missingSecondary" in view
    assert "finalizeDraft" in view
    assert "finalizationBlocked" in view
    assert "getDraftSamplePreview" in view
