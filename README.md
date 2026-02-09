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
A utility designed to simplify the management of encrypted and plain removable media.
It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring
that disks are recognized reliably even if device nodes change.

COMMANDS:
  list
      Shows all configured mappings and unmapped system disks in one table.
  layout
      Displays the physical partition layout and free space for all disks.
  boot
      Displays all boot entries and submenus from GRUB.
  map <id/name> <name>
      Assigns a friendly name to a disk or renames an existing mapping.
  unmap <name>
      Removes an existing mapping from the configuration.
  open <name>
      Unlocks LUKS (if encrypted) and mounts the disk.
      Mounts to /media/$USER/<label> (prefers label over mapping name).
  close <name>
      Unmounts and closes the disk.
  label <name> [new_label]
      Get or set the filesystem label of an OPEN disk.
  remount <name>
      Move an OPEN disk's mount to /media/$USER/<label> (and clean up old mountpoint dirs).
  luks <passwd|backup|restore>
      LUKS management: change password, backup/restore headers.
  create <name> [options]
      Initializes a new disk (Erase -> LUKS -> Format -> Mount).
  erase <name>
      Securely erases a disk (multi-step hardware-aware wipe).
  clone <src_name> <dst_name>
      Clones one disk to another (requires target >= source size).
  sync <sec_name> <pri_name>
      Syncs two mounted disks (rsync pri -> sec).
  health <name>
      Shows SMART health (smartctl -a) for the underlying disk (USB uses -d sat).
  exit / quit / Ctrl+D
      Exit the application.

Type 'help <command>' for more specific details.
```

## Command Reference: `list`

```text
List all configured mappings and available system disks in a single table.

        UNDER THE HOOD:
        1.  Resolution: Refreshes mappings from diskmap.tsv.
        2.  Hardware Discovery: Uses 'lsblk' to gather hardware properties and identifies
            underlying physical partitions even when opened as virtual devices.
        3.  Zero-Sudo LUKS Detection: Queries the system 'udev' database via 'udevadm info'
            to accurately identify encrypted disks without requiring root privileges.
        4.  Status Logic:
            - MISSING: Persistent path not found in /dev.
            - CLOSED: Present but locked (LUKS) or unmounted (Plain).
            - OPEN: Unlocked/Decrypted but not yet mounted.
            - MOUNTED: Active filesystem attached to the preferred path (/media/$USER/name).
        5.  Dynamic Formatting: Pre-calculates the maximum width of every column across
            all rows for a perfectly aligned, readable table.
        6.  Exclusion Logic: Rigorously filters out virtual mapper devices and their
            kernel aliases (dm-X) from the unmapped list once they are active.
```

### Example Output

```text
--- Disk Management Table (/home/lewis/Dev/diskmgr/diskmap.tsv) ---
#     NAME  LUKS  STATE      FSTYPE  LABEL  MOUNTPOINT          DEVICE      SIZE    PERSISTENT PATH
------------------------------------------------------------------------------------------------------------------------------------------------------
[1]   1b    Y     MOUNTED    ext4    1b     /media/lewis/1b     sdb(dm-1)   1.8T    /dev/disk/by-id/wwn-0x5000c500e31e6cb2
[2]   1a    Y     MOUNTED    ext4    1a     /media/lewis/1a     sda2(dm-0)  931.4G  /dev/disk/by-id/wwn-0x5000c500a89d6e44-part2
[3]   data  N     MOUNTED    ext4    data   /media/lewis/data1  nvme1n1p1   931.5G  /dev/disk/by-id/nvme-WD_Blue_SN570_1TB_21353X644609-part1
[U1]  -     N     UNMOUNTED  -       -      -                   sda         931.5G  /dev/disk/by-id/wwn-0x5000c500a89d6e44
[U2]  -     N     UNMOUNTED  -       -      -                   sda1        128M    /dev/disk/by-id/ata-ST1000LM035-1RK172_WDE63N22-part1
[U3]  -     N     UNMOUNTED  -       -      -                   nvme0n1     1.8T    /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852
[U4]  -     N     MOUNTED    ext4    -      /                   nvme0n1p1   1.8T    /dev/disk/by-id/nvme-WD_BLACK_SN8100_2000GB_25334X800147_1-part1
[U5]  -     N     UNMOUNTED  -       -      -                   nvme1n1     931.5G  /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9
```

## Command Reference: `layout`

```text
Display the physical partition layout and free space for all plugged-in disks.

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
[ GPT Primary 34s (17408.00B) ] [ free 2014s (1007.00KiB) ] [ sda1 - 262144s (128.00MiB) (msftres, no_automount) ] [ sda2 - 1953259520s (953740.00MiB ≈ 931.4GiB) (msftdata) ] [ free 1423s (711.50KiB) ] [ GPT Backup 33s (16896.00B) ]

NAME                      FSTYPE       FSVER  LABEL        UUID                                   FSAVAIL    FSUSE%   MOUNTPOINTS
sda
├─sda1
└─sda2                    crypto_LUKS  2                   e038a8b5-d3a7-4bbb-bbea-5bed8cc07a04
    └─1a                  ext4         1.0    1a           5933d845-1098-4f16-ad7f-ff1f4a4a2105   18.3G      98%      /media/lewis/1a
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/sdb (ST2000DM008-2FR102)

Disk: /dev/nvme0n1 (WD_BLACK SN8100 2000GB) [msdos] [Sector: L512/P512] [Total Sectors: 3907029168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme0n1p1 ext4 3907026944s (1907728.00MiB ≈ 1863.0GiB) (boot) ] [ free 176s (88.00KiB) ]

NAME                      FSTYPE       FSVER  LABEL        UUID                                   FSAVAIL    FSUSE%   MOUNTPOINTS
nvme0n1
└─nvme0n1p1               ext4         1.0                 88f1dad3-95c6-418e-bea8-f5f3e072ea29   765.9G     53%      /
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme1n1 (WD Blue SN570 1TB) [msdos] [Sector: L512/P512] [Total Sectors: 1953525168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme1n1p1 ext4 1953523120s (953868.71MiB ≈ 931.5GiB) ]

NAME                      FSTYPE       FSVER  LABEL        UUID                                   FSAVAIL    FSUSE%   MOUNTPOINTS
nvme1n1
└─nvme1n1p1               ext4         1.0    data         72c22012-b161-4e2a-a762-94ff7fda47f9   142.3G     79%      /media/lewis/data1
-----------------------------------------------------------------------------------------------------------------------------------------------------------
```

## Command Reference: `boot`

```text
Display boot entries from the GRUB configuration of all disks.

        UNDER THE HOOD:
        Scans all block devices. If mounted, it parses /boot/grub/grub.cfg.
        If unmounted or encrypted, it explains why it cannot yet read the config.
```

### Example Output

```text
--- System Boot Configuration Scan ---

Device: /dev/sda1 (unknown FS)
  Result: No recognizable filesystem found.
------------------------------------------------------------

Device: /dev/sda2 (crypto_LUKS)
  Result: LUKS container is LOCKED. Please 'open' this disk to scan for boot entries.
------------------------------------------------------------

Device: /dev/dm-0 (ext4)
  Result: Mounted at /media/lewis/1a, but no GRUB configuration found.
------------------------------------------------------------

Device: /dev/dm-1 (ext4)
  Result: Mounted at /media/lewis/1b, but no GRUB configuration found.
------------------------------------------------------------

Device: /dev/nvme0n1p1 (ext4)
  Result: Found GRUB config at /boot/grub/grub.cfg

Top-level
  └─ Linux Mint 22.1 Xfce                                                      SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]

Advanced options for Linux Mint 22.1 Xfce
  ├─ Linux Mint 22.1 Xfce, with Linux 6.17.9-061709-generic                    SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.17.9-061709-generic (recovery mode)    SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.16.12-061612-generic                   SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.16.12-061612-generic (recovery mode)   SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.16.0-061600-generic                    SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.16.0-061600-generic (recovery mode)    SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-100-generic                        SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-100-generic (recovery mode)        SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-88-generic                         SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-88-generic (recovery mode)         SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-51-generic                         SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  ├─ Linux Mint 22.1 Xfce, with Linux 6.8.0-51-generic (recovery mode)         SEARCH=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]  ROOT=88f1dad3-95c6-418e-bea8-f5f3e072ea29 [nvme0n1p1]
  └─ UEFI Firmware Settings                                                    SEARCH=(firmware)                           [-]  ROOT=(firmware)                           [-]
------------------------------------------------------------

Device: /dev/nvme1n1p1 (ext4)
  Result: Mounted at /media/lewis/data1, but no GRUB configuration found.
------------------------------------------------------------
```

## Command Reference: `map`

```text
Create or modify a persistent mapping: map <name/id> <name>

        Usage:
          map [U1] backup    Assigns friendly name to discovery ID (e.g., map U1 backup)
          map 1a backup      Renames an existing mapping (e.g., map 1a backup)

        Note: Raw device paths (e.g., /dev/sdb) are NOT allowed.

        UNDER THE HOOD:
        1.  Input Resolution:
            - discovery ID (e.g., [U1]): Resolves the temporary device to its Persistent Device Path (PDP).
            - mapping name (e.g., 1a): Selects an existing mapping for RENAME operations.
        2.  PDP Linking: Extracts the /dev/disk/by-id/ path for the target hardware.
        3.  Conflict Check: Ensures the new friendly name is not already in use.
        4.  Persistence: Writes the [Name <TAB> PDP] pair to diskmap.tsv.

        This ensures the disk is recognized correctly regardless of USB port or device node changes.
```

## Command Reference: `unmap`

```text
Remove a persistent mapping: unmap <name>

        UNDER THE HOOD:
        1.  Resolution: Verifies the mapping exists in diskmap.tsv.
        2.  Removal: Deletes the [Name <TAB> PDP] pair from the internal dictionary.
        3.  Persistence: Re-writes diskmap.tsv with the mapping removed.
```

## Command Reference: `open`

```text
Unlock (if encrypted) and mount a disk: open <name>

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
            - Identifies the preferred mountpoint: /media/$USER/<label>.
            - If no hardware label is present, falls back to /media/$USER/<mapping_name>.
            - Note: Prioritizing the label ensures that the disk mounts to the same
              path used by standard OS automounters for plain removable media.
            - Ensures the directory exists and attaches the device.
        6.  Policy Enforcement: If the disk is already mounted at a non-standard path,
            it unmounts and remounts it to the preferred path.
```

## Command Reference: `close`

```text
Unmount and lock (if encrypted) a disk: close <name>

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

## Command Reference: `label`

```text
Get or set the filesystem label of an OPEN disk: label <name> [new_label]

        UNDER THE HOOD:
        1.  Validation: Verifies that the disk is currently open/unlocked.
        2.  Identification: Queries the filesystem type (ext4, xfs, etc.) via 'lsblk'.
        3.  Labeling:
            - ext4: Uses 'e2label' on the active device.
            - xfs: Requires a temporary unmount, then uses 'xfs_admin -L', then remounts.
        4.  Refresh: Executes 'udevadm trigger' to force tools like 'lsblk' to see the change.

        The label is written directly to the disk hardware and persists across different computers.
```

## Command Reference: `remount`

```text
Remount an OPEN disk to its label mountpoint: remount <name>

        This fixes "mounted twice" and "data1/data2 suffix" issues by moving the mount
        to the canonical path: /media/$USER/<label>.

        SAFETY RULES:
        - Refuses if the target mountpoint is already mounted by a different device.
        - Refuses if the target directory exists, is not a mountpoint, and is non-empty.
        - Refuses if the filesystem has no LABEL (set one with: label <name> <new_label>).

        UNDER THE HOOD:
        1.  Resolve Device: Uses /dev/mapper/<name> if present, otherwise the mapped source path.
            If the mapping is LUKS and not OPEN, it refuses.
        2.  Identify Label: Reads the filesystem LABEL via blkid.
        3.  Preflight: Validates /media/$USER/<label> is safe to use.
        4.  Unmount: Unmounts all current mount targets for the device (if any).
        5.  Cleanup: Removes empty old mountpoint directories under /media/$USER (best-effort rmdir).
        6.  Mount: Mounts the device at /media/$USER/<label>.
```

## Command Reference: `luks`

```text
LUKS encryption management: luks <passwd|backup|restore> [options]

        Subcommands:
          passwd <name>           Change the LUKS passphrase.
          backup <name> [file]    Save the LUKS header to a file.
          restore <name> <file>   Restore the LUKS header from a file (Destructive).
```

## Command Reference: `erase`

```text
Securely erase a disk: erase <name> [options]

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

## Command Reference: `create`

```text
Initialize a disk: create <name> [options]

        Note: You must 'map' a disk first to give it a name before initializing it.

        NUANCES & SCOPE:
        1. Running create on a Partition (e.g., sda2)
           The Result: Container-in-a-Box.
           The script treats the existing partition as its "entire world."
           - Partitioning: It skips the GPT/MBR step because you've already given it a partition.
           - Encryption: It sets up LUKS directly inside the sda2 boundary.
           - Filesystem: It formats the area inside sda2.
           - The Big Picture: The rest of your disk (like sda1 or sda3) is untouched.
             You are simply replacing whatever was inside partition #2 with a new encrypted volume.

        2. Running create on a Whole Disk (e.g., sda)
           The Result: Total Takeover.
           The script wipes the slate clean and rebuilds the drive from scratch.
           - Wipe: It deletes the Partition Table (GPT/MBR) at the start of the disk.
             All existing partitions (sda1, sda2, etc.) are instantly lost.
           - Rebuild:
             * If you didn't use --gpt or --mbr: It formats the Entire Disk as one
               giant LUKS container (no partition table).
             * If you used --gpt: It creates a fresh GPT table, creates a new
               partition #1 spanning the whole drive, and puts LUKS inside that.
           - The Big Picture: You lose everything on the physical drive, and it
             becomes a single, clean encrypted volume.

        3. Using --plain with --gpt or --mbr
           The Result: Standard Unencrypted Disk.
           - Partitioning: Creates a fresh GPT/MBR table and one primary partition.
           - Encryption: Skipped entirely.
           - Mapping: The friendly name in diskmap.tsv points directly to the
             raw hardware partition (e.g., /dev/disk/by-id/...-part1).
           - The Big Picture: You get a standard unencrypted partitioned volume
             manageable via diskmgr's persistent naming.

        Options:
          --fs <ext4|xfs|btrfs>   Filesystem type (default: ext4)
          --label <label>   Set a different internal filesystem label (other than <name>)
          --plain           Create a non-encrypted disk (skips LUKS)
          --gpt             Create GPT partition table + 1 partition (Whole disk only)
          --mbr             Create MBR partition table + 1 partition (Whole disk only)

        UNDER THE HOOD:
        1.  Unmount: Forcefully unmounts any existing partitions on the target.
        2.  Wipe: Executes 'wipefs' to remove old filesystem signatures.
        3.  Partitioning (Optional): Uses 'sgdisk' (GPT) or 'sfdisk' (MBR) to create a single partition.
        4.  LUKS Format (Default):
            - Uses 'passgen' to generate a master key.
            - Runs 'cryptsetup luksFormat' with LUKS2 encryption.
        5.  Filesystem:
            - Formats the cleartext device with ext4, xfs, or btrfs.
            - (ext4 only): Reclaims the 5% reserved space for root using 'tune2fs -m 0'.
        6.  Persistence: Adds the new disk's PDP to diskmap.tsv automatically.

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.
```

## Command Reference: `clone`

```text
Clone one disk or partition to another: clone <src_name> <dst_name>

        WARNING (DATA DESTRUCTION):
        - This command writes directly to the destination block device (like running dd).
        - The destination is overwritten starting at byte 0. Any existing partition table,
          filesystems, and files on the destination WILL BE DESTROYED.
        - If the destination is larger than the source, bytes beyond the source size are
          not overwritten. Old data may still physically exist there, but it will not be
          referenced by the cloned partition table.
        - diskmgr does NOT unmount the destination for you. Unmount/close it first to
          avoid live corruption.
        - If you need to sanitize the destination, run: erase <dst_name>

        Note: The target disk MUST be the same size or larger than the source.

        STEP-BY-STEP PROCESS:
        1.  Resolution: Maps both friendly names to their physical device nodes (PDP).
        2.  Size Validation: Queries 'blockdev --getsize64' for both. Aborts if dst < src.
        3.  Safety Audit: Verifies that the target is NOT the system root drive.
        4.  Confirmation: Requires solving two math problems to authorize data destruction.
        5.  Cloning: Executes 'dd' with 16MiB buffers and direct I/O for maximum throughput.
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

## Command Reference: `sync`

```text
Synchronize two mounted disks: sync <secondary_name> <primary_name>

        UNDER THE HOOD:
        1.  Validation: Verifies both disks are mapped and currently mounted.
        2.  Confirmation: Requires solving two math problems (DESTRUCTIVE for secondary).
        3.  Execution: Runs 'rsync -avh --delete --progress <primary_mnt>/ <secondary_mnt>/'.

        Note: The SECONDARY disk will be modified to match the PRIMARY disk.
        All files on the secondary that do not exist on the primary will be DELETED.
```

## Command Reference: `health`

```text
Display SMART health for a mapped disk: health <name>

        Runs smartctl against the underlying DISK device for the mapping.
        - If the mapping points to a partition, diskmgr automatically targets the parent disk.
        - If the disk transport is USB and the device is /dev/sdX, diskmgr uses:
              smartctl -d sat -a /dev/sdX
          (common for USB-SATA bridges).
```

## Configuration

Mappings are stored in `diskmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
