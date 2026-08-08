"""Raw block-device operations and recovery commands."""

from pathlib import Path
import argparse
import cmd
import json
import os
import re
import shlex
import time
from ..runtime import Colors, _cmd_log_close, _cmd_log_open, _find_tool_or_common_paths, _first_int_from_text, _fmt_hms, log, run_command
from ..devices import _sysfs_block_name, _sysfs_to_parent_disk_name
from ..rawio import _parse_ddrescue_failed_ranges, secure_erase_disk
from ..shell_core import CmdArgumentParser


class BlockCommands:

    def _raw_destructive_preflight(self, name, operation):
        """Resolve and lock a raw target before any destructive confirmation."""
        real_target = self.resolve_target(name, allow_id=True)
        if not real_target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return None, None
        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return None, None
        real_target = os.path.realpath(real_target)
        log(f"Target resolved: {real_target}")
        if self._block_if_root_drive(real_target, f"{operation} {name}"):
            return None, None

        preflight = self._destructive_safety_preflight(real_target)
        if not preflight.get('ok'):
            self._format_print_preflight(preflight)
            for error in preflight.get('errors') or ['destructive preflight failed']:
                log(error, 'ERROR')
            return None, None
        self._format_print_preflight(preflight)
        if self._active_use_present(preflight.get('active')):
            log(f"OPERATION BLOCKED: {real_target} is mounted, swapped, or held by another device.", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return None, None
        dtype = str(preflight.get('dev_type') or '').lower()
        if dtype not in {'disk', 'part'}:
            log(f"OPERATION BLOCKED: unsupported raw target type '{dtype or 'unknown'}'.", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return None, None
        return real_target, preflight

    def _raw_release(self, preflight):
        if preflight:
            self._format_release_device_lock(preflight.get('lock_fd'))

    def do_erase(self, arg):
        '''Fast metadata wipe (soft erase): erase <name/id> [--soft]

        This is a fast "re-provisioning" wipe. It removes recognizable signatures and zaps GPT/MBR metadata
        (when the target is a whole disk). It is NOT a secure wipe.

        It performs:
          - wipefs -a (and --force for whole-disk partition-table signatures)
          - sgdisk --zap-all (GPT) when available
          - sfdisk (MBR)

        Note: This is a DESTRUCTIVE operation. You must type both the resolved device path and persistent path to proceed.
        '''
        parser = CmdArgumentParser(prog='erase', add_help=False)
        parser.add_argument('target', help='Target name or device')
        parser.add_argument('--soft', action='store_true', help='(Deprecated) erase is already soft; flag is ignored')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.target
        real_target, preflight = self._raw_destructive_preflight(name, 'erase')
        if not real_target:
            return
        if not self.extensive_confirm(real_target):
            self._raw_release(preflight)
            return
        stable, postflight = self._destructive_revalidate(preflight, preflight['lock_fd'])
        if not stable:
            for error in postflight.get('errors') or ['target changed after confirmation']:
                log(error, 'ERROR')
            self._raw_release(preflight)
            return

        if args.soft:
            log("--soft is deprecated: erase is already a soft/metadata wipe.", 'WARN')

        try:
            if not self._soft_erase_target(real_target):
                log("Soft erase failed or signatures remain after verification.", 'ERROR')
        finally:
            self._raw_release(preflight)

    def do_nuke(self, arg):
        '''Securely erase a disk: nuke <name/id>

        Note: You must 'map' a disk first to give it a name before erasing it.

        NUANCES & SAFETY:
        - Whole Disk (sda):
          Attempts deep hardware-level wipes (NVMe Sanitize, ATA Secure Erase, etc.).
          Destroys the Partition Table and ALL partitions on the drive.
        - Partition (sda2):
          Hardware-level wipes are SKIPPED for safety. The script falls back to
          highly effective software wipes (blkdiscard or dd zero-overwrite).
          ONLY the specified partition is wiped; other partitions remain safe.
        - Mapped Name (1a):
          Resolves to the physical partition and follows partition-level safety rules.

        UNDER THE HOOD:
        1.  Target Resolution: Maps friendly name to a raw block device.
        2.  Destructive Wipe:
            - NVMe: Prioritizes (1) Sanitize Crypto Erase, (2) Sanitize Block Erase,
              (3) Format Crypto Erase, and (4) Format Block Erase.
            - SSD: Prioritizes (1) PSID Revert, (2) ATA Sanitize, (3) ATA Secure Erase (Enhanced),
              (4) ATA Secure Erase (Standard), (5) blkdiscard --secure, and (6) blkdiscard.
            - HDD: Prioritizes (1) ATA Sanitize, (2) ATA Secure Erase (Enhanced),
              (3) ATA Secure Erase (Standard), and (4) Zero Overwrite + Verify.
        3.  Verification: Executes 'udevadm settle' and 'sync' to ensure all operations are committed.

        Note: This is a DESTRUCTIVE operation. You must type both the resolved device path and persistent path to proceed.

        WARNING: This operation is IRREVERSIBLE.
        '''
        parser = CmdArgumentParser(prog='nuke', add_help=False)
        parser.add_argument('target', help='Target name or device')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.target
        real_target, preflight = self._raw_destructive_preflight(name, 'nuke')
        if not real_target:
            return
        if not self.extensive_confirm(real_target):
            self._raw_release(preflight)
            return
        stable, postflight = self._destructive_revalidate(preflight, preflight['lock_fd'])
        if not stable:
            for error in postflight.get('errors') or ['target changed after confirmation']:
                log(error, 'ERROR')
            self._raw_release(preflight)
            return

        start_ts = time.time()
        log_path = _cmd_log_open("nuke")
        if log_path:
            print(f"Log: {log_path}")
        ok = False
        try:
            ok = bool(secure_erase_disk(real_target))
            if ok:
                log("Secure erase completed successfully.")
            else:
                log("Secure erase failed.", 'ERROR')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            _cmd_log_close()
            self._raw_release(preflight)

    def do_entropise(self, arg):
        '''High-entropy random overwrite on a disk or partition: entropise <name/id>

        Performs a full-device single pass using /dev/urandom via dd with an
        explicit byte count (count_bytes) so completion is clean at end-of-device:
          dd if=/dev/urandom of=<device> bs=16M count=<device_size_bytes> count_bytes \
             status=progress iflag=fullblock oflag=direct conv=fsync

        This destroys all existing data on the target.
        '''
        parser = CmdArgumentParser(prog='entropise', add_help=False)
        parser.add_argument('target', help='Target name or device')

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.target
        real_target, preflight = self._raw_destructive_preflight(name, 'entropise')
        if not real_target:
            return

        log(f"ENTROPISE WARNING: About to overwrite all data on {real_target} with high-entropy random bytes from /dev/urandom.")
        if not self.extensive_confirm(f"{name} ({real_target})"):
            self._raw_release(preflight)
            return
        stable, postflight = self._destructive_revalidate(preflight, preflight['lock_fd'])
        if not stable:
            for error in postflight.get('errors') or ['target changed after confirmation']:
                log(error, 'ERROR')
            self._raw_release(preflight)
            return

        start_ts = time.time()
        log_path = _cmd_log_open("entropise")
        if log_path:
            print(f"Log: {log_path}")
        try:
            size_bytes = None
            # Prefer blockdev exact byte size.
            try:
                res_sz = run_command(['blockdev', '--getsize64', real_target], sudo=True, check=False)
                size_bytes = _first_int_from_text(getattr(res_sz, 'stdout', '') or '')
            except Exception:
                size_bytes = None
            # Fallback to lsblk byte size.
            if size_bytes is None or int(size_bytes) <= 0:
                try:
                    res_ls = run_command(['lsblk', '-bno', 'SIZE', real_target], check=False)
                    size_bytes = _first_int_from_text(getattr(res_ls, 'stdout', '') or '')
                except Exception:
                    size_bytes = None
            if size_bytes is None or int(size_bytes) <= 0:
                log(f"Failed to determine device size for {real_target}. Aborting entropise.", 'ERROR')
                return

            log(f"Starting entropise on {real_target} (single random pass)...")
            run_command(
                [
                    'dd',
                    'if=/dev/urandom',
                    f'of={real_target}',
                    'bs=16M',
                    f'count={int(size_bytes)}',
                    'iflag=fullblock,count_bytes',
                    'status=progress',
                    'oflag=direct',
                    'conv=fsync',
                ],
                sudo=True,
                capture_output=False
            )
            run_command(['sync'], sudo=True, check=False, capture_output=False)
            log("Entropise completed successfully.")
        except Exception as e:
            log(f"Entropise failed: {e}", 'ERROR')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            _cmd_log_close()
            self._raw_release(preflight)

    def do_clone(self, arg):
        '''Clone one disk or partition to another: clone <src_name/id> <dst_name/id>

        WARNING (DATA DESTRUCTION):
        - This command writes directly to the destination block device (like running ddrescue/dd).
        - The destination is overwritten starting at byte 0. Any existing partition table,
          filesystems, and files on the destination WILL BE DESTROYED.
        - If the destination is larger than the source, bytes beyond the source size are
          not overwritten. Old data may still physically exist there, but it will not be
          referenced by the cloned partition table.
        - diskmgr does NOT unmount the destination for you. Unmount/close it first to
          avoid live corruption.
        - If you need to sanitize the destination (secure wipe), run: nuke <dst_name>
        - If you only need a fast metadata wipe for re-provisioning, run: erase <dst_name>

        Note: The target disk MUST be the same size or larger than the source.

        STEP-BY-STEP PROCESS:
        1.  Resolution: Maps both friendly names to their physical device nodes (PDP).
        2.  Size Validation: Queries 'blockdev --getsize64' for both. Aborts if dst < src.
        3.  Safety Audit: Verifies that the target is NOT the system root drive.
        4.  Confirmation: Requires typing resolved device and persistent path to authorize data destruction.
        5.  Cloning: Executes ddrescue in two phases:
            - Pass 1: 'ddrescue --force <src> <dst> <mapfile>'
            - Retry:  'ddrescue --force -r3 <src> <dst> <mapfile>'
        6.  Sync: Flushes kernel buffers to ensure all data is physically committed to disk.

        Note: This is a DESTRUCTIVE operation. You must type both the resolved device path and persistent path to proceed.

        SCENARIOS:
        - Drive to Drive:
          Creates a 1:1 bit-perfect clone. The target disk becomes an identical twin,
          including the Partition Table, UUIDs, and all partitions.
          Note: If the target is larger, the extra space appears as 'free' at the end.
        - Partition to Partition:
          Copies the internal data of the source partition into the target partition.
          Useful for moving a LUKS container or a specific filesystem.
          Warning: Filesystem UUIDs will be duplicated; avoid mounting both simultaneously.
        - Partition to Drive:
          The source partition's content is written to the start of the physical disk.
          This destroys the target's partition table and turns the disk into a
          "partitionless" volume (e.g., a raw LUKS device).
        - Drive to Partition (DANGEROUS):
          Writes the source's boot sectors and partition table into the target partition.
          This usually results in an unreadable "nested" structure.

        CLONING & ENCRYPTION (CRITICAL):
        - Source is LOCKED (e.g., clone sda sdb):
          Creates a bit-perfect "Encrypted Twin." The destination remains encrypted
          and requires the same password. (Recommended for backups).
        - Source is OPEN (e.g., clone sda sdb):
          Copies encrypted data but may capture a "dirty" filesystem state if
          files are currently being written. (Close before cloning if possible).
        - Source is MAPPER (e.g., clone dm-0 sdb):
          Performs a "Strip-and-Clone." The destination receives RAW DECRYPTED
          DATA. The resulting clone will be completely UNENCRYPTED.
        '''
        try:
            args = shlex.split(arg)
        except ValueError as exc:
            log(f"Invalid clone arguments: {exc}", 'ERROR')
            return
        if len(args) != 2:
            log("Usage: clone <src_name/id> <dst_name/id>", 'ERROR')
            return

        src_name, dst_name = args
        src_path = self.resolve_target(src_name, allow_id=True)
        dst_path = self.resolve_target(dst_name, allow_id=True)

        if not src_path:
            log(f"Unknown source target: '{src_name}'. Use mapping name or discovery ID (#N).", 'ERROR')
            return
        if not dst_path:
            log(f"Unknown destination target: '{dst_name}'. Use mapping name or discovery ID (#N).", 'ERROR')
            return

        src_real = os.path.realpath(src_path)
        dst_real = os.path.realpath(dst_path)

        if src_real == dst_real:
            log("Source and target are the same device!", 'ERROR')
            return

        # Also block cloning within the same physical disk (e.g. name for disk + #N resolving to same disk).
        def _top_level_disk(dev_path):
            try:
                mapped_name = _sysfs_block_name(dev_path)
                disk_name = _sysfs_to_parent_disk_name(mapped_name)
                candidate = os.path.realpath(f"/dev/{disk_name}")
                if os.path.exists(candidate):
                    return candidate
            except Exception:
                pass
            return None

        src_disk = _top_level_disk(src_real)
        dst_disk = _top_level_disk(dst_real)
        if src_disk and dst_disk and src_disk == dst_disk:
            log(f"OPERATION BLOCKED: source ({src_real}) and target ({dst_real}) are on the same physical disk ({src_disk}).", 'ERROR')
            log("Choose a destination on a different physical disk.", 'ERROR')
            return

        if self._block_if_root_drive(dst_real, f"clone {src_name} {dst_name}"):
            return

        # Compare sizes
        try:
            src_bytes = int(run_command(['blockdev', '--getsize64', src_real], sudo=True, capture_output=True).stdout.strip())
            dst_bytes = int(run_command(['blockdev', '--getsize64', dst_real], sudo=True, capture_output=True).stdout.strip())

            if dst_bytes < src_bytes:
                log(f"Target disk is too small! (Source: {src_bytes}B, Target: {dst_bytes}B)", 'ERROR')
                return
        except Exception as e:
            log(f"Failed to verify disk sizes: {e}", 'ERROR')
            return

        ddrescue_bin = _find_tool_or_common_paths('ddrescue', [
            '/usr/bin/ddrescue',
            '/bin/ddrescue',
            '/usr/local/bin/ddrescue',
        ])
        if ddrescue_bin is None:
            log("ddrescue not found. Install 'gddrescue' and retry.", 'ERROR')
            return

        # Prefer logical sector size for reporting failures.
        sector_size = 512
        try:
            res_ss = run_command(['blockdev', '--getss', src_real], sudo=True, capture_output=True, check=False)
            ss = (getattr(res_ss, 'stdout', '') or '').strip()
            if ss.isdigit():
                sector_size = int(ss)
        except Exception:
            sector_size = 512

        preflight = self._destructive_safety_preflight(dst_real)
        if not preflight.get('ok'):
            self._format_print_preflight(preflight)
            for error in preflight.get('errors') or ['destination preflight failed']:
                log(error, 'ERROR')
            return
        self._format_print_preflight(preflight)
        if self._active_use_present(preflight.get('active')):
            log(f"Target has active mounts, swaps, or holders. Unmount/close {dst_name} first.", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        src_identity, src_identity_error = self._format_identity_snapshot(src_real)
        if src_identity_error:
            log(f"Failed to snapshot source identity: {src_identity_error}", 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        # Confirmation
        if not self.extensive_confirm(f"{dst_name} ({dst_real})"):
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        stable, postflight = self._destructive_revalidate(preflight, preflight['lock_fd'])
        current_src, current_src_error = self._format_identity_snapshot(src_real)
        src_changed = current_src_error or self._format_identity_changed(src_identity, current_src or {})
        if not stable or src_changed:
            log("Source or destination identity changed after confirmation; clone aborted.", 'ERROR')
            for error in postflight.get('errors') or []:
                log(error, 'ERROR')
            self._format_release_device_lock(preflight.get('lock_fd'))
            return

        log(f"Cloning {src_name} -> {dst_name}...")
        start_ts = time.time()

        # Perform clone
        try:
            mapdir = Path.home() / '.local' / 'state' / 'diskmgr' / 'clone-maps'
            mapdir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(mapdir, 0o700)
            map_key = json.dumps(
                {'source': src_identity, 'destination': preflight['identity']},
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
            import hashlib
            key = hashlib.sha256(map_key).hexdigest()[:32]
            mapfile = str(mapdir / f"{key}.map")
            metadata_file = Path(f"{mapfile}.json")
            expected_metadata = {
                'source': src_identity,
                'destination': preflight['identity'],
                'source_name': src_name,
                'destination_name': dst_name,
            }
            if metadata_file.exists():
                try:
                    existing_metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"cannot read clone map metadata: {exc}") from exc
                if existing_metadata.get('source') != src_identity or existing_metadata.get('destination') != preflight['identity']:
                    raise RuntimeError("existing clone map is bound to a different source or destination")
            elif Path(mapfile).exists():
                raise RuntimeError("existing clone map has no identity metadata; remove it manually before reuse")
            else:
                temp_metadata = metadata_file.with_suffix('.json.tmp')
                temp_metadata.write_text(json.dumps(expected_metadata, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                os.chmod(temp_metadata, 0o600)
                os.replace(temp_metadata, metadata_file)

            log(f"ddrescue mapfile: {mapfile}")
            # Pass 1: fast clone + mapfile progress.
            cmd = [ddrescue_bin, '--force', src_real, dst_real, mapfile]
            res1 = run_command(cmd, sudo=True, capture_output=False, check=False)

            # Pass 2: retry failed areas (up to 3 times).
            log("ddrescue retry pass: -r3 (retry failed sectors up to 3 times)...")
            cmd_retry = [ddrescue_bin, '--force', '-r3', src_real, dst_real, mapfile]
            res2 = run_command(cmd_retry, sudo=True, capture_output=False, check=False)
            sync_result = run_command(['sync'], sudo=True, check=False)
            if getattr(sync_result, 'returncode', 1) != 0:
                raise RuntimeError(f"sync failed with status {getattr(sync_result, 'returncode', 1)}")

            # Report failures based on mapfile after retries.
            failed = _parse_ddrescue_failed_ranges(mapfile, sector_size=sector_size)
            rc1 = getattr(res1, 'returncode', 1)
            rc2 = getattr(res2, 'returncode', 1)
            if failed or rc1 != 0 or rc2 != 0:
                log(f"Clone did not complete cleanly (pass1={rc1}, retry={rc2}; mapfile: {mapfile}).", 'ERROR')
                print(f"\n{Colors.FAIL}{Colors.BOLD}Unrecovered sector ranges:{Colors.ENDC}")
                max_show = 80
                for r in failed[:max_show]:
                    # LBA range shown as [start, end) for clarity.
                    print(f"  LBA [{r['start_lba']}, {r['end_lba']})  count={r['count_lba']}  bytes={r['size_b']}")
                if len(failed) > max_show:
                    print(f"  ... truncated ({len(failed)} ranges total)")
                print(f"\n{Colors.WARNING}Mapfile:{Colors.ENDC} {mapfile}")
                raise RuntimeError("ddrescue reported errors or unrecovered ranges")
            else:
                log("Cloning complete (no unrecovered sectors reported in the ddrescue mapfile).")
        except Exception as e:
            log(f"Cloning failed: {e}", 'ERROR')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            self._format_release_device_lock(preflight.get('lock_fd'))
