"""MountingCommands command implementations."""

import argparse
import cmd
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from ..runtime import LUKS_HEADER_BACKUP_DIR, PASSGEN_BIN, log, run_command, run_command_hard_timeout
from ..devices import _lsblk_fstype, _lsblk_partitions, _lsblk_type, _sysfs_block_name, _sysfs_child_partition_devs, _sysfs_is_whole_disk, _sysfs_to_parent_disk_name
from ..mounts import cleanup_mountpoint_dir, find_mount_targets
from ..mappings import read_luks_map, update_luks_map, validate_mapping_name
from ..safety import (
    safe_mount_path,
    validate_absolute_path,
    validate_filesystem_label,
    validate_storage_name,
)
from ..shell_core import CmdArgumentParser


class MountingCommands:

    def do_open(self, arg):
        '''Unlock (if encrypted) and mount a disk: open <name/id>

        UNDER THE HOOD:
        1.  Identity Resolution: Looks up the friendly name in diskmap.tsv.
        2.  Hardware Wait: Polls for up to 60 seconds to allow for hardware spin-up/udev events.
        3.  Validation:
            - Runs 'cryptsetup isLuks' to check for encryption.
            - If NOT encrypted (Plain Disk):
              * Skips decryption step.
              * Verifies the existence of a valid filesystem.
              * Proceeds to label detection and mounting.
        4.  Decryption (LUKS only):
            - Executes 'passgen' to retrieve the passphrase.
            - Tries 'cryptsetup open' with on-disk LUKS header first.
            - If that fails, retries with detached header at ~/.local/share/diskmgr/<mapping_name> when present.
        5.  Mounting:
            - Uses /etc/fstab mountpoint/options when an entry exists for the device.
            - Otherwise identifies preferred mountpoint: /media/$USER/<label>.
            - If no hardware label is present, falls back to /media/$USER/<mapping_name>.
            - Ensures the directory exists and attaches the device.
            - Clears a stale mount whose /dev source no longer exists before reusing the preferred path.
              A preferred path occupied by an existing different device remains blocked.
            - For btrfs, default mount policy is HDD => compress-force=zstd:12; non-HDD => no compression option.
            - Override per command with --compress=<mode> or --compress-force=<mode>.
        6.  Policy Enforcement: If the disk is already mounted at a non-standard path,
            it unmounts and remounts it to the preferred path.

        SAFETY NOTE:
        - If your mapping points to a whole disk (e.g. /dev/sda) but the actual LUKS/filesystem
          lives on a partition (e.g. /dev/sda2), diskmgr will only auto-select a partition when
          it is unambiguous (exactly one candidate). Otherwise it will refuse and ask you to map
          the correct partition explicitly.
        '''
        parser = CmdArgumentParser(prog='open', add_help=False)
        parser.add_argument('target')
        cgroup = parser.add_mutually_exclusive_group()
        cgroup.add_argument('--compress', dest='compress', metavar='MODE')
        cgroup.add_argument('--compress-force', dest='compress_force', metavar='MODE')

        try:
            args = parser.parse_args(shlex.split(arg) if arg else [])
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            log("Usage: open <name/id> [--compress MODE | --compress-force MODE]", 'ERROR')
            return
        except SystemExit:
            return

        target = args.target
        try:
            btrfs_compression_opt = self._normalize_btrfs_compression_override(
                compress=args.compress,
                compress_force=args.compress_force
            )
        except ValueError as e:
            log(str(e), 'ERROR')
            log("Usage: open <name/id> [--compress MODE | --compress-force MODE]", 'ERROR')
            return

        self.mappings = read_luks_map()
        mapping_name = None
        src = None

        if target.startswith('#') and target[1:].isdigit():
            rid = target[1:]
            src = self.id_cache.get(rid)
            if not src:
                # Backward-compatible fallback (unmapped discovery subset).
                src = self.resolve_target(target, allow_id=True)
            if not src:
                log(f"Unknown discovery ID: {target}. Run 'list' first to refresh IDs.", 'ERROR')
                return
            src_real = os.path.realpath(src)
            for n, p in self.mappings.items():
                try:
                    if os.path.realpath(p) == src_real:
                        mapping_name = n
                        break
                except Exception:
                    continue
        else:
            try:
                mapping_name = validate_mapping_name(target)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            if mapping_name not in self.mappings:
                log(f"Unknown mapping: {mapping_name}. Use 'list' to find Discovery IDs and 'map' them first.", 'ERROR')
                return
            src = self.mappings[mapping_name]

        normalized_src = self._normalize_mapping_target(src)
        if normalized_src != src and mapping_name:
            def update_target(current):
                if current.get(mapping_name) != src:
                    raise ValueError(f"Mapping '{mapping_name}' changed concurrently; retry open.")
                current[mapping_name] = normalized_src
                return current
            try:
                self.mappings = update_luks_map(update_target)
            except ValueError as exc:
                log(str(exc), 'ERROR')
                return
            log(f"Normalized mapping target: {src} -> {normalized_src}")
            src = normalized_src

        # Slow USB HDDs can expose their bridge well before spin-up and udev create
        # the persistent disk link. Keep reporting that distinction while waiting.
        wait_seconds = 60.0
        wait_interval = 0.25
        wait_started = time.monotonic()
        next_wait_report = 5.0
        last_wait_state = ""

        def _usb_storage_bridge_count():
            count = 0
            for driver_name in ('uas', 'usb-storage'):
                driver_dir = f"/sys/bus/usb/drivers/{driver_name}"
                try:
                    count += sum(1 for entry in os.listdir(driver_dir) if ':' in entry)
                except OSError:
                    pass
            return count

        def _persistent_device_ready(path):
            if not os.path.exists(path):
                bridges = _usb_storage_bridge_count()
                if bridges:
                    return False, (
                        f"{bridges} USB storage bridge{'s are' if bridges != 1 else ' is'} present system-wide; "
                        "the target disk is not ready (it may still be spinning up or recovering)"
                    )
                return False, "persistent device path is not present"

            resolved = os.path.realpath(path)
            kname = os.path.basename(resolved)
            state_path = f"/sys/class/block/{kname}/device/state"
            try:
                with open(state_path, 'r', encoding='utf-8', errors='replace') as state_fh:
                    device_state = (state_fh.read() or '').strip().lower()
            except OSError:
                device_state = ""
            if device_state and device_state != 'running':
                return False, f"{resolved} exists but reports device state '{device_state}'"
            return True, f"ready as {resolved}"

        log(f"Waiting up to {int(wait_seconds)} seconds for device {src}...")
        device_ready = False
        while True:
            device_ready, wait_state = _persistent_device_ready(src)
            if device_ready:
                elapsed = time.monotonic() - wait_started
                if elapsed >= 1.0:
                    log(f"Device became ready after {elapsed:.1f}s: {src} -> {os.path.realpath(src)}")
                break

            elapsed = time.monotonic() - wait_started
            state_changed = wait_state != last_wait_state
            if elapsed >= next_wait_report or (state_changed and elapsed >= 1.0):
                log(
                    f"Still waiting for device ({int(elapsed)}s/{int(wait_seconds)}s): {wait_state}...",
                    'WARN',
                )
                next_wait_report = max(next_wait_report + 5.0, elapsed + 5.0)
            last_wait_state = wait_state
            if elapsed >= wait_seconds:
                break
            time.sleep(wait_interval)

        if not device_ready:
            log(f"Device not ready after {int(wait_seconds)} seconds: {src} ({last_wait_state}).", 'ERROR')
            return

        mapped_devnode = os.path.realpath(src)
        op_ref = mapping_name or target
        if self._block_if_root_drive(mapped_devnode, f"open {op_ref}", allow_sibling_partitions=True):
            return
        devnode = mapped_devnode
        detached_header = (
            validate_absolute_path(str(LUKS_HEADER_BACKUP_DIR / mapping_name), 'detached header')
            if mapping_name else None
        )
        has_detached_header = bool(detached_header and os.path.isfile(detached_header))
        force_detached_open = False

        # Check LUKS / filesystem, with safe partition auto-detection when mapping points at a disk.
        is_luks = False
        try:
            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode == 0:
                is_luks = True
        except Exception:
            pass

        # If on-disk header was wiped but a detached backup exists for this mapping,
        # still treat it as a LUKS open path.
        if not is_luks and has_detached_header:
            log(
                f"On-disk LUKS signature not found on {devnode}; "
                f"detached header backup found at {detached_header}. Will attempt detached-header unlock."
            )
            is_luks = True
            force_detached_open = True

        if not is_luks:
            dev_type = _lsblk_type(devnode)

            # If mapping points at a whole disk, try to locate the real payload on a partition.
            if dev_type == 'disk':
                disk_fs = _lsblk_fstype(devnode)
                if disk_fs:
                    log(f"Device {devnode} is a whole-disk filesystem ({disk_fs}). Proceeding with plain mount.")
                else:
                    parts = _lsblk_partitions(devnode)
                    luks_parts = [p for p in parts if p.get('fstype') == 'crypto_LUKS']
                    fs_parts = [p for p in parts if p.get('fstype') and p.get('fstype') != 'crypto_LUKS']

                    if len(luks_parts) == 1:
                        part_path = os.path.realpath(f"/dev/{luks_parts[0]['name']}")
                        log(f"Target '{op_ref}' points to disk {mapped_devnode}, but LUKS was detected on {part_path}. Using the partition for open.")
                        devnode = part_path
                        is_luks = True
                    elif len(luks_parts) > 1:
                        log(f"Target '{op_ref}' points to disk {mapped_devnode}, but multiple LUKS partitions were found.", 'ERROR')
                        for p in luks_parts:
                            log(f"  candidate: /dev/{p['name']} (fstype={p.get('fstype')})", 'ERROR')
                        log("Please map the correct partition explicitly and open that mapping.", 'ERROR')
                        return
                    elif len(fs_parts) == 1:
                        part_path = os.path.realpath(f"/dev/{fs_parts[0]['name']}")
                        log(f"Target '{op_ref}' points to disk {mapped_devnode}, but a filesystem was detected on {part_path} ({fs_parts[0].get('fstype')}). Using the partition for open.")
                        devnode = part_path
                    elif len(fs_parts) > 1:
                        log(f"Target '{op_ref}' points to disk {mapped_devnode}, but multiple filesystem partitions were found.", 'ERROR')
                        for p in fs_parts:
                            log(f"  candidate: /dev/{p['name']} (fstype={p.get('fstype')})", 'ERROR')
                        log("Please map the correct partition explicitly and open that mapping.", 'ERROR')
                        return
                    else:
                        log(f"Device {devnode} is not a valid LUKS device and has no recognizable filesystem (disk has partitions but none look mountable).", 'ERROR')
                        return

            # Re-check LUKS if we switched from disk -> partition.
            if devnode != mapped_devnode and not is_luks:
                try:
                    res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
                    if res.returncode == 0:
                        is_luks = True
                except Exception:
                    pass

            if not is_luks:
                # Not LUKS, check if it has a filesystem
                fs = _lsblk_fstype(devnode)
                if not fs:
                    log(f"Device {devnode} is not a valid LUKS device and has no recognizable filesystem.", 'ERROR')
                    return
                log(f"Device {devnode} is not LUKS, but has a filesystem ({fs}). Proceeding with plain mount.")

        mapper_path = f"/dev/mapper/{mapping_name}" if mapping_name else None
        target_to_mount = mapper_path if mapper_path else devnode

        if is_luks:
            if not mapping_name:
                log("ID target points to a LUKS container. Map it first (map #N <name>) and run open <name>.", 'ERROR')
                return
            needs_open = True
            if os.path.exists(mapper_path):
                # Validate that the existing mapper is still backed by the expected source device.
                expected_src_real = os.path.realpath(src)
                mapper_real = os.path.realpath(mapper_path)
                dm_name = os.path.basename(mapper_real)
                slaves_dir = f"/sys/class/block/{dm_name}/slaves"
                mapper_is_stale = False
                stale_reason = ""
                try:
                    expected_kname = os.path.basename(expected_src_real)
                    slaves = sorted(os.listdir(slaves_dir)) if os.path.isdir(slaves_dir) else []
                    if not slaves:
                        mapper_is_stale = True
                        stale_reason = "no backing slave device"
                    elif expected_kname and expected_kname not in slaves:
                        mapper_is_stale = True
                        stale_reason = f"backing slave mismatch (expected {expected_kname}, got {', '.join(slaves)})"
                except Exception as e:
                    mapper_is_stale = True
                    stale_reason = f"unable to validate mapper backing ({e})"

                if mapper_is_stale:
                    log(f"Existing mapping {mapping_name} looks stale: {stale_reason}. Recycling mapping...")
                    stale_targets = find_mount_targets(mapper_path)
                    for mp in stale_targets:
                        log(f"Unmounting stale target {mp}...")
                        res_um = run_command(['umount', mp], sudo=True, check=False)
                        if getattr(res_um, 'returncode', 1) != 0:
                            detail = (getattr(res_um, 'stderr', '') or '').strip()
                            log(
                                f"Cannot recycle stale mapping because normal unmount failed for "
                                f"{mp}: {detail or 'unknown error'}. Close holders and retry; "
                                "diskmgr will not use lazy unmount.",
                                'ERROR',
                            )
                            return
                        cleanup_mountpoint_dir(mp)
                    close_result = run_command(
                        ['cryptsetup', 'close', mapping_name], sudo=True, check=False
                    )
                    if getattr(close_result, 'returncode', 1) != 0:
                        detail = (getattr(close_result, 'stderr', '') or '').strip()
                        log(
                            f"Failed to close stale mapping {mapping_name}: "
                            f"{detail or 'cryptsetup failed'}",
                            'ERROR',
                        )
                        return
                    for _ in range(20):
                        if not os.path.exists(mapper_path):
                            break
                        time.sleep(0.1)
                    if os.path.exists(mapper_path):
                        log(f"Failed to recycle stale mapping {mapping_name}; mapper still exists.", 'ERROR')
                        return
                else:
                    log("Mapping already exists.")
                    needs_open = False

            if needs_open:
                log(f"Opening LUKS mapping {mapping_name}...")
                # Get passphrase from passgen once and reuse for any fallback attempt.
                passphrase = run_command([PASSGEN_BIN], capture_output=True).stdout
                if not str(passphrase or '').strip():
                    log("passgen returned an empty passphrase.", 'ERROR')
                    return

                if force_detached_open:
                    if not os.path.isfile(detached_header):
                        log(f"Detached header backup missing at open-time: {detached_header}", 'ERROR')
                        return
                    log(f"Opening with detached header backup: {detached_header}")
                    res_detached = run_command(
                        ['cryptsetup', 'open', '--header', detached_header, '--key-file', '-', devnode, mapping_name],
                        input_str=passphrase,
                        sudo=True,
                        check=False
                    )
                    if getattr(res_detached, 'returncode', 1) == 0:
                        log("LUKS opened using detached header backup.")
                    else:
                        detached_err = (getattr(res_detached, 'stderr', '') or '').strip()
                        log("Failed to open LUKS using detached header backup.", 'ERROR')
                        if detached_err:
                            log(f"Detached header error: {detached_err}", 'ERROR')
                        return
                else:
                    # Primary attempt: on-disk header.
                    res_open = run_command(
                        ['cryptsetup', 'open', '--key-file', '-', devnode, mapping_name],
                        input_str=passphrase,
                        sudo=True,
                        check=False
                    )
                    if getattr(res_open, 'returncode', 1) == 0:
                        log("LUKS opened.")
                    else:
                        primary_err = (getattr(res_open, 'stderr', '') or '').strip()
                        if os.path.isfile(detached_header):
                            log(f"On-disk LUKS header open failed; retrying with detached header: {detached_header}")
                            res_detached = run_command(
                                ['cryptsetup', 'open', '--header', detached_header, '--key-file', '-', devnode, mapping_name],
                                input_str=passphrase,
                                sudo=True,
                                check=False
                            )
                            if getattr(res_detached, 'returncode', 1) == 0:
                                log("LUKS opened using detached header backup.")
                            else:
                                detached_err = (getattr(res_detached, 'stderr', '') or '').strip()
                                log("Failed to open LUKS with on-disk and detached headers.", 'ERROR')
                                if primary_err:
                                    log(f"On-disk header error: {primary_err}", 'ERROR')
                                if detached_err:
                                    log(f"Detached header error: {detached_err}", 'ERROR')
                                return
                        else:
                            log("Failed to open LUKS with on-disk header.", 'ERROR')
                            if primary_err:
                                log(primary_err, 'ERROR')
                            log(f"No detached header backup found at: {detached_header}", 'ERROR')
                            return
        else:
            # Plain disk
            target_to_mount = devnode

        # Determine fallback mountpoint name.
        # Hardened behavior: when opening by a mapped name, keep that mapped name
        # as the fallback directory (/media/$USER/<mapping>) even if FS label differs.
        mount_name = mapping_name if mapping_name else os.path.basename(devnode)
        fs_label = ""
        try:
            res_b = run_command(['blkid', '-o', 'value', '-s', 'LABEL', target_to_mount], sudo=True, check=False)
            if res_b.stdout.strip():
                fs_label = res_b.stdout.strip()
                if not mapping_name:
                    try:
                        mount_name = validate_storage_name(fs_label, 'filesystem label')
                    except ValueError as exc:
                        log(
                            f"Filesystem label is unsafe for use as a mountpoint ({exc}); "
                            f"using device name {os.path.basename(devnode)!r} instead.",
                            'WARN',
                        )
        except:
            pass

        media_root = f"/media/{os.environ.get('USER', 'root')}"
        fallback_mountpoint = safe_mount_path(media_root, mount_name)
        mountpoint, use_fstab_mount, fstab_entry = self._select_mountpoint_for_device(
            target_to_mount,
            fallback_mountpoint,
            preferred_label=(fs_label or mount_name)
        )
        if use_fstab_mount:
            mountpoint = validate_absolute_path(mountpoint, 'fstab mountpoint')
            log(f"Using fstab mount for {target_to_mount}: {fstab_entry['spec']} -> {mountpoint}")

        # Safety Check: Is this mountpoint already in use by another device?
        res_check = run_command(['findmnt', '-rn', '-M', mountpoint], check=False)
        if res_check.returncode == 0:
            # Check if it's a DIFFERENT device
            res_src = run_command(
                ['findmnt', '-rn', '-M', mountpoint, '-o', 'SOURCE'],
                capture_output=True,
                check=False,
            )
            current_src_lines = (getattr(res_src, 'stdout', '') or '').strip().splitlines()
            if not current_src_lines:
                log(f"MOUNT BLOCKED: Could not identify the source mounted at {mountpoint}.", 'ERROR')
                return
            current_src_raw = current_src_lines[0]
            # findmnt can append a btrfs subvolume in brackets; existence applies to the device path.
            current_src_device = re.sub(r'\[.*\]$', '', current_src_raw)
            current_src = os.path.realpath(current_src_device)
            if current_src != os.path.realpath(target_to_mount):
                stale_device_mount = (
                    current_src_device.startswith('/dev/')
                    and not os.path.exists(current_src_device)
                )
                if not stale_device_mount:
                    log(f"MOUNT BLOCKED: Path {mountpoint} is already in use by {current_src_raw}.", 'ERROR')
                    return

                log(
                    f"Stale mount detected: {mountpoint} still references missing source "
                    f"{current_src_raw}. Attempting a normal unmount...",
                    'WARN',
                )
                stale_umount = run_command(['umount', mountpoint], sudo=True, check=False)
                if stale_umount.returncode != 0:
                    stale_error = (
                        (getattr(stale_umount, 'stderr', '') or '').strip()
                        or (getattr(stale_umount, 'stdout', '') or '').strip()
                        or "unknown unmount error"
                    )
                    log(f"Failed to clear stale mount {mountpoint}: {stale_error}", 'ERROR')
                    holders = run_command(
                        ['fuser', '-vm', mountpoint],
                        sudo=True,
                        capture_output=True,
                        check=False,
                    )
                    holder_text = '\n'.join(
                        part.strip('\n')
                        for part in (
                            getattr(holders, 'stdout', '') or '',
                            getattr(holders, 'stderr', '') or '',
                        )
                        if part.strip()
                    )
                    if holder_text:
                        log(f"Holders for {mountpoint} (fuser -vm):\n{holder_text}", 'WARN')
                    return

                if run_command(['findmnt', '-rn', '-M', mountpoint], check=False).returncode == 0:
                    log(
                        f"MOUNT BLOCKED: {mountpoint} remains mounted after the stale-mount cleanup attempt.",
                        'ERROR',
                    )
                    return

                cleanup_mountpoint_dir(mountpoint)
                log(f"Cleared stale mount {mountpoint}; continuing with {target_to_mount}.")

        # Check if mounted (may have multiple mount targets)
        current_targets = find_mount_targets(target_to_mount)
        preferred_mount_real = os.path.realpath(mountpoint)

        def _prune_extra_mounts(rounds=6, sleep_s=0.25):
            """
            Remove duplicate mount targets for the same source, keeping only the selected mountpoint.
            This handles late desktop auto-mount races that create suffix mounts (e.g. label1).
            """
            for attempt in range(rounds):
                targets_now = find_mount_targets(target_to_mount)
                extras_now = [t for t in targets_now if os.path.realpath(t) != preferred_mount_real]
                if not extras_now:
                    return
                for mp in extras_now:
                    log(
                        f"Unmounting extra mountpoint {mp} to keep preferred path {mountpoint} "
                        f"(attempt {attempt + 1}/{rounds})..."
                    )
                    res_um = run_command(['umount', mp], sudo=True, check=False)
                    if getattr(res_um, 'returncode', 1) != 0:
                        if attempt + 1 < rounds:
                            log(
                                f"Failed to unmount extra mountpoint {mp}; it may be busy. Retrying...",
                                'WARN'
                            )
                        else:
                            log(
                                f"Failed to unmount extra mountpoint {mp} after {rounds} attempts; it may be busy.",
                                'WARN'
                            )
                        continue
                    cleanup_mountpoint_dir(mp)
                time.sleep(sleep_s)

        if not current_targets:
            # If a prior empty mountpoint dir was left behind, clean it before remount.
            # Non-empty dirs are reused as-is to avoid deleting user data.
            if os.path.isdir(mountpoint) and run_command(['findmnt', '-rn', '-M', mountpoint], check=False).returncode != 0:
                try:
                    if not os.listdir(mountpoint):
                        cleanup_mountpoint_dir(mountpoint)
                except Exception:
                    pass
            log(f"Mounting {target_to_mount} to {mountpoint}...")
            try:
                self._mount_device(
                    target_to_mount,
                    mountpoint,
                    use_fstab=use_fstab_mount,
                    announce_btrfs=True,
                    btrfs_compression_opt=btrfs_compression_opt
                )
            except Exception as e:
                log(f"Mount failed for {target_to_mount} at {mountpoint}: {e}", 'ERROR')
                return
            _prune_extra_mounts()
            log("Mounted.")
        elif mountpoint in current_targets:
            extra = [t for t in current_targets if t != mountpoint]
            if extra:
                log(f"Disk is also mounted at {', '.join(extra)}. Unmounting extra mount(s)...")
                for mp in extra:
                    try:
                        run_command(['umount', mp], sudo=True)
                    except Exception as e:
                        log(f"Failed to unmount {mp}: {e}. It may be in use.", 'WARN')
            _prune_extra_mounts()
            try:
                desired_btrfs_opt = self._effective_btrfs_mount_option(target_to_mount, btrfs_compression_opt)
                self._ensure_btrfs_compression_on_mount(mountpoint, desired_opt=desired_btrfs_opt)
            except Exception as e:
                log(f"Failed to enforce btrfs compression on {mountpoint}: {e}", 'ERROR')
                return
            log(f"Already mounted at {mountpoint}.")
        else:
            log(f"Disk is mounted at {', '.join(current_targets)} (system default). Overriding...")
            try:
                for mp in current_targets:
                    run_command(['umount', mp], sudo=True)
                self._mount_device(
                    target_to_mount,
                    mountpoint,
                    use_fstab=use_fstab_mount,
                    announce_btrfs=True,
                    btrfs_compression_opt=btrfs_compression_opt
                )
                _prune_extra_mounts()
                log(f"Successfully moved mount to {mountpoint}")
            except Exception as e:
                log(f"Failed to override mount: {e}. It may be in use.", 'WARN')

    def do_close(self, arg):
        '''Unmount and lock (if encrypted) a disk: close <name/id> [--force]

        UNDER THE HOOD:
        1.  Unmounting (Encrypted & Plain):
            - With --force, terminates userspace processes holding the filesystem.
            - Flushes the target filesystem, then attempts a normal unmount.
            - Waits for a bounded recovery period if only kernel writeback remains.
            - --force cannot kill kernel tasks stuck in uninterruptible D-state I/O.
            - Treats a vanished source as an unplug and cleans stale mount directories after teardown.
            - Attempts unmount by mapper path (LUKS), source path (Plain), or guessed mountpoint.
            - If target is a whole disk, also unmounts mounted child partitions on that disk.
        2.  Locking (LUKS only):
            - Commands the kernel to wipe encryption keys from RAM.
            - Removes the virtual cleartext device from /dev/mapper/.
        3.  Audit: Checks and displays remaining active mappings for security awareness.
        4.  Rescue: A --force kernel detach is single-attempt and only runs after all
            filesystems and child LUKS mappings have been closed.
        '''
        argv = shlex.split(arg) if arg else []
        force = False
        positional = []
        for tok in argv:
            if tok == '--force':
                force = True
            elif tok.startswith('-'):
                log(f"Unknown option: {tok}", 'ERROR')
                log("Usage: close <name/id> [--force]", 'ERROR')
                return
            else:
                positional.append(tok)

        if len(positional) != 1:
            log("Usage: close <name/id> [--force]", 'ERROR')
            return
        target = positional[0]
        close_state = {
            'flush_timed_out': False,
            'kernel_blocked': False,
            'device_vanished': False,
        }
        flushed_mounts = set()
        flush_timed_out_mounts = set()
        detach_attempted_disks = set()

        def _log_mount_holders(mp):
            """Best-effort diagnostics: show processes holding a busy mount."""
            holder_shown = False
            fuser_bin = shutil.which('fuser')
            if fuser_bin:
                res_f = run_command_hard_timeout(
                    ['fuser', '-vm', mp],
                    3,
                    sudo=True,
                    check=False,
                )
                out_f = (getattr(res_f, 'stdout', '') or '').strip()
                err_f = (getattr(res_f, 'stderr', '') or '').strip()
                combined = "\n".join([x for x in (out_f, err_f) if x]).strip()
                if combined:
                    log(f"Holders for {mp} (fuser -vm):", 'WARN')
                    print(combined)
                    holder_shown = True

            if not holder_shown:
                lsof_bin = shutil.which('lsof')
                if lsof_bin:
                    res_l = run_command_hard_timeout(
                        ['lsof', '+D', mp],
                        3,
                        sudo=True,
                        check=False,
                    )
                    out_l = (getattr(res_l, 'stdout', '') or '').strip()
                    err_l = (getattr(res_l, 'stderr', '') or '').strip()
                    combined = "\n".join([x for x in (out_l, err_l) if x]).strip()
                    if combined:
                        log(f"Holders for {mp} (lsof +D):", 'WARN')
                        print(combined)
                        holder_shown = True

            if not holder_shown:
                log(f"Could not determine mount holders for {mp}.", 'WARN')

        def _collect_mount_holder_pids(mp):
            """Return holder PIDs from fuser/lsof for a mountpoint."""
            pids = []

            fuser_bin = shutil.which('fuser')
            if fuser_bin:
                res_f = run_command_hard_timeout(
                    ['fuser', '-m', mp],
                    3,
                    sudo=True,
                    check=False,
                )
                out_f = (getattr(res_f, 'stdout', '') or '')
                for ln in out_f.splitlines():
                    line = (ln or '').strip()
                    if not line:
                        continue
                    # fuser -m typically returns either:
                    #   /mount/path: 123 456
                    # or just PID tokens depending on version/locale.
                    rhs = line.split(':', 1)[1] if ':' in line else line
                    for tok in rhs.split():
                        m = re.match(r'^(\d+)', tok)
                        if not m:
                            continue
                        try:
                            pids.append(int(m.group(1)))
                        except Exception:
                            pass

            # Do not recursively walk a large/failing filesystem with lsof when
            # fuser was available and simply found no userspace holder.
            if not pids and not fuser_bin:
                lsof_bin = shutil.which('lsof')
                if lsof_bin:
                    res_l = run_command_hard_timeout(
                        ['lsof', '+D', mp],
                        3,
                        sudo=True,
                        check=False,
                    )
                    out_l = (getattr(res_l, 'stdout', '') or '').splitlines()
                    for line in out_l[1:]:
                        cols = line.split()
                        if len(cols) >= 2 and cols[1].isdigit():
                            try:
                                pids.append(int(cols[1]))
                            except Exception:
                                pass

            uniq = []
            seen = set()
            for pid in pids:
                if pid <= 1:
                    continue
                if pid == os.getpid():
                    continue
                if pid in seen:
                    continue
                seen.add(pid)
                uniq.append(pid)
            return uniq

        def _pid_start_time(pid):
            """Return Linux process start time so a reused PID cannot be killed."""
            try:
                fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding='ascii').split()
                return fields[21] if len(fields) > 21 else None
            except (OSError, ValueError):
                return None

        def _force_kill_mount_holders(mp):
            """Kill mount holders with SIGKILL in force mode."""
            pids = _collect_mount_holder_pids(mp)
            if not pids:
                log(f"--force enabled, but no killable holder PIDs found for {mp}.", 'WARN')
                return []
            guarded = {
                pid: _pid_start_time(pid)
                for pid in pids
            }
            guarded = {
                pid: start
                for pid, start in guarded.items()
                if start is not None
            }
            if not guarded:
                log(f"--force could not verify holder PID identities for {mp}; no processes killed.", 'WARN')
                return []
            log(
                f"--force enabled: sending SIGKILL to verified holder PIDs: "
                f"{', '.join(str(p) for p in guarded)}",
                'WARN',
            )
            chunk_size = 64
            verified = [
                pid for pid, start in guarded.items()
                if _pid_start_time(pid) == start
            ]
            for i in range(0, len(verified), chunk_size):
                chunk = verified[i:i+chunk_size]
                run_command(['kill', '-9'] + [str(p) for p in chunk], sudo=True, check=False)
            return verified

        def _dm_mapper_name_from_kname(kname):
            k = str(kname or "").strip()
            if not k:
                return ""
            try:
                p = f"/sys/class/block/{k}/dm/name"
                if os.path.exists(p):
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        v = (f.read() or "").strip()
                        if v:
                            return v
            except Exception:
                pass
            return k

        def _collect_child_crypt_mappers_for_disk(devnode):
            """
            Return mapper names for open crypt targets in a disk subtree.
            """
            dev_real = os.path.realpath(devnode)
            mappers = []
            seen = set()
            res = run_command_hard_timeout(['lsblk', '-nr', '-o', 'KNAME,TYPE', dev_real], 3, check=False)
            for ln in (getattr(res, 'stdout', '') or '').splitlines():
                parts = (ln or '').split()
                if len(parts) < 2:
                    continue
                kname, dtype = parts[0].strip(), parts[1].strip().lower()
                if dtype != 'crypt' or not kname:
                    continue
                mname = _dm_mapper_name_from_kname(kname)
                if not mname or mname in seen:
                    continue
                seen.add(mname)
                mappers.append(mname)
            return mappers

        def _close_child_crypt_mappers_for_disk(devnode, skip_names=None):
            """
            Close open child LUKS mappings under a whole-disk target.
            """
            skip = set(str(x).strip() for x in (skip_names or []) if str(x).strip())
            mappers = _collect_child_crypt_mappers_for_disk(devnode)
            if skip:
                mappers = [m for m in mappers if m not in skip]
            if not mappers:
                return 0
            log(f"Closing child LUKS mappings on disk {devnode}: {', '.join(mappers)}")
            closed = 0
            for mname in mappers:
                mapper_dev = f"/dev/mapper/{mname}"
                # Child mappers can be mounted even when the parent disk itself
                # has no direct mount; unmount their targets before close.
                child_targets = find_mount_targets(mapper_dev)
                child_unmount_failed = False
                for mp in child_targets:
                    if not _try_unmount(mp):
                        child_unmount_failed = True
                        break
                    cleanup_mountpoint_dir(mp)
                if child_unmount_failed:
                    log(f"Skipping close of child mapping {mname} because one or more mount targets are still busy.", 'WARN')
                    continue

                res = run_command_hard_timeout(
                    ['cryptsetup', 'close', mname],
                    8,
                    sudo=True,
                    check=False,
                )
                if getattr(res, 'returncode', 1) == 0:
                    closed += 1
                else:
                    err = (getattr(res, 'stderr', '') or '').strip()
                    if err:
                        log(f"Failed to close child mapping {mname}: {err}", 'WARN')
                    else:
                        log(f"Failed to close child mapping {mname}.", 'WARN')
            return closed

        def _collect_mount_targets_for_device(devnode, include_children=False):
            """
            Collect mount targets for a device; optionally include all descendant block devices.
            This is required when a mapping points to a whole disk but filesystems are mounted
            on child partitions.
            """
            out = []
            seen = set()

            def _add_targets_for(src_dev):
                for mp in find_mount_targets(src_dev):
                    if mp and mp not in seen:
                        seen.add(mp)
                        out.append(mp)

            dev_real = os.path.realpath(devnode)
            _add_targets_for(dev_real)
            if not include_children:
                return out

            # Prefer sysfs child partition walk; avoids potentially blocking device probes.
            child_parts = _sysfs_child_partition_devs(dev_real)
            for child_dev in child_parts:
                if child_dev != dev_real:
                    _add_targets_for(child_dev)

            # Fallback to lsblk subtree only when sysfs did not produce children.
            if not child_parts:
                res_k = run_command_hard_timeout(['lsblk', '-nr', '-o', 'KNAME', dev_real], 3, check=False)
                for ln in (getattr(res_k, 'stdout', '') or '').splitlines():
                    kname = (ln or '').strip()
                    if not kname:
                        continue
                    child_dev = os.path.realpath(f"/dev/{kname}")
                    if child_dev == dev_real:
                        continue
                    _add_targets_for(child_dev)
            return out

        def _top_level_disk(devnode):
            """Resolve any block device to its top-level /dev/<disk>."""
            try:
                bname = _sysfs_block_name(devnode)
                parent = _sysfs_to_parent_disk_name(bname)
                if not parent:
                    return None
                parent_dev = os.path.realpath(f"/dev/{parent}")
                if _sysfs_is_whole_disk(parent_dev):
                    return parent_dev
            except Exception:
                pass
            return None

        def _mount_is_active(mp):
            res = run_command_hard_timeout(
                ['findmnt', '-rn', '-M', mp],
                2,
                check=False,
            )
            return getattr(res, 'returncode', 1) == 0

        def _mount_source(mp):
            res = run_command_hard_timeout(
                ['findmnt', '-rn', '-M', mp, '-o', 'SOURCE'],
                2,
                check=False,
            )
            if getattr(res, 'returncode', 1) != 0:
                return ""
            lines = (getattr(res, 'stdout', '') or '').splitlines()
            if not lines:
                return ""
            # findmnt appends a Btrfs subvolume as /dev/mapper/name[/subvol].
            return re.sub(r'\[[^]]*\]$', '', lines[0].strip())

        def _mount_source_is_present(source):
            """Check a mount source and any device-mapper backing devices via sysfs."""
            if not source.startswith('/dev/') or not os.path.exists(source):
                return not source.startswith('/dev/')

            kname = _sysfs_block_name(source)
            if not kname.startswith('dm-'):
                return True
            slaves_dir = f"/sys/class/block/{kname}/slaves"
            try:
                slaves = os.listdir(slaves_dir)
            except OSError:
                return False
            if not slaves:
                return False
            return all(os.path.exists(f"/dev/{slave}") for slave in slaves)

        def _flush_mount(mp):
            """Flush one mounted filesystem without blocking close indefinitely."""
            if mp in flushed_mounts:
                return True
            flushed_mounts.add(mp)

            res_fs = run_command_hard_timeout(
                ['findmnt', '-rn', '-M', mp, '-o', 'FSTYPE'],
                2,
                check=False,
            )
            fstype = (getattr(res_fs, 'stdout', '') or '').splitlines()
            fstype = fstype[0].strip().lower() if fstype else ""
            if fstype == 'btrfs' and shutil.which('btrfs'):
                command = ['btrfs', 'filesystem', 'sync', mp]
                description = "Btrfs filesystem sync"
            else:
                command = ['sync', '-f', mp]
                description = f"{fstype or 'filesystem'} syncfs flush"

            log(f"Flushing {mp} ({description})...")
            res = run_command_hard_timeout(command, 8, sudo=True, check=False)
            rc = getattr(res, 'returncode', 1)
            if rc == 0:
                return True
            if rc == 124:
                close_state['flush_timed_out'] = True
                flush_timed_out_mounts.add(mp)
                log(
                    f"Filesystem flush timed out for {mp}; kernel writeback may still be outstanding.",
                    'WARN',
                )
            else:
                err = (getattr(res, 'stderr', '') or '').strip()
                detail = f": {err}" if err else ""
                log(f"Filesystem flush failed for {mp}{detail}; continuing to normal unmount.", 'WARN')
            return False

        def _wait_for_kernel_release(mp, seconds=30, retry_unmount=True):
            """Wait for kernel teardown/writeback while monitoring mount and source state."""
            close_state['kernel_blocked'] = True
            log(
                f"No killable userspace holders remain for {mp}; the filesystem is blocked in kernel writeback/I/O.",
                'WARN',
            )
            if force:
                log(
                    "--force cannot kill kernel tasks in uninterruptible D-state; waiting for kernel recovery.",
                    'WARN',
                )
            else:
                log("Waiting for bounded kernel recovery; --force would not bypass D-state I/O.", 'WARN')

            started = time.monotonic()
            next_report = 5
            vanished_reported = False
            while True:
                if not _mount_is_active(mp):
                    cleanup_mountpoint_dir(mp)
                    return True

                elapsed = time.monotonic() - started
                if elapsed >= seconds:
                    break

                source = _mount_source(mp)
                source_missing = bool(source and not _mount_source_is_present(source))
                if source_missing:
                    close_state['device_vanished'] = True
                    if not vanished_reported:
                        log(
                            f"Device source {source} vanished during close; treating this as an unplug event "
                            "and waiting for kernel mount teardown.",
                            'WARN',
                        )
                        vanished_reported = True

                if elapsed >= next_report:
                    remaining = max(0, int(seconds - elapsed))
                    state = "device vanished; kernel teardown pending" if source_missing else "kernel writeback/I/O pending"
                    log(f"Close recovery: {mp} still mounted ({state}); up to {remaining}s remaining.", 'WARN')
                    if retry_unmount:
                        # Retry only when the previous unmount returned. A timed-out
                        # unmount may still be blocked in D-state below its launcher.
                        res_retry = run_command_hard_timeout(
                            ['umount', mp],
                            4,
                            sudo=True,
                            check=False,
                        )
                        if getattr(res_retry, 'returncode', 1) == 0:
                            cleanup_mountpoint_dir(mp)
                            return True
                        if getattr(res_retry, 'returncode', 1) == 124:
                            retry_unmount = False
                            log(
                                f"Unmount retry for {mp} timed out; monitoring only to avoid overlapping "
                                "kernel operations.",
                                'WARN',
                            )
                    next_report += 5
                time.sleep(1)

            if not _mount_is_active(mp):
                cleanup_mountpoint_dir(mp)
                return True
            log(
                f"Close recovery period expired after {seconds}s; {mp} remains mounted in kernel.",
                'ERROR',
            )
            return False

        def _attempt_rescue_for_disk(devnode, reason, allow_detach=False, persistent_hint=None):
            """Detach one already-unmounted whole disk at most once per close operation."""
            disk_dev = _top_level_disk(devnode)
            if not disk_dev or (_lsblk_type(disk_dev) or '').strip().lower() != 'disk':
                return False

            disk_real = os.path.realpath(disk_dev)
            if disk_real in detach_attempted_disks:
                log(f"Detach for {disk_dev} was already attempted; not launching another rescue operation.", 'WARN')
                return False
            detach_attempted_disks.add(disk_real)

            log(f"Close rescue path: {disk_dev} ({reason})", 'WARN')
            targets = _collect_mount_targets_for_device(disk_dev, include_children=True)
            if targets:
                log(
                    f"Refusing kernel detach for {disk_dev}: filesystem remains mounted at "
                    f"{', '.join(targets)}.",
                    'ERROR',
                )
                return False
            child_mappers = _collect_child_crypt_mappers_for_disk(disk_dev)
            if child_mappers:
                log(
                    f"Refusing kernel detach for {disk_dev}: LUKS mappings remain open: "
                    f"{', '.join(child_mappers)}.",
                    'ERROR',
                )
                return False

            if not allow_detach:
                log(f"Rescue completed for {disk_dev} (no kernel detach without --force).", 'WARN')
                return False

            # Prefer caller-provided persistent path; otherwise derive one before detach.
            if not persistent_hint:
                try:
                    pdp = self.find_persistent_path(os.path.basename(disk_dev), type_='disk')
                    if pdp and pdp != '-':
                        persistent_hint = pdp
                except Exception:
                    persistent_hint = None

            # Final step: remove dead/stuck block device from kernel view.
            kname = os.path.basename(disk_dev)
            delete_path = f"/sys/block/{kname}/device/delete"
            if not os.path.exists(delete_path):
                log(f"Rescue could not find device-delete path: {delete_path}", 'WARN')
                return False

            # Re-check immediately before deletion; never detach mounted storage.
            targets = _collect_mount_targets_for_device(disk_dev, include_children=True)
            if targets:
                log(
                    f"Refusing kernel detach for {disk_dev}: mount state changed and now includes "
                    f"{', '.join(targets)}.",
                    'ERROR',
                )
                return False

            res_del = run_command_hard_timeout(
                ['tee', delete_path],
                3,
                input_str="1\n",
                sudo=True,
                check=False,
            )
            if getattr(res_del, 'returncode', 1) == 0:
                log(f"Rescue detached {disk_dev} from kernel. Replug to re-discover.", 'WARN')
                log("Rescue: attempting automatic bus rescan and re-attach...", 'WARN')
                # Trigger broad re-discovery across common buses.
                run_command(['udevadm', 'settle'], sudo=True, check=False, timeout=4)
                run_command(['tee', '/sys/bus/pci/rescan'], input_str="1\n", sudo=True, check=False, timeout=3)
                try:
                    for host in sorted(os.listdir('/sys/class/scsi_host')):
                        scan_path = f"/sys/class/scsi_host/{host}/scan"
                        if os.path.exists(scan_path):
                            run_command(['tee', scan_path], input_str="- - -\n", sudo=True, check=False, timeout=3)
                except Exception:
                    pass
                run_command(['udevadm', 'settle'], sudo=True, check=False, timeout=6)

                # Wait briefly for the expected path to re-appear.
                reattached = False
                for _ in range(20):
                    if persistent_hint and os.path.exists(persistent_hint):
                        new_dev = os.path.realpath(persistent_hint)
                        log(f"Rescue re-attached: {persistent_hint} -> {new_dev}", 'WARN')
                        reattached = True
                        break
                    if os.path.exists(disk_dev):
                        log(f"Rescue re-attached device node: {disk_dev}", 'WARN')
                        reattached = True
                        break
                    time.sleep(0.25)
                if not reattached:
                    if persistent_hint:
                        log(
                            f"Rescan completed, but {persistent_hint} did not reappear yet. "
                            f"Try replug or manual host scan.",
                            'WARN'
                        )
                    else:
                        log(
                            f"Rescan completed, but {disk_dev} did not reappear yet. "
                            f"Try replug or manual host scan.",
                            'WARN'
                        )
                return True

            err = (getattr(res_del, 'stderr', '') or '').strip()
            if err:
                log(f"Rescue failed to detach {disk_dev}: {err}", 'WARN')
            else:
                log(f"Rescue failed to detach {disk_dev}.", 'WARN')
            return False

        def _try_unmount(mp):
            """Stop userspace holders, flush, unmount, then wait for kernel recovery."""
            if not _mount_is_active(mp):
                cleanup_mountpoint_dir(mp)
                return True

            holders = _collect_mount_holder_pids(mp)
            if holders:
                if force:
                    _force_kill_mount_holders(mp)
                    time.sleep(0.5)
                else:
                    log(f"Userspace processes are holding {mp}; normal close will not kill them.", 'WARN')
                    _log_mount_holders(mp)

            _flush_mount(mp)
            log(f"Unmounting {mp}...")
            res_um = run_command_hard_timeout(
                ['umount', mp],
                8,
                sudo=True,
                check=False,
            )
            if getattr(res_um, 'returncode', 1) == 0:
                return True

            err = (getattr(res_um, 'stderr', '') or '').strip()
            rc = int(getattr(res_um, 'returncode', 1) or 1)
            if err:
                log(f"Unmount failed for {mp}: {err}", 'ERROR')
            else:
                log(f"Unmount failed for {mp}.", 'ERROR')
            busy = ('busy' in err.lower()) or (rc == 32)
            remaining_holders = _collect_mount_holder_pids(mp)
            if remaining_holders:
                _log_mount_holders(mp)
                if force and busy:
                    log(
                        f"Userspace holders remain after --force for {mp}; refusing kernel detach while mounted.",
                        'ERROR',
                    )
                return False

            if busy or rc == 124 or mp in flush_timed_out_mounts:
                return _wait_for_kernel_release(mp, retry_unmount=(rc != 124))
            return False

        # Support discovery IDs (#N) from the latest `list` output.
        if target.startswith('#') and target[1:].isdigit():
            rid = target[1:]
            dev_from_id = self.id_cache.get(rid)
            if not dev_from_id:
                log(f"Unknown discovery ID: {target}. Run 'list' first to refresh IDs.", 'ERROR')
                return
            dev_from_id = os.path.realpath(dev_from_id)

            # If this ID corresponds to an existing friendly mapping, delegate to close <name>
            # so LUKS mapper-name semantics (cryptsetup close <name>) are preserved.
            self.mappings = read_luks_map()
            mapped_name = None
            for n, p in self.mappings.items():
                try:
                    if os.path.realpath(p) == dev_from_id:
                        mapped_name = n
                        break
                except Exception:
                    continue
            if mapped_name:
                delegated = f"{mapped_name} --force" if force else mapped_name
                return self.do_close(delegated)

            # Otherwise perform a direct close by device path.
            if self._block_if_root_drive(dev_from_id, f"close {target}"):
                return

            dev_from_id_type = (_lsblk_type(dev_from_id) or '').strip().lower()
            dev_from_id_persistent = None
            if dev_from_id_type == 'disk':
                try:
                    pdp = self.find_persistent_path(os.path.basename(dev_from_id), type_='disk')
                    if pdp and pdp != '-':
                        dev_from_id_persistent = pdp
                except Exception:
                    dev_from_id_persistent = None
            if dev_from_id_type != 'disk' and _sysfs_is_whole_disk(dev_from_id):
                dev_from_id_type = 'disk'
            targets = _collect_mount_targets_for_device(
                dev_from_id,
                include_children=(dev_from_id_type == 'disk')
            )
            unmounted_targets = []
            for mp in targets:
                if not _try_unmount(mp):
                    log("Close aborted: target is still mounted/in use.", 'ERROR')
                    return
                unmounted_targets.append(mp)
            for mp in unmounted_targets:
                cleanup_mountpoint_dir(mp)

            # For whole-disk ID targets, also close any open child LUKS mappings.
            if dev_from_id_type == 'disk':
                _close_child_crypt_mappers_for_disk(dev_from_id)
                remaining_mappers = _collect_child_crypt_mappers_for_disk(dev_from_id)
                if remaining_mappers:
                    log(
                        f"Close aborted: child LUKS mappings remain open: {', '.join(remaining_mappers)}.",
                        'ERROR',
                    )
                    return

                if force and (close_state['flush_timed_out'] or close_state['kernel_blocked']):
                    _attempt_rescue_for_disk(
                        dev_from_id,
                        "filesystem recovered after stalled kernel I/O",
                        allow_detach=True,
                        persistent_hint=dev_from_id_persistent,
                    )

            # For non-disk ID targets, do not implicitly lock LUKS.
            # This path is used heavily for payload/embedded filesystem rows; keep it as unmount-only.
            d_type = (_lsblk_type(dev_from_id) or '').strip().lower()
            if d_type == 'crypt' and dev_from_id_type != 'disk':
                if unmounted_targets:
                    log("Unmounted target filesystem. LUKS mapping left open (ID close is unmount-only).")
                else:
                    log("Target is a crypt mapping but nothing was mounted. LUKS mapping left open (ID close is unmount-only).")
            elif not unmounted_targets:
                log("Target is not mounted and has no closable crypt mapping.")
            return

        name = target

        # Never attempt unmount/close operations on the system root drive.
        self.mappings = read_luks_map()
        src_disk_for_rescue = None
        src_persistent_for_rescue = None
        if name in self.mappings:
            real_src = os.path.realpath(self.mappings[name])
            if self._block_if_root_drive(real_src, f"close {name}"):
                return
            try:
                mapped_src = str(self.mappings[name] or "")
                if mapped_src.startswith('/dev/disk/by-id/'):
                    src_persistent_for_rescue = mapped_src
            except Exception:
                src_persistent_for_rescue = None
            try:
                if _sysfs_is_whole_disk(real_src) or (_lsblk_type(real_src) or '').strip().lower() == 'disk':
                    src_disk_for_rescue = real_src
            except Exception:
                src_disk_for_rescue = None
            if src_disk_for_rescue and not src_persistent_for_rescue:
                try:
                    pdp = self.find_persistent_path(os.path.basename(src_disk_for_rescue), type_='disk')
                    if pdp and pdp != '-':
                        src_persistent_for_rescue = pdp
                except Exception:
                    src_persistent_for_rescue = None

        mapper_path = f"/dev/mapper/{name}"
        mount_guess = f"/media/{os.environ.get('USER', 'root')}/{name}"

        # Unmount
        unmounted = False
        unmounted_targets = []
        # 1. Try by mapper
        targets = find_mount_targets(mapper_path)
        if targets:
            for mp in targets:
                if not _try_unmount(mp):
                    log("Close aborted: mapper target is still mounted/in use.", 'ERROR')
                    return
                unmounted_targets.append(mp)
            unmounted = True

        # 2. Try by source path (for non-LUKS)
        if not unmounted:
            self.mappings = read_luks_map()
            if name in self.mappings:
                src = self.mappings[name]
                src_type = 'disk' if _sysfs_is_whole_disk(src) else (_lsblk_type(src) or '').strip().lower()
                targets = _collect_mount_targets_for_device(
                    src,
                    include_children=(src_type == 'disk')
                )
                if targets:
                    for mp in targets:
                        if not _try_unmount(mp):
                            log("Close aborted: source target is still mounted/in use.", 'ERROR')
                            return
                        unmounted_targets.append(mp)
                    unmounted = True

        # 3. Try by guess
        if not unmounted:
            if run_command(['findmnt', '-rn', '-M', mount_guess], check=False).returncode == 0:
                 if not _try_unmount(mount_guess):
                     log("Close aborted: guessed target is still mounted/in use.", 'ERROR')
                     return
                 unmounted_targets.append(mount_guess)
                 unmounted = True

        # Cleanup mountpoint dir(s) (best-effort)
        for mp in unmounted_targets:
            cleanup_mountpoint_dir(mp)

        # If this mapping resolves to a whole disk, close all open child LUKS mappings too.
        disk_src = None
        child_closed = 0
        try:
            self.mappings = read_luks_map()
            src_for_name = self.mappings.get(name)
            if src_for_name:
                src_type = 'disk' if _sysfs_is_whole_disk(src_for_name) else (_lsblk_type(src_for_name) or '').strip().lower()
                if src_type == 'disk':
                    disk_src = os.path.realpath(src_for_name)
        except Exception:
            disk_src = None
        if disk_src:
            child_closed = _close_child_crypt_mappers_for_disk(disk_src, skip_names={name})

        # Close
        if os.path.exists(mapper_path):
            log(f"Closing mapping {name}...")
            res_close = run_command_hard_timeout(
                ['cryptsetup', 'close', name],
                8,
                sudo=True,
                check=False,
            )
            if getattr(res_close, 'returncode', 1) != 0:
                err = (getattr(res_close, 'stderr', '') or '').strip()
                detail = f": {err}" if err else ""
                log(f"Failed to close mapping {name}{detail}.", 'ERROR')
                return
            log("Closed.")
        else:
            if unmounted_targets:
                log("Unmounted.")
            elif child_closed:
                log(f"Closed {child_closed} child LUKS mapping{'s' if child_closed != 1 else ''}.")
            else:
                log("Mapping not open or already closed.")

        if force and src_disk_for_rescue and (close_state['flush_timed_out'] or close_state['kernel_blocked']):
            remaining_mappers = _collect_child_crypt_mappers_for_disk(src_disk_for_rescue)
            remaining_mounts = _collect_mount_targets_for_device(src_disk_for_rescue, include_children=True)
            if remaining_mappers:
                log(
                    f"Skipping kernel detach: LUKS mappings remain open: {', '.join(remaining_mappers)}.",
                    'WARN',
                )
            elif remaining_mounts:
                log(
                    f"Skipping kernel detach: filesystems remain mounted at {', '.join(remaining_mounts)}.",
                    'WARN',
                )
            else:
                _attempt_rescue_for_disk(
                    src_disk_for_rescue,
                    "filesystem recovered after stalled kernel I/O",
                    allow_detach=True,
                    persistent_hint=src_persistent_for_rescue,
                )

    def do_label(self, arg):
        '''Get or set the filesystem label of an OPEN disk: label <name> [new_label] [--fstab]

        UNDER THE HOOD:
        1.  Validation: Verifies that the disk is currently open/unlocked.
        2.  Identification: Queries the filesystem type (ext4, xfs, etc.) via 'lsblk'.
        3.  Labeling:
            - ext4: Uses 'e2label' on the active device.
            - xfs: Requires a temporary unmount, then uses 'xfs_admin -L', then remounts.
        4.  Refresh: Executes 'udevadm trigger' to force tools like 'lsblk' to see the change.
        5.  Optional fstab update (--fstab):
            - Removes old label-based /etc/fstab entry.
            - Adds UUID-based entry with mountpoint /mnt/<new_label>.

        The label is written directly to the disk hardware and persists across different computers.
        '''
        parser = CmdArgumentParser(prog='label', add_help=False)
        parser.add_argument('name')
        parser.add_argument('new_label', nargs='?')
        parser.add_argument('--fstab', action='store_true', help='Add a new LABEL= fstab entry for the new label')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.name
        new_label = args.new_label
        write_fstab = args.fstab

        if write_fstab and not new_label:
            log("Usage: label <name> <new_label> [--fstab]", 'ERROR')
            return

        # Resolve target device
        target_dev = None
        mapper_path = f"/dev/mapper/{name}"

        if os.path.exists(mapper_path):
            target_dev = mapper_path
        else:
            self.mappings = read_luks_map()
            if name in self.mappings:
                src = self.mappings[name]
                if not os.path.exists(src):
                    log(f"Device for mapping '{name}' is missing: {src}", 'ERROR')
                    return

                devnode = os.path.realpath(src)
                # Check if it's LUKS
                try:
                    res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
                    if res.returncode == 0:
                        log(f"Mapping '{name}' is LUKS but not open. Please 'open {name}' first.", 'ERROR')
                        return
                    # if returncode != 0, it's not LUKS, which is what we want for a plain disk
                except:
                    pass

                target_dev = devnode
            else:
                log(f"Device not found or unknown mapping: {name}", 'ERROR')
                return

        if self._block_if_root_drive(target_dev, f"label {name}"):
            return

        # Get info
        try:
            cmd = ['lsblk', '-J', '-o', 'FSTYPE,MOUNTPOINT,LABEL', target_dev]
            res = run_command(cmd, capture_output=True)
            data = json.loads(res.stdout)
            dev = data.get('blockdevices', [{}])[0]
            fstype = dev.get('fstype')
            mountpoint = dev.get('mountpoint')
            current_label = dev.get('label')
        except Exception as e:
            log(f"Failed to inspect device: {e}", 'ERROR')
            return

        if not new_label:
            print(f"Label for {name} ({fstype}): {current_label if current_label else '<none>'}")
            return

        try:
            new_label = validate_filesystem_label(new_label, fstype)
        except ValueError as exc:
            log(f"Invalid filesystem label: {exc}", 'ERROR')
            return

        label_changed = False
        remount_needed = False
        old_label = current_label or ""

        if current_label == new_label:
            log("Label is already set to that value.")
        else:
            log(f"Changing label: '{current_label}' -> '{new_label}' ({fstype})")

            if fstype == 'ext4':
                try:
                    run_command(['e2label', target_dev, new_label], sudo=True)
                    run_command(['udevadm', 'trigger', '--name-match=' + target_dev], sudo=True)
                    run_command(['udevadm', 'settle'], sudo=True)
                    label_changed = True
                    log("Label updated.")
                except Exception:
                    return
            elif fstype == 'xfs':
                # XFS requires unmount
                targets = find_mount_targets(target_dev)
                if targets:
                    log(f"XFS requires unmounting to label. Unmounting {', '.join(targets)}...")
                    try:
                        for mp in targets:
                            run_command(['umount', mp], sudo=True)
                        remount_needed = True
                        # Best effort: remember prior mountpoint if we don't have an fstab entry.
                        mountpoint = mountpoint or targets[0]
                    except Exception as e:
                        log(f"Failed to unmount: {e}", 'ERROR')
                        return

                try:
                    run_command(['xfs_admin', '-L', new_label, target_dev], sudo=True)
                    run_command(['udevadm', 'trigger', '--name-match=' + target_dev], sudo=True)
                    run_command(['udevadm', 'settle'], sudo=True)
                    label_changed = True
                    log("Label updated.")
                except Exception:
                    return
            else:
                log(f"Unsupported filesystem for labeling: {fstype}", 'ERROR')
                return

        if label_changed or write_fstab:
            if write_fstab and not label_changed:
                log("Label is unchanged; ensuring /etc/fstab entry is present for this label.")
            try:
                removed, added, new_mp = self._update_fstab_for_label_change(
                    target_dev=target_dev,
                    old_label=old_label,
                    new_label=new_label,
                    fstype=fstype,
                    add_entry=write_fstab
                )
                if removed:
                    log(f"Removed {removed} old fstab entr{'y' if removed == 1 else 'ies'} for previous label.")
                if write_fstab:
                    if added:
                        log(f"Added new fstab entry: UUID=<fsuuid> -> {new_mp}")
                    else:
                        log("No new fstab entry was added.")
            except Exception as e:
                log(f"Failed to update /etc/fstab after relabel: {e}", 'ERROR')

        if remount_needed:
            fallback_mp = mountpoint or f"/media/{os.environ.get('USER', 'root')}/{new_label}"
            final_mp, use_fstab_mount, fstab_entry = self._select_mountpoint_for_device(
                target_dev,
                fallback_mp,
                preferred_label=new_label
            )
            if use_fstab_mount:
                log(f"Remounting via fstab: {fstab_entry['spec']} -> {final_mp}")
            else:
                log(f"Remounting {final_mp}...")
            try:
                self._mount_device(target_dev, final_mp, use_fstab=use_fstab_mount)
            except Exception as e:
                log(f"Failed to remount: {e}. You may need to mount manually.", 'ERROR')

    def do_remount(self, arg):
        '''Remount an OPEN disk to its label mountpoint: remount <name>

        This fixes "mounted twice" and "data1/data2 suffix" issues by moving the mount
        to the canonical path: /mnt/<label> when the device has an /etc/fstab entry;
        otherwise /media/$USER/<label>.

        SAFETY RULES:
        - Refuses if the target mountpoint is already mounted by a different device.
        - Refuses if the target directory exists, is not a mountpoint, and is non-empty.
        - Refuses only when neither /etc/fstab entry nor filesystem LABEL is available.

        UNDER THE HOOD:
        1.  Resolve Device: Uses /dev/mapper/<name> if present, otherwise the mapped source path.
            If the mapping is LUKS and not OPEN, it refuses.
        2.  Target Mountpoint:
            - If /etc/fstab entry exists, enforce /mnt/<label> (and update the fstab mountpoint if needed).
            - Otherwise, fall back to /media/$USER/<label>.
        3.  Preflight: Validates selected mountpoint is safe to use.
        4.  Unmount: Unmounts all current mount targets for the device (if any).
        5.  Cleanup: Removes empty old mountpoint directories under /media/$USER (best-effort rmdir).
        6.  Mount: Uses fstab mount when present; otherwise direct mount to LABEL path.
            For btrfs, default remount policy is HDD => compress-force=zstd:12; non-HDD => no compression option.
            Override per command with --compress=<mode> or --compress-force=<mode>.
        '''
        parser = CmdArgumentParser(prog='remount', add_help=False)
        parser.add_argument('name')
        cgroup = parser.add_mutually_exclusive_group()
        cgroup.add_argument('--compress', dest='compress', metavar='MODE')
        cgroup.add_argument('--compress-force', dest='compress_force', metavar='MODE')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.name
        try:
            btrfs_compression_opt = self._normalize_btrfs_compression_override(
                compress=args.compress,
                compress_force=args.compress_force
            )
        except ValueError as e:
            log(str(e), 'ERROR')
            log("Usage: remount <name> [--compress MODE | --compress-force MODE]", 'ERROR')
            return

        # Resolve target device (must be OPEN if it's LUKS).
        target_dev = None
        mapper_path = f"/dev/mapper/{name}"
        if os.path.exists(mapper_path):
            target_dev = mapper_path
        else:
            self.mappings = read_luks_map()
            if name not in self.mappings:
                log(f"Device not found or unknown mapping: {name}", 'ERROR')
                return

            src = self.mappings[name]
            if not os.path.exists(src):
                log(f"Device for mapping '{name}' is missing: {src}", 'ERROR')
                return

            devnode = os.path.realpath(src)
            try:
                res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
                if res.returncode == 0:
                    log(f"Mapping '{name}' is LUKS but not open. Please 'open {name}' first.", 'ERROR')
                    return
            except:
                pass

            target_dev = devnode

        if self._block_if_root_drive(target_dev, f"remount {name}"):
            return

        # Preferred mountpoint: fstab entry first, otherwise /media/$USER/<label>.
        label = ""
        try:
            res_b = run_command(['blkid', '-o', 'value', '-s', 'LABEL', target_dev], sudo=True, check=False)
            label = (getattr(res_b, 'stdout', '') or '').strip()
        except:
            pass
        fs_type = self._detect_fstype(target_dev)

        fallback_mountpoint = f"/media/{os.environ.get('USER', 'root')}/{label}" if label else ""
        fstab_entry = self._find_fstab_entry_for_device(target_dev, preferred_label=label or None)
        use_fstab_mount = bool(fstab_entry)

        if use_fstab_mount:
            if label:
                desired_mountpoint = f"/mnt/{label}"
                desired_opts = self._desired_fstab_options(label, fs_type, target_dev=target_dev)
                need_mountpoint_update = (fstab_entry.get('mountpoint') != desired_mountpoint)
                need_opts_update = (str(fstab_entry.get('opts') or '') != desired_opts)
                if need_mountpoint_update or need_opts_update:
                    try:
                        self._update_fstab_mountpoint(fstab_entry, desired_mountpoint, new_opts=desired_opts)
                        if need_mountpoint_update and need_opts_update:
                            log(f"Updated fstab mountpoint/options for remount: {fstab_entry['spec']} -> {desired_mountpoint} ({desired_opts})")
                        elif need_mountpoint_update:
                            log(f"Updated fstab mountpoint for remount: {fstab_entry['spec']} -> {desired_mountpoint}")
                        else:
                            log(f"Updated fstab options for remount: {fstab_entry['spec']} -> {desired_opts}")
                    except Exception as e:
                        log(f"Failed to update fstab entry for remount: {e}", 'ERROR')
                        return
                new_mountpoint = desired_mountpoint
            else:
                new_mountpoint = fstab_entry['mountpoint']
            log(f"Using fstab mount for remount: {fstab_entry['spec']} -> {new_mountpoint}")
        else:
            if not label:
                log(f"REMOUNT BLOCKED: {name} has no filesystem label and no fstab entry.", 'ERROR')
                log("Set a label with: label <name> <new_label> or add an /etc/fstab entry.", 'ERROR')
                return
            new_mountpoint = fallback_mountpoint

        # Safety: is target mountpoint already in use by another device?
        res_check = run_command(['findmnt', '-rn', '-M', new_mountpoint], check=False)
        if res_check.returncode == 0:
            res_src = run_command(['findmnt', '-rn', '-M', new_mountpoint, '-o', 'SOURCE'], check=False)
            current_src = os.path.realpath(res_src.stdout.strip()) if res_src.stdout.strip() else ""
            if current_src and current_src != os.path.realpath(target_dev):
                log(f"REMOUNT BLOCKED: Mountpoint {new_mountpoint} is already in use by {current_src}.", 'ERROR')
                return

        # Figure out where it's mounted now (may be multiple targets).
        current_targets = find_mount_targets(target_dev)

        if new_mountpoint in current_targets:
            extra = [t for t in current_targets if t != new_mountpoint]
            if not extra:
                try:
                    desired_btrfs_opt = self._effective_btrfs_mount_option(target_dev, btrfs_compression_opt)
                    self._ensure_btrfs_compression_on_mount(new_mountpoint, desired_opt=desired_btrfs_opt)
                except Exception as e:
                    log(f"Failed to enforce btrfs compression on {new_mountpoint}: {e}", 'ERROR')
                    return
                log(f"Already mounted at {new_mountpoint}.")
                return

            log(f"Disk is also mounted at {', '.join(extra)}. Unmounting extra mount(s)...")
            try:
                for mp in extra:
                    run_command(['umount', mp], sudo=True)
                    cleanup_mountpoint_dir(mp)
            except Exception as e:
                log(f"Failed to unmount extra mount(s): {e}", 'ERROR')
                return
            try:
                desired_btrfs_opt = self._effective_btrfs_mount_option(target_dev, btrfs_compression_opt)
                self._ensure_btrfs_compression_on_mount(new_mountpoint, desired_opt=desired_btrfs_opt)
            except Exception as e:
                log(f"Failed to enforce btrfs compression on {new_mountpoint}: {e}", 'ERROR')
            return

        if current_targets:
            log(f"Unmounting {', '.join(current_targets)} for remount...")
            try:
                for mp in current_targets:
                    run_command(['umount', mp], sudo=True)
                    if mp != new_mountpoint:
                        cleanup_mountpoint_dir(mp)
            except Exception as e:
                log(f"Failed to unmount existing mount(s): {e}", 'ERROR')
                return

        # Target dir exists but isn't a mountpoint; it must be empty to be safe to mount over.
        if os.path.exists(new_mountpoint):
            if run_command(['findmnt', '-rn', '-M', new_mountpoint], check=False).returncode != 0:
                try:
                    if os.path.isdir(new_mountpoint) and os.listdir(new_mountpoint):
                        log(f"REMOUNT BLOCKED: Directory {new_mountpoint} exists and is not empty.", 'ERROR')
                        return
                except Exception as e:
                    log(f"REMOUNT BLOCKED: Unable to inspect {new_mountpoint}: {e}", 'ERROR')
                    return

        log(f"Mounting {target_dev} to {new_mountpoint}...")
        try:
            self._mount_device(
                target_dev,
                new_mountpoint,
                use_fstab=use_fstab_mount,
                announce_btrfs=True,
                btrfs_compression_opt=btrfs_compression_opt
            )
            log(f"Remounted successfully at {new_mountpoint}")
        except Exception as e:
            log(f"Failed to mount at {new_mountpoint}: {e}", 'ERROR')
