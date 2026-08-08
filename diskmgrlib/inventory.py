"""Device inventory and presentation helpers."""

from pathlib import Path
import grp
import json
import os
import pwd
import re
from .runtime import Colors, _first_int_from_text, run_command, run_command_hard_timeout
from .mappings import read_luks_map


class InventoryMixin:

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
