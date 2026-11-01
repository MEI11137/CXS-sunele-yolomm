"""公网演示工作区的初始素材：让评委第一次打开就有东西可读、可改。"""
from pathlib import Path

SEED_FILES = {
    "说明.txt": (
        "这个文件夹是海巡智检的公网演示工作区。\n"
        "评委登录后可以让智能体在这里读文件、改文件、新建文件。\n"
        "改坏了不要紧：删掉这个文件夹里的内容，重新运行一键启动即可。\n"
    ),
    "巡检记录/2026-10-31_巡检摘要.md": (
        "# 2026-10-31 海上风电叶片巡检摘要\n\n"
        "- 巡检机组：H7、H8、H9\n"
        "- 采集图片：186 张\n"
        "- 疑似缺陷：3 处（H8 叶片前缘砂眼 2 处，H7 叶根涂层脱落 1 处）\n"
        "- 处理建议：H8 安排下次窗口期复检，H7 叶根补漆\n\n"
        "## 待办\n\n"
        "- [ ] 生成一份给运维班组的周报\n"
        "- [ ] 把 H8 的缺陷图片归档到对应机组目录\n"
    ),
    "点位清单/风机点位.csv": (
        "机组编号,纬度,经度,所在区域,备注\n"
        "H7,26.3512,119.8721,A 区,叶根涂层脱落\n"
        "H8,26.3627,119.8846,A 区,前缘砂眼 2 处\n"
        "H9,26.3743,119.8955,B 区,未发现异常\n"
    ),
    "周报模板/运维周报模板.md": (
        "# ____ 周运维周报\n\n"
        "## 一、本周完成\n\n"
        "## 二、发现缺陷\n\n"
        "## 三、下周计划\n"
    ),
}


def seed_demo_workspace(root):
    """按需补齐示例文件；已有的文件不覆盖。返回本次新建的相对路径。"""
    base = Path(root)
    created = []
    for relative, text in SEED_FILES.items():
        target = base / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        created.append(relative)
    return created