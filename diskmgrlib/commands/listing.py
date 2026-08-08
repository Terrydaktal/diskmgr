"""ListingCommands command implementations."""

import json
import os
import shlex
from ..runtime import Colors, log, run_command_hard_timeout
from ..devices import _lsblk_fstype, _lsblk_partitions
from ..mappings import get_map_file_path, read_luks_map
from ..shell_core import CmdArgumentParser


class ListingCommands:

    def do_list(self, arg):
        '''Display the physical partition layout and free space for all plugged-in disks.
        Usage:
          list            -> standard table (default)
          list concise    -> concise table
          list verbose    -> verbose key/value entries (alias: list list)

        UNDER THE HOOD:
        1.  Hardware Scan: Identifies all physical 'disk' devices (excluding partitions).
        2.  Geometry Query: Runs 'sudo parted -m <dev> unit s print free' and 'blockdev --getsz'.
        3.  Parsing:
            - Extracts Partition Table type (gpt/mbr) and sector sizes.
            - Calculates total logical sectors from blockdev output.
        4.  Formatting:
            - Adds GPT metadata blocks (Primary/Backup) if applicable.
            - Identifies 'free' space segments.
            - Calculates MiB and GiB values from sector counts.
        '''
        argv = shlex.split(arg) if arg else []
        render_mode = 'table'
        for token in argv:
            t = token.strip().lower()
            if t in ('verbose', '--verbose', '-v'):
                # verbose now means list-style key/value entries
                render_mode = 'list'
            elif t in ('list', '--list', '-l'):
                # Back-compat: list list
                render_mode = 'list'
            elif t in ('concise', '--concise', '-c'):
                render_mode = 'concise'
            elif t in ('table', '--table', '-t'):
                render_mode = 'table'
            else:
                log(f"Unknown list option: {token}. Use: list [concise|verbose]", 'ERROR')
                return

        if render_mode == 'list':
            selected_cols = self._lsblk_verbose_cols()
        elif render_mode == 'concise':
            selected_cols = self._lsblk_concise_cols()
        else:
            selected_cols = self._lsblk_standard_cols()

        all_devs = self.get_disk_info()
        disks = [d for d in all_devs if d.get('type') == 'disk']

        if not disks:
            log("No physical disks found.", 'WARN')
            return

        # Precompute lsblk rows for all disks first so we can:
        # - keep the output stable across disks
        # - compute column widths across ALL disks (not per-disk)
        # - include the diskmgr mapping name (from diskmap.tsv) per real disk/partition node
        friendly_map = {}
        missing_mappings = []
        try:
            # Refresh mappings in case they changed on disk.
            self.mappings = read_luks_map()
            for friendly, target in (self.mappings or {}).items():
                try:
                    if not target:
                        continue
                    # Only map targets that currently exist, otherwise realpath() can be misleading.
                    if os.path.exists(target):
                        friendly_map[os.path.realpath(target)] = friendly
                    else:
                        missing_mappings.append((friendly, target))
                except Exception:
                    continue
        except Exception:
            friendly_map = {}
            missing_mappings = []
        self.missing_map_id_cache = {}

        fstab_real_set, fstab_uuid_set, fstab_detail_real, fstab_detail_uuid = self._fstab_lookup()
        findmnt_lookup = self._findmnt_lookup()

        disk_rows = {}
        for d in disks:
            d_name = d.get('name', '')
            if not d_name:
                continue
            dev_path = f"/dev/{d_name}"
            try:
                ls_res = run_command_hard_timeout(
                    ['lsblk', '-J', '-b', '-f', '-o',
                     'NAME,KNAME,TYPE,SIZE,FSTYPE,FSVER,LABEL,UUID,PARTUUID,FSAVAIL,FSUSE%,FSUSED,MOUNTPOINTS,WWN',
                     dev_path],
                    6,
                    sudo=True,
                    check=False
                )
                rows = []
                if (getattr(ls_res, 'stdout', '') or '').strip():
                    data = json.loads(ls_res.stdout)
                    if 'blockdevices' in data:
                        rows = self._collect_lsblk_rows(
                            data['blockdevices'],
                            friendly_map=friendly_map,
                            fstab_real_set=fstab_real_set,
                            fstab_uuid_set=fstab_uuid_set,
                            fstab_detail_real=fstab_detail_real,
                            fstab_detail_uuid=fstab_detail_uuid,
                            findmnt_lookup=findmnt_lookup
                        )
                disk_rows[d_name] = rows
            except Exception:
                disk_rows[d_name] = []

        # Assign a single global index in print order.
        # Number disk/part and open dm-crypt rows (TYPE=crypt). Keep all other virtual rows as "-".
        idx = 1
        for d in disks:
            d_name = d.get('name', '')
            for r in disk_rows.get(d_name, []):
                if (r.get('TYPE') or '') in ('disk', 'part', 'crypt'):
                    r['#'] = str(idx)
                    idx += 1
                else:
                    r['#'] = '-'

        all_rows = []
        for d in disks:
            d_name = d.get('name', '')
            all_rows.extend(disk_rows.get(d_name, []))

        missing_rows = []
        missing_idx = idx
        for friendly, target in missing_mappings:
            rid = str(missing_idx)
            missing_idx += 1
            friendly_name = str(friendly or "")
            target_path = str(target or "")
            self.missing_map_id_cache[rid] = friendly_name
            missing_rows.append({
                "#": rid,
                "KNAME": "",
                "PKNAME": "",
                "NAME": friendly_name,
                "DEVICE": "-",
                "PERSISTENT PATH (IEEE EUI WWID)": target_path,
                "Model_Serial WWID": "-",
                "PCI": "-",
                "TYPE": "-",
                "STATE": "MISSING",
                "OWNER": "-",
                "FSTAB": "n",
                "SIZE": "-",
                "SIZE-BYTES": "",
                "FSTYPE": "-",
                "FSVER": "-",
                "FSLABEL": "-",
                "FSUUID": "-",
                "PARTUUID": "-",
                "FSAVAIL": "-",
                "FSAVAIL-BYTES": "",
                "FSUSE%": "-",
                "FSRESERVED": "-",
                "FSMETA": "-",
                "FSUSED-BYTES": "",
                "FSMOUNTPOINTS": "-",
                "MOUNTSOURCE": "-",
                "FS-OPTIONS": "-",
                "VFS-OPTIONS": "-",
                "FS-USED": "-",
                "MOUNTPOINT": "-",
                "FSTAB ENTRY": "-",
            })

        # Display rows: standard table needs different numeric formatting.
        display_disk_rows = disk_rows
        display_all_rows = all_rows
        display_missing_rows = [dict(r) for r in missing_rows]
        if render_mode in ('table', 'concise'):
            display_disk_rows = {}
            display_all_rows = []
            for d in disks:
                d_name = d.get('name', '')
                out = []
                for r in disk_rows.get(d_name, []):
                    rr = dict(r)
                    # list (table): show SIZE with 2dp and no metric bracket.
                    try:
                        rr["SIZE"] = self._format_bytes_binary(rr.get("SIZE-BYTES", ""), decimals=2) or rr.get("SIZE", "")
                    except Exception:
                        pass
                    # list (table): show FSAVAIL with 2dp and NO metric bracket.
                    try:
                        rr["FSAVAIL"] = self._format_bytes_binary(rr.get("FSAVAIL-BYTES", ""), decimals=2) or rr.get("FSAVAIL", "")
                    except Exception:
                        pass
                    out.append(rr)
                display_disk_rows[d_name] = out
                display_all_rows.extend(out)

        # Discovery cache for map #N:
        # keep only unmapped disk/partition entries and bind to displayed "#" IDs.
        self.unmapped_cache = []
        # Full ID cache for commands that support #N on displayed rows (disk/part/crypt).
        self.id_cache = {}
        for r in all_rows:
            try:
                rid = str(r.get('#') or '').strip()
                kname = str(r.get('KNAME') or '').strip()
                rtype = str(r.get('TYPE') or '').strip()
                if rid and rid != '-' and kname and rtype in ('disk', 'part', 'crypt'):
                    self.id_cache[rid] = os.path.realpath(f"/dev/{kname}")

                if (r.get('TYPE') or '') not in ('disk', 'part'):
                    continue
                if (r.get('NAME') or '') != '-':
                    continue
                pdp = str(r.get('PERSISTENT PATH (IEEE EUI WWID)') or '').strip()
                if not rid or rid == '-' or not pdp or pdp == '-':
                    continue
                self.unmapped_cache.append({'id': rid, 'pdp': pdp})
            except Exception:
                continue

        widths_global = None
        if render_mode in ('table', 'concise'):
            width_rows = display_all_rows + display_missing_rows
            widths_global = self._lsblk_col_widths(width_rows, cols=selected_cols)
            separator_width = max(1, self._lsblk_rendered_width(width_rows, widths=widths_global, cols=selected_cols))
        else:
            width_rows = all_rows + missing_rows
            separator_width = max(1, self._lsblk_rendered_list_width(width_rows, cols=selected_cols))
        separator_line = "-" * separator_width

        # concise mode: one flat table, no per-disk geometry/banner blocks
        if render_mode == 'concise':
            if display_all_rows:
                self._print_lsblk_rows(display_all_rows, widths=widths_global, cols=selected_cols)
            if display_missing_rows:
                print(f"{Colors.HEADER}Non-present mappings ({get_map_file_path()}){Colors.ENDC}")
                self._print_lsblk_rows(display_missing_rows, widths=widths_global, cols=selected_cols)
            print("")
            return

        list_entry_cursor = 1
        for disk in disks:
            d_name = disk['name']
            dev_path = f"/dev/{d_name}"
            model = disk.get('model', 'Unknown')

            try:
                # 1. Get Geometry from parted
                res = run_command_hard_timeout(
                    ['parted', '-m', '-s', dev_path, 'unit', 's', 'print', 'free'],
                    6,
                    sudo=True,
                    check=False
                )
                if getattr(res, 'returncode', 1) != 0:
                    # Common case: blank disk or missing/corrupt partition table.
                    # Avoid noisy "Command failed" logs and present a human-readable reason.
                    stderr = (getattr(res, 'stderr', '') or '').strip()
                    disk_fstype = (_lsblk_fstype(dev_path) or "").strip()
                    has_whole_disk_fs = bool(disk_fstype)

                    # Fall back to blockdev sector sizes for a useful header.
                    ls = run_command_hard_timeout(['blockdev', '--getss', dev_path], 3, sudo=True, check=False).stdout.strip()
                    ps = run_command_hard_timeout(['blockdev', '--getpbsz', dev_path], 3, sudo=True, check=False).stdout.strip()
                    logical_sector = int(ls) if ls.isdigit() else 512
                    physical_sector = int(ps) if ps.isdigit() else logical_sector

                    res_sz = run_command_hard_timeout(['blockdev', '--getsz', dev_path], 3, sudo=True, check=False)
                    total_512_sectors = int(res_sz.stdout.strip()) if (res_sz.stdout or "").strip().isdigit() else 0
                    total_logical_sectors = (total_512_sectors * 512) // logical_sector if logical_sector else 0

                    print(f"\n{Colors.BOLD}Disk: {dev_path} ({model}) [none] [Sector: L{logical_sector}/P{physical_sector}] [Total Sectors: {total_logical_sectors}]{Colors.ENDC}")
                    if stderr:
                        stderr_lower = stderr.lower()
                        if has_whole_disk_fs and "unrecognised disk label" in stderr_lower:
                            # Superfloppy / whole-disk filesystem (including crypto_LUKS) is expected
                            # to have no partition table; don't show this as a warning.
                            pass
                        elif "no medium found" in stderr_lower:
                            log(f"{dev_path}: HARDWARE FAILURE - Device exists but has no storage medium (No medium found). Possible causes: Dead flash memory, broken controller, or loose internal connection.", 'ERROR')
                        elif "unrecognised disk label" in stderr_lower:
                            log(f"{dev_path}: BLANK DISK - No partition table found (unrecognised disk label). Use 'create' to initialize a new partition table.", 'WARN')
                        else:
                            log(f"{dev_path}: could not read partition layout: {stderr}", 'WARN')

                    if has_whole_disk_fs:
                        size_bytes = total_logical_sectors * logical_sector
                        if size_bytes < 1024:
                            size_info = f"{size_bytes:.2f}B"
                        elif size_bytes < 1024**2:
                            size_info = f"{size_bytes/1024:.2f}KiB"
                        elif size_bytes < 1024**3:
                            size_info = f"{size_bytes/(1024**2):.2f}MiB"
                        else:
                            size_info = f"{size_bytes/(1024**2):.2f}MiB ≈ {size_bytes/(1024**3):.1f}GiB"
                            if size_bytes >= 1024**4:
                                size_info += f" ({size_bytes/(1024**4):.3f} TiB)"
                        print(f"{Colors.OKGREEN}[ {d_name} {disk_fstype} {total_logical_sectors}s ({size_info}) ]{Colors.ENDC}")

                    # Still show lsblk hierarchy so the user can see what's on the disk.
                    rows = display_disk_rows.get(d_name, [])
                    if rows:
                        print("")
                        if render_mode == 'list':
                            self._print_lsblk_rows_list(rows, cols=selected_cols, start_index=list_entry_cursor)
                            list_entry_cursor += len(rows)
                        else:
                            self._print_lsblk_rows(rows, widths=widths_global, cols=selected_cols)
                    print(separator_line)
                    continue

                lines = res.stdout.strip().splitlines()

                header_parts = lines[1].strip(';').split(':')
                logical_sector = int(header_parts[3])
                physical_sector = int(header_parts[4])
                ptable = header_parts[5]

                # 2. Get Total Size from blockdev (always in 512b units)
                res_sz = run_command_hard_timeout(['blockdev', '--getsz', dev_path], 3, sudo=True)
                total_512_sectors = int(res_sz.stdout.strip())
                total_logical_sectors = (total_512_sectors * 512) // logical_sector

                print(f"\n{Colors.BOLD}Disk: {dev_path} ({model}) [{ptable}] [Sector: L{logical_sector}/P{physical_sector}] [Total Sectors: {total_logical_sectors}]{Colors.ENDC}")

                # 4. Parse Data Lines from parted for visual blocks
                data_lines = [l for l in lines if l and not l.startswith('BYT') and not l.startswith('/')]
                part_fstype_by_name = {}
                try:
                    for p in _lsblk_partitions(dev_path):
                        n = (p.get('name') or '').strip()
                        if n:
                            part_fstype_by_name[n] = (p.get('fstype') or '').strip()
                except Exception:
                    part_fstype_by_name = {}

                segments = []

                # Dynamic Initial Overhead Detection
                if data_lines:
                    first_line_parts = data_lines[0].strip(';').split(':')
                    first_start = int(first_line_parts[1].strip('s'))
                    if first_start > 0:
                        overhead_size = first_start
                        overhead_bytes = overhead_size * logical_sector

                        label = "Overhead"
                        if ptable == 'gpt': label = "GPT Primary"
                        elif ptable in ['msdos', 'mbr']: label = "MBR"

                        segments.append(f"{Colors.FAIL}[ {label} {overhead_size}s ({overhead_bytes:.2f}B) ]{Colors.ENDC}")

                for line in data_lines:
                    parts = line.strip(';').split(':')
                    if len(parts) < 4: continue

                    num = parts[0]
                    size_sectors = int(parts[3].strip('s'))
                    fs_or_type = parts[4] if len(parts) > 4 else ""

                    size_bytes = size_sectors * logical_sector
                    # Use B, KiB, MiB, GiB based on size
                    if size_bytes < 1024:
                        size_info = f"{size_bytes:.2f}B"
                    elif size_bytes < 1024**2:
                        size_info = f"{size_bytes/1024:.2f}KiB"
                    elif size_bytes < 1024**3:
                        size_info = f"{size_bytes/(1024**2):.2f}MiB"
                    else:
                        size_info = f"{size_bytes/(1024**2):.2f}MiB ≈ {size_bytes/(1024**3):.1f}GiB"
                        if size_bytes >= 1024**4:
                            size_info += f" ({size_bytes/(1024**4):.3f} TiB)"

                    if fs_or_type == 'free' or (not fs_or_type and len(parts) == 5):
                        segments.append(f"{Colors.OKCYAN}[ free {size_sectors}s ({size_info}) ]{Colors.ENDC}")
                    else:
                        flags = parts[6] if len(parts) > 6 else ""
                        kname = f"{d_name}{num}"
                        if 'nvme' in d_name and not kname.startswith(f"{d_name}p"):
                            kname = f"{d_name}p{num}"

                        dtype = (fs_or_type or '').strip()
                        lsblk_fstype = (part_fstype_by_name.get(kname) or '').strip()
                        if (not dtype or dtype in ('-', 'unknown')) and lsblk_fstype:
                            dtype = lsblk_fstype
                        if not dtype:
                            dtype = "-"
                        flag_info = f" ({flags})" if flags else ""
                        segments.append(f"{Colors.OKGREEN}[ {kname} {dtype} {size_sectors}s ({size_info}){flag_info} ]{Colors.ENDC}")

                # GPT Backup Overhead
                if data_lines:
                    last_line_parts = data_lines[-1].strip(';').split(':')
                    last_end = int(last_line_parts[2].strip('s'))
                    if last_end < total_logical_sectors - 1:
                        overhead_size = total_logical_sectors - 1 - last_end
                        overhead_bytes = overhead_size * logical_sector
                        l_label = "Overhead"
                        if ptable == 'gpt': l_label = "GPT Backup"
                        segments.append(f"{Colors.FAIL}[ {l_label} {overhead_size}s ({overhead_bytes:.2f}B) ]{Colors.ENDC}")

                print(" ".join(segments))

                # 4. Print lsblk hierarchy at the bottom
                print("")
                try:
                    rows = display_disk_rows.get(d_name, [])
                    if rows:
                        if render_mode == 'list':
                            self._print_lsblk_rows_list(rows, cols=selected_cols, start_index=list_entry_cursor)
                            list_entry_cursor += len(rows)
                        else:
                            self._print_lsblk_rows(rows, widths=widths_global, cols=selected_cols)
                except Exception as e:
                    log(f"Could not render lsblk tree: {e}", 'DEBUG')
                print(separator_line)
            except Exception as e:
                print(f"\n{Colors.BOLD}Disk: {dev_path} ({model}){Colors.ENDC}")
                log(f"Could not read layout for {dev_path}: {e}", 'WARN')
        if display_missing_rows:
            print(f"{Colors.HEADER}Non-present mappings ({get_map_file_path()}){Colors.ENDC}")
            try:
                if render_mode == 'list':
                    self._print_lsblk_rows_list(display_missing_rows, cols=selected_cols, start_index=list_entry_cursor)
                else:
                    self._print_lsblk_rows(display_missing_rows, widths=widths_global, cols=selected_cols)
            except Exception as e:
                log(f"Could not render missing mappings: {e}", 'DEBUG')
            print(separator_line)
        print("")
