# Disk Manager (diskmgr)

A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.

## Overview

```text
A utility designed to simplify the management of encrypted and plain removable media.
It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring
that disks are recognized reliably even if device nodes (like /dev/sdb) change.

COMMANDS:
  list
      Shows all configured mappings and their status (Open/Mounted).
      Also lists unmapped system disks with discovery IDs (e.g., U1, U2).
  map <id> <name>
      Assigns a friendly name to a disk.
      <id> can be a discovery ID (U1) or an existing index (1).
      Example: 'map U1 backup_drive'
  open <name>
      Unlocks LUKS (if encrypted) and mounts the disk.
  close <name>
      Unmounts and closes the disk.
  label <name> [new_label]
      Get or set the filesystem label of an OPEN disk.
  create <name> [options]
      Initializes a new disk (Erase -> LUKS -> Format -> Mount).
      Use 'help create' for full options.
  erase <name/target>
      Securely erases a disk (NVMe format, blkdiscard, or dd overwrite).
      WARNING: Destructive!
  exit / quit / Ctrl+D
      Exit the application.

Type 'help <command>' for more specific details.
```

## Command Reference: `list`

```text
List configured mappings and available system disks.

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
--- Configured Mappings (/home/lewis/Dev/diskmgr/luksmap.tsv) ---
#     NAME  LUKS  STATE      FSTYPE  LABEL       MOUNTPOINT               DEVICE      SIZE    PERSISTENT PATH
----------------------------------------------------------------------------------------------------------------------------------------------------------------
[1]   1b    -     MISSING    -       -           /media/lewis/1b          -           -       /dev/disk/by-id/wwn-0x5000c500e31e6cb2
[2]   1a    Y     MOUNTED    ext4    -           /media/lewis/1a          sda2(dm-0)  931.4G  /dev/disk/by-id/wwn-0x5000c500a89d6e44-part2

--- Other System Disks (Unmapped) ---
#     NAME  LUKS  STATE      FSTYPE  LABEL       MOUNTPOINT               DEVICE      SIZE    PERSISTENT PATH
----------------------------------------------------------------------------------------------------------------------------------------------------------------
[U1]  -     N     UNMOUNTED  -       -           -                        sda         931.5G  /dev/disk/by-id/wwn-0x5000c500a89d6e44
[U2]  -     N     UNMOUNTED  -       -           -                        sda1        128M    /dev/disk/by-id/ata-ST1000LM035-1RK172_WDE63N22-part1
[U3]  -     N     UNMOUNTED  -       -           -                        nvme0n1     1.8T    /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b448b42d60852
[U4]  -     N     MOUNTED    ext4    -           /                        nvme0n1p1   1.8T    /dev/disk/by-id/nvme-WD_BLACK_SN8100_2000GB_25334X800147_1-part1
[U5]  -     N     UNMOUNTED  -       -           -                        nvme1n1     931.5G  /dev/disk/by-id/nvme-eui.e8238fa6bf530001001b444a49598af9
[U6]  -     N     MOUNTED    ext4    NewVolume1  /media/lewis/NewVolume1  nvme1n1p1   931.5G  /dev/disk/by-id/nvme-WD_Blue_SN570_1TB_21353X644609-part1
```

## Command Reference: `map`

```text
Create or modify a persistent mapping: map <id> <name>

        Usage:
          map <id> <name>   Assigns friendly name to discovery ID (e.g., map U1 backup)
          map <num> <name>  Renames an existing mapping by its index (e.g., map 1 backup)

        UNDER THE HOOD:
        1.  Input Resolution:
            - discovery ID (e.g., U1): Resolves the temporary device to its Persistent Device Path (PDP).
            - mapping index (e.g., 1): Selects an existing mapping for RENAME operations.
        2.  PDP Linking: Extracts the /dev/disk/by-id/ path for the target hardware.
        3.  Conflict Check: Ensures the new friendly name is not already in use.
        4.  Persistence: Writes the [Name <TAB> PDP] pair to luksmap.tsv.

        This ensures the disk is recognized correctly regardless of USB port or device node changes.
```

## Command Reference: `open`

```text
Unlock (if encrypted) and mount a disk: open <name>

        UNDER THE HOOD:
        1.  Identity Resolution: Looks up the friendly name in luksmap.tsv to find the PDP.
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
Securely erase a disk: erase <target> [options]

        Target can be:
          - A friendly name from your mappings (e.g., backup)
          - A discovery ID from the list command (e.g., U1)
          - A raw device path (e.g., /dev/sdb)

        Options:
          -y, --yes         Skip math confirmation questions

        UNDER THE HOOD:
        1.  Target Resolution: Maps friendly name or ID to a raw block device.
        2.  Destructive Wipe:
            - NVMe: Uses 'nvme format --ses=1' for firmware-level crypto-erase.
            - SSD: Uses 'blkdiscard' to inform the controller that all blocks are empty.
            - HDD: Uses 'dd' for a full zero-pass overwrite of the physical platters.
        3.  Verification: Executes 'udevadm settle' and 'sync' to ensure all operations are committed.

        WARNING: This operation is IRREVERSIBLE.
```

## Command Reference: `create`

```text
Initialize a new disk: create <name> [options] OR create <target> <name> [options]

        Options:
          --fs <ext4|xfs>   Filesystem type (default: ext4)
          --label <label>   Filesystem label (default: name)
          --plain           Create a non-encrypted disk (skips LUKS)
          --gpt             Create GPT partition table + 1 partition
          --mbr             Create MBR partition table + 1 partition
          -y, --yes         Skip math confirmation questions

        UNDER THE HOOD:
        1.  Unmount: Forcefully unmounts any existing partitions on the target.
        2.  Wipe: Executes 'wipefs' to remove old filesystem signatures.
        3.  Partitioning (Optional): Uses 'sgdisk' (GPT) or 'sfdisk' (MBR) to create a single partition.
        4.  LUKS Format (Default):
            - Uses 'passgen' to generate a master key.
            - Runs 'cryptsetup luksFormat' with LUKS2 encryption.
        5.  Filesystem: Formats the cleartext device with ext4 or xfs.
        6.  Persistence: Adds the new disk's PDP to luksmap.tsv automatically.
```

## Configuration

Mappings are stored in `luksmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
