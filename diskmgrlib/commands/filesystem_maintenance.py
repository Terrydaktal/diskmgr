"""Mounted filesystem maintenance and diagnostics commands."""

import argparse
import cmd
import datetime
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from ..runtime import Colors, LUKS_HEADER_BACKUP_DIR, LUKS_PBKDF_DEFAULT_MEMORY_KIB, LUKS_PBKDF_DEFAULT_MEMORY_LABEL, LUKS_PBKDF_DEFAULT_THREADS, LUKS_PBKDF_DEFAULT_TIME, PASSGEN_BIN, _cmd_log_close, _cmd_log_open, _cmd_log_write, _find_tool_or_common_paths, _fmt_hms, log, popen_command, run_command
from ..devices import _lsblk_fstype, _lsblk_partitions, _lsblk_type
from ..mounts import find_mount_targets
from ..mappings import read_luks_map, save_luks_map
from ..shell_core import CmdArgumentParser
from ..runtime import log, run_command
from ..devices import _lsblk_type


class FilesystemMaintenanceCommands:

    def do_defrag(self, arg):
        '''Defragment a mounted filesystem: defrag <name> [--compress]

        UNDER THE HOOD:
        1.  Validation: Verifies the disk is mapped and currently mounted.
        2.  Confirmation: Requires typing YES.
        3.  Execution:
            - ext4:  runs 'sudo e4defrag <mountpoint>'
            - btrfs: runs 'sudo btrfs filesystem defragment -r -v <mountpoint>'
                     optional: add '--compress' to use '-czstd' recompression mode.
                     with live progress counters (total files + current directory),
                     then 'sudo btrfs balance start -dusage=50 <mountpoint>'
        4.  Recording: Stores a timestamp on the mountpoint root via:
              sudo setfattr -n user.last_defrag -v "<date>" <mountpoint>
        '''
        parser = CmdArgumentParser(prog='defrag', add_help=False)
        parser.add_argument('name')
        parser.add_argument('--compress', action='store_true', help='Enable btrfs recompression during defrag (-czstd)')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.name
        compress_mode = bool(getattr(args, 'compress', False))
        mnt = self.get_mountpoint(name)
        if not mnt:
            log(f"Disk '{name}' is not mounted. Run 'open {name}' first.", 'ERROR')
            return

        # Block defrag on the system root drive (writes lots of blocks/metadata).
        try:
            res_src = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'SOURCE'], check=False)
        except Exception as exc:
            log(f"Could not verify defrag source: {exc}", 'ERROR')
            return
        if getattr(res_src, 'returncode', 1) != 0:
            log(f"Could not verify defrag source for {mnt}; refusing to proceed.", 'ERROR')
            return
        src = (getattr(res_src, 'stdout', '') or '').strip()
        if not src:
            log(f"Could not resolve defrag source for {mnt}; refusing to proceed.", 'ERROR')
            return
        if self._block_if_root_drive(os.path.realpath(src), f"defrag {name}"):
            return

        # Determine filesystem type.
        fstype = ""
        try:
            res_fs = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'FSTYPE'], check=False)
        except Exception as exc:
            log(f"Could not determine filesystem type: {exc}", 'ERROR')
            return
        if getattr(res_fs, 'returncode', 1) != 0:
            log(f"Could not determine filesystem type for {mnt}; refusing to proceed.", 'ERROR')
            return
        fstype = (getattr(res_fs, 'stdout', '') or '').strip().lower()

        if fstype not in ("ext4", "btrfs"):
            log(f"Defrag not supported for filesystem type '{fstype or 'unknown'}' at {mnt}.", 'ERROR')
            log("Supported: ext4 (e4defrag), btrfs (defragment + balance -dusage=50).", 'ERROR')
            return

        # Confirmation (not strictly destructive, but high-impact: rewrites lots of blocks/metadata).
        print(f"Defragmenting: {Colors.BOLD}{mnt}{Colors.ENDC} (fstype={fstype})")
        if not self.extensive_confirm(f"defrag {name} ({mnt})", destructive=False):
            return

        # Find tools (PATH may not include sbin).
        e4defrag_bin = _find_tool_or_common_paths('e4defrag', [
            '/usr/sbin/e4defrag',
            '/sbin/e4defrag',
            '/usr/local/sbin/e4defrag',
            '/usr/bin/e4defrag',
            '/bin/e4defrag',
        ])
        btrfs_bin = _find_tool_or_common_paths('btrfs', [
            '/usr/sbin/btrfs',
            '/sbin/btrfs',
            '/usr/local/sbin/btrfs',
            '/usr/bin/btrfs',
            '/bin/btrfs',
        ])
        setfattr_bin = _find_tool_or_common_paths('setfattr', [
            '/usr/bin/setfattr',
            '/bin/setfattr',
            '/usr/sbin/setfattr',
            '/sbin/setfattr',
        ])

        try:
            start_ts_total = time.time()
            if fstype == "ext4":
                start_ts = time.time()
                if compress_mode:
                    log("--compress applies to btrfs only; ignoring for ext4.", 'WARN')
                if e4defrag_bin is None:
                    log("e4defrag not found. Install 'e2fsprogs' and retry.", 'ERROR')
                    return
                log(f"Running: {e4defrag_bin} {mnt}")
                result = run_command([e4defrag_bin, mnt], sudo=True, capture_output=False, check=False)
                if getattr(result, 'returncode', 1) != 0:
                    log(f"e4defrag exited with status {getattr(result, 'returncode', 1)}; timestamp not recorded.", 'ERROR')
                    return
                print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            else:
                if btrfs_bin is None:
                    log("btrfs not found. Install 'btrfs-progs' and retry.", 'ERROR')
                    return
                start_ts_total = time.time()
                start_ts = time.time()
                log_path = f"/tmp/diskmgr_btrfs_defrag_{os.getpid()}_{int(start_ts)}.log"
                cmd = [btrfs_bin, 'filesystem', 'defragment', '-r', '-v']
                if compress_mode:
                    cmd.append('-czstd')
                cmd.append(mnt)
                cmd_str = " ".join(cmd)
                log(f"Running: {cmd_str}")
                log(f"Progress log: {log_path}")

                total_done = 0
                current_dir = "Scanning..."
                dir_counts = {}
                last_render = 0.0
                proc = popen_command(
                    cmd,
                    sudo=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                try:
                    with open(log_path, 'w', encoding='utf-8', errors='replace') as lf:
                        if proc.stdout:
                            for raw in proc.stdout:
                                line = (raw or '').rstrip('\n')
                                lf.write(line + "\n")
                                txt = line.strip()
                                if txt.startswith('/'):
                                    # btrfs -v emits one path per processed file; use it as progress signal.
                                    path = txt.rstrip(':')
                                    current_dir = os.path.dirname(path) or '/'
                                    total_done += 1
                                    dir_counts[current_dir] = dir_counts.get(current_dir, 0) + 1
                                now = time.time()
                                if now - last_render >= 1.0:
                                    in_dir = dir_counts.get(current_dir, 0)
                                    msg = (
                                        f"\rFiles: {total_done:<10} | "
                                        f"In Current Dir: {in_dir:<8} | "
                                        f"Path: {current_dir[:70]}"
                                    )
                                    sys.stdout.write(msg)
                                    sys.stdout.flush()
                                    last_render = now
                            proc.stdout.close()
                    rc_defrag = proc.wait()
                except BaseException:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise
                in_dir = dir_counts.get(current_dir, 0)
                final_msg = (
                    f"\rFiles: {total_done:<10} | "
                    f"In Current Dir: {in_dir:<8} | "
                    f"Path: {current_dir[:70]}"
                )
                sys.stdout.write(final_msg + "\n")
                sys.stdout.flush()

                elapsed = time.time() - start_ts
                print("--------------------------------------------------------")
                print(f"Btrfs defragment complete. Duration: {_fmt_hms(elapsed)}")
                print(f"Total files processed (verbose paths): {total_done}")
                print(f"Progress log saved at: {log_path}")
                if rc_defrag not in (0,):
                    log(f"btrfs defragment exited with status {rc_defrag}; balance and timestamp skipped.", 'ERROR')
                    return
                log(f"Running: {btrfs_bin} balance start -dusage=50 {mnt}")
                res_balance_start = run_command(
                    [btrfs_bin, 'balance', 'start', '-dusage=50', mnt],
                    sudo=True,
                    capture_output=True,
                    check=False
                )
                out_start = (getattr(res_balance_start, 'stdout', '') or '').strip()
                err_start = (getattr(res_balance_start, 'stderr', '') or '').strip()
                if out_start:
                    print(out_start)
                if err_start:
                    print(err_start, file=sys.stderr)
                if getattr(res_balance_start, 'returncode', 1) not in (0,):
                    log(f"btrfs balance start exited with status {getattr(res_balance_start, 'returncode', 1)}; timestamp not recorded.", 'ERROR')
                    return

                log(f"Monitoring: {btrfs_bin} balance status {mnt}")
                last_status = None
                while True:
                    res_status = run_command(
                        [btrfs_bin, 'balance', 'status', mnt],
                        sudo=True,
                        capture_output=True,
                        check=False
                    )
                    out_status = (getattr(res_status, 'stdout', '') or '').strip()
                    err_status = (getattr(res_status, 'stderr', '') or '').strip()
                    status_text = out_status or err_status or "(no status output)"

                    if status_text != last_status:
                        print(status_text)
                        last_status = status_text

                    s = status_text.lower()
                    if ("is running" in s) or ("is paused" in s):
                        time.sleep(2.0)
                        continue
                    if "error" in s or "failed" in s:
                        log(f"btrfs balance reported failure: {status_text}", 'ERROR')
                        return
                    break

            # Record timestamp in xattr on mountpoint root.
            if setfattr_bin is None:
                log("setfattr not found. Install 'attr' and retry to record last_defrag.", 'WARN')
                print(f"Duration: {_fmt_hms(time.time() - start_ts_total)}")
                return

            ds = ""
            try:
                res_date = run_command(['date'], capture_output=True, check=False)
                ds = (getattr(res_date, 'stdout', '') or '').strip()
            except Exception:
                ds = ""
            if not ds:
                ds = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')

            result = run_command([setfattr_bin, '-n', 'user.last_defrag', '-v', ds, mnt], sudo=True, check=False)
            if getattr(result, 'returncode', 1) != 0:
                log("Could not record user.last_defrag; maintenance itself completed.", 'WARN')
                print(f"Duration: {_fmt_hms(time.time() - start_ts_total)}")
                return
            log(f"Recorded xattr: user.last_defrag={ds} on {mnt}")
            print(f"Duration: {_fmt_hms(time.time() - start_ts_total)}")
        except Exception as e:
            log(f"Defrag failed: {e}", 'ERROR')

    def do_fshealth(self, arg):
        '''Filesystem health/diagnostics: fshealth <name>

        Shows filesystem-specific diagnostic output and local "maintenance" timestamps.

        - ext4:  sudo tune2fs -l <device>
                sudo e4defrag -c <mountpoint>   (fragmentation score + extents/files ratio)
        - btrfs: sudo btrfs filesystem usage <mountpoint>
                sudo btrfs filesystem show <mountpoint>
                sudo btrfs filesystem df <mountpoint>
                sudo btrfs device stats <mountpoint>
                sudo btrfs scrub status <mountpoint>
                sudo compsize <mountpoint>  (extents/files ratio)
        - xfs:   xfs_info <mountpoint>

        Also reads xattrs from the mountpoint root:
          user.last_defrag, user.last_scrub
        '''
        args = arg.split()
        if len(args) != 1:
            log("Usage: fshealth <name>", 'ERROR')
            return

        name = args[0]
        mnt = self.get_mountpoint(name)
        if not mnt:
            log(f"Disk '{name}' is not mounted. Run 'open {name}' first.", 'ERROR')
            return

        fstype = ""
        src = ""
        try:
            res = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'FSTYPE,SOURCE'], check=False)
            if getattr(res, 'returncode', 1) != 0:
                log(f"Could not query findmnt for {mnt}; refusing filesystem health inspection.", 'ERROR')
                return
            parts = (getattr(res, 'stdout', '') or '').strip().split()
            if len(parts) >= 2:
                fstype = parts[0].strip().lower()
                src = parts[1].strip()
            else:
                # Fallback to separate calls.
                res_fs = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'FSTYPE'], check=False)
                fstype = (getattr(res_fs, 'stdout', '') or '').strip().lower()
                res_src = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'SOURCE'], check=False)
                src = (getattr(res_src, 'stdout', '') or '').strip()
        except Exception as exc:
            log(f"Could not query mounted filesystem state: {exc}", 'ERROR')
            return

        if not fstype or not src:
            log(f"Could not resolve filesystem type/source for {mnt}; refusing inspection.", 'ERROR')
            return

        src_real = os.path.realpath(src) if src else ""

        def _read_xattr_text(path, key):
            try:
                v = os.getxattr(path, key)
            except OSError:
                return None
            try:
                if isinstance(v, (bytes, bytearray)):
                    return v.decode('utf-8', errors='replace')
            except Exception:
                return str(v)
            return str(v)

        last_defrag = _read_xattr_text(mnt, 'user.last_defrag')
        last_scrub = _read_xattr_text(mnt, 'user.last_scrub')

        print(f"\n{Colors.HEADER}{Colors.BOLD}=== Filesystem health: {name} ==={Colors.ENDC}")
        print(f"{Colors.BOLD}Mountpoint:{Colors.ENDC} {mnt}")
        if src_real:
            print(f"{Colors.BOLD}Source:{Colors.ENDC} {src_real}")
        if fstype:
            print(f"{Colors.BOLD}FSType:{Colors.ENDC} {fstype}")
        print(f"{Colors.BOLD}Last defrag:{Colors.ENDC} {last_defrag or '-'}")
        print(f"{Colors.BOLD}Last scrub:{Colors.ENDC} {last_scrub or '-'}")

        print(f"\n{Colors.HEADER}{Colors.BOLD}--- findmnt -A --output-all --json {mnt} ---{Colors.ENDC}")
        res_fm = run_command(['findmnt', '-A', '--output-all', '--json', mnt], check=False)
        fm_out = (getattr(res_fm, 'stdout', '') or '')
        fm_err = (getattr(res_fm, 'stderr', '') or '')
        if fm_out.strip():
            print(fm_out.rstrip())
        else:
            print("{}")
        if fm_err.strip():
            print(fm_err.rstrip(), file=sys.stderr)

        # Compute fragmentation state (best-effort), but print it at the end of fshealth output.
        frag_lines = []
        def _frag(line):
            frag_lines.append(line)

        compsize_bin = None
        compsize_out = ""
        compsize_err = ""
        def _ratio_state_ext4(epf):
            # Requested rubric: <1.1 healthy, >1.5 bad, >5 critical.
            if epf < 1.1:
                return (f"{Colors.OKGREEN}HEALTHY{Colors.ENDC}", "<1.1 healthy")
            if epf > 5.0:
                return (f"{Colors.FAIL}CRITICAL{Colors.ENDC}", ">5 critical")
            if epf > 1.5:
                return (f"{Colors.WARNING}BAD{Colors.ENDC}", ">1.5 bad")
            return (f"{Colors.WARNING}WATCH{Colors.ENDC}", "between 1.1 and 1.5")

        def _ratio_state_btrfs(epf):
            # Requested rubric: <1 good, >5 bad, >20 critical.
            if epf < 1.0:
                return (f"{Colors.OKGREEN}GOOD{Colors.ENDC}", "<1 good")
            if epf > 20.0:
                return (f"{Colors.FAIL}CRITICAL{Colors.ENDC}", ">20 critical")
            if epf > 5.0:
                return (f"{Colors.WARNING}BAD{Colors.ENDC}", ">5 bad")
            return (f"{Colors.WARNING}WATCH{Colors.ENDC}", "between 1 and 5")

        if fstype == 'ext4':
            e4defrag_bin = _find_tool_or_common_paths('e4defrag', [
                '/usr/sbin/e4defrag',
                '/sbin/e4defrag',
                '/usr/local/sbin/e4defrag',
                '/usr/bin/e4defrag',
                '/bin/e4defrag',
            ])
            if e4defrag_bin:
                res_f = run_command([e4defrag_bin, '-c', mnt], sudo=True, capture_output=True, check=False)
                frag_out = (getattr(res_f, 'stdout', '') or '') + (getattr(res_f, 'stderr', '') or '')
                m = re.search(r"(?im)Fragmentation score(?: is)?\s*([0-9]+)\b", frag_out)
                m_tb = re.search(
                    r"(?im)^\s*Total/best\s+extents\s*:?\s*([0-9][0-9,]*)\s*/\s*([0-9][0-9,]*)\s*$",
                    frag_out,
                )
                m_avg = re.search(
                    r"(?im)^\s*Average\s+size\s+per\s+extent\s*:?\s*(.+?)\s*$",
                    frag_out,
                )
                score = None
                if m:
                    try:
                        score = int(m.group(1), 10)
                    except ValueError:
                        score = None

                total_extents = None
                best_extents = None
                if m_tb:
                    try:
                        total_extents = int(m_tb.group(1).replace(",", ""), 10)
                        best_extents = int(m_tb.group(2).replace(",", ""), 10)
                    except ValueError:
                        total_extents = None
                        best_extents = None

                # Prefer a real file-count if e4defrag reports one; otherwise fall back to best_extents proxy.
                total_files = None
                m_files = re.search(
                    r"(?im)^\s*Fragmented\s+files/Total\s+files\s*:?\s*([0-9][0-9,]*)\s*/\s*([0-9][0-9,]*)\s*$",
                    frag_out,
                )
                if m_files:
                    try:
                        total_files = int(m_files.group(2).replace(",", ""), 10)
                    except ValueError:
                        total_files = None

                epf = None
                if total_extents is not None and total_files is not None and total_files > 0:
                    epf = total_extents / float(total_files)
                elif total_extents is not None and best_extents is not None and best_extents > 0:
                    epf = total_extents / float(best_extents)

                if epf is not None:
                    state, _ = _ratio_state_ext4(epf)
                    score_part = f"; score {score}" if score is not None else ""
                    _frag(
                        f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} "
                        f"{state} (ext4 ratio ~{epf:.2f} extents/file; thresholds: "
                        f"<1.1 healthy, >1.5 bad, >5 critical{score_part})"
                    )
                elif score is not None:
                    _frag(
                        f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} "
                        f"- (ext4 thresholds: <1.1 healthy, >1.5 bad, >5 critical; "
                        f"extents/files ratio unavailable; score {score})"
                    )
                else:
                    _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (could not parse e4defrag output)")

                if total_extents is not None and best_extents is not None:
                    extra_extents = max(total_extents - best_extents, 0)
                    overhead_pct = (extra_extents / float(best_extents) * 100.0) if best_extents > 0 else 0.0
                    _frag(
                        f"{Colors.BOLD}Extent overhead:{Colors.ENDC} "
                        f"{extra_extents} extra extents over ideal ({overhead_pct:.2f}%)"
                    )
                if m_avg:
                    avg_extent = (m_avg.group(1) or "").strip()
                    if avg_extent:
                        _frag(f"{Colors.BOLD}Average extent size:{Colors.ENDC} {avg_extent}")
            else:
                _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (e4defrag not found)")
        elif fstype == 'btrfs':
            compsize_bin = _find_tool_or_common_paths('compsize', [
                '/usr/sbin/compsize',
                '/sbin/compsize',
                '/usr/local/sbin/compsize',
                '/usr/bin/compsize',
                '/bin/compsize',
            ])
            if compsize_bin is None:
                _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (compsize not found)")
            else:
                res_c = run_command([compsize_bin, mnt], sudo=True, capture_output=True, check=False)
                compsize_out = (getattr(res_c, 'stdout', '') or '')
                compsize_err = (getattr(res_c, 'stderr', '') or '')

                m = re.search(
                    r"(?im)^\s*Processed\s+([0-9][0-9,]*)\s+files,\s+([0-9][0-9,]*)\s+regular\s+extents\b",
                    compsize_out + "\n" + compsize_err,
                )
                if m:
                    try:
                        files = int(m.group(1).replace(",", ""), 10)
                        extents = int(m.group(2).replace(",", ""), 10)
                    except ValueError:
                        files = None
                        extents = None
                    if files is not None and extents is not None and files > 0:
                        epf = extents / float(files)
                        state, _ = _ratio_state_btrfs(epf)
                        _frag(
                            f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} "
                            f"{state} (btrfs ratio ~{epf:.2f} extents/file; thresholds: "
                            f"<1 good, >5 bad, >20 critical; "
                            f"{extents} extents/{files} files; via compsize)"
                        )
                    else:
                        _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (could not parse compsize counters)")
                else:
                    _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (compsize did not report Processed counters)")
        else:
            _frag(f"{Colors.BOLD}Defragmentation state:{Colors.ENDC} - (no global defrag metric for {fstype or 'unknown'})")

        # Filesystem diagnostics output (read-only).
        if fstype == 'ext4':
            tune2fs_bin = _find_tool_or_common_paths('tune2fs', [
                '/usr/sbin/tune2fs',
                '/sbin/tune2fs',
                '/usr/local/sbin/tune2fs',
                '/usr/bin/tune2fs',
                '/bin/tune2fs',
            ])
            if tune2fs_bin is None:
                log("tune2fs not found. Install 'e2fsprogs' to view ext4 diagnostics.", 'WARN')
            elif not src_real:
                log("Could not determine source device for this mount (findmnt SOURCE).", 'ERROR')
            else:
                print(f"\n{Colors.HEADER}{Colors.BOLD}--- tune2fs -l {src_real} ---{Colors.ENDC}")
                res = run_command([tune2fs_bin, '-l', src_real], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)
        elif fstype == 'btrfs':
            btrfs_bin = _find_tool_or_common_paths('btrfs', [
                '/usr/sbin/btrfs',
                '/sbin/btrfs',
                '/usr/local/sbin/btrfs',
                '/usr/bin/btrfs',
                '/bin/btrfs',
            ])
            if btrfs_bin is None:
                log("btrfs not found. Install 'btrfs-progs' to view btrfs diagnostics.", 'WARN')
            else:
                print(f"\n{Colors.HEADER}{Colors.BOLD}--- btrfs filesystem usage {mnt} ---{Colors.ENDC}")
                res = run_command([btrfs_bin, 'filesystem', 'usage', mnt], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)

                print(f"\n{Colors.HEADER}{Colors.BOLD}--- btrfs filesystem show {mnt} ---{Colors.ENDC}")
                res = run_command([btrfs_bin, 'filesystem', 'show', mnt], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)

                print(f"\n{Colors.HEADER}{Colors.BOLD}--- btrfs filesystem df {mnt} ---{Colors.ENDC}")
                res = run_command([btrfs_bin, 'filesystem', 'df', mnt], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)

                print(f"\n{Colors.HEADER}{Colors.BOLD}--- btrfs device stats {mnt} ---{Colors.ENDC}")
                res = run_command([btrfs_bin, 'device', 'stats', mnt], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)

                print(f"\n{Colors.HEADER}{Colors.BOLD}--- btrfs scrub status {mnt} ---{Colors.ENDC}")
                res = run_command([btrfs_bin, 'scrub', 'status', mnt], sudo=True, capture_output=True, check=False)
                if (res.stdout or "").strip():
                    print(res.stdout.rstrip())
                if (res.stderr or "").strip():
                    print(res.stderr.rstrip(), file=sys.stderr)
                if compsize_bin is None:
                    log("compsize not found. Install 'compsize' to view btrfs fragmentation amount.", 'WARN')
                else:
                    print(f"\n{Colors.HEADER}{Colors.BOLD}--- compsize {mnt} ---{Colors.ENDC}")
                    if compsize_out.strip():
                        print(compsize_out.rstrip())
                    if compsize_err.strip():
                        print(compsize_err.rstrip(), file=sys.stderr)
        elif fstype == 'xfs':
            xfs_info_bin = _find_tool_or_common_paths('xfs_info', [
                '/usr/sbin/xfs_info',
                '/sbin/xfs_info',
                '/usr/local/sbin/xfs_info',
                '/usr/bin/xfs_info',
                '/bin/xfs_info',
            ]) or 'xfs_info'
            print(f"\n{Colors.HEADER}{Colors.BOLD}--- xfs_info {mnt} ---{Colors.ENDC}")
            res = run_command([xfs_info_bin, mnt], sudo=False, capture_output=True, check=False)
            if (res.stdout or "").strip():
                print(res.stdout.rstrip())
            if (res.stderr or "").strip():
                print(res.stderr.rstrip(), file=sys.stderr)
        else:
            log(f"Unsupported or unknown filesystem type '{fstype or 'unknown'}'.", 'ERROR')

        # Print fragmentation summary last.
        if frag_lines:
            print("")
            for line in frag_lines:
                print(line)

    def do_fsdiag(self, arg):
        '''(Deprecated) Alias for fshealth: fsdiag <name>

        This command was renamed to 'fshealth'. Prefer: fshealth <name>
        '''
        log("fsdiag was renamed to fshealth; running fshealth ...", 'WARN')
        return self.do_fshealth(arg)

    def do_scrub(self, arg):
        '''Scrub a mounted btrfs filesystem: scrub <name> [--no-watch]

        UNDER THE HOOD:
        1.  Validation: Verifies the disk is mapped and currently mounted.
        2.  Confirmation: Requires typing YES.
        3.  Execution: Runs 'sudo btrfs scrub start -B -R <mountpoint>'.
        4.  Recording: Stores a timestamp on the mountpoint root via:
              sudo setfattr -n user.last_scrub -v "<date>" <mountpoint>

        OPTIONAL:
        - default (watch mode): tails kernel logs during the scrub and prints checksum errors as they happen.
        - --no-watch: disable log tailing (quiet; you only get the scrub summary output).
          Btrfs typically logs logical addresses (and sometimes inode numbers); diskmgr will attempt
          to resolve those to paths via:
            btrfs inspect-internal logical-resolve <logical> <mountpoint>
            btrfs inspect-internal inode-resolve <ino> <mountpoint>
        '''
        parser = CmdArgumentParser(prog='scrub', add_help=False)
        parser.add_argument('name')
        g = parser.add_mutually_exclusive_group()
        g.add_argument('--watch', '-w', action='store_true', help='(default) Tail kernel logs for checksum errors during scrub')
        g.add_argument('--no-watch', action='store_true', help='Disable log tailing during scrub')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.name
        # Default to watch mode unless explicitly disabled.
        watch = (not bool(getattr(args, 'no_watch', False)))
        mnt = self.get_mountpoint(name)
        if not mnt:
            log(f"Disk '{name}' is not mounted. Run 'open {name}' first.", 'ERROR')
            return

        # Determine filesystem type.
        fstype = ""
        try:
            res_fs = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'FSTYPE'], check=False)
            fstype = (getattr(res_fs, 'stdout', '') or '').strip().lower()
        except Exception:
            fstype = ""

        if fstype != "btrfs":
            log(f"Scrub requires btrfs, but {mnt} is '{fstype or 'unknown'}'.", 'ERROR')
            return

        # Block scrub on the system root drive.
        try:
            res_src = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'SOURCE'], check=False)
            src = (getattr(res_src, 'stdout', '') or '').strip()
            if src and self._block_if_root_drive(os.path.realpath(src), f"scrub {name}"):
                return
        except Exception:
            pass

        print(f"Scrubbing: {Colors.BOLD}{mnt}{Colors.ENDC} (fstype=btrfs)")
        if not self.extensive_confirm(f"scrub {name} ({mnt})", destructive=False):
            return

        start_ts = time.time()

        btrfs_bin = _find_tool_or_common_paths('btrfs', [
            '/usr/sbin/btrfs',
            '/sbin/btrfs',
            '/usr/local/sbin/btrfs',
            '/usr/bin/btrfs',
            '/bin/btrfs',
        ])
        if btrfs_bin is None:
            log("btrfs not found. Install 'btrfs-progs' and retry.", 'ERROR')
            return

        setfattr_bin = _find_tool_or_common_paths('setfattr', [
            '/usr/bin/setfattr',
            '/bin/setfattr',
            '/usr/sbin/setfattr',
            '/sbin/setfattr',
        ])

        def _watch_btrfs_checksum_errors(mountpoint, device_tag=None):
            """
            Best-effort: follow kernel logs and print checksum errors with resolved paths, while scrub runs.
            """
            since = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cmd_j = ['journalctl', '-k', '-b', '-f', '--since', since, '--no-pager', '-o', 'short-precise']
            try:
                proc = popen_command(
                    cmd_j,
                    sudo=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except Exception as e:
                log(f"Failed to start journalctl watcher: {e}", 'WARN')
                return None, None

            stop = threading.Event()
            seen = set()

            def _resolve_paths(logical=None, ino=None):
                paths = []
                try:
                    if logical is not None:
                        res_l = run_command([btrfs_bin, 'inspect-internal', 'logical-resolve', str(logical), mountpoint],
                                            sudo=True, capture_output=True, check=False)
                        out_l = (getattr(res_l, 'stdout', '') or '').strip()
                        if out_l:
                            paths.extend(out_l.splitlines())
                    if ino is not None:
                        res_i = run_command([btrfs_bin, 'inspect-internal', 'inode-resolve', str(ino), mountpoint],
                                            sudo=True, capture_output=True, check=False)
                        out_i = (getattr(res_i, 'stdout', '') or '').strip()
                        if out_i:
                            paths.extend(out_i.splitlines())
                except Exception:
                    pass
                # Deduplicate while keeping order.
                uniq = []
                for p in paths:
                    p = p.strip()
                    if p and p not in uniq:
                        uniq.append(p)
                return uniq

            def _reader():
                # Typical kernel lines include "BTRFS ... csum failed" and/or "csum error at logical ...".
                re_logical = re.compile(r"\blogical\s+([0-9]+)\b", re.IGNORECASE)
                re_ino = re.compile(r"\bino\s+([0-9]+)\b", re.IGNORECASE)
                while not stop.is_set():
                    line = proc.stdout.readline() if proc.stdout else ""
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue
                    if "BTRFS" not in line and "btrfs" not in line:
                        continue
                    low = line.lower()
                    if "csum" not in low and "checksum" not in low:
                        continue
                    # If we know the device tag for this mount, prefer matching "(device <tag>)" to avoid unrelated noise.
                    if device_tag:
                        if re.search(rf"\(device\s+{re.escape(device_tag)}\)", line, re.IGNORECASE) is None:
                            continue

                    logical = None
                    ino = None
                    m1 = re_logical.search(line)
                    if m1:
                        try:
                            logical = int(m1.group(1), 10)
                        except ValueError:
                            logical = None
                    m2 = re_ino.search(line)
                    if m2:
                        try:
                            ino = int(m2.group(1), 10)
                        except ValueError:
                            ino = None

                    # Dedupe aggressively to reduce spam: only print first occurrence per (logical, ino).
                    # If we can't parse either, skip (no actionable mapping).
                    if logical is None and ino is None:
                        continue
                    key = (logical, ino)
                    if key in seen:
                        continue
                    seen.add(key)

                    paths = _resolve_paths(logical=logical, ino=ino) if (logical is not None or ino is not None) else []
                    parts = []
                    if logical is not None:
                        parts.append(f"logical={logical}")
                    if ino is not None:
                        parts.append(f"ino={ino}")
                    head = ", ".join(parts) if parts else "checksum error"
                    suffix = (" " + " ".join([f"[{p}]" for p in paths])) if paths else ""
                    print(f"{Colors.FAIL}{Colors.BOLD}BTRFS checksum error:{Colors.ENDC} {head}{suffix}", flush=True)

            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            return proc, stop

        try:
            watcher_proc = None
            watcher_stop = None
            if watch:
                import threading
                device_tag = None
                try:
                    res_src = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'SOURCE'], check=False)
                    src = (getattr(res_src, 'stdout', '') or '').strip()
                    if src:
                        device_tag = os.path.basename(os.path.realpath(src))
                except Exception:
                    device_tag = None

                watcher_proc, watcher_stop = _watch_btrfs_checksum_errors(mnt, device_tag=device_tag)
                if watcher_proc:
                    log("Watching kernel logs for BTRFS checksum errors during scrub (deduped).")

            # -B blocks until complete; -R reports stats.
            res_scrub = run_command([btrfs_bin, 'scrub', 'start', '-B', '-R', mnt], sudo=True, capture_output=True, check=False)

            if watch and watcher_stop is not None:
                watcher_stop.set()
                try:
                    if watcher_proc:
                        watcher_proc.terminate()
                except Exception:
                    pass

            if (getattr(res_scrub, 'stdout', '') or '').strip():
                print((getattr(res_scrub, 'stdout', '') or '').rstrip())
            if (getattr(res_scrub, 'stderr', '') or '').strip():
                print((getattr(res_scrub, 'stderr', '') or '').rstrip(), file=sys.stderr)

            if getattr(res_scrub, 'returncode', 1) != 0:
                log(f"btrfs scrub exited with status {getattr(res_scrub, 'returncode', 1)}; timestamp not recorded.", 'ERROR')
                return

            if setfattr_bin is None:
                log("setfattr not found. Install 'attr' and retry to record last_scrub.", 'WARN')
                return

            ds = ""
            try:
                res_date = run_command(['date'], capture_output=True, check=False)
                ds = (getattr(res_date, 'stdout', '') or '').strip()
            except Exception:
                ds = ""
            if not ds:
                ds = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')

            result = run_command([setfattr_bin, '-n', 'user.last_scrub', '-v', ds, mnt], sudo=True, check=False)
            if getattr(result, 'returncode', 1) != 0:
                log("Could not record user.last_scrub; scrub itself completed.", 'WARN')
            else:
                log(f"Recorded xattr: user.last_scrub={ds} on {mnt}")
        except Exception as e:
            log(f"Scrub failed: {e}", 'ERROR')
        finally:
            if 'watcher_stop' in locals() and watcher_stop is not None:
                watcher_stop.set()
            if 'watcher_proc' in locals() and watcher_proc is not None:
                try:
                    watcher_proc.terminate()
                    watcher_proc.wait(timeout=2)
                except Exception:
                    try:
                        watcher_proc.kill()
                    except Exception:
                        pass
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
