# Dependencies

diskmgr has no third-party Python runtime dependencies. It uses only the Python
standard library and requires Python 3.12 or newer.

## Required Runtime Capabilities

These commands are required for discovery, mounting, and safe command
execution:

- `python3` 3.12+
- `pkexec` (the default privilege backend; set `DISKMGR_PRIVILEGE_BACKEND=none`
  only when running as root or in a controlled test environment)
- `lsblk`, `findmnt`, `blkid`, `wipefs`, `mount`, `umount`, `udevadm`,
  `blockdev`, `partprobe`, and `partx` from `util-linux`
- `find`, `flock`, `sync`, `dd`, `timeout`, `mktemp`, `chown`, `chmod`, and
  `rmdir` from the system core utilities

## Filesystem And Encryption Capabilities

Install only the capabilities needed for the workflows you use:

- `cryptsetup` for LUKS open, format, parameter changes, header backup,
  restore, and wipe
- `e2fsprogs` for ext4 format, `e2label`, `tune2fs`, `e2fsck`, and `e4defrag`
- `btrfs-progs` for Btrfs format, defrag, balance, scrub, and conversion
- `xfsprogs` for XFS format, labels, and diagnostics
- `dosfstools` for FAT32 format (`mkfs.fat`/`mkfs.vfat`)
- `exfatprogs` for exFAT format
- `parted` and optionally `gptfdisk` (`sgdisk`) for partition operations and
  metadata erase

## Optional Operations

- `smartmontools` (`smartctl`) for `health` and `selftest`
- `gddrescue` (`ddrescue`) for `clone`
- `hdparm` for ATA secure-erase attempts
- `nvme-cli` (`nvme`) for NVMe sanitize/format erase attempts
- `attr` (`setfattr`, `getfattr`) for maintenance timestamps
- `compsize` for Btrfs compression/fragmentation diagnostics
- `gnuplot` and `xdg-utils` for entropy plots
- `fuser` or `lsof` for close-holder diagnostics
- `passgen` for generated LUKS passphrases

Missing optional capabilities are reported by the affected command before it
starts. The application does not install packages or change system services.
