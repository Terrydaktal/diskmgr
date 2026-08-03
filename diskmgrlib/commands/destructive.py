"""DestructiveCommands command implementations."""

from ..common import *
from ..shell_core import CmdArgumentParser


class DestructiveCommands:

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
        real_target = self.resolve_target(name, allow_id=True)

        if not real_target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        real_target = os.path.realpath(real_target)
        log(f"Target resolved: {real_target}")

        if self.is_root_disk(real_target):
            log(f"OPERATION BLOCKED: {real_target} is part of the system root drive!", 'ERROR')
            return

        # Refuse to operate on mounted devices (erase is destructive).
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', real_target], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

        if not self.extensive_confirm(real_target):
            return

        run_command(['sudo', '-v'])

        if args.soft:
            log("--soft is deprecated: erase is already a soft/metadata wipe.", 'WARN')

        self._soft_erase_target(real_target)

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
        real_target = self.resolve_target(name, allow_id=True)

        if not real_target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        real_target = os.path.realpath(real_target)
        log(f"Target resolved: {real_target}")

        if self.is_root_disk(real_target):
            log(f"OPERATION BLOCKED: {real_target} is part of the system root drive!", 'ERROR')
            return

        # Refuse to operate on mounted devices (soft erase is still destructive).
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', real_target], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

        if not self.extensive_confirm(real_target):
            return

        run_command(['sudo', '-v'])

        start_ts = time.time()
        log_path = _cmd_log_open("nuke") if (_CMD_LOG_FH is None) else _CMD_LOG_PATH
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
        real_target = self.resolve_target(name, allow_id=True)
        if not real_target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return
        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        real_target = os.path.realpath(real_target)
        if self._block_if_root_drive(real_target, f"entropise {name}"):
            return
        log(f"Target resolved: {real_target}")

        # Refuse to operate on mounted devices.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', real_target], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

        log(f"ENTROPISE WARNING: About to overwrite all data on {real_target} with high-entropy random bytes from /dev/urandom.")
        if not self.extensive_confirm(f"{name} ({real_target})"):
            return

        run_command(['sudo', '-v'])

        start_ts = time.time()
        log_path = _cmd_log_open("entropise") if (_CMD_LOG_FH is None) else _CMD_LOG_PATH
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
                    'count_bytes',
                    'iflag=fullblock',
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
        args = arg.split()
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

        if self.is_root_disk(dst_real):
            log(f"OPERATION BLOCKED: {dst_name} ({dst_real}) is the system root drive!", 'ERROR')
            return

        # Compare sizes
        try:
            src_bytes = int(run_command(['sudo', 'blockdev', '--getsize64', src_real], capture_output=True).stdout.strip())
            dst_bytes = int(run_command(['sudo', 'blockdev', '--getsize64', dst_real], capture_output=True).stdout.strip())

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
            res_ss = run_command(['sudo', 'blockdev', '--getss', src_real], capture_output=True, check=False)
            ss = (getattr(res_ss, 'stdout', '') or '').strip()
            if ss.isdigit():
                sector_size = int(ss)
        except Exception:
            sector_size = 512

        # Refuse to run if anything on the destination device tree is mounted.
        # (Whole-disk clone should not proceed if any target partitions are mounted.)
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', dst_real], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"Target has mounted filesystems ({', '.join(mounts)}). Unmount/close {dst_name} first.", 'ERROR')
                return
        except Exception:
            pass

        # Confirmation
        if not self.extensive_confirm(f"{dst_name} ({dst_real})"):
            return

        run_command(['sudo', '-v'])
        log(f"Cloning {src_name} -> {dst_name}...")
        start_ts = time.time()

        # Perform clone
        try:
            safe_src = re.sub(r'[^A-Za-z0-9_.-]+', '_', src_name)
            safe_dst = re.sub(r'[^A-Za-z0-9_.-]+', '_', dst_name)
            mapdir = Path('/tmp/diskmgr_clone_maps')
            os.makedirs(mapdir, exist_ok=True)
            mapfile = str(mapdir / f"{safe_src}_to_{safe_dst}.map")

            log(f"ddrescue mapfile: {mapfile}")
            # Pass 1: fast clone + mapfile progress.
            cmd = [ddrescue_bin, '--force', src_real, dst_real, mapfile]
            res1 = run_command(cmd, sudo=True, capture_output=False, check=False)

            # Pass 2: retry failed areas (up to 3 times).
            log("ddrescue retry pass: -r3 (retry failed sectors up to 3 times)...")
            cmd_retry = [ddrescue_bin, '--force', '-r3', src_real, dst_real, mapfile]
            res2 = run_command(cmd_retry, sudo=True, capture_output=False, check=False)
            run_command(['sync'], sudo=True, check=False)

            # Report failures based on mapfile after retries.
            failed = _parse_ddrescue_failed_ranges(mapfile, sector_size=sector_size)
            if failed:
                log(f"Unrecovered sectors remain after retries (mapfile: {mapfile}).", 'WARN')
                print(f"\n{Colors.FAIL}{Colors.BOLD}Unrecovered sector ranges:{Colors.ENDC}")
                max_show = 80
                for r in failed[:max_show]:
                    # LBA range shown as [start, end) for clarity.
                    print(f"  LBA [{r['start_lba']}, {r['end_lba']})  count={r['count_lba']}  bytes={r['size_b']}")
                if len(failed) > max_show:
                    print(f"  ... truncated ({len(failed)} ranges total)")
                print(f"\n{Colors.WARNING}Mapfile:{Colors.ENDC} {mapfile}")
            else:
                log("Cloning complete (no unrecovered sectors reported in the ddrescue mapfile).")

            # ddrescue uses bitmask exit codes; preserve them in logs for troubleshooting.
            rc1 = getattr(res1, 'returncode', 0)
            rc2 = getattr(res2, 'returncode', 0)
            if rc1 != 0 or rc2 != 0:
                log(f"ddrescue exit status: pass1={rc1}, retry={rc2} (non-zero can indicate read errors even if output is usable).", 'WARN')
        except Exception as e:
            log(f"Cloning failed: {e}", 'ERROR')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
