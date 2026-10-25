import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest


class DraftDatabase:
    def __init__(self):
        self.uri = f"file:draft-tests-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.connection = sqlite3.connect(self.uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE dataset_drafts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                creator_id INTEGER, source_type TEXT NOT NULL,
                source_dataset_id TEXT, video_detection_record_id TEXT,
                dataset_mode TEXT NOT NULL, primary_modality TEXT NOT NULL,
                secondary_modality TEXT, workflow_status TEXT NOT NULL,
                metadata_json TEXT NOT NULL, error_message TEXT,
                create_time TEXT NOT NULL, update_time TEXT NOT NULL,
                finalized_at TEXT
            );
            CREATE TABLE dataset_draft_samples (
                id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, sample_key TEXT NOT NULL,
                split TEXT NOT NULL, rgb_bucket TEXT, rgb_object_key TEXT,
                secondary_image_bucket TEXT, secondary_image_object_key TEXT,
                label_bucket TEXT, label_object_key TEXT, annotations_json TEXT NOT NULL,
                frame_index INTEGER, primary_timestamp REAL,
                secondary_frame_index INTEGER, secondary_timestamp REAL,
                pairing_error_ms REAL, pairing_state TEXT NOT NULL,
                create_time TEXT NOT NULL, update_time TEXT NOT NULL
            );
            CREATE UNIQUE INDEX uq_dataset_draft_name ON dataset_drafts(name);
            CREATE UNIQUE INDEX uq_dataset_draft_sample_key
                ON dataset_draft_samples(draft_id, sample_key);
            """
        )

    def get_connection(self):
        return sqlite3.connect(self.uri, uri=True)


@pytest.fixture
def database():
    return DraftDatabase()


def draft_config(**overrides):
    return {
        "id": "draft-1",
        "name": "巡检数据集",
        "description": "视频抽帧",
        "creator_id": 9,
        "source_type": "video",
        "source_dataset_id": None,
        "video_detection_record_id": "video-1",
        "dataset_mode": "single",
        "primary_modality": "rgb",
        "secondary_modality": None,
        "metadata": {"class_names": ["缺陷"]},
        **overrides,
    }


def sample_payload(sample_id="sample-1", **overrides):
    return {
        "id": sample_id,
        "sample_key": sample_id,
        "rgb_bucket": "images",
        "rgb_object_key": f"{sample_id}.jpg",
        "label_bucket": "labels",
        "label_object_key": f"{sample_id}.txt",
        "annotations": [],
        "pairing_state": "not_required",
        **overrides,
    }


def test_draft_domain_constants_cover_persisted_states():
    from services.dataset_draft_service import (
        DRAFT_STATES,
        PAIRING_STATES,
        SAMPLE_SPLITS,
    )

    assert DRAFT_STATES == {"generating", "draft", "failed", "finalized"}
    assert SAMPLE_SPLITS == {"unassigned", "train", "val", "test", "excluded"}
    assert PAIRING_STATES == {"not_required", "paired", "missing"}


def test_draft_results_serialize_nested_metadata_without_flask_context():
    from services.dataset_draft_service import DraftDatasetResult, DraftSampleResult

    now = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc).isoformat()
    draft = DraftDatasetResult(
        id="draft-1",
        name="线路巡检",
        description="每两帧抽一帧",
        creator_id=7,
        source_type="video",
        dataset_mode="multimodal",
        primary_modality="rgb",
        secondary_modality="thermal",
        workflow_status="generating",
        metadata={"class_names": ["defect"]},
        error_message=None,
        create_time=now,
        update_time=now,
    )
    sample = DraftSampleResult(
        id="sample-1",
        draft_id="draft-1",
        sample_key="frame_000002",
        split="unassigned",
        pairing_state="paired",
        frame_index=2,
        primary_timestamp=0.08,
        secondary_frame_index=3,
        secondary_timestamp=0.1,
        pairing_error_ms=20.0,
        annotations=[{"class_id": 0, "confidence": 0.9}],
    )

    assert draft.to_dict() == {
        "id": "draft-1",
        "name": "线路巡检",
        "description": "每两帧抽一帧",
        "creator_id": 7,
        "source_type": "video",
        "source_dataset_id": None,
        "video_detection_record_id": None,
        "dataset_mode": "multimodal",
        "primary_modality": "rgb",
        "secondary_modality": "thermal",
        "workflow_status": "generating",
        "metadata": {"class_names": ["defect"]},
        "error_message": None,
        "create_time": now,
        "update_time": now,
    }
    assert sample.to_dict()["annotations"] == [
        {"class_id": 0, "confidence": 0.9}
    ]
    assert sample.to_dict()["sample_key"] == "frame_000002"


def test_create_draft_starts_generating_and_serializes_metadata(database):
    from services.dataset_draft_service import create_draft

    result = create_draft(draft_config(), database)

    assert result.workflow_status == "generating"
    row = database.connection.execute(
        "SELECT metadata_json, name FROM dataset_drafts WHERE id='draft-1'"
    ).fetchone()
    assert row["name"] == "巡检数据集"
    assert json.loads(row["metadata_json"]) == {"class_names": ["缺陷"]}


def test_draft_name_must_be_unique_across_formal_and_draft_datasets(database):
    from services.dataset_draft_service import DraftValidationError, create_draft

    with database.get_connection() as conn:
        conn.execute("INSERT INTO datasets VALUES ('formal-1', '巡检数据集')")
    with pytest.raises(DraftValidationError):
        create_draft(draft_config(), database)


def test_assign_draft_samples_updates_only_owned_samples(database):
    from services.dataset_draft_service import (
        add_draft_sample,
        assign_draft_samples,
        create_draft,
    )

    create_draft(draft_config(), database)
    add_draft_sample("draft-1", sample_payload("sample-1"), database)
    add_draft_sample("draft-1", sample_payload("sample-2"), database)
    from services.dataset_draft_service import mark_draft_ready
    mark_draft_ready("draft-1", database)

    result = assign_draft_samples("draft-1", ["sample-1", "sample-2"], "train", database)

    assert result["updated"] == 2
    assert [row[0] for row in database.connection.execute(
        "SELECT split FROM dataset_draft_samples ORDER BY id"
    )] == ["train", "train"]


def test_generated_draft_rejects_new_samples(database):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        create_draft,
        mark_draft_ready,
    )

    create_draft(draft_config(), database)
    mark_draft_ready("draft-1", database)

    with pytest.raises(DraftValidationError):
        add_draft_sample("draft-1", sample_payload("late-sample"), database)


def test_repair_secondary_image_requires_owned_sample_and_multimodal_draft(database):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        create_draft,
        mark_draft_ready,
        repair_draft_sample_secondary,
    )

    create_draft(draft_config(dataset_mode="multimodal", secondary_modality="thermal"), database)
    add_draft_sample("draft-1", sample_payload("sample-1", pairing_state="missing"), database)
    mark_draft_ready("draft-1", database)
    assert repair_draft_sample_secondary(
        "draft-1", "sample-1", {"bucket": "images", "objectKey": "thermal.jpg"},
        database, frame_index=12, timestamp=0.4,
    ) == {"updated": 1}
    row = database.connection.execute(
        "SELECT secondary_image_object_key, secondary_frame_index, secondary_timestamp, pairing_state "
        "FROM dataset_draft_samples WHERE id='sample-1'"
    ).fetchone()
    assert tuple(row) == ("thermal.jpg", 12, 0.4, "paired")
    with pytest.raises(DraftValidationError, match="不属于"):
        repair_draft_sample_secondary(
            "draft-1", "foreign-sample", {"bucket": "images", "objectKey": "x.jpg"}, database
        )


def test_failed_video_draft_retry_resets_samples_but_keeps_identity(database):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        create_draft,
        mark_draft_failed,
        reset_failed_draft_for_retry,
    )

    create_draft(draft_config(metadata={"frame_interval": 3}), database)
    add_draft_sample(
        "draft-1",
        sample_payload("sample-1", secondary_image_bucket="images", secondary_image_object_key="x.jpg"),
        database,
    )
    mark_draft_failed("draft-1", "interrupted", database)

    class Storage:
        deleted = []

        def delete(self, bucket, key):
            self.deleted.append((bucket, key))

    storage = Storage()
    result = reset_failed_draft_for_retry("draft-1", database, storage)
    assert result["id"] == "draft-1"
    assert result["name"] == "巡检数据集"
    assert result["metadata"] == {"frame_interval": 3}
    assert result["workflow_status"] == "generating"
    assert set(storage.deleted) == {
        ("images", "sample-1.jpg"), ("images", "x.jpg"), ("labels", "sample-1.txt")
    }
    assert database.connection.execute(
        "SELECT COUNT(*) FROM dataset_draft_samples WHERE draft_id='draft-1'"
    ).fetchone()[0] == 0
    with pytest.raises(DraftValidationError, match="只有生成失败"):
        reset_failed_draft_for_retry("draft-1", database, storage)


@pytest.mark.parametrize("split", ["other", "", None])
def test_assign_draft_samples_rejects_invalid_split(database, split):
    from services.dataset_draft_service import DraftValidationError, assign_draft_samples

    with pytest.raises(DraftValidationError):
        assign_draft_samples("draft-1", ["sample-1"], split, database)


def test_assign_draft_samples_rejects_cross_draft_sample_without_partial_update(database):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        assign_draft_samples,
        create_draft,
    )

    create_draft(draft_config(), database)
    create_draft(draft_config(id="draft-2", name="另一数据集"), database)
    add_draft_sample("draft-1", sample_payload("sample-1"), database)
    add_draft_sample("draft-2", sample_payload("sample-2"), database)
    from services.dataset_draft_service import mark_draft_ready
    mark_draft_ready("draft-1", database)
    mark_draft_ready("draft-2", database)

    with pytest.raises(DraftValidationError):
        assign_draft_samples("draft-1", ["sample-1", "sample-2"], "train", database)
    assert database.connection.execute(
        "SELECT split FROM dataset_draft_samples WHERE id='sample-1'"
    ).fetchone()[0] == "unassigned"


def test_assign_draft_samples_rolls_back_when_database_update_fails(database):
    from services.dataset_draft_service import (
        add_draft_sample,
        assign_draft_samples,
        create_draft,
    )

    create_draft(draft_config(), database)
    add_draft_sample("draft-1", sample_payload("sample-1"), database)
    add_draft_sample("draft-1", sample_payload("sample-2"), database)
    from services.dataset_draft_service import mark_draft_ready
    mark_draft_ready("draft-1", database)
    with database.get_connection() as conn:
        conn.executescript(
            """
            CREATE TRIGGER reject_second_assignment
            BEFORE UPDATE ON dataset_draft_samples WHEN OLD.id='sample-2'
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        assign_draft_samples("draft-1", ["sample-1", "sample-2"], "train", database)
    assert [row[0] for row in database.connection.execute(
        "SELECT split FROM dataset_draft_samples ORDER BY id"
    )] == ["unassigned", "unassigned"]


def ready_draft(database, *, mode="single", secondary_modality=None):
    from services.dataset_draft_service import add_draft_sample, create_draft, mark_draft_ready

    create_draft(
        draft_config(
            dataset_mode=mode,
            secondary_modality=secondary_modality,
        ),
        database,
    )
    add_draft_sample(
        "draft-1",
        sample_payload(
            "train-1",
            split="train",
            secondary_image_bucket="thermal" if secondary_modality else None,
            secondary_image_object_key="train-1.png" if secondary_modality else None,
            pairing_state="paired" if secondary_modality else "not_required",
            annotations=[{"class_id": 0}],
        ),
        database,
    )
    add_draft_sample(
        "draft-1",
        sample_payload(
            "val-1",
            split="val",
            secondary_image_bucket="thermal" if secondary_modality else None,
            secondary_image_object_key="val-1.png" if secondary_modality else None,
            pairing_state="paired" if secondary_modality else "not_required",
        ),
        database,
    )
    add_draft_sample(
        "draft-1",
        sample_payload(
            "train-2",
            split="train",
            secondary_image_bucket="thermal" if secondary_modality else None,
            secondary_image_object_key="train-2.png" if secondary_modality else None,
            pairing_state="paired" if secondary_modality else "not_required",
        ),
        database,
    )
    mark_draft_ready("draft-1", database)


def test_finalization_requires_train_and_val_and_no_unassigned_samples(database):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        create_draft,
        mark_draft_ready,
        validate_draft_for_finalization,
    )

    create_draft(draft_config(), database)
    add_draft_sample("draft-1", sample_payload("train", split="train"), database)
    add_draft_sample("draft-1", sample_payload("val", split="val"), database)
    add_draft_sample("draft-1", sample_payload("still-unassigned"), database)
    mark_draft_ready("draft-1", database)
    with pytest.raises(DraftValidationError):
        validate_draft_for_finalization("draft-1", database)


@pytest.mark.parametrize(
    ("splits", "message"),
    [(["val", "unassigned"], "train"), (["train", "unassigned"], "val")],
)
def test_finalization_requires_nonempty_train_and_val(database, splits, message):
    from services.dataset_draft_service import (
        DraftValidationError,
        add_draft_sample,
        create_draft,
        validate_draft_for_finalization,
    )

    create_draft(draft_config(), database)
    for index, split in enumerate(splits):
        add_draft_sample(
            "draft-1",
            sample_payload(f"sample-{index}", split=split),
            database,
        )
    from services.dataset_draft_service import mark_draft_ready
    mark_draft_ready("draft-1", database)
    with pytest.raises(DraftValidationError):
        validate_draft_for_finalization("draft-1", database)


def test_finalization_exempts_excluded_samples_but_accepts_empty_label_object(database):
    from services.dataset_draft_service import validate_draft_for_finalization

    ready_draft(database)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE dataset_draft_samples SET label_object_key=NULL, split='excluded' "
            "WHERE id='train-1'"
        )
        conn.execute(
            "UPDATE dataset_draft_samples SET annotations_json='[]' WHERE id='val-1'"
        )

    result = validate_draft_for_finalization("draft-1", database)

    assert result["counts"] == {"unassigned": 0, "train": 1, "val": 1, "test": 0, "excluded": 1}
    assert result["class_names"] == ["缺陷"]


def test_finalization_rejects_missing_secondary_image_and_missing_label(database):
    from services.dataset_draft_service import DraftValidationError, validate_draft_for_finalization

    ready_draft(database, mode="multimodal", secondary_modality="thermal")
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE dataset_draft_samples SET secondary_image_object_key=NULL "
            "WHERE id='train-1'"
        )
    with pytest.raises(DraftValidationError):
        validate_draft_for_finalization("draft-1", database)


def test_finalization_rejects_class_id_outside_model_mapping(database):
    from services.dataset_draft_service import DraftValidationError, validate_draft_for_finalization

    ready_draft(database)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE dataset_draft_samples SET annotations_json=? WHERE id='train-1'",
            (json.dumps([{"class_id": 1}]),),
        )
    with pytest.raises(DraftValidationError):
        validate_draft_for_finalization("draft-1", database)

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE dataset_draft_samples SET secondary_image_object_key='restored.png', "
            "label_object_key=NULL WHERE id='train-1'"
        )
    with pytest.raises(DraftValidationError):
        validate_draft_for_finalization("draft-1", database)
