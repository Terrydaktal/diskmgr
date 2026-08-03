import unittest
from pathlib import Path
from unittest.mock import patch

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


class FormatSafetyTests(unittest.TestCase):
    def setUp(self):
        self.shell = DiskMgrShell.__new__(DiskMgrShell)

    def test_blank_blkid_probe_status_is_not_an_error(self):
        result = type("Result", (), {"returncode": 2, "stdout": "", "stderr": ""})()
        with patch("diskmgrlib.shell_helpers.run_command_hard_timeout", return_value=result):
            values, error = self.shell._format_probe_blkid("/dev/example")
        self.assertEqual(values, {})
        self.assertEqual(error, "")

    def test_blkid_probe_diagnostic_fails_closed(self):
        result = type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Input/output error"},
        )()
        with patch("diskmgrlib.shell_helpers.run_command_hard_timeout", return_value=result):
            values, error = self.shell._format_probe_blkid("/dev/example")
        self.assertIsNone(values)
        self.assertIn("probe failed", error)

    def test_identity_and_signature_changes_are_detected(self):
        before = {
            "wwn": "wwn-a",
            "serial": "serial-a",
            "pci": "pci-a",
            "major_minor": "8:0",
            "size_bytes": 100,
            "logical_sector_bytes": "512",
            "physical_sector_bytes": "4096",
        }
        after = dict(before, major_minor="8:16")
        self.assertEqual(self.shell._format_identity_changed(before, after), ["major_minor"])
        signature = [{"device": "/dev/example", "type": "ext4", "label": "x", "uuid": "u", "offset": "-"}]
        self.assertFalse(self.shell._format_signatures_changed(signature, list(signature)))
        self.assertTrue(self.shell._format_signatures_changed(signature, []))


if __name__ == "__main__":
    unittest.main()
