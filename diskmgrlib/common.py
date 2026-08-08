"""Backward-compatible shared symbol exports.

New code should import the smallest responsibility-focused module directly.
This module keeps the historical ``diskmgrlib.common`` import surface available
for external callers during the modular transition.
"""

from .runtime import (
    Colors, DEFAULT_HISTORY_FILE, HISTORY_FILE_ENV, LUKS_HEADER_BACKUP_DIR,
    LUKS_PBKDF_DEFAULT_MEMORY_KIB, LUKS_PBKDF_DEFAULT_MEMORY_LABEL,
    LUKS_PBKDF_DEFAULT_THREADS, LUKS_PBKDF_DEFAULT_TIME, MAP_FILENAME,
    MAX_HISTORY_ENTRIES, PASSGEN_BIN, VERSION, _CMD_LOG_FH, _CMD_LOG_PATH,
    _cmd_log_close, _cmd_log_open, _cmd_log_write, _find_tool_or_common_paths,
    _first_int_from_text, _fmt_hms, _split_nonempty_lines, log, run_command,
    run_command_hard_timeout,
)
from .devices import (
    _lsblk_fstype, _lsblk_partitions, _lsblk_pttype, _lsblk_type,
    _sysfs_block_name, _sysfs_child_partition_devs, _sysfs_is_whole_disk,
    _sysfs_to_parent_disk_name, disk_base_name, disk_discard_supported,
    disk_is_nvme, disk_is_rotational,
)
from .mounts import cleanup_mountpoint_dir, find_mount_targets
from .smart import (
    _decode_seagate_command_timeout, _decode_seagate_hi16_lo32,
    _parse_smart_attr_raw, _parse_smart_attr_row,
    _parse_smart_error_log_count, _parse_smart_last_error_poh,
    _parse_smart_long_selftest_failures, _smartctl_looks_seagate,
)
from .mappings import get_map_file_path, get_script_dir, read_luks_map, save_luks_map
from .rawio import _parse_ddrescue_failed_ranges, secure_erase_disk

__all__ = [name for name in globals() if not name.startswith("__")]
