from utils.models import DetectionRecord, DetectionRecordVO


def test_detection_record_dto_exposes_source_dataset_without_manual_uuid():
    record = DetectionRecord(
        {
            "id": "r1",
            "model_id": "m1",
            "model_name": "model",
            "source_dataset_id": "d1",
            "source_dataset_name": "原训练集",
        }
    )

    result = DetectionRecordVO(record).to_dict()

    assert result["source_dataset_id"] == "d1"
    assert result["source_dataset_name"] == "原训练集"
