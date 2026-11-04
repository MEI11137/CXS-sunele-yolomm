"""真实引擎进程烟测：只启动 OpenCode headless 服务，不发送任何 prompt。

用法::

    python scripts/engine_smoke.py

它会：

1. 建一个隔离数据目录（XDG/HOME/TEMP 全部指向本工程私有目录）；
2. 让 ``EngineManager`` 用与线上相同的参数拉起引擎（隐藏窗口、随机口令、最小环境）；
3. 断言版本号等于本工程验证版本、``/global/health`` 通过、端口只绑 127.0.0.1；
4. 立即停止进程。

**不调用任何模型接口**，因此不会产生费用；也不写全局 ``~/.config/opencode``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.agent import engine as engine_module  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"PASS: {message}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="hx17-engine-smoke-"))
    data_dir = workdir / "var"
    workspace = workdir / "data"
    workspace.mkdir(parents=True)

    settings = config.load_settings(data_dir=data_dir, serve_dist=False, port=0)
    exe = Path(str(settings.engine_exe))
    check(exe.is_file(), f"引擎可执行文件存在：{exe.name}")

    manager = engine_module.EngineManager(settings)
    spec = engine_module.EngineSpec(
        user_id=1,
        project_id="smoke-project",
        session_id="smoke-session",
        fingerprint="smoke-fingerprint",
        root=workspace,
        provider="custom",
        # 这里的 base_url 只写进引擎配置，本轮不发 prompt，因此不会真的请求模型。
        model="smoke-model",
        models=["smoke-model"],
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-smoke-0000000000",
        gateway_url="http://127.0.0.1:9/",
        tool_token="smoke-token",
        python=str(settings.engine_python or ""),
    )

    try:
        instance = manager.get(spec)
        url = instance.base_url
        print(json.dumps({"version": instance.version, "base_url": url}, ensure_ascii=False))
        check(instance.version == settings.engine_version, f"引擎版本为 {settings.engine_version}")
        check(url.startswith("http://127.0.0.1:"), "引擎只监听本机回环地址")

        request = urllib.request.Request(
            f"{url}/global/health",
            headers={"Authorization": _basic(instance.handle.password)},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        check(response.status == 200 and payload.get("healthy") is True, "健康检查通过（隐藏窗口下可访问）")

        # 停止项目引擎：进程必须真的退出，且运行目录被清理。
        stopped = manager.stop_project(1, "smoke-project")
        check(stopped == 1, "停止项目引擎成功")
        check(manager.active_count() == 0, "没有遗留引擎进程")
    finally:
        manager.shutdown()

    print(f"（临时目录：{workdir}）")
    print("ENGINE SMOKE OK")
    return 0


def _basic(password: str) -> str:
    import base64

    raw = f"opencode:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
