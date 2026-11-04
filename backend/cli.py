"""本机管理命令行。

用法（在工程根目录执行）::

    python -m backend.cli status
    python -m backend.cli create-user --username zhangsan --role admin
    python -m backend.cli reset-password --username zhangsan
    python -m backend.cli set-status --username zhangsan --status disabled
    python -m backend.cli add-root "D:\\data\\haixun" --label 训练数据 --grant-to zhangsan
    python -m backend.cli grant-root "D:\\data\\haixun" --username lisi
    python -m backend.cli revoke-root "D:\\data\\haixun" --username lisi
    python -m backend.cli remove-root "D:\\data\\haixun"
    python -m backend.cli list-users
    python -m backend.cli list-roots

授权根目录**默认不共享**：`add-root` 只登记目录，必须用 `--grant-to` 或 `grant-root`
明确指定哪个账号能用；普通用户只能看到、浏览、绑定被授予的目录。
密码只从交互式输入或 stdin 读取，**不接受命令行参数**，避免留在 shell 历史里。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from backend import config, db
from backend.errors import ApiError
from backend.services import roots, users


def _read_password(*, confirm: bool) -> str:
    if not sys.stdin.isatty():
        line = sys.stdin.readline().strip()
        if not line:
            raise SystemExit("未从 stdin 读到密码")
        return line
    password = getpass.getpass("密码：")
    if confirm:
        again = getpass.getpass("再输一次：")
        if password != again:
            raise SystemExit("两次输入不一致")
    return password


def _open():
    settings = config.load_settings()
    conn = db.connect(settings.db_path)
    db.ensure_schema(conn)
    return settings, conn


def _cmd_status(_args: argparse.Namespace) -> int:
    settings, conn = _open()
    total = users.count_users(conn)
    admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
    roots_count = len(roots.list_all(conn))
    print(f"数据目录：{settings.data_dir}")
    print(f"数据库：{settings.db_path}")
    print(f"账号：{total}（管理员 {admins}）")
    print(f"允许根目录：{roots_count}")
    if total == 0:
        print("尚无账号。请先执行：python -m backend.cli create-user --username <名字> --role admin")
    return 0


def _cmd_create_user(args: argparse.Namespace) -> int:
    _, conn = _open()
    password = _read_password(confirm=True)
    try:
        row = users.create(
            conn,
            username=args.username,
            password=password,
            display_name=args.display_name,
            role=args.role,
        )
    except ApiError as error:
        print(f"创建失败：{error.msg}", file=sys.stderr)
        return 2
    print(f"已创建：{row['username']}（{row['role']}）")
    return 0


def _cmd_reset_password(args: argparse.Namespace) -> int:
    _, conn = _open()
    row = users.find_by_username(conn, args.username)
    if row is None:
        print("账号不存在", file=sys.stderr)
        return 2
    password = _read_password(confirm=True)
    try:
        users.reset_password(conn, int(row["id"]), password)
    except ApiError as error:
        print(f"重置失败：{error.msg}", file=sys.stderr)
        return 2
    print(f"已重置 {row['username']} 的密码；该账号所有登录态已失效。")
    return 0


def _cmd_set_status(args: argparse.Namespace) -> int:
    _, conn = _open()
    row = users.find_by_username(conn, args.username)
    if row is None:
        print("账号不存在", file=sys.stderr)
        return 2
    users.set_status(conn, int(row["id"]), args.status)
    print(f"{row['username']} 状态已设为 {args.status}")
    return 0


def _cmd_list_users(_args: argparse.Namespace) -> int:
    _, conn = _open()
    rows = users.list_all(conn)
    if not rows:
        print("（无账号）")
        return 0
    for item in rows:
        print(f"{item['id']:>3}  {item['username']:<24} {item['role']:<7} {item['status']}")
    return 0


def _find_root(conn, raw_path: str):
    row = roots.find_by_path(conn, raw_path)
    if row is None:
        print("授权列表中找不到该目录", file=sys.stderr)
        return None
    return row


def _cmd_add_root(args: argparse.Namespace) -> int:
    _, conn = _open()
    try:
        row = roots.add(conn, actor_id=None, raw_path=args.path, label=args.label or "")
    except ApiError as error:
        print(f"添加失败：{error.msg}", file=sys.stderr)
        return 2
    print(f"已授权：{row['path']}")
    for username in args.grant_to or []:
        try:
            roots.grant_by_username(
                conn, actor_id=None, root_id=int(row["id"]), username=username
            )
        except ApiError as error:
            print(f"授予 {username} 失败：{error.msg}", file=sys.stderr)
            return 2
        print(f"  已授予账号：{username.strip().lower()}")
    if not args.grant_to:
        print("  提示：尚未授予任何账号；普通用户看不到该目录，请用 grant-root 指定。")
    return 0


def _cmd_grant_root(args: argparse.Namespace) -> int:
    _, conn = _open()
    row = _find_root(conn, args.path)
    if row is None:
        return 2
    try:
        roots.grant_by_username(
            conn, actor_id=None, root_id=int(row["id"]), username=args.username
        )
    except ApiError as error:
        print(f"授予失败：{error.msg}", file=sys.stderr)
        return 2
    print(f"已把 {row['path']} 授予 {args.username.strip().lower()}")
    return 0


def _cmd_revoke_root(args: argparse.Namespace) -> int:
    _, conn = _open()
    row = _find_root(conn, args.path)
    if row is None:
        return 2
    target = users.find_by_username(conn, args.username)
    if target is None:
        print("账号不存在", file=sys.stderr)
        return 2
    removed = roots.revoke(
        conn, actor_id=None, root_id=int(row["id"]), user_id=int(target["id"])
    )
    print("已收回授予" if removed else "该账号本来就没有这个目录的授予")
    return 0


def _cmd_remove_root(args: argparse.Namespace) -> int:
    _, conn = _open()
    match = _find_root(conn, args.path)
    if match is None:
        return 2
    try:
        roots.remove(conn, actor_id=None, root_id=int(match["id"]))
    except ApiError as error:
        print(f"移除失败：{error.msg}", file=sys.stderr)
        return 2
    print(f"已移除授权：{match['path']}")
    return 0


def _cmd_list_roots(_args: argparse.Namespace) -> int:
    _, conn = _open()
    rows = roots.list_all(conn)
    if not rows:
        print("（未配置任何授权根目录）")
        return 0
    for item in rows:
        label = f"  # {item['label']}" if item["label"] else ""
        grants = roots.grants_of_root(conn, int(item["id"]))
        names = "、".join(entry["username"] for entry in grants) or "（未授予任何账号）"
        print(f"{item['id']:>3}  {item['path']}{label}")
        print(f"     授予：{names}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli", description="海巡智检 v1.7 本机管理命令行"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看数据目录与账号概况").set_defaults(func=_cmd_status)
    sub.add_parser("list-users", help="列出账号").set_defaults(func=_cmd_list_users)
    sub.add_parser("list-roots", help="列出授权根目录").set_defaults(func=_cmd_list_roots)

    create = sub.add_parser("create-user", help="创建账号（密码从交互或 stdin 读取）")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", default=None)
    create.add_argument("--role", choices=["admin", "member"], default="member")
    create.set_defaults(func=_cmd_create_user)

    reset = sub.add_parser("reset-password", help="重置密码并吊销该账号所有登录态")
    reset.add_argument("--username", required=True)
    reset.set_defaults(func=_cmd_reset_password)

    status = sub.add_parser("set-status", help="启用或停用账号")
    status.add_argument("--username", required=True)
    status.add_argument("--status", choices=["active", "disabled"], required=True)
    status.set_defaults(func=_cmd_set_status)

    add_root = sub.add_parser("add-root", help="添加允许的根目录（默认不授予任何账号）")
    add_root.add_argument("path")
    add_root.add_argument("--label", default="")
    add_root.add_argument(
        "--grant-to", action="append", metavar="USERNAME", help="同时授予该账号，可重复"
    )
    add_root.set_defaults(func=_cmd_add_root)

    grant_root = sub.add_parser("grant-root", help="把已有授权根目录授予某个账号")
    grant_root.add_argument("path")
    grant_root.add_argument("--username", required=True)
    grant_root.set_defaults(func=_cmd_grant_root)

    revoke_root = sub.add_parser("revoke-root", help="收回某账号对授权根目录的使用资格")
    revoke_root.add_argument("path")
    revoke_root.add_argument("--username", required=True)
    revoke_root.set_defaults(func=_cmd_revoke_root)

    remove_root = sub.add_parser("remove-root", help="移除允许的根目录")
    remove_root.add_argument("path")
    remove_root.set_defaults(func=_cmd_remove_root)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
