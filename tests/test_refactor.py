import unittest
from pathlib import Path
from unittest.mock import patch

from diskmgrlib.common import get_map_file_path, get_script_dir
from diskmgrlib.commands.block import BlockCommands
from diskmgrlib.commands.destructive import DestructiveCommands
from diskmgrlib.commands.filesystem import FilesystemCommands
from diskmgrlib.commands.partition import PartitionCommands
from diskmgrlib.commands.provisioning import ProvisioningCommands
from diskmgrlib.inventory import InventoryMixin
from diskmgrlib.mappings import validate_mapping_name, validate_persistent_target
from diskmgrlib.mount_policy import MountPolicyMixin
from diskmgrlib.rawio import _ata_security_block, _parse_ddrescue_failed_ranges, _parse_nvme_sanitize_status, _software_zero_overwrite
from diskmgrlib.runtime import _command_environment, privileged_command, sanitize_terminal_text
from diskmgrlib.safety import SafetyMixin
from diskmgrlib.shell import DiskMgrShell
from diskmgrlib.smart import _parse_smart_attr_row, _parse_smart_last_error_poh


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
        self.assertIn("MountPolicyMixin", names)
        self.assertIn("InventoryMixin", names)
        self.assertIn("SafetyMixin", names)
        self.assertIn("ListingCommands", names)
        self.assertIn("FilesystemCommands", names)

    def test_command_compatibility_aliases_preserve_old_imports(self):
        self.assertTrue(issubclass(FilesystemCommands, ProvisioningCommands))
        self.assertTrue(issubclass(DestructiveCommands, BlockCommands))
        self.assertTrue(issubclass(PartitionCommands, ProvisioningCommands))
        for command in ("do_create", "do_remove", "do_format", "do_convert"):
            self.assertTrue(hasattr(ProvisioningCommands, command), command)

    def test_command_modules_do_not_use_wildcard_common_imports(self):
        project_root = Path(__file__).resolve().parents[1]
        sources = list((project_root / "diskmgrlib" / "commands").glob("*.py"))
        sources += [
            project_root / "diskmgrlib" / "shell_core.py",
            project_root / "diskmgrlib" / "shell_helpers.py",
            project_root / "diskmgrlib" / "mount_policy.py",
            project_root / "diskmgrlib" / "inventory.py",
            project_root / "diskmgrlib" / "safety.py",
        ]
        for source in sources:
            self.assertNotIn("import *", source.read_text(), source.name)


class FormatSafetyTests(unittest.TestCase):
    def setUp(self):
        self.shell = DiskMgrShell.__new__(DiskMgrShell)

    def test_blank_blkid_probe_status_is_not_an_error(self):
        result = type("Result", (), {"returncode": 2, "stdout": "", "stderr": ""})()
        with patch("diskmgrlib.safety.run_command_hard_timeout", return_value=result):
            values, error = self.shell._format_probe_blkid("/dev/example")
        self.assertEqual(values, {})
        self.assertEqual(error, "")

    def test_blkid_probe_diagnostic_fails_closed(self):
        result = type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Input/output error"},
        )()
        with patch("diskmgrlib.safety.run_command_hard_timeout", return_value=result):
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


class RuntimeAndParserSafetyTests(unittest.TestCase):
    def test_terminal_control_characters_are_neutralized(self):
        self.assertEqual(sanitize_terminal_text("ok\x1b[31mBAD\x07\nnext"), "ok\\x1b[31mBAD\\x07\nnext")

    def test_machine_commands_force_c_locale(self):
        with patch.dict("os.environ", {"LC_ALL": "en_GB.UTF-8", "LANG": "en_GB.UTF-8"}, clear=False):
            env = _command_environment()
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["LANG"], "C")

    def test_privileged_commands_use_pkexec_by_default(self):
        with patch("diskmgrlib.runtime.os.geteuid", return_value=1000), patch(
            "diskmgrlib.runtime.shutil.which", return_value="/usr/bin/pkexec"
        ), patch.dict("os.environ", {}, clear=False):
            self.assertEqual(privileged_command(["mount", "/dev/x", "/mnt/x"])[0], "/usr/bin/pkexec")

    def test_command_logs_redact_ata_password_arguments(self):
        from diskmgrlib.runtime import _logged_command

        rendered = _logged_command(["hdparm", "--user-master", "u", "--security-set-pass", "secret", "/dev/sda"])
        self.assertNotIn("secret", rendered)
        self.assertIn("<redacted>", rendered)

    def test_mapping_names_and_targets_are_restricted(self):
        self.assertEqual(validate_mapping_name("backup-01"), "backup-01")
        for bad in ("", "#1", "1", "../backup", "bad name", "a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_mapping_name(bad)
        self.assertEqual(validate_persistent_target("/dev/disk/by-id/wwn-test"), "/dev/disk/by-id/wwn-test")
        with self.assertRaises(ValueError):
            validate_persistent_target("/dev/sda")

    def test_ddrescue_failed_ranges_are_parsed_as_byte_ranges(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii") as handle:
            handle.write("0x00001000 0x00002000 -\n")
            handle.write("0x00003000 0x00001000 +\n")
            handle.flush()
            ranges = _parse_ddrescue_failed_ranges(handle.name, sector_size=512)
        self.assertEqual(ranges[0]["start_b"], 0x1000)
        self.assertEqual(ranges[0]["size_b"], 0x2000)
        self.assertEqual(ranges[0]["count_lba"], 16)

    def test_zero_overwrite_requests_byte_exact_dd_and_verifies_edges(self):
        sample = 1024 * 1024
        result = type("Result", (), {"returncode": 0, "stdout": b"\0" * sample, "stderr": ""})()
        with patch("diskmgrlib.rawio.run_command", return_value=result) as run, patch(
            "diskmgrlib.rawio.run_command_bytes", return_value=result
        ) as read:
            _software_zero_overwrite("/dev/example", 4 * sample)
        dd_command = run.call_args_list[0].args[0]
        self.assertIn(f"count={4 * sample}", dd_command)
        self.assertTrue(any("count_bytes" in item for item in dd_command))
        self.assertEqual(read.call_count, 3)

    def test_smart_parsers_handle_raw_tail_and_non_one_error_numbers(self):
        text = (
            "ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE\n"
            "  194 Temperature_Celsius 0x0002 064 030 000 Old_age Always - 29 (Min/Max 4/70)\n"
            "Error 667 occurred at disk power-on lifetime: 20,140 hours (839 days)\n"
        )
        row = _parse_smart_attr_row(text, 194)
        self.assertEqual(row["raw"], "29 (Min/Max 4/70)")
        self.assertEqual(_parse_smart_last_error_poh(text), (667, 20140))

    def test_erase_capability_parsers_accept_real_world_formats(self):
        hdparm = """Security:\n\t\tsupported\n\t\tnot enabled\n\t\tnot locked\n\t\tnot frozen\n\t\tsupported: enhanced erase\n"""
        security = _ata_security_block(hdparm)
        self.assertIn("supported", security)
        self.assertIn("not frozen", security)
        self.assertEqual(_parse_nvme_sanitize_status("0x2 (in progress)"), 2)
        self.assertEqual(_parse_nvme_sanitize_status("1"), 1)
        self.assertIsNone(_parse_nvme_sanitize_status("unknown"))


if __name__ == "__main__":
    unittest.main()
