"""前后端契约测试：用**真实前端适配器**调用**真实 HTTP 后端**。

为什么不是再写一遍请求：WB17-03 的 405 就是因为后端单测只打服务层、前端路径写错也没人发现。
这个脚本 import 的是 `frontend/src/api/business.ts` 本身（Node 直接跑 TS），
经过真实 Cookie/CSRF/HTTP 到 Flask，再回读响应字段。

覆盖面：数据集导入 + 事件游标 + 列表 +（有 ``best.pt`` 时）模型导入发布 +
**人工复核闭环**（记录 → 复核状态 → 空草稿 → 刷新 → 确认无缺陷 → 反馈 →
身份检查 → 回流草稿 → 预览 → 定稿 → 导出 ZIP 真实字节）。

假模型边界：只有"权重 → 框"这一步由本脚本通过 ``vision_runtime._DETECTOR_FACTORY``
显式注入一个固定实现（两个框）。适配器、HTTP、鉴权、服务层、结果图渲染（cv2 真画）、
文件落盘、ZIP 打包全部是真实执行；没有任何响应被 mock 或改写。

用法::

    python scripts/contract_frontend.py              # 全流程（需要验收用 best.pt）
    python scripts/contract_frontend.py --no-model   # 只跑数据集部分

全程隔离目录、本机回环；不调用任何付费模型接口，不写工程外的位置。
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

import numpy as np  # noqa: E402
import requests  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from backend.tests.support import Harness, PASSWORD  # noqa: E402
from backend.vision import runtime as vision_runtime  # noqa: E402

WORKSPACE = ROOT / "var" / "acceptance-20261104" / "workspace"
ZIP = WORKSPACE / "dataset.zip"
WEIGHT = WORKSPACE / "weights" / "best.pt"
RGB = WORKSPACE / "dataset" / "images" / "train" / "000084.jpg"
IR = WORKSPACE / "dataset" / "images_ir" / "train" / "000084.jpg"
FRONTEND = ROOT / "frontend"


class _ContractDetector:
    """固定两个框的检测器；坐标是原图像素，由 runtime 自己读图取尺寸。"""

    def predict(self, **kwargs):  # noqa: ANN003, ANN201
        boxes = np.array(
            [[60.0, 80.0, 260.0, 300.0, 0.88, 0], [300.0, 120.0, 520.0, 360.0, 0.61, 1]],
            dtype=float,
        )
        return [type("ContractResult", (), {"boxes": boxes})()]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"PASS: {message}")


def main() -> int:
    with_model = "--no-model" not in sys.argv and WEIGHT.is_file()
    if not with_model:
        print("SKIP: 没有 best.pt 或指定了 --no-model，只跑数据集部分（复核闭环未覆盖）")
    # 临时目录固定放在工程 var/ 下：便于确认清理范围，也不碰系统 TEMP。
    sandbox = Path(tempfile.mkdtemp(prefix="contract-", dir=ROOT / "var")).resolve()
    previous_temp = tempfile.tempdir
    tempfile.tempdir = str(sandbox)
    os.environ["TEMP"] = str(sandbox)
    os.environ["TMP"] = str(sandbox)
    harness = Harness()
    original_factory = vision_runtime._DETECTOR_FACTORY
    if with_model:
        # 显式测试注入：只替换“权重 → 框”，其余全真。
        vision_runtime._DETECTOR_FACTORY = lambda path, mode: _ContractDetector()
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

        device = session.get(f"{base}/api/projects/{project['id']}/training/device", timeout=10)
        check(device.status_code == 200, "训练设备能力接口真实可用")
        capabilities = device.json()["data"]
        check(
            capabilities.get("cpu") is True and isinstance(capabilities.get("cuda_available"), bool),
            "设备能力字段齐全：CPU 恒可用，GPU 按部署机真实探测（不写死）",
        )

        check(ZIP.is_file(), f"验收 ZIP 存在：{ZIP.name}")
        check(RGB.is_file() and IR.is_file(), "验收数据集里同时有 RGB 与 IR 真图")
        env = dict(os.environ)
        env.update(
            {
                "HX17_BASE": base,
                "HX17_COOKIE": cookie,
                "HX17_PROJECT": project["id"],
                "HX17_ZIP": str(ZIP),
                "HX17_WEIGHT": str(WEIGHT) if with_model else "",
                "HX17_RGB": str(RGB) if with_model else "",
                "HX17_IR": str(IR) if with_model else "",
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
            timeout=600,
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

            state = steps["review_state"]["data"]
            check(bool(state["record_id"]), "GET /review 返回 {state.record}（页面读的就是这个形状）")
            check(bool(state["has_x"]), "多模态记录带真实 IR 地址")
            check(bool(state["result_url"]), "检测成功后有结果图地址")
            panels = steps["review_images"]["data"]
            check(
                panels["rgb"]["status"] == panels["x"]["status"] == panels["result"]["status"] == 200,
                "RGB / IR / 结果图三张都能取到 200",
            )
            check(
                all(item["type"].startswith("image/") and item["size"] > 1024 for item in panels.values()),
                f"三张都是真实图片字节（{', '.join(str(item['size']) for item in panels.values())} 字节）",
            )
            check(
                steps["review_draft_empty"]["data"]["boxes"] == 0,
                "保存空草稿后确实是 0 个框（空数组是有效结果）",
            )
            check(
                steps["review_reload_empty"]["data"]["preferred_boxes"] == 0,
                "刷新后取框仍为 0（没有回退到模型框）",
            )
            submitted = steps["review_submit"]["data"]
            check(
                submitted["status"] == "confirmed" and submitted["outcome"] == "no_defect",
                f"确认无缺陷写入 v{submitted['version']}（confirmed / no_defect）",
            )
            check(
                steps["review_after_submit"]["data"]["preferred_boxes"] == 0
                and steps["review_after_submit"]["data"]["draft"] is None,
                "重新进入复核页：无缺陷结论仍在，草稿已清空",
            )
            check(steps["feedback"]["data"]["pending"] >= 1, "生效结论能加入反馈池并读回")
            check(
                steps["identity"]["data"]["matches"][0]["status"] in {"new", "unique", "ambiguous"},
                "身份检查返回真实匹配结果（不是空壳）",
            )
            check(
                steps["reflow_draft"]["data"]["status"] == "ready",
                "回流草稿保存后是 ready（真实预览通过）",
            )
            check(steps["reflow_preview"]["data"]["counts"]["conflicts"] == 0, "回流预览没有冲突")
            finalized = steps["reflow_finalize"]["data"]
            check(finalized["kind"] == "revision", "定稿生成的是回原修订数据集")
            check(finalized["parent"] == dataset["id"], "修订版指向原数据集（原版保留）")
            check(steps["reflow_export"]["data"]["head"][:2] == [0x50, 0x4B], "导出包真实字节以 PK 开头")
            check(steps["reflow_samples"]["data"]["with_x"] >= 1, "修订版里 RGB 与 IR 都落了盘")
            check(
                steps["review_queue"]["data"]["counts"]["confirmed"] >= 1,
                "已确认队列里能查到刚提交的记录",
            )
        print(f"PASS: 契约流程用时 {time.time() - started:.1f}s")
        print("CONTRACT OK")
        return 0
    finally:
        vision_runtime._DETECTOR_FACTORY = original_factory
        server.shutdown()
        harness.close()
        tempfile.tempdir = previous_temp
        # 只删本次 sandbox（已 resolve 且确认在工程 var/ 内）。
        if sandbox.is_dir() and sandbox.parent == (ROOT / "var").resolve():
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
