"""Mount and fstab policy helpers."""

from pathlib import Path
import grp
import os
import pwd
import re
import shutil
import sys
import tempfile
import time
from .runtime import log, run_command
from .devices import _lsblk_fstype, disk_is_rotational
from .mounts import find_mount_targets
from .mappings import read_luks_map
from .safety import safe_mount_path, validate_absolute_path, validate_storage_name


class MountPolicyMixin:

    def _resolve_fstab_spec_to_realdev(self, spec):
        """
        Resolve one /etc/fstab source spec to a real /dev path when possible.
        Returns a realpath string or None.
        """
        s = (spec or "").strip()
        if not s:
            return None

        # Common non-block pseudo filesystems.
        if s in ("none", "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay", "swap"):
            return None

        try:
            if s.startswith("/dev/"):
                return os.path.realpath(s) if os.path.exists(s) else None

            if s.startswith("UUID="):
                uuid = s[len("UUID="):]
                by_uuid = f"/dev/disk/by-uuid/{uuid}"
                if os.path.exists(by_uuid):
                    return os.path.realpath(by_uuid)
                res = run_command(['blkid', '-U', uuid], capture_output=True, check=False, timeout=3)
                p = (getattr(res, 'stdout', '') or '').strip()
                return os.path.realpath(p) if p and os.path.exists(p) else None

            if s.startswith("PARTUUID="):
                pu = s[len("PARTUUID="):]
                by_partuuid = f"/dev/disk/by-partuuid/{pu}"
                if os.path.exists(by_partuuid):
                    return os.path.realpath(by_partuuid)
                res = run_command(['blkid', '-t', f'PARTUUID={pu}', '-o', 'device'], capture_output=True, check=False, timeout=3)
                lines = [ln.strip() for ln in (getattr(res, 'stdout', '') or '').splitlines() if ln.strip()]
                if lines and os.path.exists(lines[0]):
                    return os.path.realpath(lines[0])
                return None

            if s.startswith("LABEL="):
                lbl = s[len("LABEL="):]
                by_label = f"/dev/disk/by-label/{lbl}"
                if os.path.exists(by_label):
                    return os.path.realpath(by_label)
                res = run_command(['blkid', '-L', lbl], capture_output=True, check=False, timeout=3)
                p = (getattr(res, 'stdout', '') or '').strip()
                return os.path.realpath(p) if p and os.path.exists(p) else None

            if s.startswith("PARTLABEL="):
                pl = s[len("PARTLABEL="):]
                by_partlabel = f"/dev/disk/by-partlabel/{pl}"
                if os.path.exists(by_partlabel):
                    return os.path.realpath(by_partlabel)
                res = run_command(['blkid', '-t', f'PARTLABEL={pl}', '-o', 'device'], capture_output=True, check=False, timeout=3)
                lines = [ln.strip() for ln in (getattr(res, 'stdout', '') or '').splitlines() if ln.strip()]
                if lines and os.path.exists(lines[0]):
                    return os.path.realpath(lines[0])
                return None
        except Exception:
            return None

        return None

    def _fstab_real_devices(self):
        """
        Build set of real /dev paths referenced by active OS /etc/fstab.
        """
        real_set, _, _, _ = self._fstab_lookup()
        return real_set

    def _fstab_uuid_values(self):
        """
        Build set of UUID values referenced as UUID=... in /etc/fstab.
        """
        _, uuid_set, _, _ = self._fstab_lookup()
        return uuid_set

    def _fstab_lookup(self):
        """
        Parse /etc/fstab once and return:
          real device set, UUID set, real-device detail map, UUID detail map.
        """
        real_set = set()
        uuid_set = set()
        real_detail = {}
        uuid_detail = {}
        fstab = Path('/etc/fstab')
        if not fstab.exists():
            return real_set, uuid_set, real_detail, uuid_detail
        try:
            lines = fstab.read_text(errors='replace').splitlines()
        except Exception:
            return real_set, uuid_set, real_detail, uuid_detail

        for raw in lines:
            line = (raw or '').strip()
            if not line or line.startswith('#'):
                continue
            body = raw.split('#', 1)[0].strip()
            if not body:
                continue
            parts = body.split()
            if len(parts) < 2:
                continue
            spec = (parts[0] or '').strip()
            mountpoint = parts[1] if len(parts) > 1 else "-"
            fstype = parts[2] if len(parts) > 2 else "-"
            opts = parts[3] if len(parts) > 3 else "-"
            dumpf = parts[4] if len(parts) > 4 else "0"
            passno = parts[5] if len(parts) > 5 else "0"
            detail = f"{mountpoint} ({fstype}, opts={opts}, dump={dumpf}, pass={passno})"

            real_dev = self._resolve_fstab_spec_to_realdev(spec)
            if real_dev:
                real_set.add(real_dev)
                real_detail.setdefault(real_dev, []).append(detail)

            if spec.startswith("UUID="):
                uuid = spec[len("UUID="):].strip().lower()
                if uuid:
                    uuid_set.add(uuid)
                    uuid_detail.setdefault(uuid, []).append(detail)

        return real_set, uuid_set, real_detail, uuid_detail

    def _fstab_unescape(self, value):
        s = str(value or "")
        return s.replace("\\040", " ").replace("\\011", "\t")

    def _fstab_escape(self, value):
        s = str(value or "")
        if '\n' in s or '\r' in s or '\x00' in s:
            raise ValueError("fstab fields cannot contain NUL or newline characters")
        return (
            s.replace("\\", "\\134")
            .replace("\t", "\\011")
            .replace(" ", "\\040")
            .replace("#", "\\043")
        )

    def _read_system_fstab_lines(self):
        fstab = Path('/etc/fstab')
        if not fstab.exists():
            return []
        try:
            if fstab.is_symlink() or not fstab.is_file():
                raise RuntimeError("/etc/fstab is not a regular file")
            return fstab.read_text(encoding='utf-8', errors='strict').splitlines()
        except Exception as exc:
            raise RuntimeError(f"could not read /etc/fstab safely: {exc}") from exc

    def _write_system_fstab_lines(self, lines):
        normalized = []
        for line in lines:
            value = str(line)
            if '\x00' in value or '\n' in value or '\r' in value:
                raise ValueError("fstab records must each contain exactly one text line")
            normalized.append(value)
        content = "\n".join(normalized).rstrip("\n") + "\n"
        state_dir = Path.home() / '.local' / 'state' / 'diskmgr'
        state_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix='fstab.', suffix='.new', dir=state_dir)
        tmp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            verify = run_command(
                ['findmnt', '--verify', '--verbose', '--tab-file', str(tmp)],
                check=False,
            )
            verify_text = ((getattr(verify, 'stdout', '') or '') + '\n' +
                           (getattr(verify, 'stderr', '') or ''))
            match = re.search(r'([0-9]+) parse errors?', verify_text)
            if match is None or int(match.group(1)) != 0:
                raise RuntimeError(
                    "refusing to install fstab because findmnt could not verify its syntax: "
                    + verify_text.strip()
                )

            python_bin = shutil.which('python3') or sys.executable
            helper = r'''
import os, shutil, stat, sys, tempfile
source, target = sys.argv[1:3]
if os.path.lexists(target):
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SystemExit("refusing non-regular or symlink /etc/fstab")
directory = os.path.dirname(target)
backup_fd, backup = tempfile.mkstemp(prefix="fstab.diskmgr-backup.", dir=directory)
os.close(backup_fd)
if os.path.exists(target):
    shutil.copyfile(target, backup, follow_symlinks=False)
    os.chmod(backup, 0o600)
new_fd, new_path = tempfile.mkstemp(prefix=".fstab.diskmgr.", dir=directory)
try:
    with os.fdopen(new_fd, "wb", closefd=True) as dst, open(source, "rb") as src:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.chmod(new_path, 0o644)
    os.replace(new_path, target)
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    try:
        os.unlink(new_path)
    except FileNotFoundError:
        pass
print(backup)
'''
            result = run_command(
                [python_bin, '-c', helper, str(tmp), '/etc/fstab'],
                sudo=True,
            )
            backup = (getattr(result, 'stdout', '') or '').strip()
            if backup:
                log(f"Backed up previous fstab to {backup}")
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _iter_fstab_entries(self, lines=None):
        if lines is None:
            lines = self._read_system_fstab_lines()
        out = []
        for idx, raw in enumerate(lines):
            stripped = (raw or "").strip()
            if not stripped or stripped.startswith('#'):
                continue
            body = raw.split('#', 1)[0].strip()
            if not body:
                continue
            parts = body.split()
            if len(parts) < 2:
                continue
            spec = (parts[0] or "").strip()
            mountpoint = self._fstab_unescape(parts[1].strip()) if len(parts) > 1 else ""
            fstype = (parts[2] or "").strip() if len(parts) > 2 else "auto"
            opts = (parts[3] or "").strip() if len(parts) > 3 else "defaults"
            dumpf = (parts[4] or "").strip() if len(parts) > 4 else "0"
            passno = (parts[5] or "").strip() if len(parts) > 5 else "0"
            out.append({
                'index': idx,
                'raw': raw,
                'spec': spec,
                'mountpoint': mountpoint,
                'fstype': fstype,
                'opts': opts,
                'dump': dumpf,
                'passno': passno,
            })
        return out

    def _find_fstab_entry_for_device(self, devnode, preferred_label=None):
        """
        Return the first /etc/fstab entry that appears to target this device.
        Matching priority: real device path, UUID, then LABEL.
        """
        target_real = os.path.realpath(devnode)
        target_uuid = ""
        target_label = preferred_label or ""
        try:
            r_uuid = run_command(['blkid', '-o', 'value', '-s', 'UUID', devnode], sudo=True, check=False)
            target_uuid = (getattr(r_uuid, 'stdout', '') or '').strip().lower()
        except Exception:
            target_uuid = ""
        if not target_label:
            try:
                r_lbl = run_command(['blkid', '-o', 'value', '-s', 'LABEL', devnode], sudo=True, check=False)
                target_label = (getattr(r_lbl, 'stdout', '') or '').strip()
            except Exception:
                target_label = ""

        entries = self._iter_fstab_entries()

        # 1) Exact device resolution
        for ent in entries:
            try:
                resolved = self._resolve_fstab_spec_to_realdev(ent['spec'])
            except Exception:
                resolved = None
            if resolved and os.path.realpath(resolved) == target_real:
                return ent

        # 2) UUID/LABEL text match
        for ent in entries:
            spec = ent['spec']
            if target_uuid and spec.startswith("UUID="):
                uuid = self._fstab_unescape(spec[len("UUID="):]).strip().lower()
                if uuid == target_uuid:
                    return ent
            if target_label and spec.startswith("LABEL="):
                lbl = self._fstab_unescape(spec[len("LABEL="):]).strip()
                if lbl == target_label:
                    return ent

        return None

    def _select_mountpoint_for_device(self, devnode, fallback_mountpoint, preferred_label=None):
        """
        Choose mountpoint for a device. Prefer /etc/fstab when present.
        Returns: (mountpoint, use_fstab, fstab_entry_or_none)
        """
        fstab_entry = self._find_fstab_entry_for_device(devnode, preferred_label=preferred_label)
        if fstab_entry:
            return fstab_entry['mountpoint'], True, fstab_entry
        return fallback_mountpoint, False, None

    def _detect_fstype(self, devnode):
        fstype = (_lsblk_fstype(devnode) or "").strip().lower()
        if fstype:
            return fstype
        try:
            res = run_command(['blkid', '-o', 'value', '-s', 'TYPE', devnode], sudo=True, check=False)
            return (getattr(res, 'stdout', '') or '').strip().lower()
        except Exception:
            return ""

    def _normalize_btrfs_compression_override(self, compress=None, compress_force=None):
        """Return a validated mount option like 'compress=zstd:12' or 'compress-force=zstd:12'."""
        comp = str(compress or '').strip()
        compf = str(compress_force or '').strip()
        if comp and compf:
            raise ValueError("Use only one of --compress or --compress-force.")

        mode = comp or compf
        if not mode:
            return None
        if any(ch.isspace() for ch in mode):
            raise ValueError("Compression mode must not contain whitespace.")
        if ',' in mode or '=' in mode:
            raise ValueError("Compression mode must be a single value like zstd:12.")
        if mode.startswith('-'):
            raise ValueError("Compression mode value is invalid.")

        return f"compress-force={mode}" if compf else f"compress={mode}"

    def _default_btrfs_mount_option(self, devnode):
        """Default btrfs mount policy: HDD => compress-force=zstd:12, otherwise no compression option."""
        if disk_is_rotational(devnode):
            return 'compress-force=zstd:12'
        return None

    def _effective_btrfs_mount_option(self, devnode, btrfs_compression_opt=None):
        if btrfs_compression_opt:
            return btrfs_compression_opt
        return self._default_btrfs_mount_option(devnode)

    def _ensure_btrfs_compression_on_mount(self, mountpoint, desired_opt=None):
        res = run_command(['findmnt', '-rn', '-M', mountpoint, '-o', 'FSTYPE,OPTIONS'], check=False)
        if getattr(res, 'returncode', 1) != 0:
            return
        out = (getattr(res, 'stdout', '') or '').strip()
        if not out:
            return

        parts = out.split(None, 1)
        fstype = (parts[0] if parts else '').strip().lower()
        opts = (parts[1] if len(parts) > 1 else '').strip().lower()
        if fstype != 'btrfs':
            return
        if not desired_opt:
            return

        desired_opt_l = str(desired_opt).strip().lower()
        if desired_opt_l in opts:
            return

        log(f"Enabling btrfs compression on {mountpoint} ({desired_opt}).")
        run_command(['mount', '-o', f'remount,{desired_opt}', mountpoint], sudo=True)

        verify = run_command(['findmnt', '-rn', '-M', mountpoint, '-o', 'OPTIONS'], check=False)
        vopts = (getattr(verify, 'stdout', '') or '').strip().lower()
        if desired_opt_l not in vopts:
            raise RuntimeError(f"Btrfs mount at {mountpoint} is missing required option after remount: {desired_opt}")

    def _mount_device(self, devnode, mountpoint, use_fstab=False, announce_btrfs=False, btrfs_compression_opt=None):
        mountpoint = validate_absolute_path(mountpoint, 'mountpoint')
        dev_fstype = self._detect_fstype(devnode)
        desired_btrfs_opt = self._effective_btrfs_mount_option(devnode, btrfs_compression_opt)

        if announce_btrfs and dev_fstype == 'btrfs' and desired_btrfs_opt:
            log(f"Enabling btrfs compression on {mountpoint} ({desired_btrfs_opt}).")

        run_command(['mkdir', '-p', mountpoint], sudo=True)
        # This path is still the hidden mountpoint directory. Chowning it after
        # mount would instead mutate the root directory of an existing filesystem.
        self._chown_mountpoint_dir(mountpoint)
        if use_fstab:
            # Use /etc/fstab mount options and source selection.
            run_command(['mount', mountpoint], sudo=True)
        else:
            if dev_fstype == 'btrfs' and desired_btrfs_opt:
                run_command(['mount', '-o', desired_btrfs_opt, devnode, mountpoint], sudo=True)
            else:
                run_command(['mount', devnode, mountpoint], sudo=True)

        if dev_fstype == 'btrfs' or use_fstab:
            self._ensure_btrfs_compression_on_mount(mountpoint, desired_opt=desired_btrfs_opt)

    def _invoking_user_group(self):
        """
        Return (user, group) for ownership operations.
        Prefer the invoking non-root user when diskmgr itself is launched via sudo.
        """
        user = (os.environ.get('USER') or os.environ.get('SUDO_USER') or '').strip()
        pkexec_uid = str(os.environ.get('PKEXEC_UID') or '').strip()
        if pkexec_uid.isdigit():
            try:
                user = pwd.getpwuid(int(pkexec_uid)).pw_name
            except KeyError:
                pass
        if user:
            try:
                pw = pwd.getpwnam(user)
                gid = int(pw.pw_gid)
                try:
                    group = grp.getgrgid(gid).gr_name
                except KeyError:
                    group = str(gid)
                return user, group
            except Exception:
                pass

        uid = os.getuid()
        gid = os.getgid()
        return str(uid), str(gid)

    def _chown_new_filesystem_root(self, mountpoint):
        """
        Set ownership of a newly-created filesystem root directory to the invoking user.
        Non-recursive by design.
        """
        mp = str(mountpoint or '').strip()
        if not mp:
            return
        user, group = self._invoking_user_group()
        try:
            run_command(['chown', f'{user}:{group}', mp], sudo=True, check=True)
            log(f"Set new filesystem ownership: {mp} -> {user}:{group}")
        except Exception as e:
            log(f"Could not set ownership on new filesystem root {mp}: {e}", 'ERROR')
            raise

    def _chown_mountpoint_dir(self, mountpoint):
        """
        Set ownership of the mountpoint directory itself to the invoking user.
        This keeps /media/$USER/<name> user-owned after root-created mkdir -p.
        """
        mp = str(mountpoint or '').strip()
        if not mp:
            return
        user, group = self._invoking_user_group()
        try:
            mounted = run_command(['findmnt', '-rn', '-M', mp], check=False)
            if getattr(mounted, 'returncode', 1) == 0:
                raise RuntimeError("mountpoint is already mounted; refusing to chown filesystem root")
            if getattr(mounted, 'returncode', 1) not in (0, 1):
                raise RuntimeError("could not verify mountpoint state")
            run_command(['chown', f'{user}:{group}', mp], sudo=True, check=True)
            log(f"Set mountpoint ownership: {mp} -> {user}:{group}")
        except Exception as e:
            log(f"Could not set ownership on mountpoint directory {mp}: {e}", 'ERROR')
            raise

    def _desired_fstab_options(self, label, fstype=None, target_dev=None):
        label = validate_storage_name(label, 'filesystem label')
        gvfs_name = str(label).replace(",", "_")
        gvfs_name = self._fstab_escape(gvfs_name)
        opts = ["defaults", "nofail"]
        if (str(fstype or "").strip().lower() == "btrfs"):
            default_btrfs_opt = self._default_btrfs_mount_option(target_dev) if target_dev else None
            if default_btrfs_opt:
                opts.append(default_btrfs_opt)
        opts.extend(["x-gvfs-show", f"x-gvfs-name={gvfs_name}"])
        return ",".join(opts)

    def _update_fstab_mountpoint(self, entry, new_mountpoint, new_opts=None):
        """
        Rewrite one existing /etc/fstab entry (matched by entry index).
        Preserves source/fstype/options/dump/pass and trailing inline comment.
        """
        new_mountpoint = validate_absolute_path(new_mountpoint, 'fstab mountpoint')
        idx = int(entry.get('index'))
        lines = self._read_system_fstab_lines()
        if idx < 0 or idx >= len(lines):
            raise RuntimeError("fstab entry index out of range")

        raw = lines[idx]
        comment = ""
        if '#' in raw:
            comment = raw.split('#', 1)[1].strip()

        line = (
            f"{entry.get('spec', '')}\t"
            f"{self._fstab_escape(new_mountpoint)}\t"
            f"{entry.get('fstype', 'auto')}\t"
            f"{new_opts if new_opts is not None else entry.get('opts', 'defaults')}\t"
            f"{entry.get('dump', '0')}\t"
            f"{entry.get('passno', '0')}"
        )
        if comment:
            line += f"  # {comment}"
        lines[idx] = line
        self._write_system_fstab_lines(lines)

    def _update_fstab_for_label_change(self, target_dev, old_label, new_label, fstype, add_entry):
        """
        Update host /etc/fstab after relabel.
        - Removes old-label fstab entry when old label is known.
        - Adds a new UUID= entry mounted at /mnt/<label> when add_entry=True.
        Returns (removed_count, added_count, new_mountpoint_or_empty).
        """
        lines = self._read_system_fstab_lines()
        if new_label:
            validate_storage_name(new_label, 'new filesystem label')
        try:
            old_mountpoint_media = safe_mount_path(
                f"/media/{os.environ.get('USER', 'root')}", old_label
            ) if old_label else ""
            old_mountpoint_mnt = safe_mount_path('/mnt', old_label) if old_label else ""
        except ValueError:
            # An existing externally-created label can be unsafe as a path. It
            # may still be matched by UUID, but must never be interpolated.
            old_mountpoint_media = ""
            old_mountpoint_mnt = ""
        new_mountpoint = safe_mount_path('/mnt', new_label) if new_label else ""
        target_uuid = ""
        if add_entry:
            try:
                r_uuid = run_command(['blkid', '-o', 'value', '-s', 'UUID', target_dev], sudo=True, check=False)
                target_uuid = (getattr(r_uuid, 'stdout', '') or '').strip()
            except Exception:
                target_uuid = ""
            if not target_uuid:
                raise RuntimeError(f"Could not determine filesystem UUID for {target_dev}")

        out = []
        removed = 0

        for raw in lines:
            stripped = (raw or "").strip()
            if not stripped or stripped.startswith('#'):
                out.append(raw)
                continue

            body = raw.split('#', 1)[0].strip()
            parts = body.split()
            if len(parts) < 2:
                out.append(raw)
                continue

            spec = (parts[0] or "").strip()
            mountpoint = self._fstab_unescape((parts[1] or "").strip()) if len(parts) > 1 else ""
            spec_unesc = self._fstab_unescape(spec)
            remove = False

            if old_label:
                if spec.startswith("LABEL="):
                    lbl = self._fstab_unescape(spec[len("LABEL="):]).strip()
                    if lbl == old_label:
                        remove = True
                if spec_unesc == f"/dev/disk/by-label/{old_label}":
                    remove = True
                if mountpoint in (old_mountpoint_media, old_mountpoint_mnt):
                    remove = True

            if add_entry and new_label:
                if spec.startswith("LABEL="):
                    lbl = self._fstab_unescape(spec[len("LABEL="):]).strip()
                    if lbl == new_label:
                        remove = True
                if target_uuid and spec.startswith("UUID="):
                    u = self._fstab_unescape(spec[len("UUID="):]).strip().lower()
                    if u == str(target_uuid).strip().lower():
                        remove = True
                if mountpoint == new_mountpoint:
                    remove = True

            if remove:
                removed += 1
                continue
            out.append(raw)

        added = 0
        if add_entry and new_label:
            fs_t = (fstype or "auto").strip() or "auto"
            opts = self._desired_fstab_options(new_label, fs_t, target_dev=target_dev)
            entry = (
                f"UUID={self._fstab_escape(target_uuid)}\t"
                f"{self._fstab_escape(new_mountpoint)}\t"
                f"{fs_t}\t{opts}\t0\t0"
            )
            if out and out[-1].strip():
                out.append("")
            out.append(entry)
            added = 1

        if removed or added:
            self._write_system_fstab_lines(out)

        return removed, added, new_mountpoint

    def get_mountpoint(self, name):
        '''Resolves the current mountpoint for a friendly name.'''
        self.mappings = read_luks_map()
        if name not in self.mappings:
            return None

        src = self.mappings[name]
        devnode = os.path.realpath(src)

        # Check if it's LUKS and open
        mapper_path = f"/dev/mapper/{name}"
        target = mapper_path if os.path.exists(mapper_path) else devnode

        targets = find_mount_targets(target)
        if not targets:
            return None

        preferred = f"/media/{os.environ.get('USER', 'root')}/{name}"
        return preferred if preferred in targets else targets[0]
