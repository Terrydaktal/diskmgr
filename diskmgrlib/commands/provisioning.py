"""Filesystem and partition provisioning commands."""

import argparse
import cmd
import datetime
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from ..runtime import Colors, LUKS_HEADER_BACKUP_DIR, LUKS_PBKDF_DEFAULT_MEMORY_KIB, LUKS_PBKDF_DEFAULT_MEMORY_LABEL, LUKS_PBKDF_DEFAULT_THREADS, LUKS_PBKDF_DEFAULT_TIME, PASSGEN_BIN, _cmd_log_close, _cmd_log_open, _cmd_log_write, _find_tool_or_common_paths, _fmt_hms, log, popen_command, run_command
from ..devices import _lsblk_fstype, _lsblk_partitions, _lsblk_type
from ..mounts import find_mount_targets
from ..mappings import (
    read_luks_map,
    update_luks_map,
    validate_mapping_name,
    validate_persistent_target,
)
from ..safety import (
    safe_mount_path,
    validate_absolute_path,
    validate_filesystem_label,
)
from ..shell_core import CmdArgumentParser


class ProvisioningCommands:

    def do_convert(self, arg):
        '''Convert ext4 -> btrfs in place (no data copy): convert <name/id>

        Uses btrfs-convert on an UNMOUNTED ext4 filesystem.
        - Plain ext4 targets are supported directly.
        - If target is crypto_LUKS, diskmgr tries to resolve the open payload device
          (e.g. /dev/mapper/<name> or a crypt child) and convert that.
        '''
        parser = CmdArgumentParser(prog='convert', add_help=False)
        parser.add_argument('target', help='Mapping name or discovery ID (#N)')
        proc = None
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        target_in = args.target
        resolved = None
        if target_in.startswith('#') and target_in[1:].isdigit():
            # For convert, allow IDs from the full list row cache (disk/part/crypt),
            # not only the map/unmapped discovery subset.
            rid = target_in[1:]
            resolved = self.id_cache.get(rid)
            if not resolved:
                # Backward-compatible fallback to discovery-ID resolver.
                resolved = self.resolve_target(target_in, allow_id=True)
                if not resolved:
                    log(f"Unknown discovery ID: '{target_in}'. Run 'list' first to refresh IDs.", 'ERROR')
                    return
        else:
            resolved = self.resolve_target(target_in, allow_id=True)
        if not resolved:
            log(f"Unknown target: '{target_in}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        real_target = os.path.realpath(resolved)
        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        self.mappings = read_luks_map()
        mapping_name = target_in if target_in in self.mappings else None

        convert_dev = None
        convert_hint = ""
        fstype = (_lsblk_fstype(real_target) or "").strip().lower()
        dev_type = (_lsblk_type(real_target) or "").strip().lower()

        def _lsblk_rows(dev_path):
            rows = []
            res = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE,FSTYPE', dev_path], check=False)
            for raw in (getattr(res, 'stdout', '') or '').splitlines():
                line = raw.strip()
                if not line:
                    continue
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                nm = parts[0].strip()
                ty = parts[1].strip().lower()
                fs = parts[2].strip().lower() if len(parts) >= 3 else ""
                rows.append({'name': nm, 'type': ty, 'fstype': fs})
            return rows

        if fstype == 'ext4':
            convert_dev = real_target
            convert_hint = "direct ext4 target"
        elif fstype == 'crypto_luks':
            payload_candidates = []

            if mapping_name:
                mapper_path = f"/dev/mapper/{mapping_name}"
                if os.path.exists(mapper_path):
                    mapper_real = os.path.realpath(mapper_path)
                    mapper_fs = (_lsblk_fstype(mapper_real) or "").strip().lower()
                    if mapper_fs == 'ext4':
                        payload_candidates.append((mapper_real, f"/dev/mapper/{mapping_name}"))

            for row in _lsblk_rows(real_target):
                if row.get('type') != 'crypt':
                    continue
                fs = (row.get('fstype') or "").strip().lower()
                if fs != 'ext4':
                    continue
                cdev = os.path.realpath(f"/dev/{row['name']}")
                payload_candidates.append((cdev, f"/dev/{row['name']}"))

            uniq = {}
            for path, hint in payload_candidates:
                uniq[path] = hint
            payload_candidates = [(p, h) for p, h in uniq.items()]

            if len(payload_candidates) == 1:
                convert_dev, convert_hint = payload_candidates[0]
            elif len(payload_candidates) > 1:
                log("Target is LUKS with multiple open ext4 payload candidates; refusing ambiguous conversion.", 'ERROR')
                for p, h in payload_candidates:
                    log(f"  candidate: {h} ({p})", 'ERROR')
                log("Map and convert the exact payload target explicitly.", 'ERROR')
                return
            else:
                log("Target is crypto_LUKS but no open ext4 payload device was detected.", 'ERROR')
                log("Open the LUKS container and ensure its payload is ext4 before converting.", 'ERROR')
                return
        elif dev_type == 'disk':
            parts = _lsblk_partitions(real_target)
            ext_parts = [p for p in parts if (p.get('fstype') or '').strip().lower() == 'ext4']
            if len(ext_parts) == 1:
                convert_dev = os.path.realpath(f"/dev/{ext_parts[0]['name']}")
                convert_hint = f"single ext4 partition /dev/{ext_parts[0]['name']}"
            elif len(ext_parts) > 1:
                log("Disk has multiple ext4 partitions; map and convert a single partition target explicitly.", 'ERROR')
                return
            else:
                log(f"No ext4 filesystem found on {real_target}.", 'ERROR')
                return
        else:
            log(f"Unsupported target type/fstype for convert: type={dev_type or '-'}, fstype={fstype or '-'}", 'ERROR')
            return

        convert_dev = os.path.realpath(convert_dev)
        if not os.path.exists(convert_dev):
            log(f"Resolved convert target does not exist: {convert_dev}", 'ERROR')
            return

        if self._block_if_root_drive(convert_dev, f"convert {target_in}"):
            return

        # convert requires an unmounted source filesystem.
        targets = find_mount_targets(convert_dev)
        if targets:
            log(f"OPERATION BLOCKED: {convert_dev} is mounted at {', '.join(targets)}. Unmount/close it first.", 'ERROR')
            return

        final_fs = (_lsblk_fstype(convert_dev) or "").strip().lower()
        if final_fs != 'ext4':
            log(f"Resolved target is '{final_fs or 'unknown'}', but convert currently supports ext4 -> btrfs only.", 'ERROR')
            return

        print(f"Converting: {Colors.BOLD}{convert_dev}{Colors.ENDC} ({convert_hint})")
        if not self.extensive_confirm(f"convert {target_in} ({convert_dev})", destructive=False):
            return

        log_path = _cmd_log_open("convert")
        if log_path:
            print(f"Log: {log_path}")
        start_ts = time.time()
        proc = None

        try:
            btrfs_convert_bin = _find_tool_or_common_paths('btrfs-convert', [
                '/usr/sbin/btrfs-convert',
                '/sbin/btrfs-convert',
                '/usr/local/sbin/btrfs-convert',
                '/usr/bin/btrfs-convert',
                '/bin/btrfs-convert',
            ])
            if btrfs_convert_bin is None:
                log("btrfs-convert not found. Install 'btrfs-progs' and retry.", 'ERROR')
                return

            e2fsck_bin = _find_tool_or_common_paths('e2fsck', [
                '/usr/sbin/e2fsck',
                '/sbin/e2fsck',
                '/usr/local/sbin/e2fsck',
                '/usr/bin/e2fsck',
                '/bin/e2fsck',
            ])
            if not e2fsck_bin:
                log("e2fsck is required before conversion; install e2fsprogs and retry.", 'ERROR')
                return
            log(f"Running pre-conversion fsck: {e2fsck_bin} -f -p {convert_dev}")
            res_ck = run_command([e2fsck_bin, '-f', '-p', convert_dev], sudo=True, capture_output=True, check=False)
            if (getattr(res_ck, 'stdout', '') or '').strip():
                print((getattr(res_ck, 'stdout', '') or '').rstrip())
            if (getattr(res_ck, 'stderr', '') or '').strip():
                print((getattr(res_ck, 'stderr', '') or '').rstrip(), file=sys.stderr)
            if getattr(res_ck, 'returncode', 0) not in (0, 1):
                log(f"Pre-conversion fsck failed with exit code {res_ck.returncode}. Aborting conversion.", 'ERROR')
                return

            log(f"Running in-place conversion (streaming output): {btrfs_convert_bin} {convert_dev}")
            _cmd_log_write(f"CMD: {btrfs_convert_bin} {convert_dev}")
            proc = popen_command(
                [btrfs_convert_bin, convert_dev],
                sudo=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        # Keep output live so conversions continue safely over flaky SSH.
                        print(line, end='', flush=True)
                        _cmd_log_write(line.rstrip("\n"))
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
            rc = proc.wait()
            proc = None
            _cmd_log_write(f"RC: {rc}")

            if rc != 0:
                log(f"Conversion failed with exit code {rc}.", 'ERROR')
                return

            log("Conversion complete: ext4 -> btrfs.")
            log("Rollback data is kept by btrfs-convert. Avoid btrfs balance if you may need rollback.", 'WARN')
        finally:
            if proc is not None and proc.poll() is None:
                log("Stopping interrupted btrfs-convert child process.", 'WARN')
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            _cmd_log_close()

    def do_format(self, arg):
        '''Format a superfloppy disk/partition volume: format <name/id> [options]

        Note: You must 'map' a disk first to give it a name before initializing it.

        NUANCES & SCOPE:
        1. Running format on a Partition (e.g., sda2)
           - Formats inside the existing partition boundary (plain or LUKS + payload FS).
           - Other partitions on the disk are untouched.

        2. Running format on a Whole Disk (e.g., sda)
           - Creates a superfloppy-style volume directly on the disk (plain or LUKS + payload FS).
           - Refuses if the disk already has a partition table (non-destructive policy).
           - To wipe partition metadata first, use: erase <name>

        Options:
          --fs <ext4|xfs|btrfs|fat32>   Filesystem type (default: ext4)
          --label <label>   Set a different internal filesystem label (other than <name>)
          --luks            Encrypt target first with LUKS2, then format payload filesystem.
                            PBKDF defaults: argon2id, memory=4GiB, threads=4, time=8.
          --reformat-existing
                            Explicitly allow replacement of an existing filesystem/signature.
                            Requires an additional exact UUID or label confirmation.
          --detached-header [FILE]
                            Store LUKS header detached from the target device.
                            If FILE is omitted: ~/.local/share/diskmgr/<name>

        UNDER THE HOOD:
        1.  Safety: Probes the target and every child with wipefs/blkid, checks all
            mounts, swaps, kernel holders, and mapper/RAID memberships before asking
            for confirmation. Probe errors fail closed.
        2.  Disk Type Policy:
            - If target is a whole disk, it must be unpartitioned (no GPT/MBR table present).
            - If target is a partition, format is applied directly within that partition.
            - Existing content requires --reformat-existing and an exact UUID/label.
        3.  LUKS Format (only when --luks is used):
            - Uses 'passgen' to generate a master key.
            - Runs 'cryptsetup luksFormat' with LUKS2 encryption
              (and --header FILE when --detached-header is used).
            - Opens the container as /dev/mapper/<name>.
        4.  Filesystem:
            - Plain mode (default): formats target directly with ext4, xfs, btrfs, or fat32.
            - --luks mode: formats the opened mapper payload with ext4, xfs, btrfs, or fat32.
            - (ext4 only): Reclaims the 5% reserved space for root using 'tune2fs -m 0'.
        5.  Persistence: Adds the new disk's PDP to diskmap.tsv automatically (best-effort).

        Note: This is a DESTRUCTIVE operation. You must type the resolved device,
        persistent path, and PCI path to proceed.
        '''
        parser = CmdArgumentParser(prog='format', add_help=False)
        parser.add_argument('args', nargs=1, help='<name>')
        parser.add_argument(
            '--fs', default='ext4', choices=['ext4', 'xfs', 'btrfs', 'fat32', 'exfat']
        )
        parser.add_argument('--label', help='Filesystem label')
        parser.add_argument('--luks', action='store_true', help='Encrypt target with LUKS2 before mkfs')
        parser.add_argument(
            '--reformat-existing',
            action='store_true',
            help='Allow replacement of detected existing content after UUID/label confirmation',
        )
        parser.add_argument(
            '--detached-header',
            nargs='?',
            const='__DEFAULT_DETACHED_HEADER__',
            metavar='[FILE]',
            help='Use detached LUKS header file (default: ~/.local/share/diskmgr/<name>)'
        )

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        if args.detached_header and not args.luks:
            log("--detached-header requires --luks.", 'ERROR')
            return

        tool_specs = {
            'ext4': (
                ('mkfs.ext4', ('/usr/sbin/mkfs.ext4', '/sbin/mkfs.ext4', '/usr/bin/mkfs.ext4')),
                ('tune2fs', ('/usr/sbin/tune2fs', '/sbin/tune2fs', '/usr/bin/tune2fs')),
            ),
            'xfs': (('mkfs.xfs', ('/usr/sbin/mkfs.xfs', '/sbin/mkfs.xfs', '/usr/bin/mkfs.xfs')),),
            'btrfs': (('mkfs.btrfs', ('/usr/sbin/mkfs.btrfs', '/sbin/mkfs.btrfs', '/usr/bin/mkfs.btrfs')),),
            'fat32': (
                ('mkfs.vfat|mkfs.fat', (
                    '/usr/sbin/mkfs.vfat', '/sbin/mkfs.vfat', '/usr/bin/mkfs.vfat',
                    '/usr/sbin/mkfs.fat', '/sbin/mkfs.fat', '/usr/bin/mkfs.fat',
                )),
            ),
            'exfat': (('mkfs.exfat', ('/usr/sbin/mkfs.exfat', '/sbin/mkfs.exfat', '/usr/bin/mkfs.exfat')),),
        }
        tools = {}
        for logical_name, candidates in tool_specs[args.fs]:
            executable = None
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    executable = candidate
                    break
            if executable is None:
                for candidate_name in logical_name.split('|'):
                    executable = _find_tool_or_common_paths(candidate_name, candidates)
                    if executable:
                        break
            if executable is None:
                package = {
                    'ext4': 'e2fsprogs', 'xfs': 'xfsprogs', 'btrfs': 'btrfs-progs',
                    'fat32': 'dosfstools', 'exfat': 'exfatprogs',
                }[args.fs]
                log(f"{logical_name} not found. Install '{package}' and retry.", 'ERROR')
                return
            tools[logical_name] = executable
        if args.luks:
            cryptsetup = _find_tool_or_common_paths(
                'cryptsetup', ('/usr/sbin/cryptsetup', '/sbin/cryptsetup', '/usr/bin/cryptsetup')
            )
            passgen = _find_tool_or_common_paths(PASSGEN_BIN, (str(Path.home() / '.local/bin/passgen'),))
            if not cryptsetup or not passgen:
                missing = 'cryptsetup' if not cryptsetup else PASSGEN_BIN
                log(f"{missing} is required before LUKS formatting can begin.", 'ERROR')
                return
            tools['cryptsetup'] = cryptsetup
            tools['passgen'] = passgen

        name = args.args[0]
        input_token = name
        clean_input = input_token.strip('[]')
        input_is_id = (
            (clean_input.startswith('#') and clean_input[1:].isdigit()) or
            (clean_input.startswith('U') and clean_input[1:].isdigit())
        )
        luks_memory_kib = LUKS_PBKDF_DEFAULT_MEMORY_KIB
        target = self.resolve_target(name, allow_id=True)
        if not target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        # Operation name is used for the mapper and persistent mapping. Discovery
        # IDs are never allowed to leak into either namespace.
        op_name = input_token
        if input_is_id:
            self.mappings = read_luks_map()
            existing_name = None
            for n, p in (self.mappings or {}).items():
                try:
                    if os.path.realpath(p) == os.path.realpath(target):
                        existing_name = n
                        break
                except Exception:
                    continue

            if existing_name:
                op_name = existing_name
            elif args.label:
                op_name = str(args.label)
            else:
                log("ID-based format requires a stable name for LUKS mapper/mapping.", 'ERROR')
                log("Run: map #N <name> first, or pass --label <name>.", 'ERROR')
                return

        try:
            op_name = validate_mapping_name(op_name)
            label = validate_filesystem_label(args.label or op_name, args.fs)
        except ValueError as exc:
            log(f"Invalid format name/label: {exc}", 'ERROR')
            return

        detached_header_path = None
        if args.luks and args.detached_header:
            raw_detached = str(args.detached_header or '').strip()
            if raw_detached == '__DEFAULT_DETACHED_HEADER__':
                detached_header_path = str(LUKS_HEADER_BACKUP_DIR / op_name)
            else:
                detached_header_path = raw_detached

            try:
                detached_header_path = validate_absolute_path(
                    os.path.abspath(os.path.expanduser(detached_header_path)),
                    'detached header path',
                )
                dh_parent = Path(detached_header_path).parent
                dh_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if dh_parent.is_symlink() or not dh_parent.is_dir():
                    raise ValueError(f"unsafe detached-header parent: {dh_parent}")
            except Exception as e:
                log(f"Failed to prepare detached-header directory for {detached_header_path}: {e}", 'ERROR')
                return

            if os.path.exists(detached_header_path):
                log(f"Detached header path already exists: {detached_header_path}", 'ERROR')
                log("Refusing to overwrite existing detached header file. Choose another path or move/remove the old file.", 'ERROR')
                return

        # Wait/Verify target existence
        real_target = os.path.realpath(target)
        if not os.path.exists(real_target):
            log(f"Target device not found: {target} (resolved: {real_target})", 'ERROR')
            return

        if self._block_if_root_drive(real_target, f"format {input_token}"):
            return

        stable_path = None
        try:
            current_maps = read_luks_map()
            if op_name in current_maps:
                if os.path.realpath(current_maps[op_name]) != real_target:
                    log(f"Mapping '{op_name}' belongs to a different device.", 'ERROR')
                    return
                stable_path = current_maps[op_name]
            else:
                dtype_hint = _lsblk_type(real_target)
                candidate = self.find_persistent_path(
                    os.path.basename(real_target), type_=dtype_hint or 'disk'
                )
                stable_path = validate_persistent_target(candidate)
        except Exception as exc:
            log(f"FORMAT BLOCKED: persistent target identity is unavailable: {exc}", 'ERROR')
            return

        log(f"Target: {real_target}")
        log(f"Name: {op_name}")
        if op_name != input_token:
            log(f"Input token '{input_token}' resolved to operation name '{op_name}'.")

        format_lock = None
        mapper_opened = False
        mounted_at = None
        completed = False
        luks_created = False
        mapping_previous = None
        mapping_had_previous = False
        mountpoint_preexisting = False
        mapping_written = False
        try:
            preflight = self._format_safety_preflight(real_target)
            if not preflight.get('ok'):
                for error in preflight.get('errors') or ['unknown safety probe failure']:
                    log(f"FORMAT BLOCKED: safety probe failed: {error}", 'ERROR')
                return
            format_lock = preflight.get('lock_fd')
            log(f"Exclusive non-blocking device lock acquired for {real_target}.")
            self._format_print_preflight(preflight)

            active = preflight.get('active') or {}
            if any(active.get(key) for key in ('mounts', 'swaps', 'holders', 'memberships')):
                log(f"OPERATION BLOCKED: {real_target} or a child is active.", 'ERROR')
                return
            dev_type = preflight.get('dev_type') or '-'
            if dev_type not in ('disk', 'part'):
                log(f"Unsupported target type for format: {dev_type}.", 'ERROR')
                return
            pttype = preflight.get('pttype') or ''
            if dev_type == 'disk' and pttype:
                log(
                    f"OPERATION BLOCKED: {real_target} is partitioned ({pttype}). "
                    "Run erase first before making a whole-disk filesystem.",
                    'ERROR',
                )
                return

            signatures = preflight.get('signatures') or []
            luks_signature = any(
                'luks' in str(sig.get('type') or '').lower() for sig in signatures
            )
            if luks_signature:
                log(
                    f"OPERATION BLOCKED: {real_target} is already a LUKS container. "
                    "format will not overwrite an existing LUKS header; erase it explicitly first.",
                    'ERROR',
                )
                return
            if signatures and not args.reformat_existing:
                log(f"OPERATION BLOCKED: existing content was detected on {real_target}.", 'ERROR')
                log("Use --reformat-existing and confirm its UUID/label, or run erase first.", 'ERROR')
                return

            if not self.extensive_confirm(f"{input_token} ({real_target})"):
                return
            if signatures and not self._format_existing_confirmation(signatures):
                return

            postflight = self._format_safety_preflight(real_target, lock_fd=format_lock)
            if not postflight.get('ok'):
                for error in postflight.get('errors') or ['unknown post-confirmation failure']:
                    log(f"OPERATION BLOCKED after confirmation: {error}", 'ERROR')
                return
            changed = self._format_identity_changed(preflight['identity'], postflight['identity'])
            if changed or self._format_signatures_changed(signatures, postflight.get('signatures')):
                log("OPERATION BLOCKED: device identity or content changed after confirmation.", 'ERROR')
                return
            post_active = postflight.get('active') or {}
            if any(post_active.get(key) for key in ('mounts', 'swaps', 'holders', 'memberships')):
                log("OPERATION BLOCKED: target became active during confirmation.", 'ERROR')
                return

            if dev_type == 'disk':
                self._refresh_kernel_partition_state(real_target, drop_partitions=True)
            final = self._format_safety_preflight(real_target, lock_fd=format_lock)
            if not final.get('ok'):
                for error in final.get('errors') or ['unknown final safety failure']:
                    log(f"OPERATION BLOCKED immediately before format: {error}", 'ERROR')
                return
            if (
                self._format_identity_changed(postflight['identity'], final['identity'])
                or self._format_signatures_changed(postflight.get('signatures'), final.get('signatures'))
                or any((final.get('active') or {}).get(key) for key in ('mounts', 'swaps', 'holders', 'memberships'))
            ):
                log("OPERATION BLOCKED immediately before format: target state changed.", 'ERROR')
                return

            crypt_target = real_target
            passphrase = None
            if args.luks:
                log("Generate the new LUKS passphrase with passgen (entry 1 of 2).")
                first = run_command([tools['passgen']], capture_output=True).stdout
                log("Repeat passgen with the same inputs to confirm it (entry 2 of 2).")
                second = run_command([tools['passgen']], capture_output=True).stdout
                if not str(first or '').strip() or first != second:
                    log("LUKS passphrase confirmation failed; nothing was written.", 'ERROR')
                    return
                passphrase = first
                log(f"Formatting LUKS on {crypt_target}...")
                log(
                    f"LUKS PBKDF: memory={LUKS_PBKDF_DEFAULT_MEMORY_LABEL} "
                    f"({luks_memory_kib:,} KiB), argon2id, "
                    f"threads={LUKS_PBKDF_DEFAULT_THREADS}, time={LUKS_PBKDF_DEFAULT_TIME}"
                )
                luks_cmd = [
                    tools['cryptsetup'], 'luksFormat', '--type', 'luks2', '--batch-mode',
                    '--pbkdf', 'argon2id', '--pbkdf-memory', str(luks_memory_kib),
                    '--pbkdf-parallel', str(LUKS_PBKDF_DEFAULT_THREADS),
                    '--pbkdf-force-iterations', str(LUKS_PBKDF_DEFAULT_TIME), '--key-file', '-',
                ]
                if detached_header_path:
                    log(f"Using detached LUKS header file: {detached_header_path}")
                    luks_cmd.extend(['--header', detached_header_path])
                luks_cmd.append(crypt_target)
                run_command(luks_cmd, input_str=passphrase, sudo=True)
                luks_created = True

                verify_cmd = [tools['cryptsetup'], 'isLuks']
                if detached_header_path:
                    verify_cmd.extend(['--header', detached_header_path])
                verify_cmd.append(crypt_target)
                verify = run_command(verify_cmd, sudo=True, check=False)
                if getattr(verify, 'returncode', 1) != 0:
                    raise RuntimeError("cryptsetup could not verify the newly written LUKS header")

                log("Opening new LUKS volume with the confirmed passphrase...")
                open_cmd = [tools['cryptsetup'], 'open', '--key-file', '-']
                if detached_header_path:
                    open_cmd.extend(['--header', detached_header_path])
                open_cmd.extend([crypt_target, op_name])
                run_command(open_cmd, input_str=passphrase, sudo=True)
                mapper_opened = True
                fs_target = f"/dev/mapper/{op_name}"
                if not os.path.exists(fs_target):
                    raise RuntimeError(f"new mapper did not appear: {fs_target}")
                if detached_header_path:
                    user, group = self._invoking_user_group()
                    run_command(['chown', f'{user}:{group}', detached_header_path], sudo=True)
                    run_command(['chmod', '600', detached_header_path], sudo=True)
            else:
                log("Using plain format mode (no LUKS).")
                fs_target = crypt_target

            log(f"Formatting filesystem {args.fs} (label={label}) on {fs_target}...")
            if args.fs == 'ext4':
                run_command([tools['mkfs.ext4'], '-F', '-L', label, fs_target], sudo=True)
                log("Reclaiming ext4 reserved blocks (tune2fs -m 0)...")
                run_command([tools['tune2fs'], '-m', '0', fs_target], sudo=True)
            elif args.fs == 'xfs':
                run_command([tools['mkfs.xfs'], '-f', '-L', label, fs_target], sudo=True)
            elif args.fs == 'btrfs':
                run_command([tools['mkfs.btrfs'], '-f', '-L', label, fs_target], sudo=True)
            elif args.fs == 'fat32':
                run_command([tools['mkfs.vfat|mkfs.fat'], '-F', '32', '-n', label, fs_target], sudo=True)
            elif args.fs == 'exfat':
                run_command([tools['mkfs.exfat'], '-L', label, fs_target], sudo=True)

            if dev_type == 'disk':
                self._refresh_kernel_partition_state(real_target, drop_partitions=True)

            fallback = safe_mount_path(f"/media/{os.environ.get('USER', 'root')}", label)
            mountpoint, use_fstab, fstab_entry = self._select_mountpoint_for_device(
                fs_target, fallback, preferred_label=label
            )
            mountpoint = validate_absolute_path(mountpoint, 'mountpoint')
            mountpoint_preexisting = os.path.isdir(mountpoint)
            if use_fstab:
                log(f"Using fstab mount after format: {fstab_entry['spec']} -> {mountpoint}")
            occupied = run_command(['findmnt', '-rn', '-M', mountpoint, '-o', 'SOURCE'], check=False)
            if getattr(occupied, 'returncode', 1) not in (0, 1):
                raise RuntimeError(f"could not verify mountpoint state for {mountpoint}")
            if occupied.returncode == 0:
                source = (occupied.stdout or '').strip().splitlines()
                source = source[0].split('[', 1)[0] if source else ''
                if not source or os.path.realpath(source) != os.path.realpath(fs_target):
                    raise RuntimeError(f"mountpoint {mountpoint} is occupied by {source or 'unknown source'}")

            def save_mapping(current):
                nonlocal mapping_previous, mapping_had_previous
                existing = current.get(op_name)
                if existing and os.path.realpath(existing) != real_target:
                    raise ValueError(f"mapping '{op_name}' changed to another device")
                if op_name in current:
                    mapping_previous = current[op_name]
                    mapping_had_previous = True
                current[op_name] = stable_path
                return current

            self.mappings = update_luks_map(save_mapping)
            mapping_written = True
            mounted_at = mountpoint
            self._mount_device(fs_target, mountpoint, use_fstab=use_fstab)
            self._chown_new_filesystem_root(mountpoint)
            completed = True
            log("Disk initialization complete.")
        except KeyboardInterrupt:
            log("Format interrupted; cleaning up resources created by this invocation.", 'ERROR')
            raise
        except Exception as exc:
            log(f"Format failed: {exc}", 'ERROR')
        finally:
            if not completed and mounted_at:
                result = run_command(['umount', mounted_at], sudo=True, check=False)
                if getattr(result, 'returncode', 1) != 0:
                    log(f"Cleanup could not unmount {mounted_at}; mapper will remain open.", 'ERROR')
                else:
                    if not mountpoint_preexisting:
                        run_command(['rmdir', mounted_at], sudo=True, check=False)
            if not completed and mapper_opened:
                result = run_command(['cryptsetup', 'close', op_name], sudo=True, check=False)
                if getattr(result, 'returncode', 1) != 0:
                    log(f"Cleanup could not close mapper {op_name}.", 'ERROR')
                else:
                    mapper_opened = False
            if not completed and mapping_written:
                try:
                    def restore_mapping(current):
                        if not mapping_had_previous:
                            current.pop(op_name, None)
                        else:
                            current[op_name] = mapping_previous
                        return current
                    self.mappings = update_luks_map(restore_mapping)
                except Exception as exc:
                    log(f"Cleanup could not restore mapping state: {exc}", 'ERROR')
            if detached_header_path and not luks_created and os.path.exists(detached_header_path):
                run_command(['unlink', detached_header_path], sudo=True, check=False)
            self._format_release_device_lock(format_lock)

    def do_create(self, arg):
        '''Create partition table or partition on a whole disk: create <name/id> [--gpt|--mbr] [--partition] [--start X] [--end Y]

        Scope:
          - Whole disks only (not partitions).
          - Table creation requires prior erase: target must look erased (no partitions, no PT metadata, no signatures).
          - Partition-only mode can add a partition to an existing partitioned disk.

        Actions:
          - --gpt / --mbr: create a new partition table (erased disk only)
          - --partition:
              * with --gpt/--mbr: create first partition after table creation
              * without --gpt/--mbr: create an additional partition on existing table
              * when --start/--end are omitted, the largest free extent is selected automatically
              * overlapping ranges are rejected; existing partitions are not overwritten

        Examples:
          erase 1b
          create 1b --gpt
          create 1b --gpt --partition
          create 1b --partition
          create 1b --partition --start 500GiB --end 100%
          create #4 --mbr --partition
        '''
        parser = CmdArgumentParser(prog='create', add_help=False)
        parser.add_argument('target', help='Whole-disk target mapping name or discovery ID (#N)')
        group = parser.add_mutually_exclusive_group(required=False)
        group.add_argument('--gpt', action='store_true', help='Create GPT partition table')
        group.add_argument('--mbr', action='store_true', help='Create MBR (msdos) partition table')
        parser.add_argument('--partition', action='store_true', help='Create partition (new table first if --gpt/--mbr is provided)')
        parser.add_argument('--start', help='Partition start (parted syntax, e.g. 1MiB, 500GiB, 2048s)')
        parser.add_argument('--end', help='Partition end (parted syntax, e.g. 100%, 750GiB)')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.target
        real_target = self.resolve_target(name, allow_id=True)

        if not real_target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        real_target = os.path.realpath(real_target)
        if self._block_if_root_drive(real_target, f"create {name}"):
            return

        if _lsblk_type(real_target) != 'disk':
            log(f"create only supports whole-disk targets. '{name}' resolved to {real_target} ({_lsblk_type(real_target) or 'unknown'}).", 'ERROR')
            return

        if not args.gpt and not args.mbr and not args.partition:
            log("Usage: create <name/id> [--gpt|--mbr] [--partition] [--start X] [--end Y]", 'ERROR')
            log("Provide --gpt/--mbr to create a table, and/or --partition to create a partition.", 'ERROR')
            return

        # Refuse to operate on mounted devices. A failed probe is not proof of
        # safety, so it must abort rather than fall through to parted.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINTS', real_target], check=False)
            if getattr(res_m, 'returncode', 1) != 0:
                log(f"Could not verify active use of {real_target}; refusing to modify the partition table.", 'ERROR')
                return
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception as exc:
            log(f"Could not verify active use of {real_target}: {exc}", 'ERROR')
            return

        creating_table = args.gpt or args.mbr
        creating_partition = args.partition

        table = 'gpt' if args.gpt else ('msdos' if args.mbr else None)
        p_start = args.start
        p_end = args.end

        if creating_table:
            erased_ok, erased_reason = self._disk_looks_erased_for_create(real_target)
            if not erased_ok:
                log(f"OPERATION BLOCKED: create requires erase first; {erased_reason}.", 'ERROR')
                log(f"Run: erase {name}", 'ERROR')
                return

        if creating_partition and not creating_table:
            # Existing table required when creating an additional partition.
            try:
                res_pt = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
                pttype = (getattr(res_pt, 'stdout', '') or '').strip().lower()
            except Exception:
                pttype = ""
            if not pttype:
                log("OPERATION BLOCKED: no partition table found on target disk. Create one first with --gpt or --mbr.", 'ERROR')
                return

            if p_end and not p_start:
                log("Invalid partition range: --end requires --start when adding to an existing table.", 'ERROR')
                return

            # Auto-place in largest free extent when no explicit start/end is provided.
            if not p_start and not p_end:
                extent = self._largest_free_extent_sectors(real_target)
                if not extent:
                    log("No free extent found for new partition.", 'ERROR')
                    return
                p_start = f"{extent[0]}s"
                p_end = f"{extent[1]}s"
                # Ignore tiny gaps (e.g., 34s..2047s on GPT/MBR).
                if extent[2] < 4096:
                    log("Largest free extent is too small to create a useful partition.", 'ERROR')
                    return

        if creating_partition and creating_table:
            if not p_start:
                p_start = '1MiB'
            if not p_end:
                p_end = '100%'

        action_parts = []
        if creating_table:
            action_parts.append(f"create {table} table")
        if creating_partition:
            action_parts.append(f"create partition ({p_start or '?'}..{p_end or '?'})")
        action = " + ".join(action_parts)
        log(f"Planned action on {real_target}: {action}")

        preflight = self._destructive_safety_preflight(real_target)
        if not preflight.get('ok'):
            for error in preflight.get('errors') or ['safety probe failed']:
                log(f"CREATE BLOCKED: {error}", 'ERROR')
            return
        self._format_print_preflight(preflight)
        if self._active_use_present(preflight.get('active')):
            log("CREATE BLOCKED: target or a child is active. Close/unmount it first.", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        if not self.extensive_confirm(real_target):
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        try:
            stable, postflight = self._destructive_revalidate(preflight, preflight.get('lock_fd'))
            if not stable:
                for error in postflight.get('errors') or ['target changed after confirmation']:
                    log(f"CREATE BLOCKED after confirmation: {error}", 'ERROR')
                return

            if creating_table:
                run_command(['parted', '-s', real_target, 'mklabel', table], sudo=True)

            if creating_partition:
                run_command(['parted', '-s', real_target, 'mkpart', 'primary', p_start, p_end], sudo=True)

            run_command(['udevadm', 'settle'], sudo=True, check=False)
            run_command(['partprobe', real_target], sudo=True, check=False)

            if creating_partition:
                created_parts = []
                try:
                    res_p = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE', real_target], check=False)
                    if getattr(res_p, 'returncode', 1) != 0:
                        raise RuntimeError("post-create partition probe failed")
                    for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                        cols = line.strip().split()
                        if len(cols) >= 2 and cols[1] == 'part':
                            created_parts.append(f"/dev/{cols[0]}")
                except Exception as exc:
                    log(f"Could not verify created partition(s): {exc}", 'ERROR')
                    return

                if created_parts:
                    if creating_table:
                        log(f"Created partition table ({table}) and partition(s): {', '.join(created_parts)}")
                    else:
                        log(f"Created partition(s): {', '.join(created_parts)}")
                else:
                    log("Requested partition creation completed, but no new partition was detected.", 'ERROR')
            elif creating_table:
                log(f"Created empty partition table: {table} on {real_target}")
        finally:
            self._format_release_device_lock(preflight.get('lock_fd'))

    def do_remove(self, arg):
        '''Remove a partition from its parent disk: remove <name/id>

        Scope:
          - Partition targets only.
          - Whole-disk targets are refused.
        '''
        target = arg.strip()
        if not target:
            log("Usage: remove <name/id>", 'ERROR')
            return

        resolved = self.resolve_target(target, allow_id=True)
        if not resolved:
            log(f"Unknown target: '{target}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        part_dev = os.path.realpath(resolved)
        if not os.path.exists(part_dev):
            log(f"Target not found: {part_dev}", 'ERROR')
            return

        if self._block_if_root_drive(part_dev, f"remove {target}"):
            return

        if _lsblk_type(part_dev) != 'part':
            log(f"remove only supports partitions. '{target}' resolved to {part_dev} ({_lsblk_type(part_dev) or 'unknown'}).", 'ERROR')
            return

        # Refuse to remove mounted partition. Probe failures are unsafe.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINTS', part_dev], check=False)
            if getattr(res_m, 'returncode', 1) != 0:
                log(f"Could not verify active use of {part_dev}; refusing to remove it.", 'ERROR')
                return
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {part_dev} is mounted at {', '.join(mounts)}. Unmount/close it first.", 'ERROR')
                return
        except Exception as exc:
            log(f"Could not verify active use of {part_dev}: {exc}", 'ERROR')
            return

        # Find parent disk and partition number.
        res_pk = run_command(['lsblk', '-no', 'PKNAME', part_dev], check=False)
        pkname = (getattr(res_pk, 'stdout', '') or '').strip()
        res_partn = run_command(['lsblk', '-no', 'PARTN', part_dev], check=False)
        partn = (getattr(res_partn, 'stdout', '') or '').strip()
        if not pkname or not partn:
            log(f"Could not determine parent disk/partition number for {part_dev}.", 'ERROR')
            return

        parent_disk = f"/dev/{pkname}"
        if self._block_if_root_drive(parent_disk, f"remove {target}"):
            return

        log(f"Planned action: remove partition {part_dev} (part #{partn}) from {parent_disk}")
        preflight = self._destructive_safety_preflight(parent_disk)
        if not preflight.get('ok'):
            for error in preflight.get('errors') or ['safety probe failed']:
                log(f"REMOVE BLOCKED: {error}", 'ERROR')
            return
        self._format_print_preflight(preflight)
        if self._active_use_present(preflight.get('active')):
            log("REMOVE BLOCKED: target disk or a child is active. Close/unmount it first.", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return
        if not self.extensive_confirm(part_dev):
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        try:
            stable, postflight = self._destructive_revalidate(preflight, preflight.get('lock_fd'))
            if not stable:
                for error in postflight.get('errors') or ['target changed after confirmation']:
                    log(f"REMOVE BLOCKED after confirmation: {error}", 'ERROR')
                return
            run_command(['parted', '-s', parent_disk, 'rm', partn], sudo=True)
            run_command(['udevadm', 'settle'], sudo=True, check=False)
            run_command(['partprobe', parent_disk], sudo=True, check=False)
            log(f"Removed partition {part_dev} from {parent_disk}")
        finally:
            self._format_release_device_lock(preflight.get('lock_fd'))
