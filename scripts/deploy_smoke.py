"""部署启动冒烟：真实起停 WSGI 实例，验证单端口、重复启动、端口占用与身份核验。

跑法（用装了 waitress 的项目 venv）::

    python scripts/deploy_smoke.py

它会：

1. 用临时数据目录 + 空闲端口启动 ``deploy/serve.py``；
2. 等 ``/api/health`` 就绪，检查 ``/`` 返回真实前端 HTML；
3. 再启动一次 → 必须提示“已在运行”，不以第二实例监听；
4. 篡改运行记录的实例指纹 → ``stop`` 必须拒绝（退出码 7）且服务仍在；
5. 正常 ``stop`` → 退出码 0、端口关闭；
6. 另起一个假监听占住端口 → 启动必须退出码 5，且假监听仍活着（不被杀）。

结果写到 ``var/deploy-smoke/<时间戳>/report.json``，同时打印摘要。
这个脚本只用标准库；不装依赖、不训练、不调用外部 API。
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "deploy" / "serve.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(python: str, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [python, str(SERVE), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def http_status(url: str, timeout: float = 2.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(64)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return 0


def http_headers(url: str, timeout: float = 2.0) -> dict[str, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(64)
            return {key.lower(): value for key, value in response.headers.items()}
    except (urllib.error.URLError, OSError):
        return {}


def wait_health(url: str, seconds: float = 40.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http_status(url) == 200:
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable, help="运行启动器的解释器（需已装 waitress）")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "var" / "deploy-smoke" / f"{stamp}-{secrets.token_hex(3)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = out_dir / "data"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    checks: dict[str, bool] = {}
    detail: dict[str, object] = {"python": args.python, "port": port, "data_dir": str(data_dir)}

    proc: subprocess.Popen | None = None
    dummy: socket.socket | None = None
    try:
        checked = run(args.python, "check", "--data-dir", str(data_dir), "--port", str(port))
        detail["check"] = checked.stdout + checked.stderr
        checks["check_ok"] = checked.returncode == 0

        log = (out_dir / "server.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [args.python, str(SERVE), "start", "--data-dir", str(data_dir), "--port", str(port)],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        checks["health_ready"] = wait_health(f"{base}/api/health")
        checks["root_serves_html"] = http_status(f"{base}/") == 200
        checks["deep_link_falls_back"] = http_status(f"{base}/projects/x/review") == 200
        index_html = (ROOT / "frontend" / "dist" / "index.html").read_text(encoding="utf-8")
        marker = 'src="/'
        start = index_html.find(marker)
        asset = ""
        if start >= 0:
            end = index_html.find('"', start + len(marker))
            asset = index_html[start + len(marker) : end]
        detail["asset"] = asset
        checks["static_asset_ok"] = bool(asset) and http_status(f"{base}/{asset}") == 200
        checks["meta_public"] = http_status(f"{base}/api/meta") == 200
        server_header = http_headers(f"{base}/api/health").get("server", "")
        detail["server_header"] = server_header
        checks["wsgi_not_dev_server"] = server_header.lower().startswith("haixun-1.7")

        # 进行中的对话：停止必须拒绝（退出码 9），服务不能被悄悄打断。
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(data_dir / "haixun.db")
        try:
            conn.execute(
                "INSERT INTO turns (id, session_id, project_id, owner_id, request_id, mode,"
                " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("trn_smoke_busy", "ses_smoke", "prj_smoke", 1, "smoke-busy", "full", "queued", now, now),
            )
            conn.commit()
        finally:
            conn.close()
        busy = run(args.python, "stop", "--data-dir", str(data_dir))
        detail["stop_busy"] = busy.stdout + busy.stderr
        checks["busy_turn_blocks_stop"] = busy.returncode == 9
        checks["server_survives_busy_stop"] = http_status(f"{base}/api/health") == 200
        conn = sqlite3.connect(data_dir / "haixun.db")
        try:
            conn.execute("UPDATE turns SET status = 'stopped' WHERE id = 'trn_smoke_busy'")
            conn.commit()
        finally:
            conn.close()

        again = run(args.python, "start", "--data-dir", str(data_dir), "--port", str(port))
        detail["duplicate"] = again.stdout + again.stderr
        checks["duplicate_start_detected"] = again.returncode == 0 and "已在运行" in again.stdout

        status = run(args.python, "status", "--data-dir", str(data_dir))
        detail["status"] = status.stdout
        checks["status_reports_running"] = status.returncode == 0 and "运行中" in status.stdout

        state_file = data_dir / "run" / "server.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        real_instance = state["instance"]
        state["instance"] = "deadbeefcafe"
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        wrong = run(args.python, "stop", "--data-dir", str(data_dir))
        detail["stop_mismatch"] = wrong.stdout + wrong.stderr
        checks["stop_refuses_identity_mismatch"] = wrong.returncode == 7
        checks["server_survives_bad_stop"] = http_status(f"{base}/api/health") == 200

        state["instance"] = real_instance
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        stopped = run(args.python, "stop", "--data-dir", str(data_dir))
        detail["stop_ok"] = stopped.stdout + stopped.stderr
        checks["stop_ok"] = stopped.returncode == 0 and "已停止" in stopped.stdout
        time.sleep(1.2)
        checks["port_released"] = http_status(f"{base}/api/health") == 0
        checks["state_removed"] = not state_file.exists()

        if proc is not None:
            proc.wait(timeout=15)
        checks["server_exit_zero"] = proc is not None and proc.returncode == 0

        # 端口被陌生程序占用：不启动、不杀它。
        dummy = socket.socket()
        dummy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        dummy.bind(("127.0.0.1", 0))
        dummy.listen(5)
        busy_port = int(dummy.getsockname()[1])
        blocked = run(
            args.python,
            "start",
            "--data-dir",
            str(out_dir / "busy-data"),
            "--port",
            str(busy_port),
        )
        detail["port_busy"] = blocked.stdout + blocked.stderr
        checks["busy_port_refused"] = blocked.returncode == 5
        try:
            probe = socket.create_connection(("127.0.0.1", busy_port), timeout=1)
            probe.close()
            alive = True
        except OSError:
            alive = False
        checks["busy_listener_survived"] = alive
    finally:
        if dummy is not None:
            dummy.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    report = {
        "passed": sum(1 for value in checks.values() if value),
        "total": len(checks),
        "all_passed": all(checks.values()),
        "checks": checks,
        "detail": detail,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, value in checks.items():
        print(f"{'PASS' if value else 'FAIL'}  {name}")
    print(f"\n{report['passed']}/{report['total']} passed · {out_dir / 'report.json'}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
