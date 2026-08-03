#!/usr/bin/env python3
import cmd
import subprocess
import os
import sys
import shlex
import csv
import json
import time
import argparse
import random
import re
import shutil
import datetime
import threading
import atexit
import tempfile
import pwd
import grp
from pathlib import Path
try:
    import readline
except ImportError:
    readline = None

# Configuration
MAP_FILENAME = 'diskmap.tsv'
PASSGEN_BIN = 'passgen'
VERSION = '3.6.6'
HISTORY_FILE_ENV = 'DISKMGR_HISTORY'
DEFAULT_HISTORY_FILE = Path.home() / '.local' / 'state' / 'diskmgr' / 'history'
MAX_HISTORY_ENTRIES = 5000
LUKS_PBKDF_DEFAULT_THREADS = 4
LUKS_PBKDF_DEFAULT_TIME = 8
LUKS_PBKDF_DEFAULT_MEMORY_KIB = 4 * 1024 * 1024
LUKS_PBKDF_DEFAULT_MEMORY_LABEL = '4GiB'
LUKS_HEADER_BACKUP_DIR = Path.home() / '.local' / 'share' / 'diskmgr'

# ANSI Colors
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[38;5;117m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

_CMD_LOG_FH = None
_CMD_LOG_PATH = None

def _cmd_log_write(text):
    global _CMD_LOG_FH
    if _CMD_LOG_FH is None:
        return
    try:
        _CMD_LOG_FH.write(text)
        if not text.endswith("\n"):
            _CMD_LOG_FH.write("\n")
        _CMD_LOG_FH.flush()
    except Exception:
        # Best-effort logging: never break the tool because logs can't be written.
        pass

def _cmd_log_open(prefix):
    """Enable per-command logging to /tmp; returns the path."""
    global _CMD_LOG_FH, _CMD_LOG_PATH
    try:
        ts = int(time.time())
        path = f"/tmp/diskmgr_{prefix}_{os.getpid()}_{ts}.log"
        _CMD_LOG_FH = open(path, "w", encoding="utf-8", errors="replace")
        _CMD_LOG_PATH = path
        _cmd_log_write(f"# diskmgr {VERSION}")
        _cmd_log_write(f"# started: {datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}")
        return path
    except Exception:
        _CMD_LOG_FH = None
        _CMD_LOG_PATH = None
        return None

def _cmd_log_close():
    global _CMD_LOG_FH, _CMD_LOG_PATH
    try:
        if _CMD_LOG_FH is not None:
            _cmd_log_write(f"# ended: {datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}")
            _CMD_LOG_FH.close()
    except Exception:
        pass
    _CMD_LOG_FH = None
    _CMD_LOG_PATH = None

def log(msg, level='INFO'):
    _cmd_log_write(f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] {level}: {msg}")
    if level == 'ERROR':
        print(f"{Colors.FAIL}ERROR: {msg}{Colors.ENDC}", file=sys.stderr)
    elif level == 'WARN':
        print(f"{Colors.WARNING}WARNING: {msg}{Colors.ENDC}", file=sys.stderr)
    else:
        print(f"{Colors.OKBLUE}diskmgr: {msg}{Colors.ENDC}")

def _fmt_hms(total_seconds):
    s = int(max(total_seconds, 0))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"

def run_command(command, check=True, input_str=None, capture_output=True, sudo=False, timeout=None):
    if sudo:
        command = ['sudo'] + command

    try:
        start_ts = time.time()
        _cmd_log_write(f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] CMD: {' '.join(command)}")
        result = subprocess.run(
            command,
            input=input_str,
            text=True,
            check=check,
            timeout=timeout,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None
        )
        _cmd_log_write(f"RC: {getattr(result, 'returncode', 0)}  elapsed={_fmt_hms(time.time() - start_ts)}")
        if capture_output:
            out = getattr(result, 'stdout', '') or ''
            err = getattr(result, 'stderr', '') or ''
            if out.strip():
                _cmd_log_write("--- STDOUT ---")
                _cmd_log_write(out.rstrip())
            if err.strip():
                _cmd_log_write("--- STDERR ---")
                _cmd_log_write(err.rstrip())
        return result
    except subprocess.CalledProcessError as e:
        if check:
            log(f"Command failed: {' '.join(command)}", 'ERROR')
            if e.stderr:
                log(e.stderr.strip(), 'ERROR')
            raise
        return e
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode('utf-8', errors='replace') if e.stdout else '')
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode('utf-8', errors='replace') if e.stderr else '')
        msg = f"Command timed out after {timeout}s: {' '.join(command)}"
        if check:
            log(msg, 'ERROR')
            raise
        _cmd_log_write(f"TIMEOUT: {msg}")
        if out.strip():
            _cmd_log_write("--- STDOUT (partial) ---")
            _cmd_log_write(out.rstrip())
        if err.strip():
            _cmd_log_write("--- STDERR (partial) ---")
            _cmd_log_write(err.rstrip())
        return subprocess.CompletedProcess(command, 124, out, err)

def run_command_hard_timeout(command, seconds, check=True, input_str=None, capture_output=True, sudo=False):
    """
    Run a command with a hard timeout using coreutils `timeout` when available.

    This is used for device-probe commands that may enter uninterruptible I/O wait
    on stale block devices; wrapping with `timeout` avoids blocking the shell.
    """
    try:
        sec = float(seconds)
    except Exception:
        sec = 0.0
    if sec <= 0:
        return run_command(
            command,
            check=check,
            input_str=input_str,
            capture_output=capture_output,
            sudo=sudo,
        )

    timeout_bin = shutil.which('timeout')
    if timeout_bin:
        sec_s = f"{sec:g}s"
        wrapped = ['timeout', '-k', '1s', sec_s] + list(command)
        return run_command(
            wrapped,
            check=check,
            input_str=input_str,
            capture_output=capture_output,
            sudo=sudo,
        )

    # Fallback to Python-level timeout when coreutils timeout is unavailable.
    return run_command(
        command,
        check=check,
        input_str=input_str,
        capture_output=capture_output,
        sudo=sudo,
        timeout=sec,
    )

def _split_nonempty_lines(s):
    if not s:
        return []
    out = []
    for line in str(s).splitlines():
        line = line.strip()
        if line and line not in out:
            out.append(line)
    return out

def find_mount_targets(source):
    """
    Return a list of mount TARGETs for a given SOURCE.

    Notes:
    - A single filesystem can be mounted at multiple targets; findmnt will then
      return multiple lines. Callers must not treat stdout as a single path.
    - We resolve the source to a real path so /dev/mapper/<name> and /dev/dm-X
      match the same mount.
    """
    src_real = os.path.realpath(source)
    res = run_command(['findmnt', '-rn', '-S', src_real, '-o', 'TARGET'], check=False)
    if getattr(res, 'returncode', 1) != 0:
        return []
    return _split_nonempty_lines(getattr(res, 'stdout', ''))

def cleanup_mountpoint_dir(mountpoint):
    """
    Best-effort cleanup of a mountpoint directory after unmount.

    Only attempts removal for mountpoints under /media/$USER/ and only if the
    directory is no longer a mount target. Uses rmdir (so it only removes empty
    directories) to avoid deleting real data.
    """
    if not mountpoint:
        return

    user = os.environ.get('USER', 'root')
    media_root = os.path.realpath(f"/media/{user}")
    mp_real = os.path.realpath(mountpoint)
    if not (mp_real == media_root or mp_real.startswith(media_root + os.sep)):
        return

    # Still mounted? Don't touch it.
    if run_command(['findmnt', '-rn', '-M', mountpoint], check=False).returncode == 0:
        return

    res = run_command(['rmdir', mountpoint], sudo=True, check=False)
    if getattr(res, 'returncode', 1) == 0:
        log(f"Removed mountpoint directory {mountpoint}")

def _sysfs_block_name(dev_path):
    """Return kernel block name (e.g. sda2, nvme0n1p1, dm-0) for a /dev path."""
    return os.path.basename(os.path.realpath(dev_path))

def _sysfs_to_parent_disk_name(block_name, max_hops=16):
    """
    Best-effort: resolve a block device name to its underlying whole-disk name.

    - Partitions: sda2 -> sda, nvme0n1p1 -> nvme0n1
    - dm devices: dm-0 -> first slave (often sda2), then keep resolving
    """
    cur = block_name
    for _ in range(max_hops):
        sys_path = f"/sys/class/block/{cur}"
        if not os.path.exists(sys_path):
            break

        # If it's a device-mapper node, walk down to its first slave.
        if cur.startswith("dm-"):
            slaves_dir = os.path.join(sys_path, "slaves")
            try:
                slaves = sorted(os.listdir(slaves_dir)) if os.path.isdir(slaves_dir) else []
            except Exception:
                slaves = []
            if slaves:
                cur = slaves[0]
                continue
            break

        # If it's a partition, its parent is the directory above in sysfs.
        if os.path.exists(os.path.join(sys_path, "partition")):
            parent = os.path.basename(os.path.realpath(os.path.join(sys_path, "..")))
            if parent and parent != cur:
                cur = parent
                continue
            break

        # Already a whole-disk node (or at least not a partition we can detect).
        break

    return cur

def _sysfs_is_whole_disk(dev_path):
    """Best-effort whole-disk check via sysfs without probing the device."""
    k = _sysfs_block_name(dev_path)
    if not k:
        return False
    sys_path = f"/sys/class/block/{k}"
    if not os.path.exists(sys_path):
        return False
    return not os.path.exists(os.path.join(sys_path, "partition"))

def _sysfs_child_partition_devs(dev_path):
    """Return /dev paths for child partitions of a whole disk via sysfs."""
    out = []
    seen = set()
    k = _sysfs_block_name(dev_path)
    if not k:
        return out
    base = f"/sys/class/block/{k}"
    if not os.path.isdir(base):
        return out
    try:
        entries = sorted(os.listdir(base))
    except Exception:
        return out
    for ent in entries:
        part_flag = os.path.join(base, ent, "partition")
        if not os.path.exists(part_flag):
            continue
        devp = f"/dev/{ent}"
        if not os.path.exists(devp):
            continue
        realp = os.path.realpath(devp)
        if realp in seen:
            continue
        seen.add(realp)
        out.append(realp)
    return out

#
# NOTE: refresh-related helpers were removed along with the refresh command.
#

def _parse_smart_attr_raw(out, attr_id):
    """
    Parse a smartctl -a attribute table RAW_VALUE for a given attribute ID.
    Returns a string (raw value) or None.
    """
    if not out:
        return None
    for line in str(out).splitlines():
        s = line.strip()
        if not s:
            continue
        # Attribute table rows typically start with the numeric ID.
        if not s.startswith(str(attr_id) + " "):
            continue
        parts = s.split()
        if len(parts) < 2 or parts[0] != str(attr_id):
            continue
        # Old format: ID NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW...
        if len(parts) >= 10 and re.fullmatch(r"0x[0-9a-fA-F]+", parts[2]):
            return " ".join(parts[9:]).strip()
        # Brief format: ID NAME FLAGS VALUE WORST THRESH FAIL RAW...
        if len(parts) >= 8:
            return " ".join(parts[7:]).strip()
        # Fallback
        return parts[-1]
    return None

def _parse_smart_attr_row(out, attr_id):
    """
    Parse a smartctl -a attribute table row for a given attribute ID.

    Supports both smartctl ATA table formats:
      old:   ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
      brief: ID# ATTRIBUTE_NAME FLAGS VALUE WORST THRESH FAIL RAW_VALUE
    RAW_VALUE may contain spaces (e.g. temperatures with Min/Max), so we capture the tail.
    """
    if not out:
        return None
    for line in str(out).splitlines():
        s = line.strip()
        if not s or not s.startswith(str(attr_id) + " "):
            continue
        parts = s.split()
        if len(parts) < 8 or parts[0] != str(attr_id):
            continue

        name = parts[1]
        try:
            # Old format has a hex flag in column 3.
            if re.fullmatch(r"0x[0-9a-fA-F]+", parts[2]) and len(parts) >= 10:
                value = int(parts[3], 10)
                worst = int(parts[4], 10)
                thresh = int(parts[5], 10)
                raw = " ".join(parts[9:]).strip()
            else:
                # Brief format.
                value = int(parts[3], 10)
                worst = int(parts[4], 10)
                thresh = int(parts[5], 10)
                raw = " ".join(parts[7:]).strip()
        except ValueError:
            continue

        return {
            "id": int(attr_id),
            "name": name,
            "value": value,
            "worst": worst,
            "thresh": thresh,
            "raw": raw,
        }
    return None

def _first_int_from_text(s):
    if s is None:
        return None
    m = re.search(r"([0-9][0-9,]*)", str(s))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""), 10)
    except ValueError:
        return None

def _parse_smart_error_log_count(out):
    if not out:
        return None
    lines = str(out).splitlines()
    for i, line in enumerate(lines):
        if "SMART Error Log" in line:
            # Fast-path: "No Errors Logged"
            for j in range(i, min(i + 40, len(lines))):
                if lines[j].strip() == "No Errors Logged":
                    return 0
            # Otherwise count "Error N occurred at" lines in the next chunk.
            cnt = 0
            for j in range(i, min(i + 300, len(lines))):
                if re.search(r"^\s*Error\s+[0-9]+\s+occurred\s+at\b", lines[j]):
                    cnt += 1
            return cnt
    return None

def _parse_smart_last_error_poh(out):
    """
    Parse the most recent ATA SMART Error Log entry's power-on lifetime (hours).

    smartctl typically formats the most recent entry as:
      "Error 667 occurred at disk power-on lifetime: 20140 hours (839 days + 4 hours)"
    Note: The error number is not necessarily "1" (it can be a running counter).

    Returns (error_number, power_on_hours) or (None, None) if not found / not an ATA SMART error log.
    """
    if not out:
        return (None, None)
    m = re.search(
        r"(?m)^\s*Error\s+([0-9]+)\s+occurred\s+at\s+disk\s+power-on\s+lifetime:\s*([0-9,]+)\s*(?:hours|h)\b",
        str(out),
    )
    if not m:
        return (None, None)
    try:
        n = int(m.group(1), 10)
        h = int(m.group(2).replace(",", ""), 10)
        return (n, h)
    except ValueError:
        return (None, None)

def _smartctl_looks_seagate(out):
    if not out:
        return False
    # Common smartctl identifiers for Seagate HDDs.
    if re.search(r"(?im)^Model Family:.*Seagate", out):
        return True
    if re.search(r"(?im)^(Device Model|Product):\s*Seagate", out):
        return True
    # Most Seagate HDDs report model starting with "ST".
    if re.search(r"(?im)^Device Model:\s*ST[0-9A-Z]", out):
        return True
    return False

def _decode_seagate_command_timeout(raw_val):
    """
    Seagate often packs SMART 188 into 6 bytes (3x 16-bit counters).

    smartctl prints the entire 48-bit value as a decimal integer, which can look huge.
    Decode it as:
      hi word:  >7.5s bucket (included in >5s)
      mid word: >5s bucket
      lo word:  total command timeouts
    """
    if raw_val is None:
        return None
    s = str(raw_val).strip().replace(",", "")
    if not s or not re.fullmatch(r"[0-9]+", s):
        return None
    try:
        v = int(s, 10)
    except ValueError:
        return None
    if v < 0 or v >= (1 << 48):
        return None
    hx = f"{v:012x}"  # 6 bytes
    hi = int(hx[0:4], 16)
    mid = int(hx[4:8], 16)
    lo = int(hx[8:12], 16)
    return {
        "raw_int": v,
        "hex": "0x" + hx,
        "timeouts": lo,
        "gt_5s": mid,
        "gt_7_5s": hi,
    }

def _decode_seagate_hi16_lo32(raw_val):
    """
    Common Seagate packing for some SMART RAW fields:
      RAW = (hi16_error_count << 32) | lo32_operation_count

    This is often seen for attribute 1 (Raw_Read_Error_Rate) and 7 (Seek_Error_Rate),
    where RAW is not "number of errors" in the intuitive sense.

    Returns dict with raw_int, hex, errors, ops; or None if not parseable.
    """
    if raw_val is None:
        return None
    s = str(raw_val).strip().replace(",", "")
    if not s or not re.fullmatch(r"[0-9]+", s):
        return None
    try:
        v = int(s, 10)
    except ValueError:
        return None
    if v < 0 or v >= (1 << 48):
        return None
    return {
        "raw_int": v,
        "hex": f"0x{v:012x}",
        "errors": (v >> 32) & 0xFFFF,
        "ops": v & 0xFFFFFFFF,
    }

def _parse_smart_long_selftest_failures(out):
    """
    Count non-success statuses in the SMART Self-test log for extended/long tests.
    Returns an int or None if the section isn't present.
    """
    if not out:
        return None
    lines = str(out).splitlines()
    start = None
    for i, line in enumerate(lines):
        if "SMART Self-test log" in line:
            start = i
            break
    if start is None:
        return None

    # Find the table header line with "#"
    header = None
    for i in range(start, min(start + 60, len(lines))):
        if lines[i].lstrip().startswith("#"):
            header = i
            break
        if "No self-tests have been logged" in lines[i]:
            return 0
    if header is None:
        # Section exists but we couldn't find the table.
        return None

    fail = 0
    for i in range(header + 1, min(header + 200, len(lines))):
        line = lines[i].strip()
        if not line:
            break
        if not line.startswith("#"):
            continue
        # Common format: "# 1  Extended offline  Completed without error  00%  1234  -"
        cols = line.split()
        if len(cols) < 4:
            continue
        desc = " ".join(cols[2:4])  # "Extended offline" or "Short offline"
        if "Extended offline" not in desc and "Long offline" not in desc:
            continue
        # Status begins after description; search the raw line for "Completed without error"
        if "Completed without error" in line:
            continue
        fail += 1
    return fail

def _find_tool_or_common_paths(tool_name, common_paths):
    """
    Find an executable by PATH, or fall back to common sbin locations.

    This avoids failures when running as a normal user with /usr/sbin not in PATH.
    Returns an absolute path or None.
    """
    p = shutil.which(tool_name)
    if p:
        return p
    for cp in common_paths:
        try:
            if cp and os.path.exists(cp) and os.access(cp, os.X_OK):
                return cp
        except Exception:
            continue
    return None

def _parse_ddrescue_failed_ranges(map_path, sector_size=512):
    """
    Return a list of failed ranges from a ddrescue mapfile.

    ddrescue map lines are typically: <start_hex> <size_hex> <status_char>
    We treat status '-' as "unrecovered".
    Ranges are returned as dicts with byte and sector offsets.
    """
    if not map_path or not os.path.exists(map_path):
        return []
    try:
        ss = int(sector_size)
        if ss <= 0:
            ss = 512
    except Exception:
        ss = 512

    out = []
    try:
        with open(map_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                a, b, st = parts[0], parts[1], parts[2]
                if not (a.startswith('0x') and b.startswith('0x') and st):
                    continue
                if st[0] != '-':
                    continue
                try:
                    start_b = int(a, 16)
                    size_b = int(b, 16)
                except ValueError:
                    continue
                if size_b <= 0:
                    continue
                end_b = start_b + size_b
                # Prefer reporting in sectors, but keep bytes if not aligned.
                start_lba = start_b // ss
                end_lba = (end_b + ss - 1) // ss  # ceil
                out.append({
                    "start_b": start_b,
                    "end_b": end_b,
                    "size_b": size_b,
                    "start_lba": start_lba,
                    "end_lba": end_lba,
                    "count_lba": max(0, end_lba - start_lba),
                })
    except Exception:
        return []
    return out

def _lsblk_type(dev_path):
    res = run_command_hard_timeout(['lsblk', '-no', 'TYPE', dev_path], 3, check=False)
    out = (getattr(res, 'stdout', '') or '')
    # lsblk may return multiple rows (device + children); use only the target's first row.
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""

def _lsblk_fstype(dev_path):
    # Query only the requested node. Without --nodeps, lsblk prints blank parent
    # rows followed by child filesystems, which can misidentify a disk as NTFS.
    res = run_command_hard_timeout(['lsblk', '--nodeps', '-no', 'FSTYPE', dev_path], 3, check=False)
    out = (getattr(res, 'stdout', '') or '')
    # The target may legitimately have no filesystem, so preserve that blank result.
    for line in out.splitlines():
        return line.strip()
    return ""

def _lsblk_pttype(dev_path):
    res = run_command_hard_timeout(['lsblk', '-no', 'PTTYPE', dev_path], 3, check=False)
    out = (getattr(res, 'stdout', '') or '')
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""

def _lsblk_partitions(dev_path):
    """
    Return a list of partitions under a disk device (NAME, FSTYPE).
    """
    res = run_command_hard_timeout(['lsblk', '-nr', '-o', 'NAME,TYPE,FSTYPE', dev_path], 3, check=False)
    rows = []
    for raw in (getattr(res, 'stdout', '') or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        n, t = parts[0], parts[1]
        fs = parts[2].strip() if len(parts) >= 3 else ""
        if t == 'part':
            rows.append({"name": n, "fstype": fs})
    return rows

def get_script_dir():
    # diskmap.tsv remains next to the project entry point after modularization.
    return Path(__file__).resolve().parent.parent

def get_map_file_path():
    return get_script_dir() / MAP_FILENAME

def read_luks_map():
    map_file = get_map_file_path()
    if not map_file.exists():
        return {}

    mappings = {}
    with open(map_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 1) # Split on first whitespace
            if len(parts) == 2:
                name, path = parts
                mappings[name] = path
    return mappings

def save_luks_map(mappings):
    map_file = get_map_file_path()
    with open(map_file, 'w') as f:
        for name, path in mappings.items():
            f.write(f"{name}\t{path}\n")

def disk_base_name(dev_path):
    # Given /dev/sdb or /dev/sdb1 -> sdb
    try:
        dev_name = os.path.basename(dev_path)
        # simplistic, better to use lsblk
        res = run_command(['lsblk', '-no', 'PKNAME', dev_path], check=False)
        if res.stdout.strip():
            return res.stdout.strip()
        return dev_name
    except:
        return os.path.basename(dev_path)

def disk_is_nvme(dev_path):
    # Check if NVMe
    try:
        res = run_command(['lsblk', '-dno', 'TRAN', dev_path], check=False)
        if res.stdout.strip() == 'nvme':
            return True
        if 'nvme' in dev_path:
            return True
    except:
        pass
    return False

def disk_is_rotational(dev_path):
    """Best-effort rotational check that works for disks, partitions, and dm-crypt mappers."""
    try:
        kname = _sysfs_block_name(dev_path)
        parent = _sysfs_to_parent_disk_name(kname)
        p = Path(f"/sys/class/block/{parent}/queue/rotational")
        if p.exists():
            return p.read_text().strip() == "1"
    except:
        pass

    try:
        res = run_command(['lsblk', '-dno', 'ROTA', dev_path], check=False)
        v = (getattr(res, 'stdout', '') or '').strip()
        if v in ('0', '1'):
            return v == '1'
    except:
        pass

    return False

def disk_discard_supported(dev_path):
    try:
        res = run_command(['lsblk', '-dno', 'DISC-MAX', dev_path], check=False)
        val = res.stdout.strip()
        return val and val != "0B" and val != "0"
    except:
        return False

def secure_erase_disk(dev_path):
    if not os.path.exists(dev_path):
        log(f"Device not found: {dev_path}", 'ERROR')
        return False

    # Detect if partition or disk
    is_part = False
    try:
        res = run_command(['lsblk', '-dno', 'TYPE', dev_path], capture_output=True)
        if res.stdout.strip() == 'part':
            is_part = True
    except:
        pass

    target_type = "PARTITION" if is_part else "FULL DISK"
    log(f"Starting secure erase on {dev_path} ({target_type})")

    if disk_is_nvme(dev_path):
        if is_part:
            log("NVMe hardware-level erase (Sanitize/Format) skipped: Target is a partition, not a full disk.", 'WARN')
        else:
            # Query capabilities
            try:
                res = run_command(['nvme', 'id-ctrl', '-o', 'json', dev_path], sudo=True, capture_output=True)
                ctrl_data = json.loads(res.stdout)
                oacs = ctrl_data.get('oacs', 0)
                sanicap = ctrl_data.get('sanicap', 0)
                fna = ctrl_data.get('fna', 0)

                can_format = bool(oacs & 0x02)
                can_format_block = can_format # Baseline if Format is supported
                can_format_crypto = can_format and bool(fna & 0x04)
                can_sanitize_block = bool(sanicap & 0x02)
                can_sanitize_crypto = bool(sanicap & 0x01)

                # 1. Sanitize Crypto Erase (Priority 1)
                if can_sanitize_crypto:
                    log(f"Attempting NVMe Sanitize Crypto Erase (Action 4) on {dev_path}...")
                    try:
                        run_command(['nvme', 'sanitize', dev_path, '-a', '4'], sudo=True)
                        run_command(['udevadm', 'settle'], sudo=True)
                        log("NVMe Sanitize Crypto Erase completed successfully.")
                        return True
                    except Exception as e:
                        log(f"NVMe Sanitize Crypto Erase failed: {e}. Falling back...", 'WARN')

                # 2. Sanitize Block Erase (Priority 2)
                if can_sanitize_block:
                    log(f"Attempting NVMe Sanitize Block Erase (Action 2) on {dev_path}...")
                    try:
                        run_command(['nvme', 'sanitize', dev_path, '-a', '2'], sudo=True)
                        run_command(['udevadm', 'settle'], sudo=True)
                        log("NVMe Sanitize Block Erase completed successfully.")
                        return True
                    except Exception as e:
                        log(f"NVMe Sanitize Block Erase failed: {e}. Falling back...", 'WARN')

                # 3. Format Crypto Erase (Priority 3)
                if can_format_crypto:
                    log(f"Attempting NVMe Format Crypto Erase (SES 2) on {dev_path}...")
                    try:
                        run_command(['nvme', 'format', dev_path, '--ses=2'], sudo=True)
                        run_command(['udevadm', 'settle'], sudo=True)
                        log("NVMe Format Crypto Erase completed successfully.")
                        return True
                    except Exception as e:
                        log(f"NVMe Format Crypto Erase failed: {e}. Falling back...", 'WARN')

                # 4. Format Block Erase (Last NVMe Fallback)
                if can_format_block:
                    log(f"Attempting NVMe Format Block Erase (SES 1) on {dev_path}...")
                    try:
                        run_command(['nvme', 'format', dev_path, '--ses=1'], sudo=True)
                        run_command(['udevadm', 'settle'], sudo=True)
                        log("NVMe Format Block Erase completed successfully.")
                        return True
                    except Exception as e:
                        log(f"NVMe Format Block Erase failed: {e}. Falling back...", 'WARN')

                log("No supported NVMe hardware erase methods found. Falling back to software discard/overwrite.")

            except Exception as e:
                log(f"Failed to query NVMe capabilities: {e}. Falling back to software methods.", 'WARN')

    elif not disk_is_rotational(dev_path):
        # SSD/Flash (SATA/SAS)
        if is_part:
            log("SATA SSD hardware-level erase (ATA Sanitize/Secure Erase) skipped: Target is a partition.", 'WARN')
        else:
            # 1. PSID Revert / TCG Opal (Placeholder)
            log(f"Checking for PSID Revert / TCG Opal support (Currently Unimplemented)...")

            # 2. ATA Sanitize
            try:
                res = run_command(['hdparm', '-I', dev_path], sudo=True, capture_output=True)
                if "sanitize" in res.stdout.lower():
                     log(f"Attempting ATA Sanitize Block Erase on {dev_path}...")
                     try:
                         run_command(['hdparm', '--sanitize-block-erase', dev_path], sudo=True)
                         log("ATA Sanitize Block Erase completed successfully.")
                         return True
                     except Exception as e:
                         log(f"ATA Sanitize failed: {e}. Falling back...", 'WARN')
            except:
                pass

            # 3. ATA Secure Erase (Enhanced & Standard)
            try:
                res = run_command(['hdparm', '-I', dev_path], sudo=True, capture_output=True)
                if "supported" in res.stdout.lower() and "security:" in res.stdout.lower():
                    if "frozen" in res.stdout.lower():
                        log("ATA Secure Erase is FROZEN by BIOS/EFI. Skipping...", 'WARN')
                    else:
                        log("ATA Secure Erase supported. Setting temporary password 'diskmgr'...")
                        try:
                            pw = "diskmgr"
                            run_command(['hdparm', '--user-master', 'u', '--security-set-pass', pw, dev_path], sudo=True)

                            # Enhanced
                            if "enhanced" in res.stdout.lower():
                                log(f"Attempting ATA Secure Erase (Enhanced) on {dev_path}...")
                                try:
                                    run_command(['hdparm', '--user-master', 'u', '--security-erase-enhanced', pw, dev_path], sudo=True)
                                    log("ATA Secure Erase (Enhanced) completed successfully.")
                                    return True
                                except Exception as e:
                                    log(f"ATA Secure Erase (Enhanced) failed: {e}. Falling back...", 'WARN')

                            # Standard
                            log(f"Attempting ATA Secure Erase (Standard) on {dev_path}...")
                            try:
                                run_command(['hdparm', '--user-master', 'u', '--security-erase', pw, dev_path], sudo=True)
                                log("ATA Secure Erase (Standard) completed successfully.")
                                return True
                            except Exception as e:
                                log(f"ATA Secure Erase (Standard) failed: {e}. Falling back...", 'WARN')
                        except Exception as e:
                            log(f"Failed to set security password: {e}. Falling back...", 'WARN')
            except:
                pass

    if disk_is_rotational(dev_path):
        if not is_part:
            # Try ATA methods for HDD too
            try:
                res = run_command(['hdparm', '-I', dev_path], sudo=True, capture_output=True)
                if "sanitize" in res.stdout.lower():
                     log(f"Attempting ATA Sanitize on HDD {dev_path}...")
                     try:
                         run_command(['hdparm', '--sanitize-block-erase', dev_path], sudo=True)
                         log("HDD ATA Sanitize completed successfully.")
                         return True
                     except Exception as e:
                         log(f"HDD ATA Sanitize failed: {e}. Falling back...", 'WARN')

                if "supported" in res.stdout.lower() and "security:" in res.stdout.lower() and "frozen" not in res.stdout.lower():
                    log(f"Attempting ATA Secure Erase on HDD {dev_path}...")
                    try:
                        pw = "diskmgr"
                        run_command(['hdparm', '--user-master', 'u', '--security-set-pass', pw, dev_path], sudo=True)
                        erase_cmd = '--security-erase-enhanced' if "enhanced" in res.stdout.lower() else '--security-erase'
                        run_command(['hdparm', '--user-master', 'u', erase_cmd, pw, dev_path], sudo=True)
                        log("HDD ATA Secure Erase completed successfully.")
                        return True
                    except Exception as e:
                        log(f"HDD ATA Secure Erase failed: {e}. Falling back...", 'WARN')
            except:
                pass

        # Final HDD Fallback
        log(f"Performing software zero-overwrite (dd) on {dev_path}...")
        try:
            run_command(['dd', 'if=/dev/zero', f'of={dev_path}', 'bs=16M', 'status=progress', 'oflag=direct'], sudo=True, capture_output=False)
            run_command(['sync'], sudo=True)
            log("Zero overwrite completed. Verifying first 1MB...")
            res_v = run_command(['dd', f'if={dev_path}', 'bs=1M', 'count=1'], sudo=True, capture_output=True)
            if any(b != 0 for b in res_v.stdout.encode('latin1') if isinstance(b, int)):
                 log("Verification failed: First 1MB is not zeroed.", 'ERROR')
                 return False
            log("Verification successful.")
            return True
        except Exception as e:
            log(f"Software overwrite failed: {e}", 'ERROR')
            return False

    # SSD Software Fallbacks
    log(f"Attempting blkdiscard --secure on {dev_path}...")
    try:
        run_command(['blkdiscard', '--secure', dev_path], sudo=True)
        run_command(['udevadm', 'settle'], sudo=True)
        log("blkdiscard --secure completed successfully.")
        return True
    except Exception as e:
        log(f"blkdiscard --secure not supported or failed: {e}. Falling back...", 'WARN')

    log(f"Attempting standard blkdiscard on {dev_path}...")
    if disk_discard_supported(dev_path):
        try:
            run_command(['blkdiscard', dev_path], sudo=True)
            run_command(['udevadm', 'settle'], sudo=True)
            log("Standard blkdiscard completed successfully.")
            return True
        except Exception as e:
            log(f"Standard blkdiscard failed: {e}", 'ERROR')
            return False
    else:
        log("Discard not supported on this device. Final software overwrite attempted.", 'WARN')
        try:
            run_command(['dd', 'if=/dev/zero', f'of={dev_path}', 'bs=16M', 'status=progress', 'oflag=direct'], sudo=True, capture_output=False)
            return True
        except:
            return False


# Export private helpers too: command mixins intentionally share this runtime API.
__all__ = [name for name in globals() if not name.startswith('__')]
