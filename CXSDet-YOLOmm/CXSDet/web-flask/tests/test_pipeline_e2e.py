from pathlib import Path


def test_tiny_single_and_multimodal_archives_follow_runtime_contract(tmp_path):
    from scripts.verify_multimodal_pipeline import build_tiny_datasets
    from services.dataset_contract import validate_dataset_archive
    from services.yolo_runtime import resolve_inference_inputs

    artifacts = build_tiny_datasets(tmp_path)
    single = validate_dataset_archive(str(artifacts["single_zip"]), "single")
    multimodal = validate_dataset_archive(str(artifacts["multimodal_zip"]), "multimodal")
    assert single.split_counts["train"] >= 1
    assert multimodal.secondary_modality == "edge"

    inputs = resolve_inference_inputs(
        {"model_mode": "multimodal", "secondary_modality": "edge"},
        str(artifacts["rgb_image"]),
        None,
        str(tmp_path / "generated"),
    )
    assert inputs.x_origin == "generated"
    assert Path(inputs.x_path).is_file()
