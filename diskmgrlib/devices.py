"""Low-level block-device discovery and media capability helpers."""

import os
import re
from pathlib import Path

from .runtime import run_command, run_command_hard_timeout


class DeviceProbeError(RuntimeError):
    """Raised when a device probe cannot produce a trustworthy result."""


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
