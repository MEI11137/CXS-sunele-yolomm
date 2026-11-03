"""确定性的文件/命令预检；解析未知即请求批准，不让模型替自己授权。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

MODES = {'request': '请求批准', 'assist': '帮我批准', 'full': '完全访问'}
READ_COMMANDS = {'get-childitem', 'get-content', 'get-item', 'test-path', 'resolve-path', 'get-filehash'}
FORMAT_COMMANDS = {'select-object', 'sort-object', 'measure-object', 'format-table', 'format-list',
                   'out-string', 'write-output', 'join-string'}
WRITE_COMMANDS = {'new-item', 'copy-item', 'set-content', 'add-content'}
DELETE_COMMANDS = {'remove-item', 'clear-content', 'move-item', 'rename-item', 'rm', 'del', 'erase', 'rmdir'}
PATH_FLAGS = {'path', 'literalpath', 'destination'}
SWITCHES = {'file', 'directory', 'recurse', 'force', 'name', 'raw', 'hidden', 'readonly', 'system',
            'leaf', 'isvalid', 'container', 'autosize', 'hidetableheaders', 'wrap', 'descending',
            'unique', 'sum', 'average', 'minimum', 'maximum', 'allstats', 'line', 'word', 'character'}
VALUE_FLAGS = PATH_FLAGS | {'filter', 'include', 'exclude', 'depth', 'attributes', 'totalcount',
    'tail', 'encoding', 'readcount', 'algorithm', 'pathtype', 'erroraction', 'ea', 'property',
    'first', 'last', 'skip', 'width', 'separator', 'itemtype', 'value', 'expandproperty'}


def powershell_binary():
    if os.name != 'nt':
        return None
    binary = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return str(binary) if binary.is_file() else None


def inside(workspace, value):
    if not isinstance(value, str) or not value or any(ch in value for ch in '*?[]\x00\r\n'):
        return False
    if value.startswith(('\\\\', '//', '~')):
        return False
    if ':' in value and not (os.name == 'nt' and len(value) > 2 and value[1] == ':' and value[2] in '\\/'):
        return False
    root = Path(workspace).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        # 拒绝链接、junction 或 .. 指向项目外部。
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def inspect_command(command):
    binary = powershell_binary()
    if not binary or not isinstance(command, str) or len(command) > 32000:
        return {'valid': False, 'commands': []}
    try:
        result = subprocess.run([binary, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(Path(__file__).with_name('inspect_powershell.ps1'))], input=command.encode('utf-8'),
            capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout.decode('utf-8-sig')) if result.returncode == 0 else {'valid': False, 'commands': []}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'valid': False, 'commands': []}


def command_decision(command, workspace, mode):
    parsed = inspect_command(command)
    commands = parsed.get('commands') or []
    names = {str(item.get('name') or '').lower() for item in commands}
    decision = {'auto': False, 'kind': 'unknown', 'targets': [], 'reason': '无法确定命令的全部影响，请核对后批准'}
    if names & DELETE_COMMANDS:
        decision.update(kind='delete', reason='删除、清空或移动需人工确认；整条批量命令只批准本次一次')
        # 删除/清空/移动类命令必须逐个核对“每一条命令、每一个必需路径参数”：
        # 只采集识别得出的字面量而忽略动态表达式（字符串拼接、变量、子表达式），
        # 会让 auto=True 却漏掉越界目标。任何无法证明的部分都直接放弃自动。
        if not parsed.get('valid') or not commands:
            return decision
        for item in commands:
            name = str(item.get('name') or '').lower()
            if name not in DELETE_COMMANDS:
                # 混入读取/写入/格式化或未知命令：整条命令范围无法完整证明。
                return decision
            elements = item.get('elements') or []
            index = 0
            command_targets = []
            while index < len(elements):
                element = elements[index]
                kind = element.get('kind')
                if kind == 'parameter':
                    flag = str(element.get('name') or '').lower()
                    argument = element.get('argument')
                    if flag in SWITCHES:
                        if argument is not None:
                            return decision  # -Force:$true 一类开关带参，无法证明
                        index += 1
                        continue
                    if argument is None:
                        index += 1
                        if index >= len(elements):
                            return decision
                        argument = elements[index]
                    if not isinstance(argument, dict) or argument.get('kind') != 'literal':
                        # 动态路径/变量/字符串拼接：不能确定目标，放弃自动。
                        return decision
                    values = [str(value) for value in argument.get('values') or []]
                    if not values:
                        return decision
                    if flag == 'newname':
                        # Rename-Item 的新名字必须是纯名称字面量，不能改变目录。
                        if name != 'rename-item':
                            return decision
                        if any(not value.strip() or any(ch in value for ch in ':\\/') for value in values):
                            return decision
                        index += 1
                        continue
                    if flag not in PATH_FLAGS:
                        # 删除类命令上的其它参数可能携带路径（如 -Include 相对路径），
                        # 不逐个证明影响就拒绝自动。
                        return decision
                    for value in values:
                        if not inside(workspace, value):
                            return decision
                        decision['targets'].append(value)
                        command_targets.append(value)
                    index += 1
                    continue
                if kind != 'literal':
                    return decision  # 位置参数也必须是字面量路径
                values = [str(value) for value in element.get('values') or []]
                if not values:
                    return decision
                for value in values:
                    if not inside(workspace, value):
                        return decision
                    decision['targets'].append(value)
                    command_targets.append(value)
                index += 1
            if not command_targets:
                # 每条删除/清空/移动命令都必须有可核对的明确路径。
                return decision
        if mode == 'full':
            # 完全访问只自动处理「整条命令都是项目内删除/清空/移动、目标全部为
            # 字面量且可逐个核对」的调用；parsed.valid 必须为真。
            scoped = (bool(parsed.get('valid')) and bool(decision['targets'])
                      and all(inside(workspace, path) for path in decision['targets']))
            decision.update(auto=scoped, reason='完全访问模式自动处理项目内目标明确的删除'
                            if scoped else '命令混含动态/缺失目标或其它操作，无法确认完整范围')
        return decision
    if not parsed.get('valid') or not names or not names <= READ_COMMANDS | FORMAT_COMMANDS | WRITE_COMMANDS:
        return decision
    writes = bool(names & WRITE_COMMANDS)
    clears = False
    clear_targets = []
    for item in commands:
        name = item['name'].lower()
        elements = item.get('elements') or []
        index, path_count = 0, 0
        command_targets = []
        cleared_this_command = False
        while index < len(elements):
            element = elements[index]
            flag = ''
            if element.get('kind') == 'parameter':
                flag = str(element.get('name') or '').lower()
                if flag in SWITCHES:
                    if name == 'new-item' and flag == 'force':
                        return decision
                    if element.get('argument') is not None:
                        return decision
                    index += 1
                    continue
                if flag not in VALUE_FLAGS:
                    return decision
                if element.get('argument') is not None:
                    value = element['argument']
                else:
                    index += 1
                    if index >= len(elements):
                        return decision
                    value = elements[index]
            else:
                value = element
            if name in READ_COMMANDS | WRITE_COMMANDS:
                if value.get('kind') != 'literal':
                    return decision
                if flag in PATH_FLAGS or not flag:
                    for path in value.get('values', []):
                        if not inside(workspace, path):
                            return decision
                        decision['targets'].append(path)
                        command_targets.append(path)
                        path_count += 1
                if flag == 'itemtype' and any(v.lower() not in {'file', 'directory'} for v in value.get('values', [])):
                    return decision
                if name == 'set-content' and flag == 'value' and not any(v.strip() for v in value.get('values', [])):
                    # 先记下“清空内容”，继续核对后面每条命令和路径，全部预检完才决定是否自动。
                    clears = True
                    cleared_this_command = True
            index += 1
        if cleared_this_command:
            clear_targets.extend(command_targets)
        if name in READ_COMMANDS | WRITE_COMMANDS and name != 'get-childitem' and not path_count:
            return decision
    if name == 'get-childitem' and not decision['targets']:
        decision['targets'].append(str(Path(workspace).resolve()))
    if clears:
        # 清空文件内容按删除处理；full 下也必须整条命令有效、所有命令与路径预检通过、
        # 清空目标都是项目内已存在的文件，才允许自动。
        scoped = (mode == 'full' and bool(parsed.get('valid')) and bool(clear_targets)
                  and all(inside(workspace, path) for path in decision['targets'])
                  and all((Path(workspace) / path).is_file() for path in clear_targets))
        decision.update(kind='delete', auto=scoped,
                        reason='完全访问模式自动处理项目内清空文件内容' if scoped else '清空文件内容需人工批准')
        return decision
    decision.update(kind='write' if writes else 'read',
        auto=not writes or mode == 'full',
        reason=('完全访问模式自动处理项目内明确的常规操作' if mode == 'full' and writes else
                '已核对项目内的明确常规操作' if writes else '已核对项目内的只读命令'))
    if writes and mode != 'full' and any((Path(workspace) / target).is_file() for target in decision['targets']):
        decision.update(auto=False, reason='涉及已有文件的修改或覆盖，需要一次人工批准')
    return decision


def decide(permission, workspace, mode):
    metadata = permission.get('metadata') or {}
    if permission.get('permission') in {'list', 'read', 'glob', 'grep'}:
        base = Path(workspace).resolve()
        targets = []
        for raw in permission.get('patterns') or []:
            if not isinstance(raw, str) or not raw:
                continue
            head = raw
            for index, ch in enumerate(raw):
                if ch in '*?[':
                    head = raw[:index]
                    break
            head = head.rstrip('\\/') or '.'
            candidate = Path(head)
            targets.append(str(candidate if candidate.is_absolute() else base / candidate))
        return {'auto': bool(targets) and all(inside(base, target) for target in targets), 'kind': 'read', 'targets': targets,
                'reason': '读取演示文件夹内的内容' if targets else '读取范围无法确定'}
    if permission.get('permission') in {'bash', 'shell'}:
        return command_decision(metadata.get('command'), workspace, mode)
    files = metadata.get('files') or []
    targets = [item.get('filePath') for item in files] if files else [metadata.get('filepath')]
    deletion = any(item.get('type') in {'delete', 'move'} for item in files)
    diff = str(metadata.get('diff') or '')
    deletion = deletion or '*** Delete File:' in diff or '+++ /dev/null' in diff
    removed = [line for line in diff.splitlines() if line.startswith('-') and not line.startswith('---')]
    added = [line for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++') and line[1:].strip()]
    deletion = deletion or bool(removed and not added)
    kind = 'delete' if deletion else 'write'
    modifies_existing = any(inside(workspace, target) and (Path(workspace) / target).exists() for target in targets if target)
    inside_all = bool(targets) and all(inside(workspace, target) for target in targets)
    # 完全访问：项目内、目标与差异明确的删除/覆盖也直接执行；assist 仍保留
    # 「已有文件修改/删除需一次批准」的边界。无法核对范围时 auto=False，
    # 上层在 full 下会明确拒绝而不是生成待点击卡片。
    auto = (permission.get('permission') == 'edit' and bool(diff) and inside_all
            and (mode == 'full' or (mode == 'assist' and not deletion and not modifies_existing)))
    if mode == 'full':
        reason = ('完全访问模式自动处理项目内明确目标的删除操作' if deletion else
                  '完全访问模式自动处理项目内明确的文件修改')
    else:
        reason = ('删除或仅移除内容需人工确认；本次批量操作一次批准' if deletion else
                  '修改或覆盖已有文件需要一次人工批准' if modifies_existing else
                  '按会话权限处理项目内文件修改' if auto else '请核对文件、差异和影响后批准')
    return {'auto': auto, 'kind': kind, 'targets': [path for path in targets if path],
            'reason': reason}


def confined_permission(permission, decision, workspace):
    """Remote users cannot approve commands whose complete scope is unknown.

    这里只回答“范围是否被完整证明”，与当前模式是否自动执行无关：
    普通账号 request/assist 下的明确删除也应通过范围证明并拿到确认卡，
    而不是因为 ``decision['auto']`` 为 False 就被当成越界直接拒绝。
    """
    if decision['kind'] == 'unknown' or not decision['targets'] or not all(inside(workspace, path) for path in decision['targets']):
        return False
    if decision['kind'] == 'delete' and permission.get('permission') in {'bash', 'shell'}:
        command = (permission.get('metadata') or {}).get('command')
        # 用 full 模式重新预检整条命令的完整范围：literal 路径、无动态表达式、
        # 无重定向/无效 AST、无混合命令；auto 是“范围已完整证明”的判据，
        # 不是“当前模式要自动执行”的判据。
        scope = command_decision(command, workspace, 'full')
        return bool(scope.get('auto')) and scope.get('kind') == 'delete'
    return True
