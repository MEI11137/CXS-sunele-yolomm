import json
import sqlite3

import pytest


def _database(tmp_path):
    path = tmp_path / "review.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE detection_records (
            id TEXT PRIMARY KEY, detection_result TEXT,
            human_review_status TEXT DEFAULT 'pending',
            corrected_annotations TEXT, reviewed_by INTEGER, reviewed_at TEXT,
            review_version INTEGER DEFAULT 0
        );
        CREATE TABLE detection_record_reviews (
            id TEXT PRIMARY KEY, record_id TEXT NOT NULL, reviewer_id INTEGER NOT NULL,
            decision TEXT NOT NULL, annotations TEXT NOT NULL,
            previous_version INTEGER NOT NULL, new_version INTEGER NOT NULL,
            create_time TEXT NOT NULL
        );
        INSERT INTO detection_records (id, detection_result) VALUES ('r1', '[]');
        """
    )
    conn.commit()
    conn.close()

    class Db:
        def get_connection(self):
            return sqlite3.connect(path)

    return Db(), path


def test_human_review_is_versioned_and_keeps_an_audit_snapshot(tmp_path):
    from services.review_service import submit_human_review

    db, path = _database(tmp_path)
    annotations = [{"class_name": "crack", "class_id": 1, "bbox": [0.1, 0.2, 0.7, 0.8]}]
    result = submit_human_review("r1", 9, 0, annotations, "confirmed", db)

    assert result.new_version == 1
    conn = sqlite3.connect(path)
    current = conn.execute(
        "SELECT human_review_status, corrected_annotations, review_version FROM detection_records WHERE id='r1'"
    ).fetchone()
    audit = conn.execute(
        "SELECT decision, previous_version, new_version, annotations FROM detection_record_reviews"
    ).fetchone()
    conn.close()
    assert current[0] == "confirmed"
    assert json.loads(current[1]) == annotations
    assert current[2] == 1
    assert audit[:3] == ("confirmed", 0, 1)
    assert json.loads(audit[3]) == annotations


@pytest.mark.parametrize(
    "annotations",
    [
        [{"class_name": "unknown", "class_id": 99, "bbox": [0.1, 0.1, 0.2, 0.2]}],
        [{"class_name": "crack", "class_id": 1, "bbox": [-0.1, 0.1, 0.2, 0.2]}],
        [{"class_name": "crack", "class_id": 1, "bbox": [0.7, 0.1, 0.2, 0.2]}],
    ],
)
def test_human_review_rejects_invalid_classes_and_boxes(tmp_path, annotations):
    from services.review_service import ReviewValidationError, submit_human_review

    db, _ = _database(tmp_path)
    with pytest.raises(ReviewValidationError):
        submit_human_review("r1", 9, 0, annotations, "confirmed", db)


def test_empty_annotations_can_explicitly_confirm_no_defect(tmp_path):
    from services.review_service import submit_human_review

    db, _ = _database(tmp_path)
    result = submit_human_review("r1", 9, 0, [], "no_defect", db)
    assert result.status == "confirmed"


def test_human_review_detects_optimistic_lock_conflict(tmp_path):
    from services.review_service import ReviewConflictError, submit_human_review

    db, _ = _database(tmp_path)
    submit_human_review("r1", 9, 0, [], "no_defect", db)
    with pytest.raises(ReviewConflictError):
        submit_human_review("r1", 9, 0, [], "no_defect", db)
