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
        2.  Scanning: Executes 'lsblk' to gather hardware properties (FSTYPE, SIZE, etc.).
        3.  Status Check:
            - MISSING: Persistent path not found in /dev.
            - CLOSED: Persistent path exists, but no active mapping in /dev/mapper.
            - OPEN: Active mapping exists in /dev/mapper but is not mounted.
            - MOUNTED: Filesystem is active and attached to the preferred mountpoint.
        4.  Discovery: Identifies disks not in the mapping table and assigns discovery IDs (U1, U2...).
        5.  Dynamic Layout: Calculates column widths based on current data for optimal alignment.
```

## Command Reference: `map`

```text
Create or modify a persistent mapping: map <id> <name>

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
        1.  Unmounting:
            - Flushes all pending writes to the disk (data integrity).
            - Terminates active file handles to the decrypted device.
            - Attempts unmount by mapper path, source path, or guessed mountpoint.
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
Securely erase a disk: erase <name/target> [-y]

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
