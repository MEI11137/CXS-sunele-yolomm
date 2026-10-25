import pytest

from services.video_dataset_service import (
    VideoDatasetConfig,
    nearest_secondary_frame,
    read_nearby_frame,
    selected_frame_indices,
)


def test_selected_frame_indices_uses_zero_based_interval():
    assert list(selected_frame_indices(7, 1)) == [0, 1, 2, 3, 4, 5, 6]
    assert list(selected_frame_indices(7, 2)) == [0, 2, 4, 6]


@pytest.mark.parametrize("interval", [0, -1, "nope"])
def test_selected_frame_indices_rejects_invalid_interval(interval):
    with pytest.raises(ValueError):
        selected_frame_indices(7, interval)


def test_nearest_secondary_frame_applies_signed_offset_and_clamps():
    assert nearest_secondary_frame(25, 25.0, 30.0, 0.5) == 45
    assert nearest_secondary_frame(25, 25.0, 30.0, -0.5) == 15
    assert nearest_secondary_frame(0, 25.0, 30.0, -5.0) == 0


@pytest.mark.parametrize(
    "args",
    [(-1, 25.0, 30.0, 0), (1, 0.0, 30.0, 0), (1, 25.0, -1.0, 0)],
)
def test_nearest_secondary_frame_rejects_invalid_fps_or_index(args):
    with pytest.raises(ValueError):
        nearest_secondary_frame(*args)


def test_read_nearby_frame_uses_first_readable_neighbor():
    import numpy as np

    class Capture:
        def __init__(self):
            self.position = None

        def set(self, _property, position):
            self.position = position

        def read(self):
            if self.position == 9:
                return True, np.zeros((1, 1, 3), dtype=np.uint8)
            return False, None

    frame, actual_index = read_nearby_frame(Capture(), 8, 12, radius=2)
    assert frame is not None
    assert actual_index == 9


def test_read_nearby_frame_preserves_missing_when_no_neighbor_is_readable():
    class Capture:
        def set(self, _property, _position):
            pass

        def read(self):
            return False, None

    assert read_nearby_frame(Capture(), 1, 3, radius=2) == (None, None)


def test_video_dataset_config_parses_optional_single_and_multimodal_settings():
    config = VideoDatasetConfig.from_form(
        {
            "generate_dataset": "true",
            "dataset_name": " field run ",
            "dataset_description": "notes",
            "dataset_mode": "multimodal",
            "frame_interval": "3",
            "annotation_model_id": "model-2",
            "annotation_confidence": "0.4",
            "secondary_modality": "thermal",
            "secondary_time_offset": "-0.25",
        }
    )
    assert config.enabled is True
    assert config.name == "field run"
    assert config.mode == "multimodal"
    assert config.frame_interval == 3
    assert config.annotation_model_id == "model-2"
    assert config.confidence == pytest.approx(0.4)
    assert config.secondary_modality == "thermal"
    assert config.secondary_time_offset == pytest.approx(-0.25)


@pytest.mark.parametrize(
    "form",
    [
        {"generate_dataset": "true", "dataset_mode": "single", "frame_interval": "0"},
        {"generate_dataset": "true", "dataset_mode": "single", "frame_interval": "1", "annotation_confidence": "2"},
        {"generate_dataset": "true", "dataset_mode": "unknown", "frame_interval": "1"},
    ],
)
def test_video_dataset_config_rejects_invalid_values(form):
    with pytest.raises(ValueError):
        VideoDatasetConfig.from_form(form)
