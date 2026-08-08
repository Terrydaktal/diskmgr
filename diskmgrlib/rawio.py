"""Raw-device operations and block-copy recovery helpers."""

import json
import os
import re
import time

from .devices import (
    _lsblk_type,
    _sysfs_block_name,
    _sysfs_to_parent_disk_name,
    disk_discard_supported,
)
from .runtime import (
    _find_tool_or_common_paths,
    _first_int_from_text,
    log,
    run_command,
    run_command_bytes,
)

def _parse_ddrescue_failed_ranges(map_path, sector_size=512):
    """
    Return a list of failed ranges from a ddrescue mapfile.

    ddrescue map lines are typically: <start_hex> <size_hex> <status_char>
    We treat status '-' as "unrecovered".
    Ranges are returned as dicts with byte and sector offsets.
    """
    if not map_path or not os.path.exists(map_path):
        return []
    try:
        ss = int(sector_size)
        if ss <= 0:
            ss = 512
    except Exception:
        ss = 512

    out = []
    try:
        with open(map_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                a, b, st = parts[0], parts[1], parts[2]
                if not (a.startswith('0x') and b.startswith('0x') and st):
                    continue
                if st[0] != '-':
                    continue
                try:
                    start_b = int(a, 16)
                    size_b = int(b, 16)
                except ValueError:
                    continue
                if size_b <= 0:
                    continue
                end_b = start_b + size_b
                # Prefer reporting in sectors, but keep bytes if not aligned.
                start_lba = start_b // ss
                end_lba = (end_b + ss - 1) // ss  # ceil
                out.append({
                    "start_b": start_b,
                    "end_b": end_b,
                    "size_b": size_b,
                    "start_lba": start_lba,
                    "end_lba": end_lba,
                    "count_lba": max(0, end_lba - start_lba),
                })
    except Exception:
        return []
    return out

def _target_geometry(dev_path):
    """Return (disk|part, byte_size, logical_sector) or fail closed."""
    if not os.path.exists(dev_path):
        raise RuntimeError(f"device not found: {dev_path}")
    result = run_command(['lsblk', '--nodeps', '-no', 'TYPE', dev_path], check=False)
    if getattr(result, 'returncode', 1) != 0:
        raise RuntimeError("could not classify target device")
    dtype = (getattr(result, 'stdout', '') or '').strip().splitlines()
    dtype = dtype[0].strip().lower() if dtype else ''
    if dtype not in ('disk', 'part'):
        raise RuntimeError(f"refusing unsupported raw target type: {dtype or 'unknown'}")
    size = run_command(['blockdev', '--getsize64', dev_path], sudo=True, check=False)
    if getattr(size, 'returncode', 1) != 0:
        raise RuntimeError("could not determine exact target size")
    size_bytes = _first_int_from_text(getattr(size, 'stdout', '') or '')
    sector = run_command(['blockdev', '--getss', dev_path], sudo=True, check=False)
    logical = _first_int_from_text(getattr(sector, 'stdout', '') or '')
    if not size_bytes or size_bytes <= 0 or not logical or logical <= 0:
        raise RuntimeError("target geometry is unavailable or invalid")
    return dtype, int(size_bytes), int(logical)


def _target_media_kind(dev_path):
    """Return (is_nvme, is_rotational), failing closed on unknown media."""
    kname = _sysfs_block_name(dev_path)
    parent = _sysfs_to_parent_disk_name(kname)
    query = f"/dev/{parent}" if parent else dev_path
    result = run_command(
        ['lsblk', '--nodeps', '-no', 'TRAN,ROTA', query],
        check=False,
    )
    if getattr(result, 'returncode', 1) != 0:
        raise RuntimeError("could not determine transport and rotational state")
    rows = [line.split() for line in (getattr(result, 'stdout', '') or '').splitlines() if line.strip()]
    if not rows or len(rows[0]) < 2 or rows[0][1] not in {'0', '1'}:
        raise RuntimeError("transport or rotational state is unknown")
    transport = rows[0][0].lower()
    rotational = rows[0][1] == '1'
    is_nvme = transport == 'nvme' or parent.startswith('nvme')
    return is_nvme, rotational


def _nvme_controller(dev_path):
    name = os.path.basename(os.path.realpath(dev_path))
    match = re.match(r'(nvme[0-9]+)', name)
    return f"/dev/{match.group(1)}" if match else dev_path


def _parse_nvme_sanitize_status(value):
    """Parse the low three status bits from nvme sanitize-log output."""
    text = str(value if value is not None else '').strip()
    match = re.search(r'0x[0-9a-fA-F]+|[0-9]+', text)
    if not match:
        return None
    try:
        return int(match.group(0), 0) & 0x7
    except ValueError:
        return None


def _nvme_sanitize_and_wait(dev_path, action, label, timeout_seconds=24 * 60 * 60):
    log(f"Attempting {label} on {dev_path}...")
    start = time.monotonic()
    run_command(['nvme', 'sanitize', dev_path, '-a', str(action)], sudo=True)
    controller = _nvme_controller(dev_path)
    last_state = None
    while True:
        if time.monotonic() - start > timeout_seconds:
            raise RuntimeError(f"{label} exceeded {timeout_seconds}s timeout")
        status = run_command(
            ['nvme', 'sanitize-log', controller, '-o', 'json'],
            sudo=True,
            check=False,
        )
        if getattr(status, 'returncode', 1) != 0:
            raise RuntimeError("could not read NVMe sanitize status")
        try:
            data = json.loads(getattr(status, 'stdout', '') or '{}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid NVMe sanitize status: {exc}") from exc
        raw_sstat = data.get('sstat', data.get('status'))
        sstat = _parse_nvme_sanitize_status(raw_sstat)
        if sstat == 1:
            run_command(['udevadm', 'settle'], sudo=True)
            log(f"{label} completed successfully.")
            return True
        if sstat == 0:
            raise RuntimeError(f"NVMe sanitize is not active after start: {raw_sstat}")
        if sstat in (3, 4, 5, 6, 7):
            raise RuntimeError(f"NVMe sanitize reported failure: {raw_sstat}")
        if sstat != 2:
            raise RuntimeError(f"unrecognized NVMe sanitize status: {raw_sstat}")
        progress = data.get('sprog', data.get('progress'))
        state = (sstat, progress)
        if state != last_state:
            log(f"{label} still in progress: {progress if progress is not None else 'status pending'}")
            last_state = state
        time.sleep(2)


def _ata_security_block(output):
    lines = str(output or '').splitlines()
    for index, line in enumerate(lines):
        if not re.match(r'^\s*Security\s*:', line, flags=re.IGNORECASE):
            continue
        block = [line.split(':', 1)[1]]
        for child in lines[index + 1:]:
            if child and not child[0].isspace():
                break
            block.append(child)
        return '\n'.join(block)
    return ''


def _ata_hardware_erase(dev_path):
    info = run_command(['hdparm', '-I', dev_path], sudo=True, capture_output=True, check=False)
    if getattr(info, 'returncode', 1) != 0:
        return False
    output = getattr(info, 'stdout', '') or ''
    lower = output.lower()
    if 'sanitize' in lower:
        try:
            run_command(['hdparm', '--sanitize-block-erase', dev_path], sudo=True)
            log("ATA sanitize block erase completed successfully.")
            return True
        except Exception as exc:
            log(f"ATA sanitize failed: {exc}", 'WARN')

    security = _ata_security_block(output)
    if not re.search(r'^\s*supported\s*$', security, flags=re.MULTILINE | re.IGNORECASE):
        return False
    if re.search(r'^\s*frozen\s*$', security, flags=re.MULTILINE | re.IGNORECASE):
        log("ATA Secure Erase is frozen by firmware. Skipping.", 'WARN')
        return False
    if not re.search(r'^\s*not frozen\s*$', security, flags=re.MULTILINE | re.IGNORECASE):
        log("ATA security state could not be proven unfrozen. Skipping.", 'WARN')
        return False

    password = 'diskmgr'
    password_set = False
    try:
        run_command(['hdparm', '--user-master', 'u', '--security-set-pass', password, dev_path], sudo=True)
        password_set = True
        erase = '--security-erase-enhanced' if re.search(r'enhanced', security, re.I) else '--security-erase'
        run_command(['hdparm', '--user-master', 'u', erase, password, dev_path], sudo=True)
        log("ATA Secure Erase completed successfully.")
        return True
    except Exception as exc:
        log(f"ATA Secure Erase failed: {exc}", 'WARN')
        return False
    finally:
        if password_set:
            # A failed security erase can leave the temporary password enabled.
            run_command(
                ['hdparm', '--user-master', 'u', '--security-disable', password, dev_path],
                sudo=True,
                check=False,
            )


def _software_zero_overwrite(dev_path, size_bytes):
    block_size = 4 * 1024 * 1024
    log(f"Performing exact zero overwrite on {dev_path}: {size_bytes:,} bytes...")
    run_command(
        ['dd', 'if=/dev/zero', f'of={dev_path}', f'bs={block_size}',
         f'count={size_bytes}', 'iflag=fullblock,count_bytes', 'conv=fsync', 'status=progress'],
        sudo=True,
        capture_output=False,
    )
    run_command(['sync'], sudo=True, capture_output=False)
    sample_size = min(1024 * 1024, size_bytes)
    offsets = sorted({0, max(0, (size_bytes - sample_size) // 2), max(0, size_bytes - sample_size)})
    for offset in offsets:
        result = run_command_bytes(
            ['dd', f'if={dev_path}', f'skip={offset}', f'count={sample_size}',
             'iflag=skip_bytes,count_bytes', 'status=none'],
            sudo=True,
        )
        data = getattr(result, 'stdout', b'') or b''
        if len(data) != sample_size or any(data):
            raise RuntimeError(f"zero verification failed at byte offset {offset}")
    log("Exact zero overwrite and beginning/middle/end verification completed.")
    return True


def secure_erase_disk(dev_path):
    try:
        dtype, size_bytes, _logical_sector = _target_geometry(dev_path)
    except Exception as exc:
        log(f"Secure erase blocked: {exc}", 'ERROR')
        return False
    is_part = dtype == 'part'
    log(f"Starting secure erase on {dev_path} ({'PARTITION' if is_part else 'FULL DISK'})")

    try:
        is_nvme, rotational = _target_media_kind(dev_path)
    except Exception as exc:
        log(f"Secure erase blocked: {exc}", 'ERROR')
        return False

    if is_nvme and not is_part:
        try:
            result = run_command(['nvme', 'id-ctrl', '-o', 'json', dev_path], sudo=True)
            ctrl = json.loads(result.stdout)
            oacs = int(ctrl.get('oacs', 0))
            sanicap = int(ctrl.get('sanicap', 0))
            fna = int(ctrl.get('fna', 0))
            if sanicap & 0x1:
                try:
                    return _nvme_sanitize_and_wait(dev_path, 4, 'NVMe Sanitize Crypto Erase')
                except Exception as exc:
                    log(f"NVMe crypto sanitize failed: {exc}", 'WARN')
            if sanicap & 0x2:
                try:
                    return _nvme_sanitize_and_wait(dev_path, 2, 'NVMe Sanitize Block Erase')
                except Exception as exc:
                    log(f"NVMe block sanitize failed: {exc}", 'WARN')
            if oacs & 0x2 and (fna & 0x4):
                try:
                    run_command(['nvme', 'format', dev_path, '--ses=2'], sudo=True)
                    run_command(['udevadm', 'settle'], sudo=True)
                    log("NVMe Format Crypto Erase completed successfully.")
                    return True
                except Exception as exc:
                    log(f"NVMe crypto format failed: {exc}", 'WARN')
            if oacs & 0x2:
                try:
                    run_command(['nvme', 'format', dev_path, '--ses=1'], sudo=True)
                    run_command(['udevadm', 'settle'], sudo=True)
                    log("NVMe Format Block Erase completed successfully.")
                    return True
                except Exception as exc:
                    log(f"NVMe block format failed: {exc}", 'WARN')
        except Exception as exc:
            log(f"NVMe capability query failed: {exc}", 'WARN')
    elif not rotational and not is_part:
        try:
            if _ata_hardware_erase(dev_path):
                return True
        except Exception as exc:
            log(f"SATA hardware erase probe failed: {exc}", 'WARN')
    elif rotational and not is_part:
        try:
            if _ata_hardware_erase(dev_path):
                return True
        except Exception as exc:
            log(f"HDD hardware erase probe failed: {exc}", 'WARN')

    if rotational or is_part:
        try:
            return _software_zero_overwrite(dev_path, size_bytes)
        except Exception as exc:
            log(f"Software overwrite failed: {exc}", 'ERROR')
            return False

    log(f"Attempting secure discard on {dev_path}...")
    try:
        run_command(['blkdiscard', '--secure', dev_path], sudo=True)
        run_command(['udevadm', 'settle'], sudo=True)
        log("Secure discard completed successfully.")
        return True
    except Exception as exc:
        log(f"Secure discard failed: {exc}", 'WARN')
    if disk_discard_supported(dev_path):
        try:
            run_command(['blkdiscard', dev_path], sudo=True)
            run_command(['udevadm', 'settle'], sudo=True)
            log("Standard discard completed successfully.")
            return True
        except Exception as exc:
            log(f"Standard discard failed: {exc}", 'ERROR')
            return False
    try:
        return _software_zero_overwrite(dev_path, size_bytes)
    except Exception as exc:
        log(f"Software overwrite failed: {exc}", 'ERROR')
        return False
