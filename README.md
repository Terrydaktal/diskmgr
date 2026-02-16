# Disk Manager (diskmgr)

A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.

## Project Structure

```text
.
├── diskmap.tsv      # Configuration file storing disk mappings
├── diskmgr          # Main Python-based interactive CLI tool
├── gen_readme.py    # Script to regenerate this documentation
└── README.md        # This file
```

## File Descriptions

### `diskmgr` (Main Application)
- **Description**: A comprehensive interactive shell for managing disks, LUKS containers, and filesystems.
- **Inputs**: User commands via interactive shell or pipe; system hardware information via `lsblk`, `udevadm`, `cryptsetup`, etc.
- **Outputs**: Formatted tables, system state changes (mounts, encryption status), and updates to `diskmap.tsv`.

### `diskmap.tsv` (Configuration)
- **Description**: Tab-separated values file that stores the mapping between user-defined friendly names and persistent device paths (e.g., `/dev/disk/by-id/...`).
- **Format**: `<friendly_name>\t<persistent_device_path>`

### `gen_readme.py` (Documentation Generator)
- **Description**: Automates the generation of `README.md` by querying `diskmgr`'s help system and examples.
- **Inputs**: `diskmgr` help output and command examples.
- **Outputs**: An updated `README.md` file.

## Overview

```text
A utility to manage mapped disks/partitions, encrypted containers, and filesystems.
Mappings point to persistent device paths so names remain stable across reboots/ports.

all:
  list [verbose]
      Displays disk layout (table by default).
      Use verbose for key/value entries (alias: list list).
  boot
      Displays boot entries/submenus from GRUB and /etc/fstab detection per partition.

disk:
  create <name/id> [--gpt|--mbr] [--partition] [--start X] [--end Y]
      Creates a new GPT/MBR partition table on a whole disk (mapping name or discovery ID).
      Also supports adding another partition to an existing partitioned disk via --partition.
      If --start/--end are omitted in partition-only mode, uses the largest free extent.
      Overlapping ranges are rejected by parted; existing partitions are not overwritten.
      Safety policy for table creation: disk must be erased first (run: erase <name>).
  selftest <name/id>
      Starts a SMART long self-test (smartctl -t long) for the underlying disk
      (USB uses -d sat). Use: selftest <name/id> --watch to poll progress until complete.
  health <name/id> [alias: smart]
      Shows SMART health (smartctl -a) for the underlying disk (USB uses -d sat).

disk/part (applied to mapped disk/partition targets):
  map <name/id> <name>
      Assigns a friendly name to a disk/partition or renames an existing mapping.
      Discovery IDs use #N format (example: map #1 backup).
  unmap <name/id>
      Removes an existing mapping by name, or by discovery ID (#N).
  format <name/id> [options]
      Whole disk target: creates a superfloppy-style volume and mounts it.
      Partition target: formats inside that partition boundary (not a superfloppy), then mounts it.
      Default is plain format. Use --luks to run cryptsetup luksFormat, open a mapper,
      then mkfs the decrypted payload filesystem inside the LUKS container.
      Whole disks must be unpartitioned. format never creates, deletes, resizes, or moves partitions;
      it only writes a filesystem/LUKS+filesystem inside the selected disk or partition target.
      Use: erase <name> first if the whole disk is currently partitioned.
  erase <name/id>
      Fast metadata wipe for re-provisioning (wipefs + zap GPT/MBR metadata) on disk/part.
      Whole-disk erase wipes partition signatures/metadata, GPT headers, protective MBR metadata,
      and rewrites an empty MBR table in MBR mode.
  nuke <name/id>
      Secure erase (multi-step hardware-aware wipe) on disk/part.
  remove <name/id>
      Removes a partition from its parent disk (partition targets only).
  clone <src_name/id> <dst_name/id>
      Bit-perfect block-level clone using ddrescue (requires target >= source size).
      Includes a multi-pass rescue phase to recover data from failing sectors.

file system (applied to disk/part entries with mountable FSTYPE):
  open <name/id>
      Opens and mounts a plain or encrypted superfloppy disk/partition.
      For encrypted targets, unlocks LUKS then mounts payload filesystem.
      Btrfs mounts are enforced with compression (compress=zstd:3).
      Uses /etc/fstab mountpoint/options when an entry exists; otherwise /media/$USER/<label>.
  close <name/id>
      close <name>: unmounts filesystem(s) and closes /dev/mapper/<name> when present (locks LUKS).
      close #id (example: close #6): unmount-only for that discovered row; does NOT run cryptsetup close.
      Use #id if you want to close only the payload filesystem and keep the LUKS container open.
  luks <passwd|backup|restore|header>
      LUKS management for mapped containers (grouped under filesystem workflows):
      password change, header backup/print, and header restore.
  label <name> [new_label] [--fstab]
      Get or set the filesystem label of an OPEN disk.
      On relabel: removes old LABEL-based fstab entry. With --fstab, adds UUID entry at /mnt/<label>.
      Generated fstab options: defaults,nofail,x-gvfs-show,x-gvfs-name=<label>.
      For LUKS: acts on payload filesystem when open; errors if locked.
  remount <name>
      Move an OPEN disk's mount to /mnt/<label> when /etc/fstab entry exists;
      otherwise /media/$USER/<label>, and clean old empty mountpoint dirs.
      For LUKS: acts on payload filesystem when open; errors if locked.
  sync <pri_name> <sec_name>
      Syncs two mounted filesystems (rsync pri -> sec).
      Primary/source is copied FROM. Secondary/destination is replaced to match source.
      Shows global rsync progress via --info=progress2.
      Endpoints may be mapped names or absolute directory paths.
      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.
  diff <pri_name> <sec_name> [--depth N] [-d] [--fast] [--checksum]
      Dry-run filesystem diff (rsync pri -> sec): shows create/modify/delete counts+bytes,
      Primary/source is copied FROM. Secondary/destination is what would be replaced.
      then a tree-style hierarchy summary of dirs/files (+new, ~updated, -deleted regular files).
      Depth default: 2.
      Use -d to show directories only.
      Use --fast to print raw rsync -an --delete --stats output only (no summaries).
      Use --checksum to compare file contents via rsync checksums (slower, ignores mtime-only changes).
      In --fast, created(new+updated) regular files = 'Number of regular files transferred'.
      Unchanged entries are shown uncolored.
      Endpoints may be mapped names/IDs or absolute directory paths.
      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.
  defrag <name> [--compress]
      Defragments a mounted filesystem and records user.last_defrag xattr.
      On btrfs, default runs defragment -r -v with live per-directory progress.
      Use --compress to add -czstd recompression,
      then balance start -dusage=50 and live balance status monitoring.
      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.
  fshealth <name>
      Shows filesystem diagnostics, last_defrag/last_scrub xattrs, and extents/files ratios.
      ext4: <1.1 healthy, >1.5 bad, >5 critical. btrfs: <1 good, >5 bad, >20 critical.
      For btrfs, also shows filesystem/device stats and scrub status.
      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.
  convert <name/id>
      Converts an UNMOUNTED ext4 filesystem to btrfs in place (btrfs-convert).
      Preserves data, supports plain ext4 and open LUKS payload ext4 when resolvable.
      For safety, target must be unmounted/closed before conversion.
  scrub <name> [--no-watch]
      Runs a blocking btrfs scrub on a mounted filesystem and records user.last_scrub xattr.
      By default tails kernel checksum/error logs and resolves paths when possible.
      For LUKS: resolves to payload filesystem when open; errors if locked/not mounted.

shell:
  version
      Print diskmgr version.
      Command history persists across sessions in /home/lewis/.local/state/diskmgr/history (override with $DISKMGR_HISTORY).
  exit / quit / Ctrl+D
      Exit the application.
```

## Command Reference: `list`

```text
Display the physical partition layout and free space for all plugged-in disks.
        Usage:
          list            -> standard table (default)
          list verbose    -> verbose key/value entries (alias: list list)

        UNDER THE HOOD:
        1.  Hardware Scan: Identifies all physical 'disk' devices (excluding partitions).
        2.  Geometry Query: Runs 'sudo parted -m <dev> unit s print free' and 'blockdev --getsz'.
        3.  Parsing:
            - Extracts Partition Table type (gpt/mbr) and sector sizes.
            - Calculates total logical sectors from blockdev output.
        4.  Formatting:
            - Adds GPT metadata blocks (Primary/Backup) if applicable.
            - Identifies 'free' space segments.
            - Calculates MiB and GiB values from sector counts.
```

### Example Output

```text
Disk: /dev/sda (ST1000LM035-1RK172) [gpt] [Sector: L512/P4096] [Total Sectors: 1953525168]
[ GPT Primary 34s (17408.00B) ] [ free 2014s (1007.00KiB) ] [ sda1 - 262144s (128.00MiB) (msftres, no_automount) ] [ sda2 crypto_LUKS 1953259520s (953740.00MiB ≈ 931.4GiB) (msftdata) ] [ free 1423s (711.50KiB) ] [ GPT Backup 33s (16896.00B) ]

 #   NAME  DEVICE           TYPE   FSTYPE       FSLABEL  FSUUID                                SIZE        FSAVAIL     FSMOUNTPOINTS    PERSISTENT PATH (IEEE)
 1   -     sda              disk                                                               931.51 GiB                               /dev/disk/by-id/wwn-0x5000c500a89d6e44
 2   -     ├─sda1           part                                                               128.00 MiB                               /dev/disk/by-id/wwn-0x5000c500a89d6e44-part1
 3   1a    └─sda2           part   crypto_LUKS           e038a8b5-d3a7-4bbb-bbea-5bed8cc07a04  931.39 GiB                               /dev/disk/by-id/wwn-0x5000c500a89d6e44-part2
 4   -         └─dm-0 (1a)  crypt  ext4         1a       5933d845-1098-4f16-ad7f-ff1f4a4a2105  931.37 GiB  18.32 GiB   /media/lewis/1a  -
-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/sdb (ST2000DM008-2FR102) [none] [Sector: L512/P4096] [Total Sectors: 3907029168]
[ sdb crypto_LUKS 3907029168s (1907729.09MiB ≈ 1863.0GiB (1.819 TiB)) ]

 #   NAME  DEVICE           TYPE   FSTYPE       FSLABEL  FSUUID                                SIZE        FSAVAIL     FSMOUNTPOINTS    PERSISTENT PATH (IEEE)
 5   1b    sdb              disk   crypto_LUKS           885a66c1-6d5f-4d24-adfd-e7c7975dfe65  1.82 TiB                                 /dev/disk/by-id/wwn-0x5000c500e31e6cb2
 6   -     └─dm-1 (1b)      crypt  btrfs        1b       08aad883-1143-4d5d-84b9-d715665e332a  1.82 TiB    1.07 TiB    /media/lewis/1b  -
-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme0n1 (WD_BLACK SN8100 2000GB) [msdos] [Sector: L512/P512] [Total Sectors: 3907029168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme0n1p1 ext4 3907026944s (1907728.00MiB ≈ 1863.0GiB (1.819 TiB)) (boot) ] [ free 176s (88.00KiB) ]

 #   NAME  DEVICE           TYPE   FSTYPE       FSLABEL  FSUUID                                SIZE        FSAVAIL     FSMOUNTPOINTS    PERSISTENT PATH (IEEE)
 7   -     nvme0n1          disk                                                               1.82 TiB                                 /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852
 8   os    └─nvme0n1p1      part   ext4                  88f1dad3-95c6-418e-bea8-f5f3e072ea29  1.82 TiB    991.50 GiB  /                /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852-part1
-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme1n1 (WD Blue SN570 1TB) [msdos] [Sector: L512/P512] [Total Sectors: 1953525168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme1n1p1 ext4 1953523120s (953868.71MiB ≈ 931.5GiB) ]

 #   NAME  DEVICE           TYPE   FSTYPE       FSLABEL  FSUUID                                SIZE        FSAVAIL     FSMOUNTPOINTS    PERSISTENT PATH (IEEE)
 9   -     nvme1n1          disk                                                               931.51 GiB                               /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9
 10  data  └─nvme1n1p1      part   ext4         data     72c22012-b161-4e2a-a762-94ff7fda47f9  931.51 GiB  398.08 GiB  /mnt/data        /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9-part1
-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
```

## Command Reference: `boot`

```text
Display boot entries from GRUB and fstab detection for each partition.

        UNDER THE HOOD:
        Scans partition devices. If mounted, it parses /boot/grub/grub.cfg
        and checks for /etc/fstab inside that mounted partition.
        If unmounted or encrypted, it explains why it cannot yet read the config.
```

## Command Reference: `map`

```text
Create or modify a persistent mapping: map <name/id> <name>

        Usage:
          map [#1] backup    Assigns friendly name to discovery ID (e.g., map #1 backup)
          map 1a backup      Renames an existing mapping (e.g., map 1a backup)

        Note: Raw device paths (e.g., /dev/sdb) are NOT allowed.

        UNDER THE HOOD:
        1.  Input Resolution:
            - discovery ID (e.g., [#1]): Resolves the temporary device to its Persistent Device Path (PDP).
            - mapping name (e.g., 1a): Selects an existing mapping for RENAME operations.
        2.  PDP Linking: Extracts the /dev/disk/by-id/ path for the target hardware.
        3.  Conflict Check: Ensures the new friendly name is not already in use.
        4.  Persistence: Writes the [Name <TAB> PDP] pair to diskmap.tsv.

        This ensures the disk is recognized correctly regardless of USB port or device node changes.
```

## Command Reference: `unmap`

```text
Remove a persistent mapping: unmap <name/id>

        UNDER THE HOOD:
        1.  Resolution:
            - Name mode: removes the exact mapping name.
            - ID mode (#N): resolves to a device and removes mapping(s) pointing to that device.
        2.  Removal: Deletes the [Name <TAB> PDP] pair(s) from the internal dictionary.
        3.  Persistence: Re-writes diskmap.tsv with the mapping(s) removed.
```

## Command Reference: `create`

```text
Create partition table or partition on a whole disk: create <name/id> [--gpt|--mbr] [--partition] [--start X] [--end Y]

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
```

## Command Reference: `format`

```text
Format a superfloppy disk/partition volume: format <name/id> [options]

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
          --fs <ext4|xfs|btrfs>   Filesystem type (default: ext4)
          --label <label>   Set a different internal filesystem label (other than <name>)
          --luks            Encrypt target first with LUKS2, then format payload filesystem

        UNDER THE HOOD:
        1.  Safety: Refuses to run if anything is mounted on the target device tree.
        2.  Disk Type Policy:
            - If target is a whole disk, it must be unpartitioned (no GPT/MBR table present).
            - If target is a partition, format is applied directly within that partition.
        3.  LUKS Format (only when --luks is used):
            - Uses 'passgen' to generate a master key.
            - Runs 'cryptsetup luksFormat' with LUKS2 encryption.
            - Opens the container as /dev/mapper/<name>.
        4.  Filesystem:
            - Plain mode (default): formats target directly with ext4, xfs, or btrfs.
            - --luks mode: formats the opened mapper payload with ext4, xfs, or btrfs.
            - (ext4 only): Reclaims the 5% reserved space for root using 'tune2fs -m 0'.
        5.  Persistence: Adds the new disk's PDP to diskmap.tsv automatically (best-effort).

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.
```

## Command Reference: `erase`

```text
Fast metadata wipe (soft erase): erase <name/id> [--soft]

        This is a fast "re-provisioning" wipe. It removes recognizable signatures and zaps GPT/MBR metadata
        (when the target is a whole disk). It is NOT a secure wipe.

        It performs:
          - wipefs -a (and --force for whole-disk partition-table signatures)
          - sgdisk --zap-all (GPT) when available
          - sfdisk (MBR)

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.
```

## Command Reference: `nuke`

```text
Securely erase a disk: nuke <name/id>

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

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.

        WARNING: This operation is IRREVERSIBLE.
```

## Command Reference: `remove`

```text
Remove a partition from its parent disk: remove <name/id>

        Scope:
          - Partition targets only.
          - Whole-disk targets are refused.
```

## Command Reference: `selftest`

```text
Start a SMART long self-test: selftest <name/id>

        Runs smartctl long test against the underlying DISK device for the mapping.
        - If the mapping points to a partition, diskmgr targets the parent disk.
        - If the disk transport is USB and the device is /dev/sdX, diskmgr uses:
              smartctl -d sat -t long /dev/sdX
          (common for USB-SATA bridges).
```

## Command Reference: `health`

```text
Display SMART health for a mapped disk: health <name/id>

        Runs smartctl against the underlying DISK device for the mapping.
        - If the mapping points to a partition, diskmgr automatically targets the parent disk.
        - If the disk transport is USB and the device is /dev/sdX, diskmgr uses:
              smartctl -d sat -a /dev/sdX
          (common for USB-SATA bridges).
```

## Command Reference: `clone`

```text
Clone one disk or partition to another: clone <src_name/id> <dst_name/id>

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
        4.  Confirmation: Requires solving two math problems to authorize data destruction.
        5.  Cloning: Executes ddrescue in two phases:
            - Pass 1: 'ddrescue --force <src> <dst> <mapfile>'
            - Retry:  'ddrescue --force -r3 <src> <dst> <mapfile>'
        6.  Sync: Flushes kernel buffers to ensure all data is physically committed to disk.

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.

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
```

## Command Reference: `open`

```text
Unlock (if encrypted) and mount a disk: open <name/id>

        UNDER THE HOOD:
        1.  Identity Resolution: Looks up the friendly name in diskmap.tsv.
        2.  Hardware Wait: Polls for up to 10 seconds to allow for hardware spin-up/udev events.
        3.  Validation:
            - Runs 'cryptsetup isLuks' to check for encryption.
            - If NOT encrypted (Plain Disk):
              * Skips decryption step.
              * Verifies the existence of a valid filesystem.
              * Proceeds to label detection and mounting.
        4.  Decryption (LUKS only):
            - Executes 'passgen' to retrieve the passphrase.
            - Pipes the passphrase into 'cryptsetup open' to create a cleartext device in /dev/mapper/.
        5.  Mounting:
            - Uses /etc/fstab mountpoint/options when an entry exists for the device.
            - Otherwise identifies preferred mountpoint: /media/$USER/<label>.
            - If no hardware label is present, falls back to /media/$USER/<mapping_name>.
            - Ensures the directory exists and attaches the device.
            - For btrfs, enforces compression (compress=zstd:3) on the mounted filesystem.
        6.  Policy Enforcement: If the disk is already mounted at a non-standard path,
            it unmounts and remounts it to the preferred path.

        SAFETY NOTE:
        - If your mapping points to a whole disk (e.g. /dev/sda) but the actual LUKS/filesystem
          lives on a partition (e.g. /dev/sda2), diskmgr will only auto-select a partition when
          it is unambiguous (exactly one candidate). Otherwise it will refuse and ask you to map
          the correct partition explicitly.
```

## Command Reference: `close`

```text
Unmount and lock (if encrypted) a disk: close <name/id>

        UNDER THE HOOD:
        1.  Unmounting (Encrypted & Plain):
            - Flushes all pending writes to the disk (data integrity).
            - Terminates active file handles to the device.
            - Attempts unmount by mapper path (LUKS), source path (Plain), or guessed mountpoint.
        2.  Locking (LUKS only):
            - Commands the kernel to wipe encryption keys from RAM.
            - Removes the virtual cleartext device from /dev/mapper/.
        3.  Audit: Checks and displays remaining active mappings for security awareness.
```

## Command Reference: `luks`

```text
LUKS encryption management: luks <passwd|backup|restore|header> [options]

        Subcommands:
          passwd <name>           Change the LUKS passphrase.
          backup <name> [file]    Save the LUKS header to a file.
          restore <name> <file>   Restore the LUKS header from a file (Destructive).
          header <name>           Print the current LUKS header (cryptsetup luksDump).
```

## Command Reference: `label`

```text
Get or set the filesystem label of an OPEN disk: label <name> [new_label] [--fstab]

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
```

## Command Reference: `remount`

```text
Remount an OPEN disk to its label mountpoint: remount <name>

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
            For btrfs, compression is enforced (compress=zstd:3).
```

## Command Reference: `sync`

```text
Synchronize two filesystems: sync <primary> <secondary>

        UNDER THE HOOD:
        1.  Validation: Verifies both endpoints resolve to directories.
            - Mapped names must already be mounted.
            - Absolute paths must exist and be directories.
        2.  Confirmation: Requires solving two math problems (DESTRUCTIVE for secondary).
        3.  Execution: Runs 'rsync -avh --delete --info=progress2 <primary_mnt>/ <secondary_mnt>/'.

        Note: The SECONDARY disk will be modified to match the PRIMARY disk.
        All files on the secondary that do not exist on the primary will be DELETED.
```

## Command Reference: `diff`

```text
Preview differences between two mounted filesystems: diff <primary_name> <secondary_name> [--depth N] [-d] [--fast] [--checksum]

        Endpoints may be mapping names/IDs (must be mounted) or absolute directory paths.
        Uses rsync dry-run itemized output (primary -> secondary) and prints:
        1) Change counts and byte estimates (created/modified/deleted, net change).
        2) Hierarchy summary by subtree up to --depth levels.
```

## Command Reference: `defrag`

```text
Defragment a mounted filesystem: defrag <name> [--compress]

        UNDER THE HOOD:
        1.  Validation: Verifies the disk is mapped and currently mounted.
        2.  Confirmation: Requires solving two math problems.
        3.  Execution:
            - ext4:  runs 'sudo e4defrag <mountpoint>'
            - btrfs: runs 'sudo btrfs filesystem defragment -r -v <mountpoint>'
                     optional: add '--compress' to use '-czstd' recompression mode.
                     with live progress counters (total files + current directory),
                     then 'sudo btrfs balance start -dusage=50 <mountpoint>'
        4.  Recording: Stores a timestamp on the mountpoint root via:
              sudo setfattr -n user.last_defrag -v "<date>" <mountpoint>
```

## Command Reference: `fshealth`

```text
Filesystem health/diagnostics: fshealth <name>

        Shows filesystem-specific diagnostic output and local "maintenance" timestamps.

        - ext4:  sudo tune2fs -l <device>
                sudo e4defrag -c <mountpoint>   (fragmentation score + extents/files ratio)
        - btrfs: sudo btrfs filesystem usage <mountpoint>
                sudo btrfs filesystem show <mountpoint>
                sudo btrfs filesystem df <mountpoint>
                sudo btrfs device stats <mountpoint>
                sudo btrfs scrub status <mountpoint>
                sudo compsize <mountpoint>  (extents/files ratio)
        - xfs:   xfs_info <mountpoint>

        Also reads xattrs from the mountpoint root:
          user.last_defrag, user.last_scrub
```

## Command Reference: `scrub`

```text
Scrub a mounted btrfs filesystem: scrub <name> [--no-watch]

        UNDER THE HOOD:
        1.  Validation: Verifies the disk is mapped and currently mounted.
        2.  Confirmation: Requires solving two math problems.
        3.  Execution: Runs 'sudo btrfs scrub start -B -R <mountpoint>'.
        4.  Recording: Stores a timestamp on the mountpoint root via:
              sudo setfattr -n user.last_scrub -v "<date>" <mountpoint>

        OPTIONAL:
        - default (watch mode): tails kernel logs during the scrub and prints checksum errors as they happen.
        - --no-watch: disable log tailing (quiet; you only get the scrub summary output).
          Btrfs typically logs logical addresses (and sometimes inode numbers); diskmgr will attempt
          to resolve those to paths via:
            btrfs inspect-internal logical-resolve <logical> <mountpoint>
            btrfs inspect-internal inode-resolve <ino> <mountpoint>
```

## Command Reference: `version`

```text
Print diskmgr version
```

## Command Reference: `convert`

```text
Convert ext4 -> btrfs in place (no data copy): convert <name/id>

        Uses btrfs-convert on an UNMOUNTED ext4 filesystem.
        - Plain ext4 targets are supported directly.
        - If target is crypto_LUKS, diskmgr tries to resolve the open payload device
          (e.g. /dev/mapper/<name> or a crypt child) and convert that.
```

## Configuration

Mappings are stored in `diskmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
