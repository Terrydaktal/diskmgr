"""Validated, atomic friendly-name to persistent-device mapping storage."""

import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .runtime import MAP_FILENAME


def get_script_dir():
    # diskmap.tsv remains next to the project entry point after modularization.
    return Path(__file__).resolve().parent.parent

def get_map_file_path():
    override = os.environ.get('DISKMGR_MAP_FILE', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_script_dir() / MAP_FILENAME


_MAPPING_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PERSISTENT_PREFIX = "/dev/disk/by-id/"


def validate_mapping_name(name):
    value = str(name or '')
    if not _MAPPING_NAME_RE.fullmatch(value):
        raise ValueError(
            "mapping names must be 1-64 characters and contain only letters, "
            "digits, '.', '_' or '-'"
        )
    if value != value.strip() or value in ('.', '..'):
        raise ValueError("mapping names cannot have surrounding whitespace or be '.'/'..'")
    if value.isdigit() or re.fullmatch(r"(?:#|U)[0-9]+", value):
        raise ValueError("mapping names cannot look like discovery IDs")
    return value


def _persistent_link_for_device(path):
    target = os.path.realpath(path)
    by_id = Path('/dev/disk/by-id')
    if not target.startswith('/dev/') or not os.path.exists(target) or not by_id.is_dir():
        return None
    candidates = []
    for link in by_id.iterdir():
        try:
            if os.path.realpath(link) == target:
                candidates.append(str(link))
        except OSError:
            continue
    if not candidates:
        return None

    def score(candidate):
        name = os.path.basename(candidate)
        if name.startswith('nvme-eui.') or name.startswith('wwn-'):
            return (0, name)
        if name.startswith(('nvme-', 'ata-', 'usb-')):
            return (1, name)
        return (2, name)

    return min(candidates, key=score)


def validate_persistent_target(path, migrate_legacy=False):
    value = str(path or '').strip()
    if migrate_legacy and value.startswith('/dev/') and not value.startswith(_PERSISTENT_PREFIX):
        migrated = _persistent_link_for_device(value)
        if migrated:
            value = migrated
    if not value.startswith(_PERSISTENT_PREFIX):
        raise ValueError(
            f"mapping target {value!r} is not persistent; remap it using a discovery ID"
        )
    suffix = value[len(_PERSISTENT_PREFIX):]
    if not suffix or '/' in suffix or '\\' in suffix or any(ord(c) < 32 for c in suffix):
        raise ValueError("invalid /dev/disk/by-id mapping target")
    return value


@contextmanager
def _mapping_lock(exclusive):
    map_file = get_map_file_path()
    map_file.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = Path.home() / '.local' / 'state' / 'diskmgr'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / 'diskmap.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH))
        yield map_file
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_map_unlocked(map_file):
    if not map_file.exists():
        return {}, False
    mappings = {}
    migrated = False
    with map_file.open('r', encoding='utf-8', errors='strict') as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) != 2:
                raise ValueError(f"invalid mapping record at {map_file}:{line_no}")
            name = validate_mapping_name(parts[0])
            target = validate_persistent_target(parts[1], migrate_legacy=True)
            migrated = migrated or target != parts[1]
            if name in mappings:
                raise ValueError(f"duplicate mapping name {name!r} at {map_file}:{line_no}")
            mappings[name] = target
    return mappings, migrated


def _write_map_unlocked(map_file, mappings):
    normalized = {
        validate_mapping_name(name): validate_persistent_target(path)
        for name, path in mappings.items()
    }
    map_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f'.{map_file.name}.', dir=map_file.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            fd = -1
            for name in sorted(normalized):
                handle.write(f"{name}\t{normalized[name]}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, map_file)
        dir_fd = os.open(map_file.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass

def read_luks_map():
    # Use an exclusive lock because legacy raw /dev entries are migrated in
    # place the first time they can be resolved safely.
    with _mapping_lock(exclusive=True) as map_file:
        mappings, migrated = _read_map_unlocked(map_file)
        if migrated:
            _write_map_unlocked(map_file, mappings)
        return mappings

def save_luks_map(mappings):
    with _mapping_lock(exclusive=True) as map_file:
        _write_map_unlocked(map_file, mappings)


def update_luks_map(mutator):
    """Atomically read, mutate, and replace the mapping file under one lock."""
    with _mapping_lock(exclusive=True) as map_file:
        mappings, _ = _read_map_unlocked(map_file)
        updated = mutator(dict(mappings))
        if updated is None:
            updated = mappings
        _write_map_unlocked(map_file, updated)
        return dict(updated)
