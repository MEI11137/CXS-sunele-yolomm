"""生产同源启动器（本机联调版）。

* 由后端直接托管已构建的 frontend/dist，浏览器只看到一个端口；
* 数据目录默认落在本工程 var/，可用 HX17_DATA_DIR 覆盖；
* 端口默认 8080，可用 HX17_PORT 覆盖。

注意：本阶段仍使用 Flask 自带服务器，仅够本机验证；正式对外部署在 WB17-06
换成 WSGI 服务器并放进 HTTPS 入口，不要直接拿本文件对外。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("HX17_DATA_DIR", str(ROOT / "var"))
os.environ.setdefault("HX17_PORT", "8080")
# 演示模式就是同源托管；不需要再根据环境变量判断。
os.environ["HX17_SERVE_DIST"] = "1"


def main() -> int:
    dist = ROOT / "frontend" / "dist" / "index.html"
    port = int(os.environ["HX17_PORT"])
    if "--check" in sys.argv:
        if not dist.is_file():
            print("CHECK_FAILED: 缺少 frontend/dist/index.html，先执行 npm run build", file=sys.stderr)
            return 1
        print(f"CHECK_OK: dist 就绪；数据目录 {os.environ['HX17_DATA_DIR']}；端口 {port}")
        return 0

    from backend.app import create_app

    app = create_app()
    print(f"DEMO READY: http://127.0.0.1:{port}  (dist={dist.parent})", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
