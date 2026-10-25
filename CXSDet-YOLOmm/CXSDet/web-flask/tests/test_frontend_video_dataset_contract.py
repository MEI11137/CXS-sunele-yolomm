from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_video_dataset_form_fields_are_optional_and_preserve_sampling_settings():
    api = _read("web-vue/src/api/video_detection.ts")
    view = _read("web-vue/src/views/video-detection/VideoDetection.vue")

    assert "appendVideoDatasetFields" in api
    assert "if (!options.enabled) return" in api
    assert "formData.append('frame_interval', String(options.frameInterval))" in api
    assert "formData.append('dataset_mode', options.mode)" in api
    assert "options.mode === 'multimodal'" in api
    assert "formData.append('secondary_video', options.secondaryVideo)" in api
    assert "formData.append('secondary_time_offset', String(options.secondaryTimeOffset))" in api
    assert "appendVideoDatasetFields(formData" in view


def test_video_dataset_form_only_defaults_to_compatible_annotation_model():
    api = _read("web-vue/src/api/video_detection.ts")
    view = _read("web-vue/src/views/video-detection/VideoDetection.vue")

    assert "getCompatibleAnnotationModels" in api
    assert "getDefaultAnnotationModelId" in api
    assert "detectionModel.model_mode !== mode" in api
    assert "detectionModel.secondary_modality" in api
    assert "userStore.isAdmin()" in view
    assert "datasetForm.annotationModelId = getDefaultAnnotationModelId" in view
    assert "datasetForm.secondaryVideo = null" in view


def test_video_dataset_progress_exposes_independent_status_and_counts():
    api = _read("web-vue/src/api/video_detection.ts")
    view = _read("web-vue/src/views/video-detection/VideoDetection.vue")

    for field in (
        "dataset_generation_status",
        "dataset_draft_id",
        "dataset_statistics",
        "missing_secondary",
        "max_pairing_error_ms",
    ):
        assert field in api
        assert field in view
