"""Confirmation, target resolution, and destructive safety helpers."""

from pathlib import Path
import json
import os
import re
import select
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .runtime import (
    Colors,
    _find_tool_or_common_paths,
    log,
    popen_command,
    run_command,
    run_command_hard_timeout,
)
from .devices import DeviceProbeError, _lsblk_type, _sysfs_is_whole_disk
from .mappings import read_luks_map


_STORAGE_NAME_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


def validate_storage_name(value, field='name', max_bytes=255):
    """Validate a user-visible name before it reaches paths, mappers, or fstab."""
    name = str(value or '')
    if not name or name != name.strip():
        raise ValueError(f"{field} cannot be empty or have surrounding whitespace")
    if name in ('.', '..') or not _STORAGE_NAME_RE.fullmatch(name):
        raise ValueError(f"{field} cannot contain '/', '\\', path traversal, or control characters")
    if len(name.encode('utf-8')) > max_bytes:
        raise ValueError(f"{field} exceeds the {max_bytes}-byte limit")
    return name


def validate_filesystem_label(value, fstype):
    """Validate labels against both path safety and filesystem-specific limits."""
    fs = str(fstype or '').strip().lower()
    limits = {'ext4': 16, 'xfs': 12, 'btrfs': 255, 'fat32': 11, 'exfat': 15}
    label = validate_storage_name(value, 'filesystem label', limits.get(fs, 255))
    if fs == 'fat32':
        forbidden = set('"*+,./:;<=>?[\\]|')
        if any(char in forbidden for char in label):
            raise ValueError("FAT32 labels contain a character forbidden by FAT")
        try:
            label.encode('ascii')
        except UnicodeEncodeError as exc:
            raise ValueError("FAT32 labels must use ASCII characters") from exc
    return label


def safe_mount_path(root, component):
    """Build one mountpoint component beneath root without symlink traversal."""
    component = validate_storage_name(component, 'mount name')
    root_path = Path(root).expanduser()
    if not root_path.is_absolute():
        raise ValueError("mount root must be absolute")
    root_real = Path(os.path.realpath(root_path))
    candidate = root_path / component
    if candidate.is_symlink():
        raise ValueError(f"refusing symlink mountpoint: {candidate}")
    parent_real = Path(os.path.realpath(candidate.parent))
    try:
        parent_real.relative_to(root_real)
    except ValueError as exc:
        raise ValueError(f"mountpoint escapes {root_path}") from exc
    return str(candidate)


def validate_absolute_path(value, field='path', allow_missing=True):
    """Validate an explicit path without following an attacker-controlled final symlink."""
    raw = str(value or '')
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError(f"{field} is empty or contains control characters")
    path = Path(raw).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f"{field} must be an absolute path without '..'")
    if path.is_symlink():
        raise ValueError(f"{field} cannot be a symlink")
    if not allow_missing and not path.exists():
        raise ValueError(f"{field} does not exist: {path}")
    return str(path)


@dataclass
class _HeldDeviceLock:
    process: subprocess.Popen
    target: str


class SafetyMixin:

    def _show_target_entries_for_confirmation(self, target_name):
        """
        Print current list-list entry/entries for the target before confirmation.
        Also includes an open crypt child when target is a LUKS container parent.
        """
        try:
            rows = self._build_list_rows_snapshot(include_ext4_details=True)
            if not rows:
                return

            target_paths = set()
            text = str(target_name or "").strip()

            # Any explicit /dev paths embedded in the confirmation label.
            for p in re.findall(r"/dev/[A-Za-z0-9._/-]+", text):
                p = p.rstrip("),]")
                if os.path.exists(p):
                    target_paths.add(os.path.realpath(p))

            # Direct path input.
            if text and os.path.exists(text):
                target_paths.add(os.path.realpath(text))

            token = text.split()[0].strip("[](),") if text else ""
            if token:
                try:
                    resolved = self.resolve_target(token, allow_id=True)
                except Exception:
                    resolved = None
                if resolved and os.path.exists(resolved):
                    target_paths.add(os.path.realpath(resolved))

                mapper_path = f"/dev/mapper/{token}"
                if os.path.exists(mapper_path):
                    target_paths.add(os.path.realpath(mapper_path))

            if not target_paths:
                return

            matched_knames = set()
            selected = []
            for r in rows:
                kname = str(r.get('KNAME') or "").strip()
                if not kname:
                    continue
                devreal = os.path.realpath(f"/dev/{kname}")
                if devreal in target_paths:
                    selected.append(r)
                    matched_knames.add(kname)

            # If a matched row is a LUKS container parent, include open crypt child row too.
            for r in rows:
                if (r.get('TYPE') or '') != 'crypt':
                    continue
                pk = str(r.get('PKNAME') or "").strip()
                if pk and pk in matched_knames and str(r.get('KNAME') or '') not in matched_knames:
                    selected.append(r)
                    matched_knames.add(str(r.get('KNAME') or ''))

            if not selected:
                return

            print(f"\n{Colors.BOLD}Target entry snapshot:{Colors.ENDC}")
            self._print_lsblk_rows_list(selected, cols=self._lsblk_verbose_cols())
        except Exception:
            # Confirmation should continue even if snapshot rendering fails.
            pass

    def _resolve_confirmation_identity(self, target_name):
        """
        Resolve confirmation identity as (device_real, persistent_path, pci_path).
        Returns (None, None, None) when identity cannot be determined reliably.
        """
        text = str(target_name or "").strip()
        if not text:
            return (None, None, None)

        candidates = []

        def _add_candidate(p):
            v = str(p or "").strip().rstrip("),]")
            if not v:
                return
            if v not in candidates:
                candidates.append(v)

        # Full text path first (important for absolute paths containing spaces).
        if os.path.exists(text):
            _add_candidate(text)

        # Explicit /dev paths first.
        for p in re.findall(r"/dev/[A-Za-z0-9._/\-+:#]+", text):
            _add_candidate(p)

        # Any explicit absolute paths (mountpoints, etc.) that might map to /dev via findmnt.
        for p in re.findall(r"/[A-Za-z0-9._/\-+:#]+", text):
            _add_candidate(p)

        # First token can be mapping name or discovery ID.
        token = text.split()[0].strip("[](),")
        if token:
            _add_candidate(token)

        def _candidate_to_device(c):
            c = str(c or "").strip()
            if not c:
                return None

            # Mapping name or #N.
            try:
                resolved = self.resolve_target(c, allow_id=True)
            except Exception:
                resolved = None
            if resolved and os.path.exists(resolved):
                return os.path.realpath(resolved)

            # Existing path.
            if os.path.exists(c):
                rc = os.path.realpath(c)
                if rc.startswith('/dev/'):
                    return rc
                # Mountpoint path -> mounted source device.
                res_src = run_command(['findmnt', '-rn', '-T', rc, '-o', 'SOURCE'], check=False)
                src = (getattr(res_src, 'stdout', '') or '').strip().splitlines()
                src = src[0].strip() if src else ""
                if src.startswith('/dev/'):
                    return os.path.realpath(src)

            # Raw /dev path token that might exist even if os.path.exists() raced.
            if c.startswith('/dev/'):
                return os.path.realpath(c)

            return None

        device_real = None
        for c in candidates:
            dev = _candidate_to_device(c)
            if dev and dev.startswith('/dev/') and os.path.exists(dev):
                device_real = dev
                break

        if not device_real:
            return (None, None, None)

        # Map virtual devices (e.g. /dev/mapper/*) to a stable underlying block
        # node before resolving persistent by-id identity.
        id_dev = device_real
        try:
            t = (_lsblk_type(id_dev) or '').strip().lower()
            hops = 0
            while t == 'crypt' and hops < 8:
                res_pk = run_command(['lsblk', '-no', 'PKNAME', id_dev], check=False)
                pk = (getattr(res_pk, 'stdout', '') or '').strip().splitlines()
                pk = pk[0].strip() if pk else ""
                if not pk:
                    break
                nxt = os.path.realpath(f"/dev/{pk}")
                if not os.path.exists(nxt) or nxt == id_dev:
                    break
                id_dev = nxt
                t = (_lsblk_type(id_dev) or '').strip().lower()
                hops += 1
        except Exception:
            pass

        try:
            kname = os.path.basename(id_dev)
            dtype = (_lsblk_type(id_dev) or '').strip().lower()
            if dtype not in ('disk', 'part'):
                if _sysfs_is_whole_disk(id_dev):
                    dtype = 'disk'
                else:
                    dtype = 'part'
            res_wwn = run_command(['lsblk', '-no', 'WWN', id_dev], check=False)
            wwn = (getattr(res_wwn, 'stdout', '') or '').strip()
            pdp = self.find_persistent_path(kname, wwn=wwn, type_=dtype)
            pci = self.find_pci_path(kname, type_=dtype)
        except Exception:
            pdp = "-"
            pci = "-"

        if not pdp or pdp == "-":
            return (device_real, None, None)
        if not pci or pci == "-":
            return (device_real, pdp, None)
        return (device_real, pdp, pci)

    def resolve_target(self, target_str, allow_id=True):
        '''Resolves a target string to a physical path.
        Supports Discovery IDs (#1, [#1], and legacy U1/[U1]) or existing mapping names.
        '''
        clean = target_str.strip('[]')

        # 1. Check Discovery ID
        if allow_id:
            if clean.startswith('#') and clean[1:].isdigit():
                wanted = clean[1:]
                # Strict lookup: exact display ID from the last `list` run.
                # Use id_cache so #N always refers to the printed row ID and
                # never falls back to positional cache ordering.
                resolved = (self.id_cache or {}).get(wanted)
                if resolved:
                    return resolved
                # Back-compat for legacy map-only flows where only unmapped cache
                # had IDs populated. Keep exact-ID match only (no positional fallback).
                for entry in (self.unmapped_cache or []):
                    try:
                        if str(entry.get('id', '')).strip() == wanted:
                            return entry.get('pdp')
                    except Exception:
                        continue
                return None
            elif clean.startswith('U') and clean[1:].isdigit():
                # Legacy ID format, now strict/exact against current ID cache.
                wanted = clean[1:]
                resolved = (self.id_cache or {}).get(wanted)
                if resolved:
                    return resolved
                return None

        # 2. Check Mapping Name
        self.mappings = read_luks_map()
        if target_str in self.mappings:
            return self.mappings[target_str]

        return None

    def extensive_confirm(self, target_name, destructive=True):
        print(f"\n{Colors.FAIL}{Colors.BOLD}!!! EXTENSIVE CONFIRMATION REQUIRED !!!{Colors.ENDC}")
        if destructive:
            print(f"You are about to perform a DESTRUCTIVE operation on: {Colors.WARNING}{target_name}{Colors.ENDC}")
        else:
            print(f"You are about to perform a HIGH-IMPACT operation on: {Colors.WARNING}{target_name}{Colors.ENDC}")
        self._show_target_entries_for_confirmation(target_name)
        if destructive:
            dev_real, pdp, pci = self._resolve_confirmation_identity(target_name)
            if not dev_real:
                log("Could not resolve confirmation device. Aborting operation.", 'ERROR')
                return False
            if not pdp:
                log("Could not resolve persistent path for confirmation. Aborting operation.", 'ERROR')
                return False
            if not pci:
                log("Could not resolve PCI path for confirmation. Aborting operation.", 'ERROR')
                return False

            print("To proceed, type ALL THREE values exactly as shown:")
            print(f"  DEVICE: {Colors.OKCYAN}{dev_real}{Colors.ENDC}")
            print(f"  PERSISTENT PATH: {Colors.OKCYAN}{pdp}{Colors.ENDC}")
            print(f"  PCI: {Colors.OKCYAN}{pci}{Colors.ENDC}")

            user_dev = self._input_no_history("Confirm DEVICE: ")
            if (user_dev or "").strip() != dev_real:
                log("Device confirmation mismatch. Aborting operation.", 'ERROR')
                return False

            user_pdp = self._input_no_history("Confirm PERSISTENT PATH: ")
            if (user_pdp or "").strip() != pdp:
                log("Persistent path confirmation mismatch. Aborting operation.", 'ERROR')
                return False

            user_pci = self._input_no_history("Confirm PCI: ")
            if (user_pci or "").strip() != pci:
                log("PCI path confirmation mismatch. Aborting operation.", 'ERROR')
                return False
        else:
            print("To proceed, type YES.")
            user_yes = self._input_no_history("Type YES to continue: ")
            if (user_yes or "").strip() != "YES":
                log("Confirmation failed. Aborting operation.", 'ERROR')
                return False

        print(f"{Colors.OKGREEN}Verification successful. Proceeding...{Colors.ENDC}")
        return True

    def is_root_disk(self, target_path, allow_sibling_partitions=False):
        """Return whether target intersects the root backing graph.

        Probe uncertainty raises ``DeviceProbeError`` so destructive callers can
        fail closed rather than treating an unknown topology as safe.
        """
        target_real = os.path.realpath(str(target_path or ''))
        if not target_real.startswith('/dev/') or not os.path.exists(target_real):
            raise DeviceProbeError(f"target block device is unavailable: {target_real or target_path}")

        root = run_command_hard_timeout(
            ['findmnt', '-nro', 'SOURCE,MAJ:MIN', '/'], 5, check=False
        )
        if getattr(root, 'returncode', 1) != 0:
            raise DeviceProbeError("findmnt could not identify the root filesystem")
        fields = (getattr(root, 'stdout', '') or '').strip().split()
        if not fields:
            raise DeviceProbeError("findmnt returned an empty root source")
        root_source_text = fields[0].split('[', 1)[0]
        root_source = os.path.realpath(root_source_text)
        if not root_source.startswith('/dev/') or not os.path.exists(root_source):
            major_minor = fields[-1] if len(fields) > 1 else ''
            sys_link = Path('/sys/dev/block') / major_minor
            if not major_minor or not sys_link.exists():
                raise DeviceProbeError(
                    f"root source {root_source_text!r} could not be mapped to a block device"
                )
            root_source = os.path.realpath(f"/dev/{os.path.basename(os.path.realpath(sys_link))}")
        if not os.path.exists(root_source):
            raise DeviceProbeError(f"resolved root block device is unavailable: {root_source}")

        def ancestry(device):
            result = run_command_hard_timeout(
                ['lsblk', '-s', '-nrpo', 'PATH,TYPE', device], 5, check=False
            )
            if getattr(result, 'returncode', 1) != 0:
                detail = (getattr(result, 'stderr', '') or '').strip()
                raise DeviceProbeError(f"lsblk topology probe failed for {device}: {detail}")
            nodes = {}
            for raw in (getattr(result, 'stdout', '') or '').splitlines():
                parts = raw.strip().split(None, 1)
                if len(parts) == 2 and parts[0].startswith('/dev/'):
                    nodes[os.path.realpath(parts[0])] = parts[1].strip().lower()
            if os.path.realpath(device) not in nodes or not nodes:
                raise DeviceProbeError(f"lsblk returned an incomplete topology for {device}")
            return nodes

        root_nodes = ancestry(root_source)
        target_nodes = ancestry(target_real)
        if target_real in root_nodes:
            return True
        root_disks = {path for path, dtype in root_nodes.items() if dtype == 'disk'}
        target_disks = {path for path, dtype in target_nodes.items() if dtype == 'disk'}
        if not root_disks or not target_disks:
            raise DeviceProbeError("could not identify top-level root and target disks")
        if root_disks.intersection(target_disks):
            target_type = target_nodes.get(target_real, '')
            if allow_sibling_partitions and target_type == 'part' and target_real not in root_nodes:
                return False
            return True
        return False

    def _block_if_root_drive(self, target_path, operation, allow_sibling_partitions=False):
        """Return True (and log) if target_path is on the system root drive."""
        try:
            blocked = self.is_root_disk(
                target_path, allow_sibling_partitions=allow_sibling_partitions
            )
        except Exception as exc:
            log(
                f"OPERATION BLOCKED: could not prove {target_path} is separate from "
                f"the system root drive for {operation}: {exc}",
                'ERROR',
            )
            return True
        if blocked:
            log(
                f"OPERATION BLOCKED: {operation} is not allowed on the system root "
                f"drive ({target_path}).",
                'ERROR',
            )
            return True
        return False

    def _format_device_tree(self, real_target):
        """Return the target and every lsblk child, or a probe error."""
        res = run_command_hard_timeout(
            ['lsblk', '-nr', '-o', 'NAME,TYPE', real_target],
            5,
            check=False,
        )
        if getattr(res, 'returncode', 1) != 0:
            detail = (getattr(res, 'stderr', '') or '').strip()
            return None, detail or f"lsblk exited with status {getattr(res, 'returncode', '?')}"

        devices = []
        missing_children = []
        for raw in (getattr(res, 'stdout', '') or '').splitlines():
            fields = raw.strip().split()
            if len(fields) < 2:
                continue
            name, dtype = fields[0], fields[1].lower()
            path = name if name.startswith('/dev/') else f'/dev/{name}'
            if os.path.exists(path):
                devices.append({'path': os.path.realpath(path), 'type': dtype})
            else:
                missing_children.append(path)

        if missing_children:
            return None, "lsblk reported child device nodes that are unavailable: " + ', '.join(missing_children)

        target_real = os.path.realpath(real_target)
        if not any(d['path'] == target_real for d in devices):
            target_type = (_lsblk_type(target_real) or '').strip().lower()
            if target_type not in ('disk', 'part'):
                return None, "lsblk did not return a supported target type"
            devices.insert(0, {'path': target_real, 'type': target_type})
        unique = []
        seen = set()
        for item in devices:
            if item['path'] in seen:
                continue
            seen.add(item['path'])
            unique.append(item)
        if not unique:
            return None, "lsblk returned no target device"
        return unique, ""

    def _format_identity_snapshot(self, real_target):
        """Capture the identity fields that must remain stable while confirming format."""
        real_target = os.path.realpath(real_target)
        if not os.path.exists(real_target):
            return None, f"device disappeared: {real_target}"

        kname = os.path.basename(real_target)
        sysfs = Path('/sys/class/block') / kname
        errors = []

        def _read_sysfs(relative, label, required=True):
            try:
                value = (sysfs / relative).read_text().strip()
            except Exception as exc:
                value = ''
                if required:
                    errors.append(f"{label}: {exc}")
            if required and not value:
                errors.append(f"{label}: no value")
            return value

        sectors = _read_sysfs('size', 'device size')
        logical = _read_sysfs('queue/logical_block_size', 'logical sector size')
        physical = _read_sysfs('queue/physical_block_size', 'physical sector size')
        try:
            size_bytes = int(sectors, 10) * 512
        except (TypeError, ValueError):
            size_bytes = 0
            errors.append('device size: invalid sector count')

        try:
            st = os.stat(real_target)
            major_minor = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        except Exception as exc:
            major_minor = ''
            errors.append(f"major/minor: {exc}")

        props = {}
        udev = run_command_hard_timeout(
            ['udevadm', 'info', '--query=property', '--name', real_target],
            5,
            check=False,
        )
        if getattr(udev, 'returncode', 1) == 0:
            for raw in (getattr(udev, 'stdout', '') or '').splitlines():
                if '=' in raw:
                    key, value = raw.split('=', 1)
                    props[key.strip()] = value.strip()
        else:
            errors.append(
                f"udevadm identity probe failed (status {getattr(udev, 'returncode', '?')})"
            )

        dtype = (_lsblk_type(real_target) or '').strip().lower()
        if dtype not in ('disk', 'part'):
            if _sysfs_is_whole_disk(real_target):
                dtype = 'disk'
            elif not dtype:
                errors.append('device type: unavailable')

        wwn_res = run_command_hard_timeout(
            ['lsblk', '--nodeps', '-no', 'WWN', real_target],
            5,
            check=False,
        )
        wwn = (getattr(wwn_res, 'stdout', '') or '').strip().splitlines()
        wwn = wwn[0].strip() if wwn else (props.get('ID_WWN') or '')
        pdp = self.find_persistent_path(kname, wwn=wwn, type_=dtype or 'disk')
        serial_path = self.find_serial_wwid_path(kname, type_=dtype or 'disk')
        pci_path = self.find_pci_path(kname, type_=dtype or 'disk')

        # USB devices can lack a true WWN or PCI by-id link. The persistent
        # model/serial path remains a useful identity, but missing required
        # values are still displayed and compared when available.
        serial = props.get('ID_SERIAL') or props.get('ID_SERIAL_SHORT') or serial_path
        pci_identity = pci_path if pci_path and pci_path != '-' else (props.get('ID_PATH') or '-')
        snapshot = {
            'device': real_target,
            'kname': kname,
            'wwn': wwn or '-',
            'serial': serial or '-',
            'pci': pci_identity or '-',
            'major_minor': major_minor or '-',
            'size_bytes': size_bytes,
            'logical_sector_bytes': logical or '-',
            'physical_sector_bytes': physical or '-',
            'persistent_path': pdp or '-',
            'serial_path': serial_path or '-',
            'pci_path': pci_path or '-',
            'type': dtype or '-',
        }
        if snapshot['persistent_path'] == '-':
            errors.append('persistent device path: unavailable')
        if size_bytes <= 0:
            errors.append('device size: zero or unavailable')
        return snapshot, '; '.join(errors)

    def _format_probe_wipefs(self, device, use_lock=True):
        wipefs_bin = _find_tool_or_common_paths('wipefs', [
            '/usr/sbin/wipefs', '/sbin/wipefs', '/usr/bin/wipefs', '/bin/wipefs'
        ]) or 'wipefs'
        command = [wipefs_bin, '--no-act', '--json']
        if use_lock:
            command.append('--lock=nonblock')
        command.append(device)
        res = run_command_hard_timeout(command, 8, check=False, sudo=True)
        rc = getattr(res, 'returncode', 1)
        if rc != 0:
            detail = (getattr(res, 'stderr', '') or '').strip()
            return None, f"wipefs probe failed for {device} (status {rc})" + (f": {detail}" if detail else '')
        try:
            data = json.loads(getattr(res, 'stdout', '') or '{}')
        except json.JSONDecodeError as exc:
            return None, f"wipefs returned invalid JSON for {device}: {exc}"
        signatures = data.get('signatures', [])
        if not isinstance(signatures, list):
            return None, f"wipefs returned an invalid signature list for {device}"
        return signatures, ""

    def _format_probe_blkid(self, device):
        res = run_command_hard_timeout(
            ['blkid', '--probe', '--output', 'export', device],
            8,
            check=False,
            sudo=True,
        )
        rc = getattr(res, 'returncode', 1)
        stdout = getattr(res, 'stdout', '') or ''
        stderr = (getattr(res, 'stderr', '') or '').strip()
        # blkid uses a non-zero status for an unrecognised blank device. Any
        # diagnostic output indicating I/O/permission/device failure is not a
        # blank result and must fail closed.
        bad_words = ('input/output error', 'permission denied', 'cannot open', 'timed out', 'no such device')
        if rc != 0 and (stdout.strip() or any(word in stderr.lower() for word in bad_words)):
            return None, f"blkid probe failed for {device} (status {rc})" + (f": {stderr}" if stderr else '')
        values = {}
        for raw in stdout.splitlines():
            if '=' in raw:
                key, value = raw.split('=', 1)
                values[key.strip()] = value.strip()
        if rc != 0 and not values and stderr:
            # A normal blank probe returns status 2 with no output. Any
            # diagnostic text means the probe did not establish that the device
            # is safely blank, so fail closed.
            return None, f"blkid probe failed for {device} (status {rc}): {stderr}"
        return values, ""

    def _format_probe_contents(self, devices, use_lock=True):
        """Probe target/children and return normalized existing signatures."""
        found = []
        errors = []
        seen = set()
        for item in devices:
            device = item['path']
            wipe_signatures, error = self._format_probe_wipefs(device, use_lock=use_lock)
            if error:
                errors.append(error)
            else:
                for sig in wipe_signatures or []:
                    if not isinstance(sig, dict):
                        errors.append(f"wipefs returned an invalid signature for {device}")
                        continue
                    entry = {
                        'device': str(sig.get('device') or device),
                        'type': str(sig.get('type') or 'unknown'),
                        'label': str(sig.get('label') or '-'),
                        'uuid': str(sig.get('uuid') or '-'),
                        'offset': str(sig.get('offset') or '-'),
                        'source': 'wipefs',
                    }
                    key = tuple(entry.get(k, '') for k in ('device', 'type', 'label', 'uuid'))
                    if key not in seen:
                        seen.add(key)
                        found.append(entry)

            blkid_values, error = self._format_probe_blkid(device)
            if error:
                errors.append(error)
            elif blkid_values:
                for key in ('PTTYPE', 'TYPE', 'USAGE'):
                    value = blkid_values.get(key)
                    if not value:
                        continue
                    entry = {
                        'device': blkid_values.get('DEVNAME') or device,
                        'type': value if key != 'USAGE' else f"{value} ({blkid_values.get('TYPE', 'unknown')})",
                        'label': (blkid_values.get('PTLABEL') if key == 'PTTYPE' else blkid_values.get('LABEL')) or '-',
                        'uuid': (blkid_values.get('PTUUID') if key == 'PTTYPE' else blkid_values.get('UUID')) or '-',
                        'offset': '-',
                        'source': 'blkid',
                    }
                    sig_key = tuple(entry.get(k, '') for k in ('device', 'type', 'label', 'uuid'))
                    if sig_key not in seen:
                        seen.add(sig_key)
                        found.append(entry)
        return found, errors

    def _format_active_use(self, devices):
        """Check mounts, swaps, holders, and active mapper-style consumers."""
        mounts = []
        swaps = []
        holders = []
        memberships = []
        errors = []
        device_paths = {os.path.realpath(item['path']) for item in devices}

        for item in devices:
            device = item['path']
            lsblk = run_command_hard_timeout(
                ['lsblk', '--nodeps', '-nr', '-o', 'MOUNTPOINTS', device],
                5,
                check=False,
            )
            if getattr(lsblk, 'returncode', 1) != 0:
                errors.append(f"lsblk mountpoint probe failed for {device}")
            else:
                for mountpoint in (getattr(lsblk, 'stdout', '') or '').splitlines():
                    mountpoint = mountpoint.strip()
                    if mountpoint and mountpoint != '-':
                        mounts.append((device, mountpoint))

            findmnt = run_command_hard_timeout(
                ['findmnt', '-rn', '-S', device, '-o', 'TARGET,SOURCE,FSTYPE'],
                5,
                check=False,
            )
            if getattr(findmnt, 'returncode', 1) not in (0, 1):
                errors.append(f"findmnt probe failed for {device}")
            elif getattr(findmnt, 'returncode', 1) == 0:
                for row in (getattr(findmnt, 'stdout', '') or '').splitlines():
                    row = row.strip()
                    if row:
                        mounts.append((device, row))

            kname = os.path.basename(device)
            holder_dir = Path('/sys/class/block') / kname / 'holders'
            try:
                if holder_dir.exists():
                    for holder in holder_dir.iterdir():
                        holders.append((device, holder.name))
            except OSError as exc:
                errors.append(f"holder probe failed for {device}: {exc}")

            # An active crypt/LVM/MD/multipath child is itself unsafe even when
            # its mountpoint is not visible through the parent node.
            dtype = item.get('type', '').lower()
            if dtype in ('crypt', 'dm', 'lvm', 'md', 'mpath', 'raid0', 'raid1', 'raid10', 'raid5', 'raid6'):
                memberships.append((device, dtype))

        try:
            swap_lines = Path('/proc/swaps').read_text().splitlines()[1:]
        except Exception as exc:
            errors.append(f"/proc/swaps probe failed: {exc}")
            swap_lines = []
        for raw in swap_lines:
            fields = raw.split()
            if not fields:
                continue
            source = fields[0]
            source_real = os.path.realpath(source)
            if source_real in device_paths:
                swaps.append(source)

        return {
            'mounts': mounts,
            'swaps': swaps,
            'holders': holders,
            'memberships': memberships,
            'errors': errors,
        }

    def _format_acquire_device_lock(self, real_target):
        """Hold an exclusive flock on the block device in a privileged helper."""
        target = os.path.realpath(real_target)
        python_bin = shutil.which('python3') or sys.executable
        helper = (
            "import fcntl,os,sys; "
            "fd=os.open(sys.argv[1],os.O_RDONLY|getattr(os,'O_CLOEXEC',0)); "
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); "
            "print('LOCKED',flush=True); sys.stdin.buffer.read()"
        )
        process = None
        try:
            process = popen_command(
                [python_bin, '-u', '-c', helper, target],
                sudo=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ready, _, _ = select.select([process.stdout], [], [], 60)
            if not ready:
                process.terminate()
                process.wait(timeout=3)
                return None, "timed out while acquiring the privileged target device lock"
            line = (process.stdout.readline() or '').strip()
            if line != 'LOCKED' or process.poll() is not None:
                detail = (process.stderr.read() or '').strip()
                return None, detail or "another process owns the target device lock"
            return _HeldDeviceLock(process=process, target=target), ""
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.terminate()
            return None, f"could not acquire target device lock: {exc}"

    @staticmethod
    def _format_release_device_lock(lock_handle):
        if lock_handle is None:
            return
        try:
            process = lock_handle.process
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _format_safety_preflight(self, real_target, lock_fd=None):
        """Run all read-only format guards and return a structured snapshot."""
        identity, error = self._format_identity_snapshot(real_target)
        if error:
            return {'ok': False, 'errors': [f"identity probe failed: {error}"]}
        devices, error = self._format_device_tree(real_target)
        if error:
            return {'ok': False, 'errors': [error]}

        dev_type = identity.get('type') or '-'
        pttype_res = run_command_hard_timeout(
            ['lsblk', '--nodeps', '-no', 'PTTYPE', real_target],
            5,
            check=False,
        )
        if getattr(pttype_res, 'returncode', 1) != 0:
            return {'ok': False, 'errors': [f"partition-table probe failed for {real_target}"]}
        pttype = (getattr(pttype_res, 'stdout', '') or '').strip().lower()

        signatures, probe_errors = self._format_probe_contents(devices, use_lock=lock_fd is None)
        active = self._format_active_use(devices)
        errors = list(probe_errors) + list(active.get('errors') or [])
        if errors:
            return {
                'ok': False,
                'errors': errors,
                'identity': identity,
                'devices': devices,
                'signatures': signatures,
                'active': active,
                'pttype': pttype,
                'dev_type': dev_type,
            }
        if lock_fd is None:
            lock_fd, lock_error = self._format_acquire_device_lock(real_target)
            if lock_error:
                return {
                    'ok': False,
                    'errors': [lock_error],
                    'identity': identity,
                    'devices': devices,
                    'signatures': signatures,
                    'active': active,
                    'pttype': pttype,
                    'dev_type': dev_type,
                }
        return {
            'ok': True,
            'errors': [],
            'lock_fd': lock_fd,
            'identity': identity,
            'devices': devices,
            'signatures': signatures,
            'active': active,
            'pttype': pttype,
            'dev_type': dev_type,
        }

    def _destructive_safety_preflight(self, real_target):
        """Common fail-closed preflight for metadata and block-device writes."""
        return self._format_safety_preflight(real_target)

    @staticmethod
    def _active_use_present(active):
        active = active or {}
        return any(active.get(key) for key in ('mounts', 'swaps', 'holders', 'memberships'))

    def _destructive_revalidate(self, before, lock_handle):
        """Repeat identity, signature, and active-use checks after confirmation."""
        after = self._format_safety_preflight(before['identity']['device'], lock_fd=lock_handle)
        if not after.get('ok'):
            return False, after
        changed = self._format_identity_changed(before['identity'], after['identity'])
        signatures_changed = self._format_signatures_changed(
            before.get('signatures'), after.get('signatures')
        )
        if changed or signatures_changed or self._active_use_present(after.get('active')):
            after.setdefault('errors', []).append(
                "identity, signatures, or active-use state changed after confirmation"
            )
            return False, after
        return True, after

    def _format_print_preflight(self, preflight):
        """Print the existing-content and active-use summary before confirmation."""
        identity = preflight.get('identity') or {}
        if identity:
            print(f"\n{Colors.BOLD}Identity snapshot:{Colors.ENDC}")
            print(f"  WWN: {identity.get('wwn', '-')}  Serial: {identity.get('serial', '-')}  PCI: {identity.get('pci', '-')}")
            print(
                f"  Major:Minor: {identity.get('major_minor', '-')}  "
                f"Size: {identity.get('size_bytes', 0):,} bytes  "
                f"Sector: L{identity.get('logical_sector_bytes', '-')} / P{identity.get('physical_sector_bytes', '-')}"
            )
        signatures = preflight.get('signatures') or []
        if signatures:
            print(f"\n{Colors.WARNING}Existing content/signatures detected:{Colors.ENDC}")
            for sig in signatures:
                print(
                    f"  {sig.get('device', '-')}  type={sig.get('type', '-')}"
                    f"  label={sig.get('label', '-')}  uuid={sig.get('uuid', '-')}"
                )
        else:
            print(f"\n{Colors.OKGREEN}Existing content/signatures: none detected on target or children.{Colors.ENDC}")

        if preflight.get('pttype'):
            print(f"  partition table: {preflight['pttype']}")
        active = preflight.get('active') or {}
        if active.get('mounts'):
            print(f"{Colors.FAIL}Active mounts:{Colors.ENDC}")
            for device, mount in active['mounts']:
                print(f"  {device}: {mount}")
        if active.get('swaps'):
            print(f"{Colors.FAIL}Active swap devices:{Colors.ENDC} {', '.join(active['swaps'])}")
        if active.get('holders'):
            print(f"{Colors.FAIL}Kernel/device-mapper holders:{Colors.ENDC}")
            for device, holder in active['holders']:
                print(f"  {device}: {holder}")
        if active.get('memberships'):
            print(f"{Colors.FAIL}Active mapper/RAID memberships:{Colors.ENDC}")
            for device, dtype in active['memberships']:
                print(f"  {device}: {dtype}")

    def _format_existing_confirmation(self, signatures):
        """Require an existing UUID or label before allowing reformat override."""
        values = []
        for sig in signatures or []:
            for key in ('uuid', 'label'):
                value = str(sig.get(key) or '').strip()
                if value and value != '-' and value not in values:
                    values.append(value)
        if not values:
            log("Existing content has no UUID or label to confirm. Use erase first.", 'ERROR')
            return False
        print("To reformat existing content, type one existing UUID or label exactly as shown:")
        print(f"  {', '.join(values)}")
        answer = self._input_no_history("Confirm existing UUID/LABEL: ")
        if (answer or '').strip() not in values:
            log("Existing UUID/label confirmation mismatch. Aborting operation.", 'ERROR')
            return False
        return True

    @staticmethod
    def _format_identity_changed(before, after):
        fields = (
            'wwn', 'serial', 'pci', 'major_minor', 'size_bytes',
            'logical_sector_bytes', 'physical_sector_bytes',
        )
        return [field for field in fields if before.get(field) != after.get(field)]

    @staticmethod
    def _format_signatures_changed(before, after):
        def normalize(items):
            return sorted(
                tuple(str(item.get(key) or '-') for key in ('device', 'type', 'label', 'uuid', 'offset'))
                for item in (items or [])
            )
        return normalize(before) != normalize(after)

    def _soft_erase_target(self, real_target):
        dev_type = _lsblk_type(real_target)

        wipefs_bin = _find_tool_or_common_paths('wipefs', [
            '/usr/sbin/wipefs',
            '/sbin/wipefs',
            '/usr/bin/wipefs',
            '/bin/wipefs',
        ]) or 'wipefs'
        sgdisk_bin = _find_tool_or_common_paths('sgdisk', [
            '/usr/sbin/sgdisk',
            '/sbin/sgdisk',
            '/usr/bin/sgdisk',
            '/bin/sgdisk',
        ])
        log(f"Soft erase: wiping signatures on {real_target} ...")

        # If this is a whole disk, wipe signatures on existing partitions first (best-effort).
        if dev_type == 'disk':
            pttype_res = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
            if getattr(pttype_res, 'returncode', 1) != 0:
                log(f"Could not determine partition-table type before erasing {real_target}.", 'ERROR')
                return False
            pttype_before = (getattr(pttype_res, 'stdout', '') or '').strip().lower()
            parts = []
            try:
                res_p = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE', real_target], check=False)
                for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                    cols = line.strip().split()
                    if len(cols) >= 2 and cols[1] == 'part':
                        parts.append(os.path.realpath(f"/dev/{cols[0]}"))
            except Exception:
                parts = []

            for p in parts:
                result = run_command([wipefs_bin, '-a', p], sudo=True, check=False)
                if getattr(result, 'returncode', 1) != 0:
                    log(f"wipefs failed for child partition {p}; refusing to report success.", 'ERROR')
                    return False

            # For whole disks, --force is required to erase partition-table signatures.
            result = run_command([wipefs_bin, '-a', '--force', real_target], sudo=True, check=False)
            if getattr(result, 'returncode', 1) != 0:
                log(f"wipefs failed for {real_target}; refusing to report success.", 'ERROR')
                return False

            # Zap GPT metadata to remove its backup header at end-of-disk. The
            # type is captured before wipefs, because afterward it is expected
            # to be blank and must not cause GPT tooling to run on an MBR disk.
            if pttype_before == 'gpt':
                if not sgdisk_bin:
                    log("sgdisk is required to clear GPT metadata safely.", 'ERROR')
                    return False
                result = run_command([sgdisk_bin, '--zap-all', real_target], sudo=True, check=False)
                if getattr(result, 'returncode', 1) != 0:
                    log(f"sgdisk failed for {real_target}; refusing to report success.", 'ERROR')
                    return False
            # Do not write a new empty DOS label.  Creating one would leave a
            # fresh partition-table signature and make the target non-blank.

            self._refresh_kernel_partition_state(real_target, drop_partitions=True)
        else:
            result = run_command([wipefs_bin, '-a', real_target], sudo=True, check=False)
            if getattr(result, 'returncode', 1) != 0:
                log(f"wipefs failed for {real_target}; refusing to report success.", 'ERROR')
                return False
            settle = run_command(['udevadm', 'settle'], sudo=True, check=False)
            if getattr(settle, 'returncode', 1) != 0:
                log("udevadm settle failed after soft erase.", 'ERROR')
                return False

        run_command(['udevadm', 'settle'], sudo=True, check=False)
        devices, tree_error = self._format_device_tree(real_target)
        if tree_error:
            log(f"Could not verify erased device tree: {tree_error}", 'ERROR')
            return False
        signatures, probe_errors = self._format_probe_contents(devices, use_lock=False)
        if probe_errors:
            log(f"Could not verify erased signatures: {'; '.join(probe_errors)}", 'ERROR')
            return False
        if signatures:
            log("Soft erase verification found signatures still present:", 'ERROR')
            for signature in signatures:
                log(
                    f"{signature.get('device', '-')} type={signature.get('type', '-')} "
                    f"label={signature.get('label', '-')} uuid={signature.get('uuid', '-')}",
                    'ERROR',
                )
            return False

        log("Soft erase completed.")
        return True

    def _refresh_kernel_partition_state(self, disk_dev, drop_partitions=False):
        """
        Best-effort refresh of kernel/udev partition state for a whole disk.
        Helps clear stale child partition nodes after destructive whole-disk ops
        (erase, superfloppy/luks-on-disk format), especially behind some USB bridges.
        """
        real_disk = os.path.realpath(str(disk_dev or ""))
        if not real_disk or not os.path.exists(real_disk):
            return

        disk_type = (_lsblk_type(real_disk) or '').strip().lower()
        if disk_type != 'disk' and not _sysfs_is_whole_disk(real_disk):
            return

        # Drain prior uevents first.
        run_command(['udevadm', 'settle'], sudo=True, check=False)

        # For whole-disk superfloppy style layouts, remove stale partition nodes
        # that may still be cached in kernel state.
        if drop_partitions:
            run_command(['partx', '-d', real_disk], sudo=True, check=False)

        # Ask kernel/userspace to re-read and re-emit current topology.
        run_command(['blockdev', '--rereadpt', real_disk], sudo=True, check=False)
        run_command(['partprobe', real_disk], sudo=True, check=False)
        run_command(['udevadm', 'trigger', '--name-match=' + os.path.basename(real_disk)], sudo=True, check=False)
        run_command(['udevadm', 'settle'], sudo=True, check=False)

    def _disk_looks_erased_for_create(self, real_target):
        """Fail-closed precheck used by create: disk must look erased."""
        if _lsblk_type(real_target) != 'disk':
            return (False, "target is not a whole disk")

        # Must have no child partitions.
        try:
            res_p = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE', real_target], check=False)
            if getattr(res_p, 'returncode', 1) != 0:
                return (False, "could not enumerate child devices")
            parts = []
            for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                cols = line.strip().split()
                if len(cols) >= 2 and cols[1] == 'part':
                    parts.append(cols[0])
            if parts:
                return (False, f"disk still has partition(s): {', '.join(parts)}")
        except Exception as exc:
            return (False, f"child-device probe failed: {exc}")

        # Must not currently report a partition table type.
        try:
            res_pt = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
            if getattr(res_pt, 'returncode', 1) != 0:
                return (False, "could not probe partition-table metadata")
            pttype = (getattr(res_pt, 'stdout', '') or '').strip().lower()
            if pttype:
                return (False, f"disk still has partition-table metadata ({pttype})")
        except Exception as exc:
            return (False, f"partition-table probe failed: {exc}")

        # Use the same privileged, direct probes as format so a probe failure
        # cannot be mistaken for a blank disk.
        signatures, errors = self._format_probe_contents(
            [{'path': os.path.realpath(real_target), 'type': 'disk'}],
            use_lock=False,
        )
        if errors:
            return (False, '; '.join(errors))
        if signatures:
            first = signatures[0]
            return (
                False,
                "disk still has metadata/signature: "
                f"{first.get('type', 'unknown')} {first.get('label', '-')} {first.get('uuid', '-')}"
            )

        return (True, "")

    def _largest_free_extent_sectors(self, disk_dev):
        """
        Return (start_sector, end_sector, size_sector) for the largest free extent on a disk,
        using `parted -m unit s print free`. Returns None if no free extent is found.
        """
        try:
            res = run_command(['parted', '-m', '-s', disk_dev, 'unit', 's', 'print', 'free'],
                              sudo=True, check=False)
            if getattr(res, 'returncode', 1) != 0:
                return None

            best = None
            for raw in (getattr(res, 'stdout', '') or '').splitlines():
                line = raw.strip()
                if not line or line.startswith('BYT') or line.startswith('/'):
                    continue
                parts = line.strip(';').split(':')
                if len(parts) < 5:
                    continue

                fs_or_type = parts[4].strip()
                if fs_or_type != 'free':
                    continue

                try:
                    start_s = int(parts[1].rstrip('s'))
                    end_s = int(parts[2].rstrip('s'))
                    size_s = int(parts[3].rstrip('s'))
                except Exception:
                    continue

                if size_s <= 0:
                    continue

                if (best is None) or (size_s > best[2]):
                    best = (start_s, end_s, size_s)

            return best
        except Exception:
            return None
