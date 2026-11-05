"""前后端契约测试：用**真实前端适配器**调用**真实 HTTP 后端**。

为什么不是再写一遍请求：WB17-03 的 405 就是因为后端单测只打服务层、前端路径写错也没人发现。
这个脚本 import 的是 `frontend/src/api/business.ts` 本身（Node 直接跑 TS），
经过真实 Cookie/CSRF/HTTP 到 Flask，再回读响应字段。

用法::

    python scripts/contract_frontend.py              # 数据集导入 + 事件游标 + 列表
    python scripts/contract_frontend.py --with-model # 额外：上传 best.pt、导入并发布模型（较慢）

全程隔离目录、本机回环；不调用任何模型接口，不写工程外的位置。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from backend.tests.support import Harness, PASSWORD  # noqa: E402

ZIP = ROOT / "var" / "acceptance-20261104" / "workspace" / "dataset.zip"
WEIGHT = ROOT / "var" / "acceptance-20261104" / "workspace" / "weights" / "best.pt"
FRONTEND = ROOT / "frontend"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"PASS: {message}")


def main() -> int:
    with_model = "--with-model" in sys.argv
    # 临时目录固定放在工程 var/ 下：便于确认清理范围，也不碰系统 TEMP。
    sandbox = Path(tempfile.mkdtemp(prefix="contract-", dir=ROOT / "var")).resolve()
    previous_temp = tempfile.tempdir
    tempfile.tempdir = str(sandbox)
    os.environ["TEMP"] = str(sandbox)
    os.environ["TMP"] = str(sandbox)
    harness = Harness()
    server = make_server("127.0.0.1", 0, harness.app, threaded=True)
    port = int(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        username = "contract_member"
        user = harness.create_user(username, password=PASSWORD, role="member")
        workspace = harness.make_dir("workspace")
        harness.add_root(workspace, grant_to=[username])
        project = harness.create_project(user["id"], "契约项目")
        harness.bind_project(project["id"], workspace, username)

        session = requests.Session()
        session.get(f"{base}/api/meta", timeout=10)  # 下发 CSRF Cookie
        token = session.cookies.get("hx17_csrf") or ""
        login = session.post(
            f"{base}/api/auth/login",
            json={"username": username, "password": PASSWORD},
            headers={"X-CSRF-Token": token},
            timeout=10,
        )
        check(login.status_code == 200, "普通成员通过真实 HTTP 登录")
        cookie = "; ".join(f"{item.name}={item.value}" for item in session.cookies)

        check(ZIP.is_file(), f"验收 ZIP 存在：{ZIP.name}")
        env = dict(os.environ)
        env.update(
            {
                "HX17_BASE": base,
                "HX17_COOKIE": cookie,
                "HX17_PROJECT": project["id"],
                "HX17_ZIP": str(ZIP),
                "HX17_WEIGHT": str(WEIGHT) if with_model and WEIGHT.is_file() else "",
                "HX17_SKIP_MODEL": "0" if with_model else "1",
            }
        )
        started = time.time()
        result = subprocess.run(
            [
                "node",
                "--experimental-strip-types",
                "--loader",
                "./tests/alias-loader.mjs",
                "./tests/contract_e2e.ts",
            ],
            cwd=FRONTEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=600 if with_model else 180,
        )
        if result.returncode != 0:
            print(result.stdout[-4000:])
            print(result.stderr[-4000:], file=sys.stderr)
            raise SystemExit("FAIL: 前端适配器调用后端失败")
        report = json.loads(result.stdout[result.stdout.index("{") :])
        steps = {item["step"]: item for item in report["steps"]}
        check(steps["upload_zip"]["data"]["kind"] == "archive", "adapter.uploadAsset 走通 multipart 上传")
        dataset = steps["import_dataset"]["data"]
        check(dataset["sample_count"] >= 1, f"adapter.importDataset 真的建了数据集（{dataset['sample_count']} 个样本）")
        check(
            steps["dataset_samples"]["data"]["with_x"] == dataset["sample_count"],
            "每个样本都有配套 IR（没有被丢掉）",
        )
        check(steps["job_events"]["data"]["last_seq"] >= 0, "job-events 游标字段符合前端契约")
        check(steps["list_datasets"]["data"]["count"] >= 1, "adapter.listDatasets 返回真实列表")
        if with_model:
            model = steps["import_model"]["data"]
            check(model["status"] == "imported", "adapter.importModel 导入成功（未发布）")
            check(model["class_map"] == {"0": 0, "1": 1}, "显式类别映射按原样保存")
            check(steps["publish_model"]["data"]["status"] == "published", "adapter.publishModel 发布成功")
        print(f"PASS: 契约流程用时 {time.time() - started:.1f}s")
        print("CONTRACT OK")
        return 0
    finally:
        server.shutdown()
        harness.close()
        tempfile.tempdir = previous_temp
        # 只删本次 sandbox（已 resolve 且确认在工程 var/ 内）。
        if sandbox.is_dir() and sandbox.parent == (ROOT / "var").resolve():
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
