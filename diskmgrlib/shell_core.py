"""Interactive shell lifecycle and built-in command plumbing."""

import argparse
import atexit
import cmd
import json
import os
import re
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

from .devices import _lsblk_type
from .mappings import read_luks_map
from .runtime import (
    Colors,
    DEFAULT_HISTORY_FILE,
    HISTORY_FILE_ENV,
    LUKS_HEADER_BACKUP_DIR,
    LUKS_PBKDF_DEFAULT_MEMORY_LABEL,
    LUKS_PBKDF_DEFAULT_THREADS,
    LUKS_PBKDF_DEFAULT_TIME,
    MAX_HISTORY_ENTRIES,
    VERSION,
    command_failed,
    log,
    reset_command_status,
    run_command_hard_timeout,
)


class CmdArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise argparse.ArgumentError(None, message)


class ShellCoreMixin:
    intro = 'Welcome to diskmgr. Type help or ? to list commands.\n'
    prompt = '(diskmgr) '

    def __init__(self):
        super().__init__()
        if readline is not None:
            # Mark ANSI escapes as non-printing so readline cursor math stays correct.
            self.prompt = f'\001{Colors.OKGREEN}\002(diskmgr) \001{Colors.ENDC}\002'
        else:
            self.prompt = f'{Colors.OKGREEN}(diskmgr) {Colors.ENDC}'
        self.mappings = read_luks_map()
        self.unmapped_cache = []
        self.id_cache = {}
        self.missing_map_id_cache = {}
        self._ext4_tune2fs_cache = {}
        self.last_command_status = 0
        self.history_file = Path(os.environ.get(HISTORY_FILE_ENV, str(DEFAULT_HISTORY_FILE))).expanduser()
        self.history_enabled = False
        self._init_history()

    def emptyline(self):
        """Do nothing on blank input (disable cmd.Cmd last-command repeat)."""
        return

    def onecmd(self, line):
        """Execute one command with status tracking and an exception boundary."""
        reset_command_status()
        self.last_command_status = 0
        if '\n' in str(line) or '\r' in str(line):
            log(
                "Multiline input was not executed. Paste the command, review it, then press Enter.",
                'ERROR',
            )
            self.last_command_status = 2
            return False
        try:
            stop = super().onecmd(line)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"Command failed unexpectedly: {type(exc).__name__}: {exc}", 'ERROR')
            stop = False
        self.last_command_status = 1 if command_failed() else 0
        return stop

    def default(self, line):
        command = str(line or '').split(None, 1)[0] if str(line or '').strip() else ''
        log(f"Unknown command: {command or '<empty>'}. Type 'help' for available commands.", 'ERROR')

    def _init_history(self):
        if readline is None:
            return
        try:
            # Prevent pasted multiline text from auto-submitting line-by-line.
            readline.parse_and_bind("set enable-bracketed-paste on")
            # Support Ctrl+Backspace in terminals that emit a distinct escape sequence.
            # Do not remap plain Backspace (^H/^?) because many terminals send that for
            # normal character deletion and readline cannot always distinguish them.
            for seq in (
                '"\\e[127;5u": backward-kill-word',
                '"\\e[8;5~": backward-kill-word',
                '"\\e[7;5~": backward-kill-word',
                '"\\e[3;5~": backward-kill-word',
                '"\\e\\C-?": backward-kill-word',
                '"\\e\\C-h": backward-kill-word',
            ):
                readline.parse_and_bind(seq)
        except Exception:
            pass
        try:
            readline.read_history_file(str(self.history_file))
        except FileNotFoundError:
            pass
        except Exception:
            # Best-effort: shell remains usable even if history cannot be read.
            pass
        try:
            readline.set_history_length(MAX_HISTORY_ENTRIES)
        except Exception:
            pass
        atexit.register(self._save_history)
        self.history_enabled = True

    def _save_history(self):
        if not self.history_enabled or readline is None:
            return
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            readline.set_history_length(MAX_HISTORY_ENTRIES)
            readline.write_history_file(str(self.history_file))
        except Exception:
            # Best-effort: do not block shell exit on history write errors.
            pass

    def _input_no_history(self, prompt):
        """
        Read one line from stdin without leaving the entered text in readline history.
        Used for confirmation/math answers so command history stays clean.
        """
        if readline is None:
            return input(prompt)

        try:
            before = int(readline.get_current_history_length() or 0)
        except Exception:
            before = 0

        try:
            val = input(prompt)
        finally:
            # Best-effort: strip any entries added during this prompt.
            try:
                after = int(readline.get_current_history_length() or 0)
                while after > before:
                    readline.remove_history_item(after - 1)
                    after -= 1
            except Exception:
                pass
        return val

    def get_disk_info(self):
        # Use lsblk -J for JSON output
        cmd = ['lsblk', '-J', '-e', '7', '-o', 'NAME,KNAME,TYPE,RM,SIZE,FSTYPE,LABEL,MOUNTPOINT,MODEL,WWN,PKNAME,TRAN,DISC-MAX']
        try:
            res = run_command_hard_timeout(cmd, 8, capture_output=True)
            data = json.loads(res.stdout)
            return data.get('blockdevices', [])
        except Exception as e:
            log(f"Failed to list disks: {e}", 'ERROR')
            return []

    def flatten_disks(self, devices):
        """Recursively flatten the lsblk tree structure."""
        flat = []
        for dev in devices:
            flat.append(dev)
            if 'children' in dev:
                flat.extend(self.flatten_disks(dev['children']))
        return flat

    def find_persistent_path(self, dev_node, wwn=None, type_='disk'):
        # Try to find a /dev/disk/by-id/ match.
        #
        # Requirement: prefer a by-id link that contains the IEEE identifier (WWN/EUI),
        # e.g. "wwn-0x..." for SATA/SCSI or "nvme-eui...." for NVMe, when available.
        # For partitions, prefer the corresponding "-partN" link.

        # 1. Try WWN logic from opendisk
        if wwn:
            prefix = "nvme-" if str(wwn).startswith("eui.") else "wwn-"
            base = f"/dev/disk/by-id/{prefix}{wwn}"
            candidates = []

            if type_ == 'part':
                # Prefer partition-scoped by-id links if this is a partition.
                m = re.search(r"p?([0-9]+)$", str(dev_node))
                if m:
                    candidates.append(f"{base}-part{m.group(1)}")
            candidates.append(base)

            try:
                target = os.path.realpath(f"/dev/{dev_node}")
                for c in candidates:
                    if os.path.exists(c) and os.path.realpath(c) == target:
                        return c
            except Exception:
                pass

        # 2. Brute force check /dev/disk/by-id
        by_id_dir = Path('/dev/disk/by-id')
        if by_id_dir.exists():
            matches = []
            for link in by_id_dir.iterdir():
                try:
                    if link.resolve() == Path(f"/dev/{dev_node}").resolve():
                        matches.append(str(link))
                except Exception:
                    continue

            if matches:
                # Prefer identifiers that include IEEE IDs when present.
                def _score(p):
                    b = os.path.basename(p)
                    if b.startswith('nvme-eui.'):
                        return 0
                    if b.startswith('wwn-'):
                        return 1
                    return 2
                matches.sort(key=_score)
                return matches[0]

        return "-"

    def _normalize_mapping_target(self, target_path):
        """Replace an existing raw disk/partition path with its by-id path."""
        raw = str(target_path or '').strip()
        if not raw or not os.path.exists(raw):
            return raw
        try:
            real = os.path.realpath(raw)
            target_type = _lsblk_type(real)
            if target_type not in ('disk', 'part'):
                return raw
            persistent = self.find_persistent_path(os.path.basename(real), type_=target_type)
            if persistent and persistent != '-':
                return persistent
        except Exception:
            pass
        return raw

    def find_serial_wwid_path(self, dev_node, type_='disk'):
        """
        Return a serial-style /dev/disk/by-id path for a disk/partition when available.
        Prefers model/serial style IDs (e.g. nvme-..., ata-...) over IEEE/EUI links.
        """
        if type_ not in ('disk', 'part'):
            return "-"
        by_id_dir = Path('/dev/disk/by-id')
        if not by_id_dir.exists():
            return "-"
        try:
            target = Path(f"/dev/{dev_node}").resolve()
        except Exception:
            return "-"

        matches = []
        for link in by_id_dir.iterdir():
            try:
                name = link.name
                if type_ == 'disk' and re.search(r'-part[0-9]+$', name):
                    continue
                if type_ == 'part' and not re.search(r'-part[0-9]+$', name):
                    continue
                if link.resolve() == target:
                    matches.append(str(link))
            except Exception:
                continue
        if not matches:
            return "-"

        def _score(p):
            b = os.path.basename(p)
            if type_ == 'part' and re.search(r'-part[0-9]+$', b):
                return -1
            if b.startswith('nvme-') and not b.startswith('nvme-eui.'):
                return 0
            if b.startswith('ata-'):
                return 1
            if b.startswith('scsi-'):
                return 2
            if b.startswith('usb-'):
                return 3
            if b.startswith('wwn-') or b.startswith('nvme-eui.'):
                return 9
            return 5

        matches.sort(key=lambda p: (_score(p), p))
        return matches[0]

    def find_pci_path(self, dev_node, type_='disk'):
        """Return a /dev/disk/by-path PCI link for a disk/partition when available."""
        if type_ not in ('disk', 'part'):
            return "-"
        by_path_dir = Path('/dev/disk/by-path')
        if not by_path_dir.exists():
            return "-"
        try:
            target = Path(f"/dev/{dev_node}").resolve()
        except Exception:
            return "-"

        matches = []
        for link in by_path_dir.iterdir():
            try:
                name = link.name
                if 'pci-' not in name:
                    continue
                if type_ == 'disk' and re.search(r'-part[0-9]+$', name):
                    continue
                if type_ == 'part' and not re.search(r'-part[0-9]+$', name):
                    continue
                if link.resolve() == target:
                    matches.append(str(link))
            except Exception:
                continue
        if not matches:
            return "-"
        matches.sort()
        return matches[0]

    def do_help(self, arg):
        'List available commands with "help" or detailed help with "help cmd".'
        if arg:
            super().do_help(arg)
            return

        print(f"\n{Colors.HEADER}Disk Manager (diskmgr){Colors.ENDC}")
        print("A utility to manage mapped disks/partitions, encrypted containers, and filesystems.")
        print("Mappings point to persistent device paths so names remain stable across reboots/ports.\n")

        print(f"{Colors.BOLD}all:{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}list [concise|verbose]{Colors.ENDC}")
        print("      Displays disk layout (table by default).")
        print("      Modes: list (standard table), list concise (compact table), list verbose/list list (key/value).")
        print(f"  {Colors.OKGREEN}boot{Colors.ENDC}")
        print("      Displays boot entries/submenus from GRUB and /etc/fstab detection per partition.")

        print(f"\n{Colors.BOLD}disk:{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}create <name/id> [--gpt|--mbr] [--partition] [--start X] [--end Y]{Colors.ENDC}")
        print("      Creates a new GPT/MBR partition table on a whole disk (mapping name or discovery ID).")
        print("      Also supports adding another partition to an existing partitioned disk via --partition.")
        print("      If --start/--end are omitted in partition-only mode, uses the largest free extent.")
        print("      Overlapping ranges are rejected by parted; existing partitions are not overwritten.")
        print("      Safety policy for table creation: disk must be erased first (run: erase <name>).")
        print(f"  {Colors.OKGREEN}selftest <name/id>{Colors.ENDC}")
        print("      Starts a SMART long self-test (smartctl -t long) for the underlying disk")
        print("      (USB uses -d sat). Use: selftest <name/id> --watch to poll progress until complete.")
        print(f"  {Colors.OKGREEN}health <name/id> [alias: smart]{Colors.ENDC}")
        print("      Shows SMART health (smartctl -x) for the underlying disk (USB uses -d sat).")
        print(f"  {Colors.OKGREEN}entropy <name/id> --begin <START> --end <END> [--step SIZE] [--window SIZE]{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}entropy <name/id> <SPAN> --samples <N>{Colors.ENDC}")
        print("      Samples Shannon entropy and plots it (built-in sampler + gnuplot).")
        print("      Random mode stitches N random windows across the disk into one contiguous graph span.")
        print("      Saves data/plot in /tmp and attempts to display the plot on screen.")

        print(f"\n{Colors.BOLD}disk/part (applied to mapped disk/partition targets):{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}map <name/id> <name>{Colors.ENDC}")
        print("      Assigns a friendly name to a disk/partition or renames an existing mapping.")
        print("      Discovery IDs use #N format (example: map #1 backup).")
        print(f"  {Colors.OKGREEN}unmap <name/id>{Colors.ENDC}")
        print("      Removes an existing mapping by name, or by discovery ID (#N).")
        print(f"  {Colors.OKGREEN}format <name/id> [options]{Colors.ENDC}")
        print("      Whole disk target: creates a superfloppy-style volume and mounts it.")
        print("      Partition target: formats inside that partition boundary (not a superfloppy), then mounts it.")
        print("      Default is plain format. Use --luks to run cryptsetup luksFormat, open a mapper,")
        print(f"      and mkfs the decrypted payload filesystem (PBKDF defaults: memory={LUKS_PBKDF_DEFAULT_MEMORY_LABEL}, parallelism={LUKS_PBKDF_DEFAULT_THREADS}, time={LUKS_PBKDF_DEFAULT_TIME}).")
        print("      Use --detached-header (optionally with a file path) to keep LUKS metadata off-disk from the start.")
        print(f"      Detached header default path: {LUKS_HEADER_BACKUP_DIR}/<name>")
        print("      Example: format data --luks")
        print("      Whole disks must be unpartitioned. format never creates, deletes, resizes, or moves partitions;")
        print("      it only writes a filesystem/LUKS+filesystem inside the selected disk or partition target.")
        print("      Newly created filesystem roots are chowned to the invoking user.")
        print("      Use: erase <name> first if the whole disk is currently partitioned.")
        print(f"  {Colors.OKGREEN}erase <name/id>{Colors.ENDC}")
        print("      Fast metadata wipe for re-provisioning (wipefs + zap GPT/MBR metadata) on disk/part.")
        print("      Whole-disk erase wipes partition signatures/metadata, GPT headers, and protective MBR metadata;")
        print("      it leaves the device without a newly-created partition table. Use 'create' afterward if needed.")
        print(f"  {Colors.OKGREEN}nuke <name/id>{Colors.ENDC}")
        print("      Secure erase (multi-step hardware-aware wipe) on disk/part.")
        print(f"  {Colors.OKGREEN}entropise <name/id>{Colors.ENDC}")
        print("      Single-pass high-entropy overwrite from /dev/urandom on disk/part.")
        print(f"  {Colors.OKGREEN}remove <name/id>{Colors.ENDC}")
        print("      Removes a partition from its parent disk (partition targets only).")
        print(f"  {Colors.OKGREEN}clone <src_name/id> <dst_name/id>{Colors.ENDC}")
        print("      Bit-perfect block-level clone using ddrescue (requires target >= source size).")
        print("      Includes a multi-pass rescue phase to recover data from failing sectors.")

        print(f"\n{Colors.BOLD}file system (applied to disk/part entries with mountable FSTYPE):{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}open <name/id> [--compress MODE | --compress-force MODE]{Colors.ENDC}")
        print("      Opens and mounts a plain or encrypted partition or superfloppy.")
        print("      For encrypted targets, unlocks LUKS then mounts payload filesystem.")
        print("      LUKS open tries on-disk header first, then ~/.local/share/diskmgr/<name> as detached header backup.")
        print("      Btrfs default mount policy: HDD => compress-force=zstd:12, non-HDD => no compression option.")
        print("      Override per open/remount with --compress=<mode> or --compress-force=<mode>.")
        print("      Mountpoint selection: /etc/fstab entry wins when present (uses fstab mountpoint/options).")
        print("      Without fstab: /media/$USER/<mapped-name> when opened by mapping name;")
        print("      else /media/$USER/<FSLABEL>; else /media/$USER/<device>.")
        print("      If the same source is mounted multiple times (e.g. <label> and <label>1),")
        print("      open keeps the preferred path and unmounts extra mountpoints.")
        print("      If the preferred path references a missing /dev source, open clears that stale mount first.")
        print("      If the preferred path is occupied by an existing different device, open is blocked.")
        print("      Device discovery waits up to 60 seconds and reports slow USB bridge/disk spin-up progress.")
        print("      A separate non-root partition on the same physical disk as / may be opened; the root partition and disk remain protected.")
        print(f"  {Colors.OKGREEN}close <name/id> [--force]{Colors.ENDC}")
        print("      close <name>: unmounts filesystem(s) and closes /dev/mapper/<name> when present (locks LUKS).")
        print("      If a whole-disk target is supplied, closes all mounted child partitions on that disk too.")
        print("      close #id (example: close #6): unmount-only for that discovered row; does NOT run cryptsetup close.")
        print("      --force kills userspace holders, but cannot kill kernel D-state writeback operations.")
        print("      close flushes each filesystem, then waits up to 30 seconds for blocked kernel I/O recovery.")
        print("      Kernel detach is attempted only after every filesystem on the disk is unmounted.")
        print("      A vanished source is treated as an unplug; stale mount directories are removed after kernel teardown.")
        print("      Only one rescue detach can be attempted by each close command.")
        print("      With --force, kills mount-holder processes (SIGKILL) before flushing and normal unmount.")
        print("      Rescue kernel-detach is last-resort and only allowed with --force.")
        print("      Use #id if you want to close only the payload filesystem and keep the LUKS container open.")
        print(f"  {Colors.OKGREEN}luks <passwd|params|backup|restore|header|wipe> [options]{Colors.ENDC}")
        print("      LUKS management for mapped containers (grouped under filesystem workflows):")
        print("      password change, PBKDF-memory tuning, header backup/print, and header restore.")
        print("      wipe overwrites the LUKS header/keyslot area with random data (destructive test helper).")
        print("      passwd/params auto-fallback to detached header ~/.local/share/diskmgr/<name> when on-disk header is missing.")
        print("      passwd uses passgen for current passphrase and double-prompts new passphrase for confirmation.")
        print(f"      params uses passgen for current passphrase and updates PBKDF params (defaults: time={LUKS_PBKDF_DEFAULT_TIME}, parallelism={LUKS_PBKDF_DEFAULT_THREADS}).")
        print(f"      backup default path: {LUKS_HEADER_BACKUP_DIR}/<name>")
        print(f"  {Colors.OKGREEN}label <name> [new_label] [--fstab]{Colors.ENDC}")
        print("      Get or set the filesystem label of an OPEN disk.")
        print("      On relabel: removes old LABEL-based fstab entry. With --fstab, adds UUID entry at /mnt/<label>.")
        print("      Generated fstab options: defaults,nofail,x-gvfs-show,x-gvfs-name=<label>.")
        print("      For btrfs on HDD targets, generated fstab options also include compress-force=zstd:12.")
        print("      For LUKS: acts on payload filesystem when open; errors if locked.")
        print(f"  {Colors.OKGREEN}remount <name> [--compress MODE | --compress-force MODE]{Colors.ENDC}")
        print("      Move an OPEN disk's mount to /mnt/<label> when /etc/fstab entry exists;")
        print("      otherwise /media/$USER/<label>, and clean old empty mountpoint dirs.")
        print("      For LUKS: acts on payload filesystem when open; errors if locked.")
        print(f"  {Colors.OKGREEN}sync <pri_name> <sec_name>{Colors.ENDC}")
        print("      Syncs two mounted filesystems (rsync pri -> sec).")
        print("      Primary/source is copied FROM. Secondary/destination is replaced to match source.")
        print("      Runs a dry-run pre-scan first and shows real progress from planned-bytes completed.")
        print("      Endpoints may be mapped names or absolute directory paths.")
        print("      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.")
        print(f"  {Colors.OKGREEN}diff <pri_name> <sec_name> [--depth N] [-d] [--fast] [--checksum]{Colors.ENDC}")
        print("      Dry-run filesystem diff (rsync pri -> sec): shows create/modify/delete counts+bytes,")
        print("      Primary/source is copied FROM. Secondary/destination is what would be replaced.")
        print("      then a tree-style hierarchy summary of dirs/files (+new, ~updated, -deleted regular files).")
        print("      Depth default: 2. Use -d to show directories only. ")
        print("      Use --fast to print raw rsync -anH --delete --stats output only (no summaries).")
        print("      In --fast created(new+updated) regular files = 'Number of regular files transferred'.")
        print("      Use --checksum to compare file contents via rsync checksums (slower, ignores mtime-only changes).")
        print("      Endpoints may be mapped names/IDs or absolute directory paths.")
        print("      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.")
        print(f"  {Colors.OKGREEN}defrag <name> [--compress]{Colors.ENDC}")
        print("      Defragments a mounted filesystem and records user.last_defrag xattr.")
        print("      On btrfs, default runs defragment -r -v with live per-directory progress.")
        print("      Use --compress to add -czstd recompression,")
        print("      then balance start -dusage=50 and live balance status monitoring.")
        print("      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.")
        print(f"  {Colors.OKGREEN}fshealth <name>{Colors.ENDC}")
        print("      Shows filesystem diagnostics, last_defrag/last_scrub xattrs, and extents/files ratios.")
        print("      ext4: <1.1 healthy, >1.5 bad, >5 critical. btrfs: <1 good, >5 bad, >20 critical.")
        print("      For btrfs, also shows filesystem/device stats and scrub status.")
        print("      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.")
        print(f"  {Colors.OKGREEN}convert <name/id>{Colors.ENDC}")
        print("      Converts an UNMOUNTED ext4 filesystem to btrfs in place (btrfs-convert).")
        print("      Preserves data, supports plain ext4 and open LUKS payload ext4 when resolvable.")
        print("      For safety, target must be unmounted/closed before conversion.")
        print(f"  {Colors.OKGREEN}scrub <name> [--no-watch]{Colors.ENDC}")
        print("      Runs a blocking btrfs scrub on a mounted filesystem and records user.last_scrub xattr.")
        print("      By default tails kernel checksum/error logs and resolves paths when possible.")
        print("      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.")

        print(f"\n{Colors.BOLD}shell:{Colors.ENDC}")
        print(f"  {Colors.OKGREEN}version{Colors.ENDC}")
        print("      Print diskmgr version.")
        print(f"      Command history persists across sessions in {self.history_file} (override with ${HISTORY_FILE_ENV}).")
        print(f"  {Colors.OKGREEN}exit / quit / Ctrl+D{Colors.ENDC}")
        print("      Exit the application.")

    def do_version(self, arg):
        'Print diskmgr version'
        print(f"diskmgr {VERSION}")

    def do_exit(self, arg):
        'Exit the application'
        self._save_history()
        return True

    def do_quit(self, arg):
        'Exit the application'
        self._save_history()
        return True

    def do_EOF(self, arg):
        'Exit the application'
        print("")
        self._save_history()
        return True
