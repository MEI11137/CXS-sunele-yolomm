from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_image_detection_exposes_multi_file_queue_and_rgb_x_stem_pairing():
    view = _read("web-vue/src/views/detection/ImageDetection.vue")
    helper = _read("web-vue/src/utils/batchDetection.ts")

    assert "multiple" in view
    assert "batchQueue" in view
    assert "pairBatchFiles" in view
    assert "for (const item of batchQueue.value)" in view
    assert "item.status = 'failed'" in view
    assert "export const fileStem" in helper
    assert "secondaryModality === 'edge'" in helper
    assert "缺少同名" in helper


def test_detection_records_exposes_batch_review_and_automatic_feedback_workflow():
    view = _read("web-vue/src/views/detection/DetectionRecords.vue")
    api = _read("web-vue/src/api/detection.ts")

    assert 'type="selection"' in view
    assert "批量AI核查" in view
    assert "批量加入反馈" in view
    assert "生成新数据集版本" in view
    assert "source_dataset_name" in view
    assert "feedbackDatasetId" not in view
    assert "reviewRecordsBatch" in api
    assert "addRecordsToFeedbackBatch" in api
