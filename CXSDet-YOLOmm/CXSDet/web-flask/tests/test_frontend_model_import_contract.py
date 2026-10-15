from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_model_management_exposes_validated_pt_import_workflow():
    view = _read("web-vue/src/views/admin/ModelManagement.vue")
    api = _read("web-vue/src/api/model.ts")

    assert "导入模型权重" in view
    assert "showImportDialog" in view
    assert 'accept=".pt"' in view
    assert ':auto-upload="false"' in view
    assert "importForm.dataset_id" in view
    assert "importWeightFile" in view
    assert "importLoading" in view
    assert "const formData = new FormData()" in view
    assert "formData.append('weight', importWeightFile.value)" in view
    assert "await modelApi.importWeight(formData)" in view
    assert "await fetchModels()" in view
    assert "importWeight(formData: FormData)" in api
    assert "'/model/import'" in api
    assert "'Content-Type': 'multipart/form-data'" in api
