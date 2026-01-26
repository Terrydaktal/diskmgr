# Disk Manager (diskmgr)

A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.

## Overview

```text
Error capturing help: Command '['sudo', './diskmgr']' timed out after 10 seconds
```

## Command Reference: `list`

```text
List all configured mappings and available system disks in a single table.

        UNDER THE HOOD:
        1.  Resolution: Refreshes mappings from luksmap.tsv.
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
--- Disk Management Table (/home/lewis/Dev/diskmgr/luksmap.tsv) ---
#     NAME  LUKS  STATE      FSTYPE       LABEL  MOUNTPOINT         DEVICE     SIZE    PERSISTENT PATH
---------------------------------------------------------------------------------------------------------------------------------------------------------
[1]   1b    -     MISSING    -            -      -                  -          -       /dev/disk/by-id/wwn-0x5000c500e31e6cb2
[2]   1a    Y     CLOSED     crypto_LUKS  -      -                  sda2       931.4G  /dev/disk/by-id/wwn-0x5000c500a89d6e44-part2
[3]   data  N     MOUNTED    ext4         data   /media/lewis/data  nvme1n1p1  931.5G  /dev/disk/by-id/nvme-WD_Blue_SN570_1TB_21353X644609-part1
[U1]  -     N     UNMOUNTED  -            -      -                  sda        931.5G  /dev/disk/by-id/wwn-0x5000c500a89d6e44
[U2]  -     N     UNMOUNTED  -            -      -                  sda1       128M    /dev/disk/by-id/usb-SABRENT_SABRENT_DD5641988396B-0:0-part1
[U3]  -     N     UNMOUNTED  -            -      -                  nvme0n1    1.8T    /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852
[U4]  -     N     MOUNTED    ext4         -      /                  nvme0n1p1  1.8T    /dev/disk/by-id/nvme-WD_BLACK_SN8100_2000GB_25334X800147_1-part1
[U5]  -     N     UNMOUNTED  -            -      -                  nvme1n1    931.5G  /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9
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

NAME   FSTYPE      FSVER LABEL UUID                                 FSAVAIL FSUSE% MOUNTPOINTS
sda
├─sda1
└─sda2 crypto_LUKS 2           e038a8b5-d3a7-4bbb-bbea-5bed8cc07a04
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme0n1 (WD_BLACK SN8100 2000GB) [msdos] [Sector: L512/P512] [Total Sectors: 3907029168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme0n1p1 ext4 3907026944s (1907728.00MiB ≈ 1863.0GiB) (boot) ] [ free 176s (88.00KiB) ]

NAME        FSTYPE FSVER LABEL UUID                                 FSAVAIL FSUSE% MOUNTPOINTS
nvme0n1
└─nvme0n1p1 ext4   1.0         88f1dad3-95c6-418e-bea8-f5f3e072ea29  771.8G    53% /
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme1n1 (WD Blue SN570 1TB) [msdos] [Sector: L512/P512] [Total Sectors: 1953525168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme1n1p1 ext4 1953523120s (953868.71MiB ≈ 931.5GiB) ]

NAME        FSTYPE FSVER LABEL UUID                                 FSAVAIL FSUSE% MOUNTPOINTS
nvme1n1
└─nvme1n1p1 ext4   1.0   data  72c22012-b161-4e2a-a762-94ff7fda47f9  311.3G    61% /media/lewis/data
-----------------------------------------------------------------------------------------------------------------------------------------------------------
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
        4.  Persistence: Writes the [Name <TAB> PDP] pair to luksmap.tsv.

        This ensures the disk is recognized correctly regardless of USB port or device node changes.
```

## Command Reference: `unmap`

```text
Remove a persistent mapping: unmap <name>

        UNDER THE HOOD:
        1.  Resolution: Verifies the mapping exists in luksmap.tsv.
        2.  Removal: Deletes the [Name <TAB> PDP] pair from the internal dictionary.
        3.  Persistence: Re-writes luksmap.tsv with the mapping removed.
```

## Command Reference: `open`

```text
Unlock (if encrypted) and mount a disk: open <name>

        UNDER THE HOOD:
        1.  Identity Resolution: Looks up the friendly name in luksmap.tsv.
        2.  Hardware Wait: Polls for up to 10 seconds to allow for hardware spin-up/udev events.
        3.  Validation:
            - Runs 'cryptsetup isLuks' to check for encryption.
            - If NOT encrypted, verifies the existence of a valid filesystem.
        4.  Decryption (LUKS only):
            - Executes 'passgen' to retrieve the passphrase.
            - Pipes the passphrase into 'cryptsetup open' to create a cleartext device in /dev/mapper/.
        5.  Mounting:
            - Identifies the preferred mountpoint: /media/$USER/<label> (falls back to mapping name).
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
Get or set the filesystem label of an OPEN disk: label <name> [new_label] [--remount]

        Options:
          --remount        Unmount and remount the disk to the new label's path.

        UNDER THE HOOD:
        1.  Validation: Verifies that the disk is currently open/unlocked.
        2.  Identification: Queries the filesystem type (ext4, xfs, etc.) via 'lsblk'.
        3.  Labeling:
            - ext4: Uses 'e2label' on the active device.
            - xfs: Requires a temporary unmount, then uses 'xfs_admin -L', then remounts.
        4.  Refresh: Executes 'udevadm trigger' to force tools like 'lsblk' to see the change.
        5.  Remount (Optional): If --remount is set, moves the mount to /media/$USER/new_label.

        The label is written directly to the disk hardware and persists across different computers.
```

## Command Reference: `passwd`

```text
Change the LUKS encryption passphrase: passwd <name>

        UNDER THE HOOD:
        1.  Resolution: Maps the friendly name to its physical device node.
        2.  Validation: Verifies the device is a valid LUKS container.
        3.  Execution: Runs 'cryptsetup luksChangeKey'. This is an interactive
            process that prompts for the current passphrase and the new one.

        Note: This command communicates directly with the kernel to update the LUKS slot.
```

## Command Reference: `backup`

```text
Back up a LUKS header to a file: backup <name> [filename]

        If no filename is provided, it defaults to <name>.header.bak.
        The header is required to recover access if the disk's on-device header
        is corrupted or overwritten.
```

## Command Reference: `restore`

```text
Restore a LUKS header from a file: restore <name> <filename>

        Note: This is a DESTRUCTIVE operation for the on-disk header.
        Solving two math problems is MANDATORY to proceed.
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

        Options:
          --fs <ext4|xfs>   Filesystem type (default: ext4)
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
            - Formats the cleartext device with ext4 or xfs.
            - (ext4 only): Reclaims the 5% reserved space for root using 'tune2fs -m 0'.
        6.  Persistence: Adds the new disk's PDP to luksmap.tsv automatically.

        Note: This is a DESTRUCTIVE operation. Solving two math problems is MANDATORY to proceed.
```

## Command Reference: `clone`

```text
Clone one disk or partition to another: clone <src_name> <dst_name>

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

## Configuration

Mappings are stored in `luksmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
