"""Shared mount-target discovery and safe mountpoint cleanup."""

import os

from .runtime import log, run_command, _split_nonempty_lines


def find_mount_targets(source):
    """
    Return a list of mount TARGETs for a given SOURCE.

    Notes:
    - A single filesystem can be mounted at multiple targets; findmnt will then
      return multiple lines. Callers must not treat stdout as a single path.
    - We resolve the source to a real path so /dev/mapper/<name> and /dev/dm-X
      match the same mount.
    """
    src_real = os.path.realpath(source)
    res = run_command(['findmnt', '-rn', '-S', src_real, '-o', 'TARGET'], check=False)
    if getattr(res, 'returncode', 1) != 0:
        return []
    return _split_nonempty_lines(getattr(res, 'stdout', ''))

def cleanup_mountpoint_dir(mountpoint):
    """
    Best-effort cleanup of a mountpoint directory after unmount.

    Only attempts removal for mountpoints under /media/$USER/ and only if the
    directory is no longer a mount target. Uses rmdir (so it only removes empty
    directories) to avoid deleting real data.
    """
    if not mountpoint:
        return

    user = os.environ.get('USER', 'root')
    media_root = os.path.realpath(f"/media/{user}")
    mp_real = os.path.realpath(mountpoint)
    if not (mp_real == media_root or mp_real.startswith(media_root + os.sep)):
        return

    # Still mounted? Don't touch it.
    if run_command(['findmnt', '-rn', '-M', mountpoint], check=False).returncode == 0:
        return

    res = run_command(['rmdir', mountpoint], sudo=True, check=False)
    if getattr(res, 'returncode', 1) == 0:
        log(f"Removed mountpoint directory {mountpoint}")
