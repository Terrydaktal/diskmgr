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
  map <name/id> <name>
      Assigns a friendly name to a disk or renames an existing mapping.
  open <name/id>
      Unlocks LUKS (if encrypted) and mounts the disk.
  close <name/id>
      Unmounts and closes the disk.
  label <name/id> [new_label]
      Get or set the filesystem label of an OPEN disk.
  create <name/id> <name> [options]
      Initializes a new disk (Erase -> LUKS -> Format -> Mount).
  erase <name/id>
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

## Command Reference: `map`

```text
Create or modify a persistent mapping: map <name/id> <name>

        Usage:
          map [U1] backup    Assigns friendly name to discovery ID (e.g., map U1 backup)
          map 1a backup      Renames an existing mapping (e.g., map 1a backup)

        Note: Raw device paths (e.g., /dev/sdb) are NOT allowed.
```

## Command Reference: `open`

```text
Unlock (if encrypted) and mount a disk: open <name>
```

## Command Reference: `close`

```text
Unmount and lock (if encrypted) a disk: close <name/id>
```

## Command Reference: `label`

```text
Get or set the filesystem label of an OPEN disk: label <name/id> [new_label]
```

## Command Reference: `erase`

```text
Securely erase a disk: erase <name/id> [options]
```

## Command Reference: `create`

```text
Initialize a new disk: create <name/id> <name> [options]
```

## Configuration

Mappings are stored in `luksmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:

```text
<friendly_name>	<persistent_device_path>
```

## Author

Terrydaktal <9lewis9@gmail.com>
