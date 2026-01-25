# Disk Manager (diskmgr)

A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.

## Overview

[0m
[0m
A utility designed to simplify the management of encrypted and plain removable media.
It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring
that disks are recognized reliably even if device nodes (like /dev/sdb) change.

[1mCOMMANDS:[0m
  [92mlist[0m
      Shows all configured mappings and their status (Open/Mounted).
      Also lists unmapped system disks with discovery IDs (e.g., U1, U2).
  [92mmap <id> <name>[0m
      Assigns a friendly name to a disk.
      <id> can be a discovery ID (U1) or an existing index (1).
      Example: 'map U1 backup_drive'
  [92mopen <name>[0m
      Unlocks LUKS (if encrypted) and mounts the disk.
  [92mclose <name>[0m
      Unmounts and closes the disk.
  [92mlabel <name> [new_label][0m
      Get or set the filesystem label of an OPEN disk.
  [92mcreate <name> [options][0m
      Initializes a new disk (Erase -> LUKS -> Format -> Mount).
      Use 'help create' for full options.
  [92merase <name/target>[0m
      Securely erases a disk (NVMe format, blkdiscard, or dd overwrite).
      WARNING: Destructive!
  [92mexit / quit / Ctrl+D[0m
      Exit the application.

Type 'help <command>' for more specific details.
[0m

## Command: 



## Command: 



## Command: 



## Command: 



## Command: 



## Command: 



## Command: 



## Configuration

Mappings are stored in  in the same directory as the script. The file uses a simple Tab-Separated Values format:



## Author

Terrydaktal <9lewis9@gmail.com>
