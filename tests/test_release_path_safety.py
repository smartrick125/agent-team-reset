"""Regression checks for Python 3.10-compatible handoff path inspection."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "agent-team-reset" / "scripts"))
import rotate


class LegacyPath:
    """Expose only path operations supported by the minimum Python version."""

    def __init__(self, path):
        self.path = path

    @property
    def parents(self):
        return tuple(LegacyPath(p) for p in self.path.parents)

    def is_symlink(self):
        return self.path.is_symlink()

    def is_file(self):
        return self.path.is_file()

    def lstat(self):
        return self.path.lstat()

    def __str__(self):
        return str(self.path)


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.file = self.root / 'core.md'
        self.file.write_text('Unfinished work and next action\n', encoding='utf-8')

    def test_minimum_version_path_shape_accepts_regular_file(self):
        legacy = LegacyPath(self.file)
        self.assertIs(rotate.ordinary_file(legacy), legacy)
        self.assertEqual(self.file.read_text(encoding='utf-8'), 'Unfinished work and next action\n')

    def test_directory_is_not_a_handoff(self):
        with self.assertRaisesRegex(ValueError, 'regular file'):
            rotate.ordinary_file(self.root)

    def test_windows_reparse_component_is_rejected_without_new_path_api(self):
        for parent in (False, True):
            with self.subTest(parent=parent):
                ordinary = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0))
                reparse = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
                candidate = SimpleNamespace(
                    parents=(reparse,) if parent else (), is_symlink=lambda: False,
                    is_file=lambda: True, lstat=ordinary.lstat if parent else reparse.lstat)
                with self.assertRaisesRegex(ValueError, 'Links'):
                    rotate.ordinary_file(candidate)

    def test_symbolic_link_and_linked_parent_leave_target_unchanged(self):
        link = self.root / 'linked'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Symbolic link creation unavailable: {error}')
        with self.assertRaisesRegex(ValueError, 'Links'):
            rotate.ordinary_file(link)
        with self.assertRaisesRegex(ValueError, 'Links'):
            rotate.ordinary_file(link / 'core.md')
        self.assertEqual(self.file.read_text(encoding='utf-8'), 'Unfinished work and next action\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction check')
    def test_real_windows_junction_and_parent_are_rejected(self):
        link = self.root / 'junction'
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(self.root)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Remove only the temporary junction itself, never recurse into its target.
        self.addCleanup(os.rmdir, link)
        self.assertTrue(link.lstat().st_file_attributes & 0x400)
        for candidate in (link, link / 'core.md'):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, 'Links'):
                    rotate.ordinary_file(candidate)
        self.assertEqual(self.file.read_text(encoding='utf-8'), 'Unfinished work and next action\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
