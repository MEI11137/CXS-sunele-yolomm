"""受控命令：只跑"范围能被证明"的 PowerShell 脚本。

判定不是字符串黑名单，而是用 **PowerShell 自己的解析器**做 AST 检查：

* 变量、脚本块、子表达式、赋值、成员调用、类型字面量、重定向 —— 一律拒绝。
  它们都能把路径/命令拼成运行时才知道的值，**范围就无法证明**；
* 命令必须全部在允许清单里（读取 + 项目内复制/移动/新建/写内容/压缩展开），
  删除不在清单内：删除只走 ``hx_delete_path``（进回收区 + 回读校验）；
* 间接成员调用同样拒绝：``ForEach-Object``（含别名 ``foreach``/``%``）整体
  不放行——``-MemberName``/``-Method`` 可按位置省略参数名（如
  ``ForEach-Object Delete``），能对任意成员发起调用，证明不了影响就不放行；
* 脚本里出现的路径字面量必须**与长度无关地**逐个检查：相对路径、不含 ``..``、
  落点在项目绑定目录内；``Registry::``/``Cert::`` 等非文件系统 provider 路径
  与绝对路径（含超长绝对路径）一律拒绝。

因为规则是"证明不了就不跑"，这条通道**不看权限模式**：``full`` 也不会让
一个越界脚本变成可执行。当前工作目录只是 ``cwd``，不是沙箱——脚本里的路径
仍要逐个校验，绝不假设"在 cwd 里就安全"。这套检查也**不等于** PowerShell
成了完整沙箱：它只是把"能证明范围"的窄通道放行，其余全部拒绝。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from backend.agent import fsops
from backend.errors import ApiError

MAX_SCRIPT = 4000
MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 60.0

_ALLOWED_CMDLETS = {
    "add-content",
    "compare-object",
    "compress-archive",
    "convertfrom-json",
    "convertto-json",
    "copy-item",
    "expand-archive",
    "format-list",
    "format-table",
    "get-childitem",
    "get-content",
    "get-date",
    "get-filehash",
    "get-item",
    "group-object",
    "join-path",
    "measure-object",
    "move-item",
    "new-item",
    "out-string",
    "rename-item",
    "resolve-path",
    "select-object",
    "select-string",
    "set-content",
    "sort-object",
    "split-path",
    "start-sleep",
    "test-path",
    "where-object",
    "write-output",
}

# 这些命令要么能拉起新进程、要么能读凭据、要么能改系统状态；单独给出更好懂的话。
_DENIED_CMDLETS = {
    "add-type": "不允许加载 .NET 类型",
    "bitsadmin": "不允许后台传输",
    "cd": "不允许切换工作目录（工作目录不是沙箱）",
    "cmd": "不允许再起 shell",
    "curl": "不允许访问网络",
    "enter-pssession": "不允许远程会话",
    "get-credential": "不允许读取凭据",
    "iex": "不允许 Invoke-Expression",
    "import-module": "不允许加载模块",
    "invoke-command": "不允许远程/脚本块执行",
    "invoke-expression": "不允许 Invoke-Expression",
    "invoke-restmethod": "不允许访问网络",
    "invoke-webrequest": "不允许访问网络",
    "net": "不允许改系统账号/服务",
    "new-object": "不允许创建 COM/.NET 对象",
    "pop-location": "不允许切换工作目录（工作目录不是沙箱）",
    "push-location": "不允许切换工作目录（工作目录不是沙箱）",
    "reg": "不允许改注册表",
    "remove-item": "删除只走 hx_delete_path（进回收区并可回读校验），命令里不允许删文件",
    "rmdir": "删除只走 hx_delete_path",
    "del": "删除只走 hx_delete_path",
    "ri": "删除只走 hx_delete_path",
    "rm": "删除只走 hx_delete_path",
    # ForEach-Object 整体拒绝（含别名 foreach / %）：-MemberName/-Method 可按
    # 位置省略参数名（Position=0），如 `ForEach-Object Delete` 能对任意成员
    # 发起调用，不实现完整参数绑定器就无法证明范围；属性提取请用 Select-Object。
    "foreach-object": (
        "ForEach-Object 的成员调用（-MemberName/-Method 及按位置省略的写法）"
        "能对任意成员发起调用，范围无法证明；属性提取请用 Select-Object"
    ),
    "foreach": "ForEach-Object 别名，按成员调用风险整体拒绝；属性提取请用 Select-Object",
    "%": "ForEach-Object 别名，按成员调用风险整体拒绝；属性提取请用 Select-Object",
    "schtasks": "不允许创建计划任务",
    "set-executionpolicy": "不允许改执行策略",
    "set-location": "不允许切换工作目录（工作目录不是沙箱）",
    "start-job": "不允许后台作业",
    "start-process": "不允许拉新进程",
    "stop-process": "不允许结束进程",
    "wmic": "不允许改系统状态",
}

_WRITE_CMDLETS = {
    "add-content",
    "compress-archive",
    "copy-item",
    "expand-archive",
    "move-item",
    "new-item",
    "rename-item",
    "set-content",
}

_SAFE_VARIABLES = {"true", "false", "null"}

_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$src = [string]$env:HX17_SCRIPT
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$out = [ordered]@{
  parse_errors    = @()
  commands        = @()
  parameters      = @()
  dynamic         = @()
  strings         = @()
  variables       = @()
  scriptblocks    = 0
  subexpressions  = 0
  parens          = 0
  assignments     = 0
  member_invokes  = 0
  type_literals   = 0
  redirections    = @()
}
foreach ($e in $errors) { $out.parse_errors += [string]$e.Message }

foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
  $name = $c.GetCommandName()
  if ($name) { $out.commands += [string]$name } else { $out.dynamic += [string]$c.Extent.Text }
  # 参数名也要收：缩写/别名参数（-MemberName 可写成 -m）只有在看到参数本身时才能判。
  foreach ($el in $c.CommandElements) {
    if ($el -is [System.Management.Automation.Language.CommandParameterAst]) {
      $out.parameters += ("{0}:{1}" -f [string]$name, [string]$el.ParameterName)
    }
  }
}
foreach ($s in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
  $out.strings += [string]$s.Value
}
foreach ($v in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
  $out.variables += [string]$v.VariablePath.UserPath
}
$out.scriptblocks = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ScriptBlockExpressionAst] }, $true)).Count
$out.subexpressions = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.SubExpressionAst] }, $true)).Count
$out.parens = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParenExpressionAst] }, $true)).Count
$out.assignments = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)).Count
$out.member_invokes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)).Count
$out.type_literals = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TypeExpressionAst] }, $true)).Count
foreach ($r in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] }, $true)) {
  $out.redirections += [string]$r.Location.Extent.Text
}
$out | ConvertTo-Json -Depth 6 -Compress
"""


def _powershell() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _env() -> dict[str, str]:
    keep = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "PSModulePath",
        "LANG",
    )
    return {key: os.environ[key] for key in keep if key in os.environ}


def _decode_output(data: bytes) -> str:
    """PowerShell 输出解码：优先 UTF-8，Windows PowerShell 5.1 回退 GB18030。

    包装脚本已要求 UTF-8，但 5.1 在 stdout 被重定向时经常按控制台代码页（简中
    系统为 GBK）写字节。用 UTF-8 严格解码失败后回退 GBK，避免中文输出变成乱码；
    两种都失败才做替换解码，保证诊断信息仍可读。
    """
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_process(argv: list[str], *, cwd: Path | None, timeout: float, env: dict[str, str]):
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(  # noqa: S603 - 参数列表，shell=False
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
        shell=False,
        creationflags=creation,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(1.0, timeout))
        return (
            process.returncode,
            _decode_output(stdout or b""),
            _decode_output(stderr or b""),
        )
    except subprocess.TimeoutExpired:
        _kill(process)
        raise ApiError(504, "SHELL_TIMEOUT", f"命令超过 {timeout:g} 秒未结束，已强制终止") from None
    finally:
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """去掉 Select-String 之类命令输出的高亮转义：模型和前端都不需要 ANSI 控制符。"""
    return _ANSI.sub("", text or "")


def _kill(process: subprocess.Popen) -> None:
    try:
        import psutil  # noqa: PLC0415 - 只有超时才需要

        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


# --------------------------------------------------------------------------- 路径


# PowerShell 的 provider 前缀（Registry:: / Cert:: / Environment:: 等）：
# 它们指向文件系统之外的世界，范围证明不了，一律拒绝。
_PROVIDER_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9]*::")


def _looks_like_path(text: str) -> bool:
    # 注意：**不能用字符串长度跳过路径检查**——超长绝对路径正是靠这条混进来的
    # 反例。先判断"像不像路径"，像就继续查，不管多长。
    if not text:
        return False
    if _PROVIDER_PATH.match(text):
        return True
    if text.startswith(("/", "\\", "~")) or ":" in text[:3]:
        return True
    if "\\" in text or "/" in text:
        return True
    if text in {".", ".."}:
        return True
    # 裸文件名（带扩展名）也算路径：这样结论里能看到"读了哪几个文件"，同时照样做包含性校验。
    return bool(re.fullmatch(r"[\w.\-\u4e00-\u9fff]{1,120}\.[A-Za-z0-9]{1,8}", text))


def _path_problem(root: Path, text: str) -> str:
    raw = text.strip()
    if not raw:
        return ""
    if _PROVIDER_PATH.match(raw):
        return f"不允许非文件系统 provider 路径（Registry::/Cert:: 等）：{raw[:80]}"
    if raw.startswith(("/", "\\", "~")) or (len(raw) > 1 and raw[1] == ":"):
        return f"不允许绝对路径：{raw[:80]}"
    if ".." in Path(raw.replace("\\", "/")).parts:
        return f"不允许用 .. 跳出项目目录：{raw[:80]}"
    literal = raw.split("*")[0].split("?")[0]
    probe = literal or "."
    try:
        fsops.resolve(root, probe)
    except Exception:
        return f"路径不在项目绑定目录内：{raw[:80]}"
    return ""


def analyze(root: Path, script: str) -> dict:
    """用 PowerShell 的解析器检查脚本；返回结论与发现（不执行脚本本身）。"""
    text = str(script or "").strip()
    if not text:
        raise ApiError(400, "VALIDATION", "命令不能为空")
    if len(text) > MAX_SCRIPT:
        raise ApiError(413, "SHELL_TOO_LONG", f"命令太长（上限 {MAX_SCRIPT} 字符）")
    binary = _powershell()
    if binary is None:
        raise ApiError(
            409,
            "SHELL_UNAVAILABLE",
            "这台机器上没有 PowerShell（pwsh / powershell 都找不到），受控命令不可用",
        )
    env = _env()
    env["HX17_SCRIPT"] = text
    code, stdout, stderr = _run_process(
        [binary, "-NoProfile", "-NonInteractive", "-Command", _ANALYZER],
        cwd=root,
        timeout=25.0,
        env=env,
    )
    if code != 0 or not stdout.strip():
        raise ApiError(
            500,
            "SHELL_ANALYZER_FAILED",
            f"语法检查没跑起来（exit {code}）"
            + (f"：{stderr.strip()[:200]}" if stderr.strip() else ""),
        )
    try:
        report = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ApiError(500, "SHELL_ANALYZER_FAILED", "语法检查返回了无法解析的结果") from None

    reasons: list[str] = []
    if report.get("parse_errors"):
        reasons.append("脚本有语法错误：" + "；".join(str(item) for item in report["parse_errors"][:3]))
    if report.get("dynamic"):
        reasons.append("命令名是运行时才确定的值（动态调用），范围无法证明")
    if int(report.get("scriptblocks") or 0):
        reasons.append("不允许脚本块（{ ... } / { param(...) ... }）")
    if int(report.get("subexpressions") or 0):
        reasons.append("不允许子表达式（$( ... )）")
    if int(report.get("parens") or 0):
        reasons.append("不允许括号表达式（( ... )）把其它命令的结果当成参数；路径请直接写成字面量")
    if int(report.get("assignments") or 0):
        reasons.append("不允许赋值（变量可以让路径变成运行时值）")
    if int(report.get("member_invokes") or 0):
        reasons.append("不允许成员调用（.Method(...)）")
    if int(report.get("type_literals") or 0):
        reasons.append("不允许类型字面量（[int] / [IO.File] 等）")
    if report.get("redirections"):
        reasons.append("不允许重定向输出到文件（请用 Set-Content / Add-Content）")

    variables = [str(item) for item in report.get("variables") or []]
    unknown = sorted({item for item in variables if item.lower() not in _SAFE_VARIABLES})
    if unknown:
        reasons.append("不允许使用变量：" + "、".join(f"${item}" for item in unknown[:5]))

    cmdlets = [str(item).lower() for item in report.get("commands") or []]
    for name in cmdlets:
        if name in _DENIED_CMDLETS:
            reasons.append(f"{name}：{_DENIED_CMDLETS[name]}")
        elif name not in _ALLOWED_CMDLETS:
            reasons.append(f"命令不在允许清单里：{name}")

    # ForEach-Object 已整体进拒绝清单（见 _DENIED_CMDLETS 注释），参数层
    # 不再需要单独拦 -MemberName/-Method。

    paths: list[str] = []
    for item in report.get("strings") or []:
        value = str(item)
        if not _looks_like_path(value):
            continue
        problem = _path_problem(root, value)
        if problem:
            reasons.append(problem)
        else:
            paths.append(value)

    return {
        "ok": not reasons,
        "reasons": reasons[:8],
        "cmdlets": sorted(set(cmdlets)),
        "paths": sorted(set(paths)),
        "writes": bool(set(cmdlets) & _WRITE_CMDLETS),
        "script": text,
    }


def run(root: Path, script: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """先证范围，再执行：范围证明不了就抛错，不会"先跑跑看"。"""
    report = analyze(root, script)
    if not report["ok"]:
        raise ApiError(
            403,
            "SHELL_SCOPE_UNPROVEN",
            "这条命令的范围无法证明，已拒绝执行：" + "；".join(report["reasons"]),
            details={"reasons": report["reasons"], "cmdlets": report["cmdlets"]},
        )
    binary = _powershell()
    assert binary is not None
    started = time.monotonic()
    # 固定前缀只做一件事：把控制台输出编码设成 UTF-8，避免中文按 OEM 代码页吐出来变乱码。
    # 它不参与 AST 判定（判定只看用户脚本），也不会放宽任何路径规则。
    wrapped = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [Console]::OutputEncoding; "
        # Windows PowerShell 5.1 的 Get-Content 默认按 ANSI 读 BOM-less 文件，
        # 项目里的图片标注/说明都是 UTF-8；只在用户没显式指定编码时给 UTF-8 默认值。
        "try { $PSDefaultParameterValues['Get-Content:Encoding'] = 'UTF8' } catch { }; "
        "try { $PSStyle.OutputRendering = 'PlainText' } catch { }; " + report["script"]
    )
    code, stdout, stderr = _run_process(
        [binary, "-NoProfile", "-NonInteractive", "-Command", wrapped],
        cwd=root,
        timeout=timeout,
        env=_env(),
    )
    duration = int((time.monotonic() - started) * 1000)
    out = _plain(stdout)[:MAX_OUTPUT]
    err = _plain(stderr)[:MAX_OUTPUT]
    return {
        "exit_code": int(code),
        "stdout": out,
        "stderr": err,
        "truncated": len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT,
        "duration_ms": duration,
        "cmdlets": report["cmdlets"],
        "paths": report["paths"],
        "scope": "已用 PowerShell 解析器确认：只用允许命令 + 项目内相对路径",
        "note": (
            "工作目录只是 cwd，不是沙箱；能跑是因为脚本里每个路径都能证明在项目目录内。"
            "删除不会走这条通道。"
        ),
    }


__all__ = ["DEFAULT_TIMEOUT", "MAX_SCRIPT", "analyze", "run"]
