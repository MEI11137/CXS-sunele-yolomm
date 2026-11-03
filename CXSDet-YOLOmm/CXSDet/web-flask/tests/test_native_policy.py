import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.native_policy import command_decision, confined_permission, decide, inspect_command


def _parsed(*commands, valid=True):
    return {'valid': valid, 'commands': list(commands)}


def _literal_flag(flag, *values):
    return {'kind': 'parameter', 'name': flag, 'argument': {'kind': 'literal', 'values': list(values)}}


def _param(flag, argument):
    return {'kind': 'parameter', 'name': flag, 'argument': argument}


def _literal(*values):
    return {'kind': 'literal', 'values': list(values)}


class NativePolicyTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='haixun-policy-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_file_edits_are_scoped_and_deletion_never_auto(self):
        file = str(self.root / 'labels.txt')
        permission = {'permission': 'edit', 'metadata': {'filepath': file, 'diff': '--- old\n+++ new\n-old\n+new'}}
        self.assertFalse(decide(permission, self.root, 'request')['auto'])
        self.assertTrue(decide(permission, self.root, 'assist')['auto'])
        self.assertTrue(decide(permission, self.root, 'full')['auto'])
        permission['metadata']['filepath'] = str(self.root.parent / 'external.txt')
        self.assertFalse(decide(permission, self.root, 'full')['auto'])
        permission['metadata'] = {'files': [{'filePath': file, 'type': 'delete'}], 'diff': 'delete'}
        self.assertFalse(decide(permission, self.root, 'request')['auto'])
        self.assertFalse(decide(permission, self.root, 'assist')['auto'])
        self.assertTrue(decide(permission, self.root, 'full')['auto'])
        permission['metadata'] = {'filepath': file, 'diff': '--- old\n+++ new\n-entire content'}
        self.assertTrue(decide(permission, self.root, 'full')['auto'])

    def test_existing_file_edits_require_approval_unless_full_mode(self):
        target = self.root / 'existing.txt'
        target.write_text('original')
        permission = {'permission': 'edit', 'metadata': {'filepath': str(target), 'diff': '--- old\n+++ new\n-original\n+updated'}}
        self.assertFalse(decide(permission, self.root, 'request')['auto'])
        self.assertFalse(decide(permission, self.root, 'assist')['auto'])
        # 完全访问：项目内已有文件的明确修改直接执行，不生成确认卡。
        self.assertTrue(decide(permission, self.root, 'full')['auto'])
        self.assertEqual(target.read_text(), 'original')

    def _ast_available(self):
        return bool(inspect_command('Get-Date').get('valid'))

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell AST 检查')
    def test_batch_read_is_automatic_but_nested_or_unknown_commands_are_not(self):
        if not self._ast_available():
            self.skipTest('当前沙箱无法运行 PowerShell AST 检查；真实解析在宿主侧验证')
        path = str(self.root).replace("'", "''")
        command = f"Get-ChildItem -LiteralPath '{path}' -Force | Select-Object -First 5 Name,Length | Format-Table -AutoSize; 'count=' + (Get-ChildItem -LiteralPath '{path}' -File -Recurse).Count"
        parsed = inspect_command(command)
        self.assertTrue(parsed['valid'], parsed)
        for mode in ['request', 'assist', 'full']:
            self.assertTrue(command_decision(command, self.root, mode)['auto'])
            self.assertTrue(command_decision(f"Get-ChildItem -LiteralPath '{path}' -Directory -Recurse -Depth 2 | Select-Object -ExpandProperty FullName", self.root, mode)['auto'])
            self.assertFalse(command_decision(f"Get-ChildItem '{path}'; Remove-Item '{path}'", self.root, mode)['auto'])
            self.assertFalse(command_decision('python -c "import os; os.remove(\'a\')"', self.root, mode)['auto'])
        cases = [f"Get-Content $(Remove-Item '{path}')", "Write-Output hi > victim.txt",
                 "Invoke-Expression 'Remove-Item x'", "& 'Get-Content' x", "(Get-Item 'x').Delete()",
                 "(Get-Item 'x').Attributes++", "Get-Content Env:SECRET", "Get-Content '../outside.txt'",
                 "New-Item -ItemType SymbolicLink -Path link -Value ."]
        for command in cases:
            with self.subTest(command=command):
                self.assertFalse(command_decision(command, self.root, 'full')['auto'])

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell AST 检查')
    def test_routine_command_writes_need_full_mode_and_never_execute_during_inspection(self):
        if not self._ast_available():
            self.skipTest('当前沙箱无法运行 PowerShell AST 检查；真实解析在宿主侧验证')
        target = self.root / 'not-executed.txt'
        command = f"Set-Content -LiteralPath '{target}' -Value 'hello'"
        self.assertFalse(command_decision(command, self.root, 'assist')['auto'])
        self.assertTrue(command_decision(command, self.root, 'full')['auto'])
        self.assertFalse(target.exists())
        self.assertFalse(command_decision(f"Set-Content -LiteralPath '{target}' -Value ''", self.root, 'full')['auto'])
        deletion = command_decision("Remove-Item -LiteralPath 'a.png','b.png' -Force -ErrorAction Stop", self.root, 'full')
        self.assertEqual(deletion['targets'], ['a.png', 'b.png'])
        self.assertFalse(deletion['auto'])

    def test_relative_read_targets_use_workspace_not_parent(self):
        decision = decide({'permission': 'read', 'patterns': ['images/a.jpg']}, self.root, 'full')
        self.assertEqual(decision['targets'], [str(self.root.resolve() / 'images/a.jpg')])
        from agent.native_policy import inside
        self.assertTrue(inside(self.root, decision['targets'][0]))
        outside = decide({'permission': 'read', 'patterns': ['../private.txt']}, self.root, 'full')
        self.assertFalse(inside(self.root, outside['targets'][0]))

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell AST 检查')
    def test_dynamic_delete_expression_never_auto_on_real_ast(self):
        if not self._ast_available():
            self.skipTest('当前沙箱无法运行 PowerShell AST 检查；真实解析在宿主侧验证')
        command = ("Remove-Item -LiteralPath 'safe.txt'; "
                   "Remove-Item -LiteralPath ('C:\\\\outside\\\\' + 'file.txt')")
        decision = command_decision(command, self.root, 'full')
        self.assertFalse(decision['auto'], decision)


class NativePolicyMockedAstTest(unittest.TestCase):
    """沙箱里无法生成真实 PowerShell AST 时，用结构化解析结果覆盖安全边界。"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='haixun-policy-mock-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_clear_content_must_preflight_every_command_and_path(self):
        target = self.root / 'victim.txt'
        target.write_text('data')
        clear_cmd = {'name': 'Set-Content', 'elements': [
            _literal_flag('LiteralPath', str(target)),
            _literal_flag('Value', ''),
        ]}
        # 空值 Set-Content 之后跟一条越界读取：不能因为第一条命令看起来可自动就放行。
        outside = {'name': 'Get-Content', 'elements': [_literal('C:/outside/secret.txt')]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(clear_cmd, outside)):
            decision = command_decision('stub', self.root, 'full')
        self.assertFalse(decision['auto'])
        # 只有清空命令、目标存在且在项目内，full 才自动。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(clear_cmd)):
            decision = command_decision('stub', self.root, 'full')
        self.assertTrue(decision['auto'], decision)
        self.assertEqual(decision['kind'], 'delete')

    def test_invalid_ast_and_mixed_commands_never_auto_in_full(self):
        deletion = {'name': 'Remove-Item', 'elements': [_literal_flag('LiteralPath', 'a.png')]}
        mixed = {'name': 'Remove-Item', 'elements': [_literal_flag('LiteralPath', 'a.png')]}
        unknown = {'name': 'Invoke-Expression', 'elements': [_literal('Remove-Item a.png')]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(deletion, valid=False)):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])
        with patch('agent.native_policy.inspect_command', return_value=_parsed(mixed, unknown)):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])

    def test_confined_permission_requires_complete_scope(self):
        target = str(self.root / 'a.png')
        Path(target).write_text('data')
        decision = {'auto': True, 'kind': 'delete', 'targets': [target]}
        clear = {'name': 'Set-Content', 'elements': [
            _literal_flag('LiteralPath', target), _literal_flag('Value', '')]}
        remove = {'name': 'Remove-Item', 'elements': [_literal_flag('LiteralPath', target)]}
        read = {'name': 'Get-Content', 'elements': [_literal_flag('LiteralPath', target)]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(remove)):
            self.assertTrue(confined_permission({'permission': 'bash'}, decision, self.root))
        # 清空文件内容（Set-Content 空值）不能被“只收删除命令”的旧规则误拒。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(clear)):
            self.assertTrue(confined_permission({'permission': 'bash'}, decision, self.root))
        # 混合读取/未知命令或无效 AST 一律不自动。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(remove, read)):
            self.assertFalse(confined_permission({'permission': 'bash'}, decision, self.root))
        with patch('agent.native_policy.inspect_command', return_value=_parsed(remove, valid=False)):
            self.assertFalse(confined_permission({'permission': 'bash'}, decision, self.root))
        with patch('agent.native_policy.inspect_command', return_value=_parsed(remove, read)):
            self.assertFalse(confined_permission({'permission': 'bash'},
                {'auto': False, 'kind': 'delete', 'targets': [target]}, self.root))

    def test_delete_targets_must_all_be_literal_and_explicit(self):
        dynamic_path = {'kind': 'pipeline', 'values': ['C:/outside/file.txt']}
        safe = {'name': 'Remove-Item', 'elements': [_literal_flag('LiteralPath', 'safe.txt')]}
        sneaky = {'name': 'Remove-Item',
                  'elements': [_param('LiteralPath', dynamic_path)]}
        # 复现审查缺陷：第一条删除可识别、第二条路径是字符串拼接/动态表达式，不能 auto。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(safe, sneaky)):
            decision = command_decision('stub', self.root, 'full')
        self.assertFalse(decision['auto'], decision)
        # 单条命令没有明确路径参数时也不能自动。
        with patch('agent.native_policy.inspect_command',
                   return_value=_parsed({'name': 'Remove-Item', 'elements': []})):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])
        # 删除类命令上的未知参数可能携带路径，拒绝自动。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(
                {'name': 'Remove-Item', 'elements': [_param('Filter', {'kind': 'literal', 'values': ['*.png']})]})):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])
        # 位置参数是动态表达式时同样拒绝。
        with patch('agent.native_policy.inspect_command', return_value=_parsed(
                {'name': 'Remove-Item', 'elements': [{'kind': 'variable', 'values': ['$p']}]})):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])

    def test_rename_and_move_destinations_are_fully_checked(self):
        rename_ok = {'name': 'Rename-Item', 'elements': [
            _literal_flag('LiteralPath', 'a.png'), _literal_flag('NewName', 'b.png')]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(rename_ok)):
            decision = command_decision('stub', self.root, 'full')
        self.assertTrue(decision['auto'], decision)
        # NewName 带目录分隔符会改变目标位置，不能自动。
        rename_escape = {'name': 'Rename-Item', 'elements': [
            _literal_flag('LiteralPath', 'a.png'), _literal_flag('NewName', 'sub/b.png')]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(rename_escape)):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])
        move_dynamic = {'name': 'Move-Item', 'elements': [
            _literal_flag('LiteralPath', 'a.png'),
            _param('Destination', {'kind': 'variable', 'values': ['$dest']})]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(move_dynamic)):
            self.assertFalse(command_decision('stub', self.root, 'full')['auto'])
        move_ok = {'name': 'Move-Item', 'elements': [
            _literal_flag('LiteralPath', 'a.png'), _literal_flag('Destination', 'archive')]}
        with patch('agent.native_policy.inspect_command', return_value=_parsed(move_ok)):
            decision = command_decision('stub', self.root, 'full')
        self.assertTrue(decision['auto'], decision)