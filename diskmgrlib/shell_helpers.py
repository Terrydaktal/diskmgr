"""Shared discovery, mount, formatting, and target-resolution helpers."""

from .common import *


class ShellHelpersMixin:

    def _lsblk_verbose_cols(self):
        return [
            "#",
            "NAME",
            "DEVICE",
            "TYPE",
            "STATE",
            "OWNER",
            "FSTYPE",
            "FSVER",
            "FSLABEL",
            "FSUUID",
            "PARTUUID",
            "SIZE",
            "FSAVAIL",
            "FSUSE%",
            "FSRESERVED",
            "FSMETA",
            "FSMOUNTPOINTS",
            "MOUNTSOURCE",
            "FS-OPTIONS",
            "VFS-OPTIONS",
            "FSTAB",
            "FSTAB ENTRY",
            "PERSISTENT PATH",
        ]

    def _lsblk_standard_cols(self):
        return [
            "#",
            "NAME",
            "DEVICE",
            "TYPE",
            "STATE",
            "FSTYPE",
            "FSLABEL",
            "SIZE",
            "FSAVAIL",
            "FSMOUNTPOINTS",
            "PERSISTENT PATH",
        ]

    def _lsblk_concise_cols(self):
        return [
            "#",
            "NAME",
            "DEVICE",
            "STATE",
            "FSTYPE",
            "SIZE",
            "FSAVAIL",
            "MOUNTPOINT",
            "PERSISTENT PATH",
        ]

    def _lsblk_row_get(self, row, col):
        """Resolve display column names to backing row keys."""
        if col == "PERSISTENT PATH":
            return row.get("PERSISTENT PATH (IEEE EUI WWID)", "")
        return row.get(col, "")

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
        return s.replace("\\", "\\\\").replace("\t", "\\011").replace(" ", "\\040")

    def _read_system_fstab_lines(self):
        fstab = Path('/etc/fstab')
        if not fstab.exists():
            return []
        try:
            return fstab.read_text(errors='replace').splitlines()
        except Exception:
            return []

    def _write_system_fstab_lines(self, lines):
        # Write through a temporary file, then copy with sudo.
        content = "\n".join(lines).rstrip("\n") + "\n"
        tmp = Path(f"/tmp/diskmgr_fstab_{os.getpid()}_{int(time.time())}.tmp")
        tmp.write_text(content, encoding='utf-8')
        try:
            run_command(['cp', str(tmp), '/etc/fstab'], sudo=True)
            run_command(['chmod', '644', '/etc/fstab'], sudo=True, check=False)
        finally:
            try:
                tmp.unlink()
            except Exception:
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
        dev_fstype = self._detect_fstype(devnode)
        desired_btrfs_opt = self._effective_btrfs_mount_option(devnode, btrfs_compression_opt)

        if announce_btrfs and dev_fstype == 'btrfs' and desired_btrfs_opt:
            log(f"Enabling btrfs compression on {mountpoint} ({desired_btrfs_opt}).")

        run_command(['mkdir', '-p', mountpoint], sudo=True)
        if use_fstab:
            # Use /etc/fstab mount options and source selection.
            run_command(['mount', mountpoint], sudo=True)
        else:
            if dev_fstype == 'btrfs' and desired_btrfs_opt:
                run_command(['mount', '-o', desired_btrfs_opt, devnode, mountpoint], sudo=True)
            else:
                run_command(['mount', devnode, mountpoint], sudo=True)

        # Root-created mountpoint directories should end up owned by the invoking user.
        self._chown_mountpoint_dir(mountpoint)
        if dev_fstype == 'btrfs' or use_fstab:
            self._ensure_btrfs_compression_on_mount(mountpoint, desired_opt=desired_btrfs_opt)

    def _invoking_user_group(self):
        """
        Return (user, group) for ownership operations.
        Prefer the invoking non-root user when diskmgr itself is launched via sudo.
        """
        user = (os.environ.get('SUDO_USER') or os.environ.get('USER') or '').strip()
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
            run_command(['chown', f'{user}:{group}', mp], sudo=True, check=False)
            log(f"Set new filesystem ownership: {mp} -> {user}:{group}")
        except Exception as e:
            log(f"Could not set ownership on new filesystem root {mp}: {e}", 'WARN')

    def _chown_mountpoint_dir(self, mountpoint):
        """
        Set ownership of the mountpoint directory itself to the invoking user.
        This keeps /media/$USER/<name> user-owned after root-created mkdir -p.
        """
        mp = str(mountpoint or '').strip()
        if not mp:
            return
        user, _group = self._invoking_user_group()
        try:
            run_command(['chown', f'{user}:{user}', mp], sudo=True, check=False)
            log(f"Set mountpoint ownership: {mp} -> {user}:{user}")
        except Exception as e:
            log(f"Could not set ownership on mountpoint directory {mp}: {e}", 'WARN')

    def _desired_fstab_options(self, label, fstype=None, target_dev=None):
        gvfs_name = str(label or "").replace(",", "_")
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
        old_mountpoint_media = f"/media/{os.environ.get('USER', 'root')}/{old_label}" if old_label else ""
        old_mountpoint_mnt = f"/mnt/{old_label}" if old_label else ""
        new_mountpoint = f"/mnt/{new_label}" if new_label else ""
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

    def _findmnt_lookup(self):
        """
        Build mount metadata by source device from findmnt JSON.
        Returns:
          {
            "<real /dev path>": {
              "SOURCE": "<joined>",
              "FS-OPTIONS": "<joined>",
              "VFS-OPTIONS": "<joined>",
              "FS-USED": "<joined>",
            },
            ...
          }
        """
        lookup = {}
        try:
            res = run_command(['findmnt', '-A', '--output-all', '--json'], check=False, timeout=5)
            if getattr(res, 'returncode', 1) != 0:
                return {}
            raw = (getattr(res, 'stdout', '') or '').strip()
            if not raw:
                return {}
            data = json.loads(raw)
            roots = data.get('filesystems', [])
            if not isinstance(roots, list):
                return {}
        except Exception:
            return {}

        def _add_unique(lst, val):
            v = str(val or '').strip()
            if not v:
                return
            if v not in lst:
                lst.append(v)

        def _iter_findmnt_nodes(nodes):
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                yield node
                children = node.get('children')
                if isinstance(children, list):
                    yield from _iter_findmnt_nodes(children)

        for fs in _iter_findmnt_nodes(roots):
            if not isinstance(fs, dict):
                continue
            src = str(fs.get('source') or '').strip()
            if not src or not src.startswith('/dev/'):
                continue

            fs_opts = str(fs.get('fs-options') or '').strip()
            vfs_opts = str(fs.get('vfs-options') or '').strip()
            fs_used = str(fs.get('used') or '').strip()

            keys = [src]
            try:
                src_real = os.path.realpath(src)
                if src_real and src_real not in keys:
                    keys.append(src_real)
            except Exception:
                pass

            for key in keys:
                slot = lookup.setdefault(key, {'SOURCE': [], 'FS-OPTIONS': [], 'VFS-OPTIONS': [], 'FS-USED': []})
                _add_unique(slot['SOURCE'], src)
                _add_unique(slot['FS-OPTIONS'], fs_opts)
                _add_unique(slot['VFS-OPTIONS'], vfs_opts)
                _add_unique(slot['FS-USED'], fs_used)

        out = {}
        for key, slot in lookup.items():
            out[key] = {
                'SOURCE': " | ".join(slot['SOURCE']) if slot['SOURCE'] else '-',
                'FS-OPTIONS': " | ".join(slot['FS-OPTIONS']) if slot['FS-OPTIONS'] else '-',
                'VFS-OPTIONS': " | ".join(slot['VFS-OPTIONS']) if slot['VFS-OPTIONS'] else '-',
                'FS-USED': " | ".join(slot['FS-USED']) if slot['FS-USED'] else '-',
            }
        return out

    def _lsblk_col_widths(self, rows, cols=None):
        if cols is None:
            cols = self._lsblk_verbose_cols()
        widths = {}
        for c in cols:
            widths[c] = len(c)
        for r in rows:
            for c in cols:
                v = str(self._lsblk_row_get(r, c) or "")
                if len(v) > widths[c]:
                    widths[c] = len(v)
        # Add 1 space padding on each side.
        for c in cols:
            widths[c] += 2
        return widths

    def _lsblk_rendered_width(self, rows, widths=None, cols=None):
        """
        Return the maximum visible width for the rendered lsblk table (header + rows)
        using the same formatting logic as _print_lsblk_rows().
        """
        if cols is None:
            cols = self._lsblk_verbose_cols()
        if widths is None:
            widths = self._lsblk_col_widths(rows, cols=cols)

        def _line_for(values):
            parts = []
            for c in cols:
                inner = max(widths[c] - 2, 0)
                if isinstance(values, dict) and c in values:
                    v = values.get(c, '')
                else:
                    v = self._lsblk_row_get(values, c) if isinstance(values, dict) else ''
                parts.append(f" {str(v or ''):<{inner}} ")
            return "".join(parts).rstrip()

        max_len = len(_line_for({c: c for c in cols}))
        for r in rows:
            max_len = max(max_len, len(_line_for(r)))
        return max_len

    def _lsblk_rendered_list_width(self, rows, cols=None):
        """Return max visible width for list-style lsblk rendering."""
        if cols is None:
            cols = self._lsblk_verbose_cols()
        max_len = len("Entry 1")
        for i, r in enumerate(rows, start=1):
            max_len = max(max_len, len(f"Entry {i}"))
            for c in cols:
                v = self._lsblk_list_value(r, c)
                max_len = max(max_len, len(f"  {c}: {v}"))
        return max_len

    def _format_size_tib_gib(self, size_value):
        """
        Format byte-count sizes with higher precision in binary units.
        Uses GiB/TiB (and higher if needed), promoting units when rounding
        would otherwise show 1024.000 of the lower unit.
        """
        raw = str(size_value or '').strip()
        if not raw:
            return ""
        try:
            n = int(raw, 10)
        except (TypeError, ValueError):
            # Fallback for unexpected non-byte strings.
            return raw

        if n == 0:
            return "0 GiB"

        value = n / float(1024 ** 3)  # start in GiB
        units = ["GiB", "TiB", "PiB", "EiB"]
        idx = 0
        while idx < len(units) - 1 and round(value, 3) >= 1024.0:
            value /= 1024.0
            idx += 1
        return f"{value:.3f} {units[idx]}"

    def _format_bytes_binary(self, byte_value, decimals=2):
        """Format a raw byte count using binary units (B, KiB, MiB, GiB, TiB...) with fixed decimals."""
        raw = str(byte_value or '').strip()
        if not raw or raw == "-":
            return ""
        try:
            n = int(raw, 10)
        except (TypeError, ValueError):
            return raw
        if n < 0:
            return ""

        units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
        idx = 0
        value = float(n)
        while idx < len(units) - 1 and value >= 1024.0:
            value /= 1024.0
            idx += 1
        while idx < len(units) - 1 and round(value, decimals) >= 1024.0:
            value /= 1024.0
            idx += 1

        if units[idx] == "B":
            return f"{int(value)} B"
        return f"{value:.{decimals}f} {units[idx]}"

    def _format_bytes_binary_with_metric(self, byte_value):
        """
        Format a raw byte count as:
          - binary: MiB/GiB/TiB (base 1024)
          - metric: (MB/GB/TB) in brackets (base 1000)
        Picks unit based on range (MiB < 1 GiB, GiB < 1 TiB, else TiB+).
        """
        raw = str(byte_value or "").strip()
        if not raw or raw == "-":
            return raw
        try:
            n = int(raw, 10)
        except (TypeError, ValueError):
            return raw
        if n < 0:
            return "-"

        if n >= 1024 ** 4:
            bin_unit = "TiB"
            bin_div = 1024 ** 4
            met_unit = "TB"
            met_div = 1000 ** 4
        elif n >= 1024 ** 3:
            bin_unit = "GiB"
            bin_div = 1024 ** 3
            met_unit = "GB"
            met_div = 1000 ** 3
        else:
            bin_unit = "MiB"
            bin_div = 1024 ** 2
            met_unit = "MB"
            met_div = 1000 ** 2

        bin_val = n / float(bin_div)
        met_val = n / float(met_div)

        # Binary: keep consistent 3-decimal precision like SIZE.
        bin_s = f"{bin_val:.3f} {bin_unit}"

        # Metric: fewer decimals for readability.
        if met_val >= 100:
            met_s = f"{met_val:.0f}{met_unit}"
        elif met_val >= 10:
            met_s = f"{met_val:.1f}{met_unit}"
        else:
            met_s = f"{met_val:.2f}{met_unit}"

        return f"{bin_s} ({met_s})"

    def _collect_lsblk_rows(self, devices, indent="", is_root=True, parent_wwn="", friendly_map=None, fstab_real_set=None, fstab_uuid_set=None, fstab_detail_real=None, fstab_detail_uuid=None, findmnt_lookup=None, include_ext4_details=True):
        """
        Collect an lsblk JSON tree into flat rows, while preserving the tree glyphs in NAME.
        Returns a list of dicts with consistent string keys.
        """
        rows = []
        if fstab_real_set is None:
            fstab_real_set = set()
        if fstab_uuid_set is None:
            fstab_uuid_set = set()
        if fstab_detail_real is None:
            fstab_detail_real = {}
        if fstab_detail_uuid is None:
            fstab_detail_uuid = {}
        if findmnt_lookup is None:
            findmnt_lookup = {}
        for i, dev in enumerate(devices):
            is_last = (i == len(devices) - 1)

            name = dev.get('name', '') or ""
            kname = dev.get('kname', '') or name
            dtype = dev.get('type') or ""
            size_bytes_raw = str(dev.get('size') or "").strip()
            size = self._format_size_tib_gib(size_bytes_raw)
            fstype = dev.get('fstype') or ""
            fsver = dev.get('fsver') or ""
            label = dev.get('label') or ""
            uuid = dev.get('uuid') or ""
            partuuid = dev.get('partuuid') or ""
            fsavail_raw = dev.get('fsavail') or ""
            fsavail = self._format_bytes_binary_with_metric(fsavail_raw)
            fsuse = dev.get('fsuse%') or ""
            fsused_bytes_raw = str(dev.get('fsused') or "").strip()
            # Partitions often don't carry WWN/EUI in lsblk output; inherit from parent disk when missing.
            wwn = dev.get('wwn') or parent_wwn or ""

            mp = dev.get('mountpoints', [])
            owner_probe_mount = ""
            if isinstance(mp, list):
                mount_list = [m for m in mp if m]
                mounts = ", ".join(mount_list)
                if mount_list:
                    owner_probe_mount = mount_list[0]
            else:
                mounts = str(mp or "")
                owner_probe_mount = mounts.split(",")[0].strip() if mounts else ""
            mounts_clean = mounts.strip()
            owner = "-"
            if owner_probe_mount:
                try:
                    # Bound owner probe so stale/unresponsive mountpoints cannot hang list.
                    st_res = run_command(
                        ['stat', '-c', '%u %g', owner_probe_mount],
                        check=False,
                        timeout=1.5
                    )
                    st_out = (getattr(st_res, 'stdout', '') or '').strip()
                    parts = st_out.split()
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        uid = int(parts[0])
                        gid = int(parts[1])
                        try:
                            owner_user = pwd.getpwuid(uid).pw_name
                        except Exception:
                            owner_user = str(uid)
                        try:
                            owner_group = grp.getgrgid(gid).gr_name
                        except Exception:
                            owner_group = str(gid)
                        owner = f"{owner_user} {owner_group}"
                except Exception:
                    owner = "-"

            # Persistent path: prefer IEEE identifier by-id links (WWN/EUI) when possible.
            pdp = "-"
            try:
                pdp = self.find_persistent_path(name, wwn=wwn, type_=dtype or 'disk')
            except Exception:
                pdp = "-"
            model_serial_wwid = "-"
            try:
                model_serial_wwid = self.find_serial_wwid_path(name, type_=dtype or 'disk')
            except Exception:
                model_serial_wwid = "-"
            pci_path = "-"
            try:
                pci_path = self.find_pci_path(name, type_=dtype or 'disk')
            except Exception:
                pci_path = "-"

            # Tree characters
            if is_root:
                # For device-mapper/crypto nodes, show the kernel name plus friendly name in brackets.
                # Example: dm-0 (1a)
                if dtype == 'crypt' and kname and name and kname != name:
                    tree_part = f"{kname} ({name})"
                else:
                    tree_part = name
                next_indent = ""
            else:
                char = "└─" if is_last else "├─"
                shown = name
                if dtype == 'crypt' and kname and name and kname != name:
                    shown = f"{kname} ({name})"
                tree_part = indent + char + shown
                next_indent = indent + ("    " if is_last else "│   ")

            # Friendly name (diskmap.tsv) is only for real disk/partition nodes.
            friendly = "-"
            if dtype in ('disk', 'part') and friendly_map:
                try:
                    devnode = f"/dev/{name}"
                    if os.path.exists(devnode):
                        friendly = friendly_map.get(os.path.realpath(devnode), "-")
                except Exception:
                    friendly = "-"

            dev_real = os.path.realpath(f"/dev/{kname}")
            uuid_norm = (str(uuid).strip().lower() if uuid else "")
            in_fstab = (dev_real in fstab_real_set) or (uuid_norm in fstab_uuid_set if uuid_norm else False)
            fstab_entries = []
            for ent in fstab_detail_real.get(dev_real, []):
                if ent and ent not in fstab_entries:
                    fstab_entries.append(ent)
            if uuid_norm:
                for ent in fstab_detail_uuid.get(uuid_norm, []):
                    if ent and ent not in fstab_entries:
                        fstab_entries.append(ent)
            fstab_entry = " | ".join(fstab_entries) if fstab_entries else "-"
            findmnt_row = findmnt_lookup.get(dev_real) or findmnt_lookup.get(f"/dev/{kname}") or {}
            mount_source = str(findmnt_row.get('SOURCE') or '-')
            fs_options = str(findmnt_row.get('FS-OPTIONS') or '-')
            vfs_options = str(findmnt_row.get('VFS-OPTIONS') or '-')
            fs_used_metric = str(findmnt_row.get('FS-USED') or '-')
            fs_reserved = "-"
            fs_meta = "-"
            if include_ext4_details and str(fstype).strip().lower() == "ext4":
                fs_reserved, fs_meta = self._ext4_reserved_and_meta(
                    dev_real=dev_real,
                    size_bytes_raw=size_bytes_raw,
                    fsused_bytes_raw=fsused_bytes_raw,
                    fsavail_bytes_raw=str(fsavail_raw or "").strip(),
                )
            fstype_l = str(fstype or "").strip().lower()
            state = "-"
            if fstype_l == "crypto_luks":
                children = dev.get('children') or []
                has_crypt_child = any((str(ch.get('type') or "").strip().lower() == "crypt") for ch in children if isinstance(ch, dict))
                state = "OPEN" if has_crypt_child else "CLOSED"
            elif fstype_l:
                # Plain filesystem entries and open LUKS child filesystems.
                state = "MOUNTED" if mounts_clean else "UNMOUNTED"
            rows.append({
                "#": "-",
                "KNAME": kname,
                "PKNAME": str(dev.get('pkname') or ""),
                "NAME": friendly,
                "DEVICE": tree_part,
                "PERSISTENT PATH (IEEE EUI WWID)": pdp,
                "Model_Serial WWID": model_serial_wwid,
                "PCI": pci_path,
                "TYPE": dtype,
                "STATE": state,
                "OWNER": owner,
                "FSTAB": "y" if in_fstab else "n",
                "SIZE": size,
                "SIZE-BYTES": size_bytes_raw,
                "FSTYPE": fstype,
                "FSVER": fsver,
                "FSLABEL": label,
                "FSUUID": uuid,
                "PARTUUID": partuuid,
                "FSAVAIL": fsavail,
                "FSAVAIL-BYTES": str(fsavail_raw or "").strip(),
                "FSUSE%": fsuse,
                "FSRESERVED": fs_reserved,
                "FSMETA": fs_meta,
                "FSUSED-BYTES": fsused_bytes_raw,
                "FSMOUNTPOINTS": mounts,
                "MOUNTSOURCE": mount_source,
                "FS-OPTIONS": fs_options,
                "VFS-OPTIONS": vfs_options,
                "FS-USED": fs_used_metric,
                "MOUNTPOINT": mounts,
                "FSTAB ENTRY": fstab_entry,
            })

            if 'children' in dev:
                rows.extend(self._collect_lsblk_rows(
                    dev['children'],
                    next_indent,
                    is_root=False,
                    parent_wwn=wwn,
                    friendly_map=friendly_map,
                    fstab_real_set=fstab_real_set,
                    fstab_uuid_set=fstab_uuid_set,
                    fstab_detail_real=fstab_detail_real,
                    fstab_detail_uuid=fstab_detail_uuid,
                    findmnt_lookup=findmnt_lookup,
                    include_ext4_details=include_ext4_details
                ))
        return rows

    def _build_list_rows_snapshot(self, include_ext4_details=True):
        """
        Build current list rows in the same shape as `list verbose`.
        Returns flat rows with global # numbering for disk/part/crypt entries.
        """
        all_devs = self.get_disk_info()
        disks = [d for d in all_devs if d.get('type') == 'disk']
        if not disks:
            return []

        friendly_map = {}
        try:
            self.mappings = read_luks_map()
            for friendly, target in (self.mappings or {}).items():
                try:
                    if target and os.path.exists(target):
                        friendly_map[os.path.realpath(target)] = friendly
                except Exception:
                    continue
        except Exception:
            friendly_map = {}

        fstab_real_set, fstab_uuid_set, fstab_detail_real, fstab_detail_uuid = self._fstab_lookup()
        findmnt_lookup = self._findmnt_lookup()

        rows = []
        for d in disks:
            d_name = d.get('name', '')
            if not d_name:
                continue
            dev_path = f"/dev/{d_name}"
            try:
                ls_res = run_command_hard_timeout(
                    ['lsblk', '-J', '-b', '-f', '-o',
                     'NAME,KNAME,PKNAME,TYPE,SIZE,FSTYPE,FSVER,LABEL,UUID,PARTUUID,FSAVAIL,FSUSE%,FSUSED,MOUNTPOINTS,WWN',
                     dev_path],
                    6,
                    sudo=True,
                    check=False
                )
                if (getattr(ls_res, 'stdout', '') or '').strip():
                    data = json.loads(ls_res.stdout)
                    blockdevices = data.get('blockdevices', [])
                    rows.extend(self._collect_lsblk_rows(
                        blockdevices,
                        friendly_map=friendly_map,
                        fstab_real_set=fstab_real_set,
                        fstab_uuid_set=fstab_uuid_set,
                        fstab_detail_real=fstab_detail_real,
                        fstab_detail_uuid=fstab_detail_uuid,
                        findmnt_lookup=findmnt_lookup,
                        include_ext4_details=include_ext4_details,
                    ))
            except Exception:
                continue

        idx = 1
        for r in rows:
            if (r.get('TYPE') or '') in ('disk', 'part', 'crypt'):
                r['#'] = str(idx)
                idx += 1
            else:
                r['#'] = '-'
        return rows

    def _show_target_entries_for_confirmation(self, target_name):
        """
        Print current list-list entry/entries for the target before confirmation.
        Also includes an open crypt child when target is a LUKS container parent.
        """
        try:
            rows = self._build_list_rows_snapshot(include_ext4_details=True)
            if not rows:
                return

            target_paths = set()
            text = str(target_name or "").strip()

            # Any explicit /dev paths embedded in the confirmation label.
            for p in re.findall(r"/dev/[A-Za-z0-9._/-]+", text):
                p = p.rstrip("),]")
                if os.path.exists(p):
                    target_paths.add(os.path.realpath(p))

            # Direct path input.
            if text and os.path.exists(text):
                target_paths.add(os.path.realpath(text))

            token = text.split()[0].strip("[](),") if text else ""
            if token:
                try:
                    resolved = self.resolve_target(token, allow_id=True)
                except Exception:
                    resolved = None
                if resolved and os.path.exists(resolved):
                    target_paths.add(os.path.realpath(resolved))

                mapper_path = f"/dev/mapper/{token}"
                if os.path.exists(mapper_path):
                    target_paths.add(os.path.realpath(mapper_path))

            if not target_paths:
                return

            matched_knames = set()
            selected = []
            for r in rows:
                kname = str(r.get('KNAME') or "").strip()
                if not kname:
                    continue
                devreal = os.path.realpath(f"/dev/{kname}")
                if devreal in target_paths:
                    selected.append(r)
                    matched_knames.add(kname)

            # If a matched row is a LUKS container parent, include open crypt child row too.
            for r in rows:
                if (r.get('TYPE') or '') != 'crypt':
                    continue
                pk = str(r.get('PKNAME') or "").strip()
                if pk and pk in matched_knames and str(r.get('KNAME') or '') not in matched_knames:
                    selected.append(r)
                    matched_knames.add(str(r.get('KNAME') or ''))

            if not selected:
                return

            print(f"\n{Colors.BOLD}Target entry snapshot:{Colors.ENDC}")
            self._print_lsblk_rows_list(selected, cols=self._lsblk_verbose_cols())
        except Exception:
            # Confirmation should continue even if snapshot rendering fails.
            pass

    def _resolve_confirmation_identity(self, target_name):
        """
        Resolve confirmation identity as (device_real, persistent_path, pci_path).
        Returns (None, None, None) when identity cannot be determined reliably.
        """
        text = str(target_name or "").strip()
        if not text:
            return (None, None, None)

        candidates = []

        def _add_candidate(p):
            v = str(p or "").strip().rstrip("),]")
            if not v:
                return
            if v not in candidates:
                candidates.append(v)

        # Full text path first (important for absolute paths containing spaces).
        if os.path.exists(text):
            _add_candidate(text)

        # Explicit /dev paths first.
        for p in re.findall(r"/dev/[A-Za-z0-9._/\-+:#]+", text):
            _add_candidate(p)

        # Any explicit absolute paths (mountpoints, etc.) that might map to /dev via findmnt.
        for p in re.findall(r"/[A-Za-z0-9._/\-+:#]+", text):
            _add_candidate(p)

        # First token can be mapping name or discovery ID.
        token = text.split()[0].strip("[](),")
        if token:
            _add_candidate(token)

        def _candidate_to_device(c):
            c = str(c or "").strip()
            if not c:
                return None

            # Mapping name or #N.
            try:
                resolved = self.resolve_target(c, allow_id=True)
            except Exception:
                resolved = None
            if resolved and os.path.exists(resolved):
                return os.path.realpath(resolved)

            # Existing path.
            if os.path.exists(c):
                rc = os.path.realpath(c)
                if rc.startswith('/dev/'):
                    return rc
                # Mountpoint path -> mounted source device.
                res_src = run_command(['findmnt', '-rn', '-T', rc, '-o', 'SOURCE'], check=False)
                src = (getattr(res_src, 'stdout', '') or '').strip().splitlines()
                src = src[0].strip() if src else ""
                if src.startswith('/dev/'):
                    return os.path.realpath(src)

            # Raw /dev path token that might exist even if os.path.exists() raced.
            if c.startswith('/dev/'):
                return os.path.realpath(c)

            return None

        device_real = None
        for c in candidates:
            dev = _candidate_to_device(c)
            if dev and dev.startswith('/dev/') and os.path.exists(dev):
                device_real = dev
                break

        if not device_real:
            return (None, None, None)

        # Map virtual devices (e.g. /dev/mapper/*) to a stable underlying block
        # node before resolving persistent by-id identity.
        id_dev = device_real
        try:
            t = (_lsblk_type(id_dev) or '').strip().lower()
            hops = 0
            while t == 'crypt' and hops < 8:
                res_pk = run_command(['lsblk', '-no', 'PKNAME', id_dev], check=False)
                pk = (getattr(res_pk, 'stdout', '') or '').strip().splitlines()
                pk = pk[0].strip() if pk else ""
                if not pk:
                    break
                nxt = os.path.realpath(f"/dev/{pk}")
                if not os.path.exists(nxt) or nxt == id_dev:
                    break
                id_dev = nxt
                t = (_lsblk_type(id_dev) or '').strip().lower()
                hops += 1
        except Exception:
            pass

        try:
            kname = os.path.basename(id_dev)
            dtype = (_lsblk_type(id_dev) or '').strip().lower()
            if dtype not in ('disk', 'part'):
                if _sysfs_is_whole_disk(id_dev):
                    dtype = 'disk'
                else:
                    dtype = 'part'
            res_wwn = run_command(['lsblk', '-no', 'WWN', id_dev], check=False)
            wwn = (getattr(res_wwn, 'stdout', '') or '').strip()
            pdp = self.find_persistent_path(kname, wwn=wwn, type_=dtype)
            pci = self.find_pci_path(kname, type_=dtype)
        except Exception:
            pdp = "-"
            pci = "-"

        if not pdp or pdp == "-":
            return (device_real, None, None)
        if not pci or pci == "-":
            return (device_real, pdp, None)
        return (device_real, pdp, pci)

    def _print_lsblk_rows(self, rows, widths=None, cols=None):
        """
        Print a column-aligned table where each cell is padded with one space on each side.
        Persistent path is printed immediately after NAME.
        """
        if cols is None:
            cols = self._lsblk_verbose_cols()
        if widths is None:
            widths = self._lsblk_col_widths(rows, cols=cols)

        def _cell(text, width):
            inner = max(width - 2, 0)
            return f" {text:<{inner}} "

        header = "".join([_cell(c, widths[c]) for c in cols]).rstrip()
        print(f"{Colors.BOLD}{header}{Colors.ENDC}")
        for r in rows:
            line = "".join([_cell(str(self._lsblk_row_get(r, c) or ''), widths[c]) for c in cols]).rstrip()
            print(line)

    def _print_lsblk_rows_list(self, rows, cols=None, start_index=1):
        """Print rows as list-style key/value entries."""
        if cols is None:
            cols = self._lsblk_verbose_cols()
        for i, r in enumerate(rows, start=1):
            print(f"{Colors.BOLD}Entry {start_index + i - 1}{Colors.ENDC}")

            is_luks_row = str(r.get('FSTYPE') or '').strip().lower() == 'crypto_luks'
            pdp_raw = str(self._lsblk_list_value(r, 'PERSISTENT PATH') or '').strip()
            is_usb_byid = pdp_raw.startswith('/dev/disk/by-id/usb-')
            for c in cols:
                value = self._lsblk_list_value(r, c)
                if str(value or "").strip() in ("", "-"):
                    continue
                label = c
                if is_luks_row and c == "FSVER":
                    label = "LUKSVER"
                elif is_luks_row and c == "FSUUID":
                    label = "LUKSUUID"
                elif c == "PERSISTENT PATH":
                    if is_usb_byid:
                        # USB by-id links are model/serial based; present a single clear label.
                        label = "PERSISTENT PATH (Model_Serial WWID)"
                    else:
                        # In list-list mode keep explicit IEEE wording for non-USB entries.
                        label = "PERSISTENT PATH (IEEE EUI WWID)"
                print(f"  {label}: {value}")
            if str(r.get('TYPE') or '').strip().lower() in ('disk', 'part'):
                # For USB rows, the persistent by-id path is already the model/serial path.
                if not is_usb_byid:
                    model_serial = self._lsblk_list_value(r, 'Model_Serial WWID')
                    if str(model_serial or "").strip() not in ("", "-"):
                        print(f"  Model_Serial WWID: {model_serial}")
                pci = self._lsblk_list_value(r, 'PCI')
                if str(pci or "").strip() not in ("", "-"):
                    print(f"  PCI: {pci}")
            if i != len(rows):
                print("")

    def _ext4_reserved_and_meta(self, dev_real, size_bytes_raw, fsused_bytes_raw, fsavail_bytes_raw):
        """
        Return (FSRESERVED, FSMETA) strings for an ext4 filesystem.
        FSRESERVED comes from tune2fs reserved block counters.
        FSMETA is metadata overhead estimate (prefers Overhead clusters when available).
        """
        key = (
            str(dev_real or ""),
            str(size_bytes_raw or ""),
            str(fsused_bytes_raw or ""),
            str(fsavail_bytes_raw or ""),
        )
        if key in self._ext4_tune2fs_cache:
            return self._ext4_tune2fs_cache[key]

        def _fmt_int(v):
            if v is None:
                return "-"
            return f"{int(v):,}"

        reserved_disp = "-"
        meta_disp = "-"
        try:
            res = run_command(['tune2fs', '-l', dev_real], sudo=True, capture_output=True, check=False, timeout=4)
            txt = (getattr(res, 'stdout', '') or '') + "\n" + (getattr(res, 'stderr', '') or '')
            if getattr(res, 'returncode', 1) != 0 or not txt.strip():
                self._ext4_tune2fs_cache[key] = (reserved_disp, meta_disp)
                return reserved_disp, meta_disp

            def _grab_int(pattern):
                m = re.search(pattern, txt, re.MULTILINE | re.IGNORECASE)
                if not m:
                    return None
                return _first_int_from_text(m.group(1))

            block_count = _grab_int(r"^\s*Block count:\s*([0-9,]+)\s*$")
            block_size = _grab_int(r"^\s*Block size:\s*([0-9,]+)\s*$")
            cluster_size = _grab_int(r"^\s*Cluster size:\s*([0-9,]+)\s*$")
            reserved_block_count = _grab_int(r"^\s*Reserved block count:\s*([0-9,]+)\s*$")
            reserved_pct = _grab_int(r"^\s*Reserved block percentage:\s*([0-9,]+)")
            overhead_clusters = _grab_int(r"^\s*Overhead clusters:\s*([0-9,]+)\s*$")

            reserved_bytes = None
            if reserved_block_count is not None and block_size is not None:
                reserved_bytes = int(reserved_block_count) * int(block_size)
                reserved_h = self._format_bytes_binary_with_metric(str(reserved_bytes))
                pct_txt = f"{reserved_pct}%" if reserved_pct is not None else "-"
                reserved_disp = f"{reserved_h} (rbc={_fmt_int(reserved_block_count)}, {pct_txt})"

            meta_bytes = None
            # Prefer ext4's explicit overhead clusters if available.
            if overhead_clusters is not None:
                unit = cluster_size if cluster_size is not None else block_size
                if unit is not None:
                    meta_bytes = int(overhead_clusters) * int(unit)
            # Fallback: derive metadata remainder from size-used-avail minus reserve.
            if meta_bytes is None:
                size_b = _first_int_from_text(size_bytes_raw)
                used_b = _first_int_from_text(fsused_bytes_raw)
                avail_b = _first_int_from_text(fsavail_bytes_raw)
                if size_b is not None and used_b is not None and avail_b is not None:
                    rem = int(size_b) - int(used_b) - int(avail_b)
                    if reserved_bytes is not None:
                        rem -= int(reserved_bytes)
                    meta_bytes = max(0, rem)

            if meta_bytes is not None:
                meta_h = self._format_bytes_binary_with_metric(str(meta_bytes))
                meta_disp = f"{meta_h} (bc={_fmt_int(block_count)}, bs={_fmt_int(block_size)})"

        except Exception:
            reserved_disp = "-"
            meta_disp = "-"

        self._ext4_tune2fs_cache[key] = (reserved_disp, meta_disp)
        return reserved_disp, meta_disp

    def _lsblk_list_value(self, row, col):
        """Return display value for list-mode cells."""
        if col == "PERSISTENT PATH":
            return str(row.get("PERSISTENT PATH (IEEE EUI WWID)", "") or "")
        if col == "DEVICE":
            kname = str(row.get("KNAME", "") or "").strip()
            if kname and kname != "-":
                return f"/dev/{kname}"
            dev = str(row.get("DEVICE", "") or "")
            dev = re.sub(r"^[\s│]*[├└]─\s*", "", dev)
            return dev
        if col == "FSUUID":
            u = str(row.get("FSUUID", "") or "").strip()
            if u and u != "-":
                return f"/dev/disk/by-uuid/{u}"
            return u
        if col == "PARTUUID":
            pu = str(row.get("PARTUUID", "") or "").strip()
            if pu and pu != "-":
                return f"/dev/disk/by-partuuid/{pu}"
            return pu
        if col == "FSLABEL":
            lbl = str(row.get("FSLABEL", "") or "").strip()
            if lbl and lbl != "-":
                return f"/dev/disk/by-label/{lbl}"
            return lbl
        if col == "SIZE":
            size = str(row.get("SIZE", "") or "")
            size_gb = self._size_bytes_to_gb_label(row.get("SIZE-BYTES", ""))
            if size and size != "-" and size_gb and size_gb != "-":
                return f"{size} ({size_gb})"
            return size
        if col == "FSUSE%":
            fsuse = str(row.get("FSUSE%", "") or "")
            used = self._bytes_to_gib_tib_with_metric(row.get("FSUSED-BYTES", "")) or self._format_used_as_gib_tib(row.get("FS-USED", ""))
            if fsuse and fsuse != "-" and used and used != "-":
                return f"{fsuse} {used}"
            return fsuse
        return str(row.get(col, "") or "")

    def _bytes_to_gib_tib_with_metric(self, value):
        """Convert a raw byte-count string into 'GiB/TiB (GB/TB)' with 3-decimal binary precision."""
        raw = str(value or "").strip()
        if not raw:
            return "-"
        try:
            n = int(raw, 10)
        except (TypeError, ValueError):
            return "-"
        if n < 0:
            return "-"
        if n >= 1024 ** 4:
            bin_val = n / float(1024 ** 4)
            met_val = n / float(1000 ** 4)
            bin_s = f"{bin_val:.3f} TiB"
            met_unit = "TB"
        else:
            bin_val = n / float(1024 ** 3)
            met_val = n / float(1000 ** 3)
            bin_s = f"{bin_val:.3f} GiB"
            met_unit = "GB"

        if met_val >= 100:
            met_s = f"{met_val:.0f}{met_unit}"
        elif met_val >= 10:
            met_s = f"{met_val:.1f}{met_unit}"
        else:
            met_s = f"{met_val:.2f}{met_unit}"

        return f"{bin_s} ({met_s})"

    def _format_used_as_gib_tib(self, value):
        """
        Convert findmnt 'used' (usually like 822.8G / 1T / bytes) into a binary GiB/TiB string.
        Only outputs GiB or TiB (no MiB) as requested.
        """
        raw = str(value or "").strip()
        if not raw or raw == "-":
            return "-"

        # Bytes (e.g. if findmnt ever returns numeric-only).
        try:
            if raw.isdigit():
                n = int(raw, 10)
            else:
                m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\\s*([KMGTPE])?$', raw, re.IGNORECASE)
                if not m:
                    return "-"
                num = float(m.group(1))
                unit = (m.group(2) or "").upper()
                pow_map = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}
                n = int(num * (1024 ** pow_map.get(unit, 0)))
        except Exception:
            return "-"

        if n < 0:
            return "-"
        if n >= 1024 ** 4:
            return f"{(n / float(1024 ** 4)):.3f} TiB"
        return f"{(n / float(1024 ** 3)):.3f} GiB"

    def _size_bytes_to_gb_label(self, size_bytes):
        """Render raw byte size as decimal GB for list-mode SIZE annotations."""
        try:
            n = int(str(size_bytes or "").strip(), 10)
        except (TypeError, ValueError):
            return "-"
        if n < 0:
            return "-"
        gb = n / float(1000 ** 3)
        if gb >= 100:
            return f"{gb:.0f}GB"
        if gb >= 10:
            return f"{gb:.1f}GB"
        return f"{gb:.2f}GB"

    def resolve_uuid_to_dev(self, uuid):
        '''Resolves a UUID to a short device name like sda1 or nvme0n1p1.'''
        uuid = uuid.strip()
        if not uuid or uuid == "(firmware)":
            return ""
        try:
            # Use check=False to avoid noisy logs if UUID doesn't resolve
            res = run_command(['blkid', '-U', uuid], sudo=True, capture_output=True, check=False)
            path = res.stdout.strip()
            if path:
                return os.path.basename(path)
        except:
            pass
        return ""

    def _render_fstab_file(self, fstab_path, indent="  "):
        """
        Render a single fstab file in a readable table.
        Returns True when a file is parsed/rendered, False otherwise.
        """
        fstab_path = Path(fstab_path)
        if not fstab_path.exists():
            print(f"{indent}{Colors.WARNING}Result: No fstab detected ({fstab_path}).{Colors.ENDC}")
            return False

        try:
            lines = fstab_path.read_text(errors='replace').splitlines()
        except Exception as e:
            print(f"{indent}{Colors.FAIL}Result: Could not read fstab ({fstab_path}): {e}{Colors.ENDC}")
            return False

        cols = ["#", "SOURCE", "TARGET", "FSTYPE", "OPTIONS", "DUMP", "PASS"]
        rows = []
        malformed = []
        ignored_comments = 0
        ignored_blank = 0

        for ln_no, raw in enumerate(lines, start=1):
            line = (raw or "").strip()
            if not line:
                ignored_blank += 1
                continue
            if line.startswith('#'):
                ignored_comments += 1
                continue

            body = raw.split('#', 1)[0].strip()
            if not body:
                ignored_comments += 1
                continue

            parts = body.split()
            if len(parts) < 4:
                malformed.append((ln_no, raw.rstrip()))
                continue

            spec, target, fstype, options = parts[:4]
            dump = parts[4] if len(parts) > 4 else "-"
            passno = parts[5] if len(parts) > 5 else "-"

            uuid_re = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
            def _colorize_uuids(s):
                return uuid_re.sub(lambda m: f"{Colors.OKBLUE}{m.group(0)}{Colors.ENDC}", s or "")

            src_display = _colorize_uuids(spec)
            if spec.startswith("UUID="):
                uuid = spec[len("UUID="):]
                dev = self.resolve_uuid_to_dev(uuid)
                src_display = f"{_colorize_uuids(spec)} {Colors.OKCYAN}[{dev if dev else '-'}]{Colors.ENDC}"

            rows.append({
                "#": str(len(rows) + 1),
                "SOURCE": src_display,
                "TARGET": target,
                "FSTYPE": fstype,
                "OPTIONS": options,
                "DUMP": dump,
                "PASS": passno,
            })

        print(f"{indent}{Colors.OKGREEN}Result: Found fstab at {fstab_path}{Colors.ENDC}")
        if rows:
            widths = self._lsblk_col_widths(rows, cols=cols)

            def _cell(text, width):
                inner = max(width - 2, 0)
                return f" {text:<{inner}} "

            header = "".join([_cell(c, widths[c]) for c in cols]).rstrip()
            print(f"{indent}{Colors.BOLD}{header}{Colors.ENDC}")
            for r in rows:
                line = "".join([_cell(str(r.get(c, '') or ''), widths[c]) for c in cols]).rstrip()
                print(f"{indent}{line}")
        else:
            print(f"{indent}(No active fstab entries found.)")

        meta = f"Entries: {len(rows)}"
        meta += f" | Ignored comments: {ignored_comments}"
        meta += f" | Ignored blank lines: {ignored_blank}"
        print(f"{indent}{meta}")

        if malformed:
            print(f"{indent}{Colors.WARNING}Malformed lines (showing up to 5):{Colors.ENDC}")
            for ln_no, txt in malformed[:5]:
                print(f"{indent}  line {ln_no}: {txt}")
            if len(malformed) > 5:
                print(f"{indent}  ... {len(malformed) - 5} more")

        return True

    def resolve_target(self, target_str, allow_id=True):
        '''Resolves a target string to a physical path.
        Supports Discovery IDs (#1, [#1], and legacy U1/[U1]) or existing mapping names.
        '''
        clean = target_str.strip('[]')

        # 1. Check Discovery ID
        if allow_id:
            if clean.startswith('#') and clean[1:].isdigit():
                wanted = clean[1:]
                # Strict lookup: exact display ID from the last `list` run.
                # Use id_cache so #N always refers to the printed row ID and
                # never falls back to positional cache ordering.
                resolved = (self.id_cache or {}).get(wanted)
                if resolved:
                    return resolved
                # Back-compat for legacy map-only flows where only unmapped cache
                # had IDs populated. Keep exact-ID match only (no positional fallback).
                for entry in (self.unmapped_cache or []):
                    try:
                        if str(entry.get('id', '')).strip() == wanted:
                            return entry.get('pdp')
                    except Exception:
                        continue
                return None
            elif clean.startswith('U') and clean[1:].isdigit():
                # Legacy ID format, now strict/exact against current ID cache.
                wanted = clean[1:]
                resolved = (self.id_cache or {}).get(wanted)
                if resolved:
                    return resolved
                return None

        # 2. Check Mapping Name
        self.mappings = read_luks_map()
        if target_str in self.mappings:
            return self.mappings[target_str]

        return None

    def extensive_confirm(self, target_name, destructive=True):
        print(f"\n{Colors.FAIL}{Colors.BOLD}!!! EXTENSIVE CONFIRMATION REQUIRED !!!{Colors.ENDC}")
        if destructive:
            print(f"You are about to perform a DESTRUCTIVE operation on: {Colors.WARNING}{target_name}{Colors.ENDC}")
        else:
            print(f"You are about to perform a HIGH-IMPACT operation on: {Colors.WARNING}{target_name}{Colors.ENDC}")
        self._show_target_entries_for_confirmation(target_name)
        if destructive:
            dev_real, pdp, pci = self._resolve_confirmation_identity(target_name)
            if not dev_real:
                log("Could not resolve confirmation device. Aborting operation.", 'ERROR')
                return False
            if not pdp:
                log("Could not resolve persistent path for confirmation. Aborting operation.", 'ERROR')
                return False
            if not pci:
                log("Could not resolve PCI path for confirmation. Aborting operation.", 'ERROR')
                return False

            print("To proceed, type ALL THREE values exactly as shown:")
            print(f"  DEVICE: {Colors.OKCYAN}{dev_real}{Colors.ENDC}")
            print(f"  PERSISTENT PATH: {Colors.OKCYAN}{pdp}{Colors.ENDC}")
            print(f"  PCI: {Colors.OKCYAN}{pci}{Colors.ENDC}")

            user_dev = self._input_no_history("Confirm DEVICE: ")
            if (user_dev or "").strip() != dev_real:
                log("Device confirmation mismatch. Aborting operation.", 'ERROR')
                return False

            user_pdp = self._input_no_history("Confirm PERSISTENT PATH: ")
            if (user_pdp or "").strip() != pdp:
                log("Persistent path confirmation mismatch. Aborting operation.", 'ERROR')
                return False

            user_pci = self._input_no_history("Confirm PCI: ")
            if (user_pci or "").strip() != pci:
                log("PCI path confirmation mismatch. Aborting operation.", 'ERROR')
                return False
        else:
            print("To proceed, type YES.")
            user_yes = self._input_no_history("Type YES to continue: ")
            if (user_yes or "").strip() != "YES":
                log("Confirmation failed. Aborting operation.", 'ERROR')
                return False

        print(f"{Colors.OKGREEN}Verification successful. Proceeding...{Colors.ENDC}")
        return True

    def is_root_disk(self, target_path, allow_sibling_partitions=False):
        """Checks if target is the root filesystem, its backing chain, or root disk."""
        try:
            # Root SOURCE can be a partition (/dev/nvme0n1p5) or a dm-crypt device.
            res = run_command(['findmnt', '-nro', 'SOURCE', '/'], capture_output=True)
            root_source = os.path.realpath(res.stdout.strip())

            target_real = os.path.realpath(target_path)

            root_backing = {root_source}
            try:
                res_chain = run_command(
                    ['lsblk', '-s', '-nrpo', 'PATH', root_source],
                    check=False
                )
                for line in (getattr(res_chain, 'stdout', '') or '').splitlines():
                    path = line.strip()
                    if path:
                        root_backing.add(os.path.realpath(path))
            except Exception:
                pass

            def _top_level_disk(dev_path):
                """Return the top-level /dev/<disk> for any block device (partition/dm/crypt), or None."""
                cur = os.path.realpath(dev_path)
                seen = set()
                for _ in range(16):
                    if cur in seen:
                        return None
                    seen.add(cur)

                    if _lsblk_type(cur) == 'disk':
                        return cur

                    res_pk = run_command(['lsblk', '-no', 'PKNAME', cur], check=False)
                    pk = (getattr(res_pk, 'stdout', '') or '').strip()
                    if not pk:
                        return None
                    cur = os.path.realpath(f"/dev/{pk}")
                return None

            root_disk = _top_level_disk(root_source)
            target_disk = _top_level_disk(target_real)

            # Block the root source and any underlying partition/device in its chain.
            if target_real in root_backing:
                return True

            # The whole root disk remains protected. A sibling partition may be
            # opened, but is still protected for destructive operations.
            if root_disk and target_disk and target_disk == root_disk:
                if allow_sibling_partitions and _lsblk_type(target_real) == 'part':
                    return False
                return True

        except:
            pass
        return False

    def _block_if_root_drive(self, target_path, operation, allow_sibling_partitions=False):
        """Return True (and log) if target_path is on the system root drive."""
        try:
            if self.is_root_disk(target_path, allow_sibling_partitions=allow_sibling_partitions):
                log(f"OPERATION BLOCKED: {operation} is not allowed on the system root drive ({target_path}).", 'ERROR')
                return True
        except Exception:
            pass
        return False

    def _soft_erase_target(self, real_target):
        dev_type = _lsblk_type(real_target)

        wipefs_bin = _find_tool_or_common_paths('wipefs', [
            '/usr/sbin/wipefs',
            '/sbin/wipefs',
            '/usr/bin/wipefs',
            '/bin/wipefs',
        ]) or 'wipefs'
        sgdisk_bin = _find_tool_or_common_paths('sgdisk', [
            '/usr/sbin/sgdisk',
            '/sbin/sgdisk',
            '/usr/bin/sgdisk',
            '/bin/sgdisk',
        ])
        sfdisk_bin = _find_tool_or_common_paths('sfdisk', [
            '/usr/sbin/sfdisk',
            '/sbin/sfdisk',
            '/usr/bin/sfdisk',
            '/bin/sfdisk',
        ]) or 'sfdisk'

        log(f"Soft erase: wiping signatures on {real_target} ...")

        # If this is a whole disk, wipe signatures on existing partitions first (best-effort).
        if dev_type == 'disk':
            parts = []
            try:
                res_p = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE', real_target], check=False)
                for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                    cols = line.strip().split()
                    if len(cols) >= 2 and cols[1] == 'part':
                        parts.append(os.path.realpath(f"/dev/{cols[0]}"))
            except Exception:
                parts = []

            for p in parts:
                run_command([wipefs_bin, '-a', p], sudo=True, check=False)

            # For whole disks, --force is required to erase partition-table signatures.
            run_command([wipefs_bin, '-a', '--force', real_target], sudo=True, check=False)

            # Zap GPT/MBR metadata to remove "ghost" headers (e.g., GPT backup at end-of-disk).
            try:
                res_pt = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
                pttype = (getattr(res_pt, 'stdout', '') or '').strip().lower()
            except Exception:
                pttype = ""

            if pttype == 'gpt' or not pttype:
                if sgdisk_bin:
                    run_command([sgdisk_bin, '--zap-all', real_target], sudo=True, check=False)
            if pttype in ('dos', 'msdos'):
                # Write an empty DOS partition table (no partitions).
                script = "label: dos\n"
                run_command([sfdisk_bin, '--wipe', 'always', '--wipe-partitions', 'always', real_target],
                            sudo=True, input_str=script, check=False)

            self._refresh_kernel_partition_state(real_target, drop_partitions=True)
        else:
            run_command([wipefs_bin, '-a', real_target], sudo=True, check=False)
            run_command(['udevadm', 'settle'], sudo=True, check=False)

        log("Soft erase completed.")

    def _refresh_kernel_partition_state(self, disk_dev, drop_partitions=False):
        """
        Best-effort refresh of kernel/udev partition state for a whole disk.
        Helps clear stale child partition nodes after destructive whole-disk ops
        (erase, superfloppy/luks-on-disk format), especially behind some USB bridges.
        """
        real_disk = os.path.realpath(str(disk_dev or ""))
        if not real_disk or not os.path.exists(real_disk):
            return

        disk_type = (_lsblk_type(real_disk) or '').strip().lower()
        if disk_type != 'disk' and not _sysfs_is_whole_disk(real_disk):
            return

        # Drain prior uevents first.
        run_command(['udevadm', 'settle'], sudo=True, check=False)

        # For whole-disk superfloppy style layouts, remove stale partition nodes
        # that may still be cached in kernel state.
        if drop_partitions:
            run_command(['partx', '-d', real_disk], sudo=True, check=False)

        # Ask kernel/userspace to re-read and re-emit current topology.
        run_command(['blockdev', '--rereadpt', real_disk], sudo=True, check=False)
        run_command(['partprobe', real_disk], sudo=True, check=False)
        run_command(['udevadm', 'trigger', '--name-match=' + os.path.basename(real_disk)], sudo=True, check=False)
        run_command(['udevadm', 'settle'], sudo=True, check=False)

    def _disk_looks_erased_for_create(self, real_target):
        """Best-effort precheck used by create: disk must look erased."""
        if _lsblk_type(real_target) != 'disk':
            return (False, "target is not a whole disk")

        # Must have no child partitions.
        try:
            res_p = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE', real_target], check=False)
            parts = []
            for line in (getattr(res_p, 'stdout', '') or '').splitlines():
                cols = line.strip().split()
                if len(cols) >= 2 and cols[1] == 'part':
                    parts.append(cols[0])
            if parts:
                return (False, f"disk still has partition(s): {', '.join(parts)}")
        except Exception:
            pass

        # Must not currently report a partition table type.
        try:
            res_pt = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
            pttype = (getattr(res_pt, 'stdout', '') or '').strip().lower()
            if pttype:
                return (False, f"disk still has partition-table metadata ({pttype})")
        except Exception:
            pass

        # Must not have a probe-detectable signature on the whole-disk node.
        try:
            res_b = run_command(['blkid', '-p', real_target], check=False)
            if getattr(res_b, 'returncode', 1) == 0:
                sig = (getattr(res_b, 'stdout', '') or '').strip() or (getattr(res_b, 'stderr', '') or '').strip()
                if sig:
                    return (False, f"disk still has metadata/signature: {sig}")
                return (False, "disk still has metadata/signature")
        except Exception:
            pass

        return (True, "")

    def _largest_free_extent_sectors(self, disk_dev):
        """
        Return (start_sector, end_sector, size_sector) for the largest free extent on a disk,
        using `parted -m unit s print free`. Returns None if no free extent is found.
        """
        try:
            res = run_command(['parted', '-m', '-s', disk_dev, 'unit', 's', 'print', 'free'],
                              sudo=True, check=False)
            if getattr(res, 'returncode', 1) != 0:
                return None

            best = None
            for raw in (getattr(res, 'stdout', '') or '').splitlines():
                line = raw.strip()
                if not line or line.startswith('BYT') or line.startswith('/'):
                    continue
                parts = line.strip(';').split(':')
                if len(parts) < 5:
                    continue

                fs_or_type = parts[4].strip()
                if fs_or_type != 'free':
                    continue

                try:
                    start_s = int(parts[1].rstrip('s'))
                    end_s = int(parts[2].rstrip('s'))
                    size_s = int(parts[3].rstrip('s'))
                except Exception:
                    continue

                if size_s <= 0:
                    continue

                if (best is None) or (size_s > best[2]):
                    best = (start_s, end_s, size_s)

            return best
        except Exception:
            return None

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
