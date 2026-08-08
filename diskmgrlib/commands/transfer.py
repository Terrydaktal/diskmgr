"""TransferCommands command implementations."""

import argparse
import cmd
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from ..runtime import Colors, _fmt_hms, log, popen_command, run_command
from ..shell_core import CmdArgumentParser


class TransferCommands:

    def do_sync(self, arg):
        '''Synchronize two filesystems: sync <primary> <secondary>

        UNDER THE HOOD:
        1.  Validation: Verifies both endpoints resolve to directories.
            - Mapped names must already be mounted.
            - Absolute paths must exist and be directories.
        2.  Pre-scan: Runs rsync dry-run stats to compute planned transfer bytes.
        3.  Confirmation: Requires typing resolved device and persistent path for the destructive secondary target.
        4.  Execution: Runs rsync and reports real progress as bytes_done / planned_bytes.

        Note: The SECONDARY disk will be modified to match the PRIMARY disk.
        All files on the secondary that do not exist on the primary will be DELETED.
        '''
        parser = CmdArgumentParser(prog='sync', add_help=False)
        parser.add_argument('primary')
        parser.add_argument('secondary')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        primary = args.primary
        secondary = args.secondary

        def _resolve_sync_endpoint(value, role_label):
            if os.path.isabs(value):
                p = os.path.realpath(value)
                if not os.path.exists(p):
                    log(f"{role_label} path does not exist: {value}", 'ERROR')
                    return None, None
                if not os.path.isdir(p):
                    log(f"{role_label} path must be a directory: {value}", 'ERROR')
                    return None, None
                return p, 'path'

            mp = self.get_mountpoint(value)
            if not mp:
                log(f"{role_label} disk '{value}' is not mounted.", 'ERROR')
                return None, None
            return mp, 'mapping'

        pri_mnt, pri_kind = _resolve_sync_endpoint(primary, "Primary")
        sec_mnt, sec_kind = _resolve_sync_endpoint(secondary, "Secondary")
        if not pri_mnt or not sec_mnt:
            return

        # Block syncing TO the system root drive (destination only).
        # Source on root is allowed for backup use-cases like /home -> external disk.
        try:
            if sec_kind == 'mapping':
                res_s = run_command(['findmnt', '-rn', '-M', sec_mnt, '-o', 'SOURCE'], check=False)
            else:
                res_s = run_command(['findmnt', '-rn', '-T', sec_mnt, '-o', 'SOURCE'], check=False)
            sec_src = (getattr(res_s, 'stdout', '') or '').strip()
            if sec_src and self._block_if_root_drive(os.path.realpath(sec_src), f"sync {primary} {secondary}"):
                return
        except Exception:
            pass

        # Ensure trailing slashes so rsync copies directory CONTENTS.
        src_path = pri_mnt.rstrip('/') + '/'
        dst_path = sec_mnt.rstrip('/') + '/'
        src_real = os.path.realpath(pri_mnt)
        dst_real = os.path.realpath(sec_mnt)
        if src_real == dst_real or dst_real.startswith(src_real.rstrip('/') + os.sep) or src_real.startswith(dst_real.rstrip('/') + os.sep):
            log("OPERATION BLOCKED: sync source and destination overlap. Choose two separate directory trees.", 'ERROR')
            return
        root_source_excludes = []
        root_source_flags = []
        if os.path.realpath(pri_mnt) == '/':
            # Root-sync safety defaults: skip pseudo-filesystems and mount roots.
            # Also stay on the root filesystem only to avoid crossing into mounted
            # filesystems (snap/overlay/fuse/etc) which can massively skew counts.
            root_source_flags = ['--one-file-system']
            root_source_excludes = [
                '--exclude=/proc/***',
                '--exclude=/sys/***',
                '--exclude=/run/***',
                '--exclude=/dev/***',
                '--exclude=/tmp/***',
                '--exclude=/mnt/***',
                '--exclude=/media/***',
            ]

        print(f"Syncing: {Colors.BOLD}{src_path}{Colors.ENDC} -> {Colors.WARNING}{dst_path}{Colors.ENDC}")
        if root_source_excludes:
            print("Root source detected: auto-excluding /proc, /sys, /run, /dev, /tmp, /mnt, /media")
            print("Root source detected: using --one-file-system to avoid crossing mountpoints")
        # Pre-scan before confirmation to compute planned bytes for stable progress.
        log("Running pre-scan (dry-run) to compute planned transfer...")
        pre_cmd = ['rsync', '-anHS', '--protect-args', '--sparse'] + root_source_flags + ['--delete', '--stats'] + root_source_excludes + [src_path, dst_path]
        pre_res = run_command(pre_cmd, sudo=True, check=False)
        pre_rc = getattr(pre_res, 'returncode', 0)
        pre_out = (getattr(pre_res, 'stdout', '') or '') + "\n" + (getattr(pre_res, 'stderr', '') or '')
        if pre_rc not in (0, 24):
            log(f"Pre-scan failed: rsync exited with status {pre_rc}.", 'ERROR')
            if pre_out.strip():
                print(pre_out.rstrip())
            return

        def _parse_stat_int(label):
            m = re.search(rf"^\s*{re.escape(label)}:\s*([0-9,]+)\b", pre_out, re.MULTILINE)
            if not m:
                return 0
            try:
                return int(m.group(1).replace(',', ''))
            except Exception:
                return 0

        planned_bytes = _parse_stat_int("Total transferred file size")
        planned_files = _parse_stat_int("Number of regular files transferred")
        if planned_files <= 0:
            planned_files = _parse_stat_int("Number of files transferred")
        planned_deletes = _parse_stat_int("Number of deleted files")

        print("Pre-scan summary (dry-run):")
        print(f"  Planned file transfers: {planned_files:,}")
        print(f"  Planned deletions:      {planned_deletes:,}")
        print(f"  Planned transfer bytes: {planned_bytes:,} ({self._format_bytes_binary(str(planned_bytes), decimals=2)})")

        if not self.extensive_confirm(secondary):
            return

        log(f"Starting rsync: {primary} -> {secondary}...")
        start_ts = time.time()

        proc = None
        progress_stop = threading.Event()
        try:
            cmd = [
                'rsync', '-aH', '--protect-args',
                '--info=progress2,stats2,name0',
            ] + root_source_flags + ['--sparse', '--delete'] + root_source_excludes + [src_path, dst_path]
            proc = popen_command(
                cmd,
                sudo=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            done_bytes = 0
            progress_lock = threading.Lock()

            def _fmt_speed_bps(bps):
                try:
                    b = max(0, int(float(bps)))
                except Exception:
                    b = 0
                return self._format_bytes_binary(str(b), decimals=2)

            def _parse_progress2_bytes(line):
                """
                Parse rsync progress2 lines.
                Supports both raw-byte and human-unit first field, e.g.:
                  "12,345,678  23% ..."
                  "12.33G      30% ..."
                Returns cumulative bytes as int, or None if not a progress2 line.
                """
                m = re.match(r'^\s*([0-9][0-9,]*(?:\.[0-9]+)?)([kKmMgGtTpPeE]?)\s+[0-9]{1,3}%', line or '')
                if not m:
                    return None
                num_txt = (m.group(1) or '').replace(',', '')
                unit = (m.group(2) or '').upper()
                try:
                    val = float(num_txt)
                except Exception:
                    return None
                mult = 1
                if unit == 'K':
                    mult = 1024
                elif unit == 'M':
                    mult = 1024 ** 2
                elif unit == 'G':
                    mult = 1024 ** 3
                elif unit == 'T':
                    mult = 1024 ** 4
                elif unit == 'P':
                    mult = 1024 ** 5
                elif unit == 'E':
                    mult = 1024 ** 6
                try:
                    return int(val * mult)
                except Exception:
                    return None

            def _progress_reporter():
                # Event-driven reporting:
                # - wake every 0.5s
                # - print only when cumulative bytes increased
                # - compute speed across total elapsed time since last byte increase
                last_report_bytes = 0
                last_report_ts = time.time()
                while not progress_stop.wait(0.5):
                    with progress_lock:
                        cur_bytes = int(done_bytes)
                    if cur_bytes <= last_report_bytes:
                        continue
                    now = time.time()
                    dt = max(now - last_report_ts, 1e-6)
                    speed = (cur_bytes - last_report_bytes) / dt
                    if planned_bytes > 0:
                        pct = min(100.0, (cur_bytes * 100.0) / planned_bytes)
                        cur_disp = self._format_bytes_binary(str(cur_bytes), decimals=2)
                        total_disp = self._format_bytes_binary(str(planned_bytes), decimals=2)
                        print(f"Progress: {pct:6.2f}% ({cur_disp} / {total_disp}) | Speed: {_fmt_speed_bps(speed)}/s", flush=True)
                    else:
                        cur_disp = self._format_bytes_binary(str(cur_bytes), decimals=2)
                        print(f"Progress: ---% ({cur_disp}) | Speed: {_fmt_speed_bps(speed)}/s", flush=True)
                    last_report_bytes = cur_bytes
                    last_report_ts = now

            progress_thread = threading.Thread(target=_progress_reporter, daemon=True)
            progress_thread.start()

            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.rstrip('\n')
                    p2_bytes = _parse_progress2_bytes(line)
                    if p2_bytes is not None:
                        try:
                            with progress_lock:
                                # progress2 is cumulative bytes transferred so far.
                                if p2_bytes > done_bytes:
                                    done_bytes = p2_bytes
                        except Exception:
                            pass
                    elif line.strip():
                        print(line)

            rc_sync = proc.wait()
            progress_stop.set()
            progress_thread.join(timeout=1.0)

            with progress_lock:
                final_done = int(done_bytes)
            if planned_bytes > 0 and rc_sync in (0, 24):
                final_done = max(final_done, planned_bytes)
            if planned_bytes > 0:
                pct = min(100.0, (final_done * 100.0) / planned_bytes)
                cur_disp = self._format_bytes_binary(str(final_done), decimals=2)
                total_disp = self._format_bytes_binary(str(planned_bytes), decimals=2)
                print(f"Progress: {pct:6.2f}% ({cur_disp} / {total_disp}) | Speed: 0 B/s", flush=True)
            else:
                cur_disp = self._format_bytes_binary(str(final_done), decimals=2)
                print(f"Progress: ---% ({cur_disp}) | Speed: 0 B/s", flush=True)

            if rc_sync == 0:
                log("Sync complete.")
            elif rc_sync == 24:
                log("Sync completed with warnings: some source files vanished during transfer (rsync exit 24).", 'WARN')
                log("This is expected on active trees (e.g., browser cache, temp files). Re-run sync to converge.", 'WARN')
            else:
                log(f"Sync failed: rsync exited with status {rc_sync}.", 'ERROR')
        except Exception as e:
            log(f"Sync failed: {e}", 'ERROR')
        finally:
            progress_stop.set()
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")

    def do_diff(self, arg):
        '''Preview differences between two mounted filesystems: diff <primary_name> <secondary_name> [--depth N] [-d] [--fast] [--checksum]

        Endpoints may be mapping names/IDs (must be mounted) or absolute directory paths.
        Uses rsync dry-run itemized output (primary -> secondary) and prints:
        1) Change counts and byte estimates (created/modified/deleted, net change).
        2) Hierarchy summary by subtree up to --depth levels.
        '''
        parser = CmdArgumentParser(prog='diff', add_help=False)
        parser.add_argument('primary_name')
        parser.add_argument('secondary_name')
        parser.add_argument('--depth', type=int, default=2, help='Hierarchy depth for subtree summary (default: 2)')
        parser.add_argument('-d', '--dirs-only', action='store_true', help='Show directories only in hierarchy output')
        parser.add_argument('--fast', action='store_true', help='Show raw rsync -anH --delete --stats output only')
        parser.add_argument('--checksum', action='store_true', help='Compare file content using checksums (slower)')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        if args.depth < 1:
            log("--depth must be >= 1.", 'ERROR')
            return

        primary = args.primary_name
        secondary = args.secondary_name
        depth = args.depth
        dirs_only = args.dirs_only
        fast_mode = args.fast
        checksum_mode = args.checksum

        def _resolve_diff_endpoint(value, role_label):
            expanded = os.path.expanduser(value)
            if os.path.isabs(expanded) or value.startswith('~'):
                p = os.path.realpath(expanded)
                if not os.path.exists(p):
                    log(f"{role_label} path does not exist: {value}", 'ERROR')
                    return None
                if not os.path.isdir(p):
                    log(f"{role_label} path must be a directory: {value}", 'ERROR')
                    return None
                return p

            mp = self.get_mountpoint(value)
            if not mp:
                log(f"{role_label} disk '{value}' is not mounted.", 'ERROR')
                return None
            return mp

        pri_mnt = _resolve_diff_endpoint(primary, "Primary")
        if not pri_mnt:
            return
        sec_mnt = _resolve_diff_endpoint(secondary, "Secondary")
        if not sec_mnt:
            return

        src_path = pri_mnt.rstrip('/') + '/'
        dst_path = sec_mnt.rstrip('/') + '/'
        src_real = os.path.realpath(pri_mnt)
        dst_real = os.path.realpath(sec_mnt)
        if src_real == dst_real or dst_real.startswith(src_real.rstrip('/') + os.sep) or src_real.startswith(dst_real.rstrip('/') + os.sep):
            log("OPERATION BLOCKED: diff source and destination overlap. Choose two separate directory trees.", 'ERROR')
            return
        root_source_excludes = []
        root_source_flags = []
        if os.path.realpath(pri_mnt) == '/':
            # Keep root diff focused on real data, not pseudo-filesystems/mount roots.
            # Avoid crossing into other mounted filesystems.
            root_source_flags = ['--one-file-system']
            root_source_excludes = [
                '--exclude=/proc/***',
                '--exclude=/sys/***',
                '--exclude=/run/***',
                '--exclude=/dev/***',
                '--exclude=/tmp/***',
                '--exclude=/mnt/***',
                '--exclude=/media/***',
            ]
        if fast_mode:
            print(f"Diff (fast dry-run, source->destination): {Colors.BOLD}{src_path}{Colors.ENDC} -> {Colors.WARNING}{dst_path}{Colors.ENDC}")
            print("  Source is copied FROM; destination is what would be replaced to match source.")
        else:
            print(f"Diff (dry-run, source->destination): {Colors.BOLD}{src_path}{Colors.ENDC} -> {Colors.WARNING}{dst_path}{Colors.ENDC}")
            print("  Source is copied FROM; destination is what would be replaced to match source.")
        if root_source_excludes:
            print("  Root source detected: auto-excluding /proc, /sys, /run, /dev, /tmp, /mnt, /media")
            print("  Root source detected: using --one-file-system to avoid crossing mountpoints")
        if checksum_mode:
            print("  Comparison mode: checksum/content-based (rsync -c).")

        if fast_mode:
            if dirs_only:
                log("--fast ignores -d/--dirs-only.", 'WARN')
            if depth != 2:
                log("--fast ignores --depth.", 'WARN')

            fast_cmd = ['rsync', '-anHS', '--protect-args', '--sparse'] + root_source_flags
            if checksum_mode:
                fast_cmd.append('-c')
            fast_cmd.extend(['--delete', '--stats'])
            fast_cmd.extend(root_source_excludes)
            fast_cmd.extend([src_path, dst_path])
            res_fast = run_command(
                fast_cmd,
                sudo=True,
                capture_output=True,
                check=False
            )
            out_fast = (getattr(res_fast, 'stdout', '') or '')
            err_fast = (getattr(res_fast, 'stderr', '') or '')
            if out_fast.strip():
                print(out_fast.rstrip())
            if err_fast.strip():
                print(err_fast.rstrip(), file=sys.stderr)

            m_rf = re.search(r'^\s*Number of regular files transferred:\s*([0-9][0-9,]*)\s*$', out_fast, flags=re.MULTILINE)
            if m_rf:
                n_rf = int(m_rf.group(1).replace(',', ''))
                print(f"Fast note: created (new+updated regular files) = {n_rf:,}.")

            rc_fast = getattr(res_fast, 'returncode', 0)
            if rc_fast not in (0, 24):
                log(f"rsync dry-run exited with status {rc_fast}. Output may be incomplete.", 'WARN')
            return

        def _parse_int(s):
            m = re.search(r"[0-9][0-9,]*", str(s or ""))
            if not m:
                return 0
            try:
                return int(m.group(0).replace(',', ''), 10)
            except ValueError:
                return 0

        def _norm_relpath(p):
            p = (p or '').strip()
            # rsync escapes control bytes in names as backslash-octal tokens
            # (for example ``\#011`` for a tab). Decode only that transport
            # representation after splitting the tab-delimited record.
            def _decode_escape(match):
                try:
                    return chr(int(match.group(1), 8))
                except ValueError:
                    return match.group(0)
            p = re.sub(r"\\#([0-7]{3})", _decode_escape, p)
            while p.startswith('./'):
                p = p[2:]
            return p

        def _top_relpath(p):
            p = _norm_relpath(p)
            if not p:
                return "."
            return p.split('/', 1)[0]

        def _add_subtree(kind, relpath, seen, counts):
            relpath = _norm_relpath(relpath)
            if not relpath or relpath.endswith('/'):
                return
            # Root always tracks full-tree totals for all changed files.
            seen["."] = True
            counts[(".", kind)] = counts.get((".", kind), 0) + 1
            parts = relpath.split('/')
            max_folder_parts = len(parts) - 1
            if max_folder_parts < 1:
                return

            cur = []
            upto = min(max_folder_parts, depth)
            for i in range(upto):
                cur.append(parts[i])
                k = "/".join(cur)
                seen[k] = True
                counts[(k, kind)] = counts.get((k, kind), 0) + 1

        def _add_subtree_bytes(kind, relpath, seen, byte_totals, amount):
            relpath = _norm_relpath(relpath)
            if not relpath or relpath.endswith('/'):
                return
            # Root always tracks full-tree totals for all changed-file byte deltas.
            seen["."] = True
            byte_totals[(".", kind)] = byte_totals.get((".", kind), 0) + int(amount)
            parts = relpath.split('/')
            max_folder_parts = len(parts) - 1
            if max_folder_parts < 1:
                return

            cur = []
            upto = min(max_folder_parts, depth)
            for i in range(upto):
                cur.append(parts[i])
                k = "/".join(cur)
                seen[k] = True
                byte_totals[(k, kind)] = byte_totals.get((k, kind), 0) + int(amount)

        def _add_file_change(kind, relpath, file_counts, file_bytes, amount):
            relpath = _norm_relpath(relpath)
            if not relpath or relpath.endswith('/'):
                return
            file_counts[(relpath, kind)] = file_counts.get((relpath, kind), 0) + 1
            file_bytes[(relpath, kind)] = file_bytes.get((relpath, kind), 0) + int(amount)

        def _add_top_bytes(acc, relpath, amount):
            top = _top_relpath(relpath)
            acc[top] = acc.get(top, 0) + int(amount)

        def _fmt_size_human(num_bytes, signed=False):
            n = int(num_bytes or 0)
            neg = n < 0
            v = abs(float(n))
            units = ['B', 'KB', 'MB', 'GB']
            idx = 0
            while idx < len(units) - 1 and v >= 1024.0:
                v /= 1024.0
                idx += 1
            if idx == 0:
                txt = f"{int(v)}B"
            else:
                txt = f"{v:.2f}".rstrip('0').rstrip('.') + units[idx]
            if signed:
                return ("-" if neg else "+") + txt
            return ("-" if neg else "") + txt

        def _build_tree_entries(root_dir, max_depth, dirs_only_mode, extra_dirs=None, extra_files=None, excluded_rel_roots=None):
            """
            Build entries in tree order (directories first) up to max_depth.
            Includes unchanged files/dirs from the primary path so output mirrors:
              tree -L <depth> --dirsfirst
            """
            entries = [('dir', '.')]
            extra_dirs = set(extra_dirs or [])
            extra_files = set(extra_files or [])
            excluded_rel_roots = set(excluded_rel_roots or [])

            def _split_parent_name(relp):
                if not relp or relp == ".":
                    return ("", "")
                if "/" in relp:
                    parent, name = relp.rsplit("/", 1)
                    return (parent, name)
                return ("", relp)

            extra_dirs_by_parent = {}
            for relp in extra_dirs:
                if relp:
                    top = relp.split('/', 1)[0]
                    if top in excluded_rel_roots:
                        continue
                parent, name = _split_parent_name(relp)
                if not name:
                    continue
                extra_dirs_by_parent.setdefault(parent, set()).add(name)

            extra_files_by_parent = {}
            for relp in extra_files:
                if relp:
                    top = relp.split('/', 1)[0]
                    if top in excluded_rel_roots:
                        continue
                parent, name = _split_parent_name(relp)
                if not name:
                    continue
                extra_files_by_parent.setdefault(parent, set()).add(name)

            def _walk_virtual(rel_dir, level):
                if level >= max_depth:
                    return
                vdirs = sorted(extra_dirs_by_parent.get(rel_dir, set()))
                vfiles = sorted(extra_files_by_parent.get(rel_dir, set())) if not dirs_only_mode else []
                for dname in vdirs:
                    if not rel_dir and dname in excluded_rel_roots:
                        continue
                    relp = dname if not rel_dir else f"{rel_dir}/{dname}"
                    entries.append(('dir', relp))
                    _walk_virtual(relp, level + 1)
                if not dirs_only_mode:
                    for fname in vfiles:
                        if not rel_dir and fname in excluded_rel_roots:
                            continue
                        relp = fname if not rel_dir else f"{rel_dir}/{fname}"
                        entries.append(('file', relp))

            def _walk(abs_dir, rel_dir, level):
                if level >= max_depth:
                    return
                source_dir_names = set()
                source_file_names = set()
                try:
                    with os.scandir(abs_dir) as it:
                        for ent in it:
                            try:
                                is_dir = ent.is_dir(follow_symlinks=False)
                            except OSError:
                                is_dir = False
                            if is_dir:
                                source_dir_names.add(ent.name)
                            elif not dirs_only_mode:
                                source_file_names.add(ent.name)
                except (OSError, PermissionError, FileNotFoundError, NotADirectoryError):
                    _walk_virtual(rel_dir, level)
                    return

                dirs = sorted(source_dir_names | extra_dirs_by_parent.get(rel_dir, set()))
                files = sorted(source_file_names | extra_files_by_parent.get(rel_dir, set())) if not dirs_only_mode else []

                for dname in dirs:
                    if not rel_dir and dname in excluded_rel_roots:
                        continue
                    relp = dname if not rel_dir else f"{rel_dir}/{dname}"
                    entries.append(('dir', relp))
                    if dname in source_dir_names:
                        _walk(os.path.join(abs_dir, dname), relp, level + 1)
                    else:
                        _walk_virtual(relp, level + 1)

                if not dirs_only_mode:
                    for fname in files:
                        if not rel_dir and fname in excluded_rel_roots:
                            continue
                        relp = fname if not rel_dir else f"{rel_dir}/{fname}"
                        entries.append(('file', relp))

            _walk(root_dir, "", 0)
            return entries

        def _batch_stat_sizes(base_path, relpaths, chunk_size=512):
            """
            Resolve file sizes for many destination-relative paths using batched stat calls.
            Falls back to os.stat for paths missing from batched output.
            Returns: {relpath: size_bytes}
            """
            sizes = {}
            if not relpaths:
                return sizes

            unique_relpaths = list(dict.fromkeys(relpaths))
            for i in range(0, len(unique_relpaths), chunk_size):
                chunk_rel = unique_relpaths[i:i + chunk_size]
                abs_chunk = []
                abs_to_rel = {}
                for rel in chunk_rel:
                    ap = os.path.join(base_path, rel)
                    abs_chunk.append(ap)
                    abs_to_rel[ap] = rel

                seen_rel = set()
                res_stat = run_command(
                    ['stat', '-c', '%n\t%s', '--'] + abs_chunk,
                    sudo=True,
                    capture_output=True,
                    check=False
                )
                out_stat = (getattr(res_stat, 'stdout', '') or '')
                for line in out_stat.splitlines():
                    line = (line or '').rstrip('\n')
                    if '\t' not in line:
                        continue
                    out_path, out_size = line.rsplit('\t', 1)
                    rel = abs_to_rel.get(out_path)
                    if not rel:
                        continue
                    sizes[rel] = _parse_int(out_size)
                    seen_rel.add(rel)

                for rel in chunk_rel:
                    if rel in seen_rel:
                        continue
                    ap = os.path.join(base_path, rel)
                    try:
                        st = os.stat(ap)
                        sizes[rel] = int(st.st_size)
                    except OSError:
                        pass
            return sizes

        # Summary 1 counters
        new_files = 0
        mod_files = 0
        del_files = 0
        del_dirs = 0
        new_bytes = 0
        mod_bytes = 0
        del_bytes = 0
        pending_delete_paths = []
        modified_entries = []
        # Logical byte totals by top-level path for sparse/compression-aware estimate.
        top_src_new = {}
        top_src_upd = {}
        top_dst_del = {}

        # Summary 2 counters (folder view)
        seen = {}
        c = {}
        cb = {}
        fc = {}
        fb = {}
        total_new = 0
        total_upd = 0
        total_del = 0

        cmd = ['rsync', '-anHS', '--protect-args', '--sparse'] + root_source_flags
        if checksum_mode:
            cmd.append('-c')
        cmd.extend([
            '--delete', '--itemize-changes',
            '--out-format=%i\t%l\t%n'
        ])
        cmd.extend(root_source_excludes)
        cmd.extend([src_path, dst_path])
        stderr_lines = []
        parse_incomplete = False

        def _drain_stderr(stream, sink):
            try:
                for line in stream:
                    sink.append(line)
            except Exception:
                pass

        proc = popen_command(
            cmd,
            sudo=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr, stderr_lines), daemon=True)
        stderr_thread.start()

        for raw in proc.stdout:
            line = (raw or "").rstrip()
            if not line:
                continue

            # Delete lines do not reliably respect out-format.
            if line.startswith('*deleting'):
                body = line[len('*deleting'):].lstrip()
                del_size = None
                # With --out-format, rsync delete lines may appear as:
                #   *deleting 0<TAB>path
                # Strip the optional leading size field so it doesn't pollute folder names.
                if '\t' in body:
                    first, rest = body.split('\t', 1)
                    if re.fullmatch(r"[0-9][0-9,]*", first.strip()):
                        del_size = _parse_int(first)
                        body = rest

                p = _norm_relpath(body.strip())
                if p.endswith('/'):
                    del_dirs += 1
                else:
                    del_files += 1
                    total_del += 1
                    _add_subtree('del', p, seen, c)
                    if del_size is not None and del_size > 0:
                        del_bytes += del_size
                        _add_subtree_bytes('del', p, seen, cb, del_size)
                        _add_file_change('del', p, fc, fb, del_size)
                        _add_top_bytes(top_dst_del, p, del_size)
                    else:
                        # rsync delete lines often report 0 size; resolve after parse in batches.
                        pending_delete_paths.append(p)
                continue

            parts = line.split('\t', 2)
            if len(parts) < 3:
                parse_incomplete = True
                continue
            item = parts[0].strip()
            lraw = parts[1].strip()
            relp = _norm_relpath(parts[2].strip())
            size_b = _parse_int(lraw)

            if len(item) < 2:
                continue
            item_type = item[1]
            is_hardlink_create = (item.startswith('h') and '+' in item[2:])
            is_new_nondir = (
                item.startswith('>f+') or item.startswith('cf+') or
                item.startswith('>L+') or item.startswith('cL+') or
                # Hard-link creation entries from rsync -H itemize output
                # (e.g. "hf+++++++++"). These are creates, not modifications.
                is_hardlink_create
            )

            # New files/symlinks.
            if is_new_nondir:
                new_files += 1
                total_new += 1
                # For hard-link creates, rsync typically transfers payload once and
                # then creates extra directory entries; counting full size for each
                # hard-link path inflates folder totals. Keep count, zero byte delta.
                effective_size_b = 0 if is_hardlink_create else size_b
                new_bytes += effective_size_b
                _add_subtree('new', relp, seen, c)
                _add_subtree_bytes('new', relp, seen, cb, effective_size_b)
                _add_file_change('new', relp, fc, fb, effective_size_b)
                _add_top_bytes(top_src_new, relp, effective_size_b)
                continue

            # Modified regular files (delta estimated as src_size - dst_size).
            if item_type == 'f':
                mod_files += 1
                total_upd += 1
                modified_entries.append((relp, size_b))
                _add_subtree('upd', relp, seen, c)
                continue

            # Modified symlinks (delta from rsync item size field only; no dst stat fallback).
            if item_type == 'L':
                mod_files += 1
                total_upd += 1
                mod_bytes += size_b
                _add_subtree('upd', relp, seen, c)
                _add_subtree_bytes('upd', relp, seen, cb, size_b)
                _add_file_change('upd', relp, fc, fb, size_b)
                _add_top_bytes(top_src_upd, relp, size_b)
                continue

        if proc.stdout:
            proc.stdout.close()
        rc = proc.wait()
        stderr_thread.join(timeout=1.0)

        if rc not in (0, 24):
            log(f"rsync dry-run exited with status {rc}. Output may be incomplete.", 'WARN')
            parse_incomplete = True
        elif rc == 24:
            parse_incomplete = True
            log("rsync dry-run saw files vanish during the scan; reported totals are incomplete. Re-run diff on a quiescent tree.", 'WARN')
        if parse_incomplete:
            print("WARNING: diff could not guarantee a complete filename-safe scan; totals may be incomplete.", file=sys.stderr)
        err = "".join(stderr_lines)
        if err.strip():
            print(err.rstrip(), file=sys.stderr)

        # Batch-resolve file sizes for delete fallback and modified destination sizes.
        if pending_delete_paths:
            del_sizes = _batch_stat_sizes(dst_path, pending_delete_paths)
            for rel in pending_delete_paths:
                sz = del_sizes.get(rel, 0)
                del_bytes += sz
                _add_subtree_bytes('del', rel, seen, cb, sz)
                _add_file_change('del', rel, fc, fb, sz)
                _add_top_bytes(top_dst_del, rel, sz)

        if modified_entries:
            mod_dst_sizes = _batch_stat_sizes(dst_path, [rel for rel, _ in modified_entries])
            for rel, src_size in modified_entries:
                delta = src_size - mod_dst_sizes.get(rel, 0)
                mod_bytes += delta
                _add_subtree_bytes('upd', rel, seen, cb, delta)
                _add_file_change('upd', rel, fc, fb, delta)
                _add_top_bytes(top_src_upd, rel, delta)

        src_tree_root = os.path.realpath(pri_mnt) or "/"
        dst_tree_root = os.path.realpath(sec_mnt) or "/"

        def _du_bytes(base_root, top_rel, apparent=False):
            p = base_root if top_rel in (".", "", None) else os.path.join(base_root, top_rel)
            if not os.path.exists(p):
                return None
            cmd = ['du', '-sx', '-B1']
            if apparent:
                cmd.append('--apparent-size')
            cmd.append(p)
            res_du = run_command(cmd, sudo=True, check=False, capture_output=True)
            if getattr(res_du, 'returncode', 1) != 0:
                return None
            out = (getattr(res_du, 'stdout', '') or '').strip()
            if not out:
                return None
            tok = out.split()[0]
            try:
                return int(tok)
            except Exception:
                return None

        def _ratio_for_top(base_root, top_rel, cache):
            key = (base_root, top_rel)
            if key in cache:
                return cache[key]
            phys = _du_bytes(base_root, top_rel, apparent=False)
            app = _du_bytes(base_root, top_rel, apparent=True)
            if phys is None or app is None or app <= 0:
                ratio = 1.0
            else:
                ratio = max(0.0, min(1.0, float(phys) / float(app)))
            cache[key] = ratio
            return ratio

        src_ratio_cache = {}
        dst_ratio_cache = {}
        est_new_bytes = 0
        for top, logical_b in top_src_new.items():
            est_new_bytes += int(round(logical_b * _ratio_for_top(src_tree_root, top, src_ratio_cache)))
        est_upd_bytes = 0
        for top, logical_b in top_src_upd.items():
            est_upd_bytes += int(round(logical_b * _ratio_for_top(src_tree_root, top, src_ratio_cache)))
        est_del_bytes = 0
        for top, logical_b in top_dst_del.items():
            est_del_bytes += int(round(logical_b * _ratio_for_top(dst_tree_root, top, dst_ratio_cache)))

        print(f"Created files:  {new_files}")
        print(f"Modified files: {mod_files}")
        print(f"Deleted files:  {del_files}")
        print(f"Deleted dirs:   {del_dirs}")
        print("-----------------------")
        print(f"New Data:       {new_bytes:,} bytes")
        print(f"Updated Data:   {mod_bytes:,} bytes")
        print(f"Deleted Data:   {del_bytes:,} bytes")
        print(f"Est. New Data:  {est_new_bytes:,} bytes (physical est.)")
        print(f"Est. Upd Data:  {est_upd_bytes:,} bytes (physical est.)")
        print(f"Est. Del Data:  {est_del_bytes:,} bytes (physical est.)")
        print("-----------------------")
        print(f"Net Change:     {new_bytes + mod_bytes - del_bytes:,} bytes")
        print(f"Est. Net Chg:   {est_new_bytes + est_upd_bytes - est_del_bytes:,} bytes")
        print("")

        print("Rsync dry-run change summary by hierarchy")
        print(f"Source -> Dest: {src_path}  ->  {dst_path}")
        print("Source = copied FROM; Dest = replaced to match source")
        print(f"Depth shown: {depth} level(s)")
        print("")
        print("Format: <name[/]>  (+N [C]  ~U [U]  -D [R])")
        print("  +N = files/symlinks that would be CREATED under this entry/subtree")
        print("  ~U = files/symlinks that would be MODIFIED/UPDATED under this entry/subtree")
        print("  -D = files/symlinks that would be DELETED under this entry/subtree")
        print("  [C]/[U]/[R] = created/updated(delta)/removed bytes (shown only when non-zero)")
        print("  Directories end with '/'.")
        print("  Unchanged entries are uncolored.")
        print("Counts are inclusive of all descendants (subtree totals).")
        print("-----------------------------------------------")

        deleted_dirs = {
            k for k in seen.keys()
            if k != "." and c.get((k, 'del'), 0) > 0
        }
        deleted_files = {
            p for (p, kind) in fc.keys()
            if kind == 'del'
        }
        entries = _build_tree_entries(
            src_tree_root,
            depth,
            dirs_only,
            extra_dirs=deleted_dirs,
            extra_files=deleted_files
        )

        for etype, path in entries:
            if etype == 'dir':
                depth_here = 0 if path == "." else (path.count('/') + 1)
                indent = " " * (depth_here * 2)
                name = dst_path if path == "." else (path.split('/')[-1] + "/")
                n_new = c.get((path, 'new'), 0)
                n_upd = c.get((path, 'upd'), 0)
                n_del = c.get((path, 'del'), 0)
                b_new = cb.get((path, 'new'), 0)
                b_upd = cb.get((path, 'upd'), 0)
                b_del = cb.get((path, 'del'), 0)
            else:
                depth_here = path.count('/') + 1
                indent = " " * (depth_here * 2)
                name = path.split('/')[-1]
                n_new = fc.get((path, 'new'), 0)
                n_upd = fc.get((path, 'upd'), 0)
                n_del = fc.get((path, 'del'), 0)
                b_new = fb.get((path, 'new'), 0)
                b_upd = fb.get((path, 'upd'), 0)
                b_del = fb.get((path, 'del'), 0)

            kinds = 0
            if n_new > 0:
                kinds += 1
            if n_upd > 0:
                kinds += 1
            if n_del > 0:
                kinds += 1

            if kinds > 1:
                color = Colors.WARNING
            elif n_new > 0:
                color = Colors.OKGREEN
            elif n_del > 0:
                color = Colors.FAIL
            elif n_upd > 0:
                color = Colors.WARNING
            else:
                color = None

            new_part = f"+{n_new}" + (f" [{_fmt_size_human(b_new)}]" if b_new != 0 else "")
            upd_part = f"~{n_upd}" + (f" [{_fmt_size_human(b_upd, signed=True)}]" if b_upd != 0 else "")
            del_part = f"-{n_del}" + (f" [{_fmt_size_human(b_del)}]" if b_del != 0 else "")
            cname = f"{color}{name}{Colors.ENDC}" if color else name
            print(
                f"{indent}{cname}  "
                f"({new_part}  "
                f"{upd_part}  "
                f"{del_part})"
            )

        print("-----------------------------------------------")
        print(f"Totals (files/symlinks): create={total_new}  modify={total_upd}  delete={total_del}")
