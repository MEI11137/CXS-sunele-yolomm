import importlib
import sys


def test_create_app_accepts_isolated_runtime_paths(isolated_app_paths):
    for module_name in list(sys.modules):
        if module_name == "app" or module_name == "config" or module_name.startswith("routes."):
            sys.modules.pop(module_name, None)

    app_module = importlib.import_module("app")
    flask_app = app_module.create_app(
        {
            "TESTING": True,
            "DATABASE": str(isolated_app_paths["db_path"]),
            "TEMP_DIR": str(isolated_app_paths["temp_dir"]),
            "FILE_STORE_PATH": str(isolated_app_paths["file_store"]),
        }
    )

    assert flask_app.testing is True
    assert flask_app.config["DATABASE"] == str(isolated_app_paths["db_path"])
    assert flask_app.config["FILE_STORE_PATH"] == str(isolated_app_paths["file_store"])
    rules = {rule.rule for rule in flask_app.url_map.iter_rules()}
    assert "/api/health/health_check" in rules
    preflight = flask_app.test_client().options(
        "/api/user/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.headers.get("Access-Control-Allow-Credentials") == "true"
