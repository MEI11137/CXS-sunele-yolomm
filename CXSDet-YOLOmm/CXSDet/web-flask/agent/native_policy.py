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
        result = subprocess.run([binary, '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
            str(Path(__file__).with_name('inspect_powershell.ps1'))], input=command.encode('utf-8'),
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
        for item in commands:
            if str(item.get('name') or '').lower() in DELETE_COMMANDS:
                elements = item.get('elements') or []
                index = 0
                while index < len(elements):
                    element = elements[index]
                    if element.get('kind') == 'literal':
                        decision['targets'].extend(element.get('values') or [])
                    elif element.get('kind') == 'parameter':
                        flag = str(element.get('name') or '').lower()
                        if flag in PATH_FLAGS:
                            argument = element.get('argument')
                            if argument is None and index + 1 < len(elements):
                                index += 1
                                argument = elements[index]
                            if argument and argument.get('kind') == 'literal':
                                decision['targets'].extend(argument.get('values') or [])
                        elif flag not in SWITCHES | {'whatif', 'confirm', 'verbose', 'debug'} and not element.get('argument'):
                            index += 1  # 非路径参数的值不当成文件。
                    index += 1
        return decision
    if not parsed.get('valid') or not names or not names <= READ_COMMANDS | FORMAT_COMMANDS | WRITE_COMMANDS:
        return decision
    writes = bool(names & WRITE_COMMANDS)
    for item in commands:
        name = item['name'].lower()
        elements = item.get('elements') or []
        index, path_count = 0, 0
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
                        path_count += 1
                if flag == 'itemtype' and any(v.lower() not in {'file', 'directory'} for v in value.get('values', [])):
                    return decision
                if name == 'set-content' and flag == 'value' and not any(v.strip() for v in value.get('values', [])):
                    decision.update(kind='delete', reason='清空文件内容需人工批准')
                    return decision
            index += 1
        if name in READ_COMMANDS | WRITE_COMMANDS and name != 'get-childitem' and not path_count:
            return decision
    decision.update(kind='write' if writes else 'read',
        auto=not writes or mode == 'full', reason='已核对项目内的明确常规操作' if writes else '已核对项目内的只读命令')
    if writes and any((Path(workspace) / target).is_file() for target in decision['targets']):
        decision.update(auto=False, reason='涉及已有文件的修改或覆盖，需要一次人工批准')
    return decision


def decide(permission, workspace, mode):
    metadata = permission.get('metadata') or {}
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
    auto = (permission.get('permission') == 'edit' and mode in {'assist', 'full'} and not deletion
            and not modifies_existing
            and bool(diff) and bool(targets) and all(inside(workspace, target) for target in targets))
    return {'auto': auto, 'kind': kind, 'targets': [path for path in targets if path],
            'reason': '删除或仅移除内容需人工确认；本次批量操作一次批准' if deletion else
                      '修改或覆盖已有文件需要一次人工批准' if modifies_existing else
                      '按会话权限处理项目内文件修改' if auto else '请核对文件、差异和影响后批准'}
