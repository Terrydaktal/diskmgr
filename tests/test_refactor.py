import unittest
from pathlib import Path

from diskmgrlib.common import get_map_file_path, get_script_dir
from diskmgrlib.shell import DiskMgrShell


class RefactorSmokeTests(unittest.TestCase):
    def test_entry_point_and_mapping_path(self):
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(get_script_dir(), project_root)
        self.assertEqual(get_map_file_path(), project_root / "diskmap.tsv")

    def test_all_command_methods_are_present(self):
        expected = {
            "boot", "clone", "close", "convert", "create", "defrag", "diff",
            "EOF", "entropy", "entropise", "erase", "exit", "format", "fsdiag",
            "fshealth", "health", "help", "label", "layout", "list", "luks", "map", "nuke",
            "open", "quit", "remount", "remove", "scrub", "selftest", "smart", "sync",
            "unmap", "version",
        }
        actual = {name[3:] for name in dir(DiskMgrShell) if name.startswith("do_")}
        self.assertEqual(actual, expected)

    def test_shell_is_composed_from_mixins(self):
        names = {base.__name__ for base in DiskMgrShell.__mro__}
        self.assertIn("ShellCoreMixin", names)
        self.assertIn("ShellHelpersMixin", names)
        self.assertIn("ListingCommands", names)
        self.assertIn("FilesystemCommands", names)


if __name__ == "__main__":
    unittest.main()
