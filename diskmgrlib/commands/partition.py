"""PartitionCommands command implementations."""

from ..common import *
from ..shell_core import CmdArgumentParser


class PartitionCommands:

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

        # Refuse to operate on mounted devices.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', real_target], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

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

        if not self.extensive_confirm(real_target):
            return

        run_command(['sudo', '-v'])
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
                for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                    cols = line.strip().split()
                    if len(cols) >= 2 and cols[1] == 'part':
                        created_parts.append(f"/dev/{cols[0]}")
            except Exception:
                created_parts = []

            if created_parts:
                if creating_table:
                    log(f"Created partition table ({table}) and partition(s): {', '.join(created_parts)}")
                else:
                    log(f"Created partition(s): {', '.join(created_parts)}")
            else:
                if creating_table:
                    log(f"Created partition table ({table}) and requested one partition.", 'WARN')
                else:
                    log("Requested partition creation completed, but no new partition was detected.", 'WARN')
        elif creating_table:
            log(f"Created empty partition table: {table} on {real_target}")

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

        # Refuse to remove mounted partition.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', part_dev], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {part_dev} is mounted at {', '.join(mounts)}. Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

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
        if not self.extensive_confirm(part_dev):
            return

        run_command(['sudo', '-v'])
        run_command(['parted', '-s', parent_disk, 'rm', partn], sudo=True)
        run_command(['udevadm', 'settle'], sudo=True, check=False)
        run_command(['partprobe', parent_disk], sudo=True, check=False)

        log(f"Removed partition {part_dev} from {parent_disk}")
