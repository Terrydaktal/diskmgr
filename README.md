# Disk Manager (diskmgr)

A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.

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
  map <name/id> <name>
      Assigns a friendly name to a disk or renames an existing mapping.
  open <name>
      Unlocks LUKS (if encrypted) and mounts the disk.
  close <name>
      Unmounts and closes the disk.
  label <name> [new_label]
      Get or set the filesystem label of an OPEN disk.
  create <name> [options]
      Initializes a new disk (Erase -> LUKS -> Format -> Mount).
  erase <name>
      Securely erases a disk (NVMe format, blkdiscard, or dd overwrite).
  exit / quit / Ctrl+D
      Exit the application.

Type 'help <command>' for more specific details.
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
#     NAME  LUKS  STATE      FSTYPE  LABEL       MOUNTPOINT               DEVICE      SIZE    PERSISTENT PATH
----------------------------------------------------------------------------------------------------------------------------------------------------------------
[1]   1b    -     MISSING    -       -           /media/lewis/1b          -           -       /dev/disk/by-id/wwn-0x5000c500e31e6cb2
[2]   1a    Y     MOUNTED    ext4    -           /media/lewis/1a          sda2(dm-0)  931.4G  /dev/disk/by-id/wwn-0x5000c500a89d6e44-part2
[U1]  -     N     UNMOUNTED  -       -           -                        sda         931.5G  /dev/disk/by-id/wwn-0x5000c500a89d6e44
[U2]  -     N     UNMOUNTED  -       -           -                        sda1        128M    /dev/disk/by-id/ata-ST1000LM035-1RK172_WDE63N22-part1
[U3]  -     N     UNMOUNTED  -       -           -                        nvme0n1     1.8T    /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852
[U4]  -     N     MOUNTED    ext4    -           /                        nvme0n1p1   1.8T    /dev/disk/by-id/nvme-WD_BLACK_SN8100_2000GB_25334X800147_1-part1
[U5]  -     N     UNMOUNTED  -       -           -                        nvme1n1     931.5G  /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9
[U6]  -     N     MOUNTED    ext4    NewVolume1  /media/lewis/NewVolume1  nvme1n1p1   931.5G  /dev/disk/by-id/nvme-WD_Blue_SN570_1TB_21353X644609-part1
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
  └─1a ext4        1.0         5933d845-1098-4f16-ad7f-ff1f4a4a2105   18.3G    98% /media/lewis/1a
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme0n1 (WD_BLACK SN8100 2000GB) [msdos] [Sector: L512/P512] [Total Sectors: 3907029168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme0n1p1 ext4 3907026944s (1907728.00MiB ≈ 1863.0GiB) (boot) ] [ free 176s (88.00KiB) ]

NAME        FSTYPE FSVER LABEL UUID                                 FSAVAIL FSUSE% MOUNTPOINTS
nvme0n1
└─nvme0n1p1 ext4   1.0         88f1dad3-95c6-418e-bea8-f5f3e072ea29  769.6G    53% /
-----------------------------------------------------------------------------------------------------------------------------------------------------------

Disk: /dev/nvme1n1 (WD Blue SN570 1TB) [msdos] [Sector: L512/P512] [Total Sectors: 1953525168]
[ MBR 2s (1024.00B) ] [ free 2046s (1023.00KiB) ] [ nvme1n1p1 ext4 1953523120s (953868.71MiB ≈ 931.5GiB) ]

NAME        FSTYPE FSVER LABEL      UUID                                 FSAVAIL FSUSE% MOUNTPOINTS
nvme1n1
└─nvme1n1p1 ext4   1.0   NewVolume1 72c22012-b161-4e2a-a762-94ff7fda47f9  311.3G    61% /media/lewis/NewVolume1
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
            - Ensures the directory /media/$USER/<name> exists.
            - Attaches the (decrypted) device to the mountpoint.
        6.  Policy Enforcement: If the disk is already mounted at a non-standard path,
            it unmounts and remounts it to the preferred /media/$USER/<name> path.
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
            - ext4: Uses 'e2label' on the active mapper device.
            - xfs: Requires a temporary unmount, then uses 'xfs_admin -L', then remounts.

        The label is written directly to the disk hardware and persists across different computers.
```

## Command Reference: `erase`

```text
Securely erase a disk: erase <name> [options]

        Note: You must 'map' a disk first to give it a name before erasing it.

        NUANCES & SAFETY:
        - Whole Disk (sda): Wipes the Partition Table (GPT/MBR) and ALL partitions.
        - Partition (sda2): Wipes only that partition. Other partitions are safe.
        - Mapped Name (1a): Resolves to the physical partition. Wiping it destroys
          the LUKS Header, making data recovery impossible even with the password.

        Options:
          -y, --yes         Skip math confirmation questions

        UNDER THE HOOD:
        1.  Target Resolution: Maps friendly name to a raw block device.
        2.  Destructive Wipe:
            - NVMe: Uses 'nvme format --ses=1' for firmware-level crypto-erase.
            - SSD: Uses 'blkdiscard' to inform the controller that all blocks are empty.
            - HDD: Uses 'dd' for a full zero-pass overwrite of the physical platters.
        3.  Verification: Executes 'udevadm settle' and 'sync' to ensure all operations are committed.

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
          -y, --yes         Skip math confirmation questions

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
```

## Configuration

Mappings are stored in `luksmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
