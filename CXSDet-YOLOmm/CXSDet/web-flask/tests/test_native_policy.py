import os
from pathlib import Path
import tempfile
import unittest

from agent.native_policy import command_decision, decide, inspect_command


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
        permission['metadata']['filepath'] = str(self.root.parent / 'external.txt')
        self.assertFalse(decide(permission, self.root, 'full')['auto'])
        permission['metadata'] = {'files': [{'filePath': file, 'type': 'delete'}], 'diff': 'delete'}
        for mode in ['request', 'assist', 'full']:
            self.assertFalse(decide(permission, self.root, mode)['auto'])
        permission['metadata'] = {'filepath': file, 'diff': '--- old\n+++ new\n-entire content'}
        self.assertFalse(decide(permission, self.root, 'full')['auto'])

    def test_existing_file_edits_require_approval_in_all_modes(self):
        target=self.root/'existing.txt';target.write_text('original')
        permission={'permission':'edit','metadata':{'filepath':str(target),'diff':'--- old\n+++ new\n-original\n+updated'}}
        for mode in ['request','assist','full']:
            self.assertFalse(decide(permission,self.root,mode)['auto'])
        self.assertEqual(target.read_text(),'original')

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell AST 检查')
    def test_batch_read_is_automatic_but_nested_or_unknown_commands_are_not(self):
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
        target = self.root / 'not-executed.txt'
        command = f"Set-Content -LiteralPath '{target}' -Value 'hello'"
        self.assertFalse(command_decision(command, self.root, 'assist')['auto'])
        self.assertTrue(command_decision(command, self.root, 'full')['auto'])
        self.assertFalse(target.exists())
        self.assertFalse(command_decision(f"Set-Content -LiteralPath '{target}' -Value ''", self.root, 'full')['auto'])
        deletion = command_decision("Remove-Item -LiteralPath 'a.png','b.png' -Force -ErrorAction Stop", self.root, 'full')
        self.assertEqual(deletion['targets'], ['a.png', 'b.png'])
        self.assertFalse(deletion['auto'])
