"""FilesystemCommands command implementations."""

from ..common import *
from ..shell_core import CmdArgumentParser


class FilesystemCommands:

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
            src = (getattr(res_src, 'stdout', '') or '').strip()
            if src and self._block_if_root_drive(os.path.realpath(src), f"defrag {name}"):
                return
        except Exception:
            pass

        # Determine filesystem type.
        fstype = ""
        try:
            res_fs = run_command(['findmnt', '-rn', '-M', mnt, '-o', 'FSTYPE'], check=False)
            fstype = (getattr(res_fs, 'stdout', '') or '').strip().lower()
        except Exception:
            fstype = ""

        if fstype not in ("ext4", "btrfs"):
            log(f"Defrag not supported for filesystem type '{fstype or 'unknown'}' at {mnt}.", 'ERROR')
            log("Supported: ext4 (e4defrag), btrfs (defragment + balance -dusage=50).", 'ERROR')
            return

        # Confirmation (not strictly destructive, but high-impact: rewrites lots of blocks/metadata).
        print(f"Defragmenting: {Colors.BOLD}{mnt}{Colors.ENDC} (fstype={fstype})")
        if not self.extensive_confirm(f"defrag {name} ({mnt})", destructive=False):
            return

        run_command(['sudo', '-v'])

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
            if fstype == "ext4":
                start_ts = time.time()
                if compress_mode:
                    log("--compress applies to btrfs only; ignoring for ext4.", 'WARN')
                if e4defrag_bin is None:
                    log("e4defrag not found. Install 'e2fsprogs' and retry.", 'ERROR')
                    return
                log(f"Running: {e4defrag_bin} {mnt}")
                run_command([e4defrag_bin, mnt], sudo=True, capture_output=False, check=False)
                print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            else:
                if btrfs_bin is None:
                    log("btrfs not found. Install 'btrfs-progs' and retry.", 'ERROR')
                    return
                start_ts_total = time.time()
                start_ts = time.time()
                log_path = f"/tmp/diskmgr_btrfs_defrag_{os.getpid()}_{int(start_ts)}.log"
                cmd = ['sudo', btrfs_bin, 'filesystem', 'defragment', '-r', '-v']
                if compress_mode:
                    cmd.append('-czstd')
                cmd.append(mnt)
                cmd_str = " ".join(cmd[1:])
                log(f"Running: {cmd_str}")
                log(f"Progress log: {log_path}")

                total_done = 0
                current_dir = "Scanning..."
                dir_counts = {}
                last_render = 0.0
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
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
                    log(f"btrfs defragment exited with status {rc_defrag}. Continuing with balance.", 'WARN')
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
                if getattr(res_balance_start, 'returncode', 0) not in (0,):
                    log(
                        f"btrfs balance start exited with status {getattr(res_balance_start, 'returncode', 0)}. "
                        f"Will still query balance status.",
                        'WARN'
                    )

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

            run_command([setfattr_bin, '-n', 'user.last_defrag', '-v', ds, mnt], sudo=True, check=False)
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
        except Exception:
            pass

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

        run_command(['sudo', '-v'])

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

        run_command(['sudo', '-v'])
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
            cmd_j = ['sudo', 'journalctl', '-k', '-b', '-f', '--since', since, '--no-pager', '-o', 'short-precise']
            try:
                proc = subprocess.Popen(cmd_j, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
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
                        if re.search(rf"\\(device\\s+{re.escape(device_tag)}\\)", line, re.IGNORECASE) is None:
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

            run_command([setfattr_bin, '-n', 'user.last_scrub', '-v', ds, mnt], sudo=True, check=False)
            log(f"Recorded xattr: user.last_scrub={ds} on {mnt}")
        except Exception as e:
            log(f"Scrub failed: {e}", 'ERROR')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")

    def do_convert(self, arg):
        '''Convert ext4 -> btrfs in place (no data copy): convert <name/id>

        Uses btrfs-convert on an UNMOUNTED ext4 filesystem.
        - Plain ext4 targets are supported directly.
        - If target is crypto_LUKS, diskmgr tries to resolve the open payload device
          (e.g. /dev/mapper/<name> or a crypt child) and convert that.
        '''
        parser = CmdArgumentParser(prog='convert', add_help=False)
        parser.add_argument('target', help='Mapping name or discovery ID (#N)')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        target_in = args.target
        resolved = None
        if target_in.startswith('#') and target_in[1:].isdigit():
            # For convert, allow IDs from the full list row cache (disk/part/crypt),
            # not only the map/unmapped discovery subset.
            rid = target_in[1:]
            resolved = self.id_cache.get(rid)
            if not resolved:
                # Backward-compatible fallback to discovery-ID resolver.
                resolved = self.resolve_target(target_in, allow_id=True)
                if not resolved:
                    log(f"Unknown discovery ID: '{target_in}'. Run 'list' first to refresh IDs.", 'ERROR')
                    return
        else:
            resolved = self.resolve_target(target_in, allow_id=True)
        if not resolved:
            log(f"Unknown target: '{target_in}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        real_target = os.path.realpath(resolved)
        if not os.path.exists(real_target):
            log(f"Target not found: {real_target}", 'ERROR')
            return

        self.mappings = read_luks_map()
        mapping_name = target_in if target_in in self.mappings else None

        convert_dev = None
        convert_hint = ""
        fstype = (_lsblk_fstype(real_target) or "").strip().lower()
        dev_type = (_lsblk_type(real_target) or "").strip().lower()

        def _lsblk_rows(dev_path):
            rows = []
            res = run_command(['lsblk', '-nr', '-o', 'NAME,TYPE,FSTYPE', dev_path], check=False)
            for raw in (getattr(res, 'stdout', '') or '').splitlines():
                line = raw.strip()
                if not line:
                    continue
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                nm = parts[0].strip()
                ty = parts[1].strip().lower()
                fs = parts[2].strip().lower() if len(parts) >= 3 else ""
                rows.append({'name': nm, 'type': ty, 'fstype': fs})
            return rows

        if fstype == 'ext4':
            convert_dev = real_target
            convert_hint = "direct ext4 target"
        elif fstype == 'crypto_luks':
            payload_candidates = []

            if mapping_name:
                mapper_path = f"/dev/mapper/{mapping_name}"
                if os.path.exists(mapper_path):
                    mapper_real = os.path.realpath(mapper_path)
                    mapper_fs = (_lsblk_fstype(mapper_real) or "").strip().lower()
                    if mapper_fs == 'ext4':
                        payload_candidates.append((mapper_real, f"/dev/mapper/{mapping_name}"))

            for row in _lsblk_rows(real_target):
                if row.get('type') != 'crypt':
                    continue
                fs = (row.get('fstype') or "").strip().lower()
                if fs != 'ext4':
                    continue
                cdev = os.path.realpath(f"/dev/{row['name']}")
                payload_candidates.append((cdev, f"/dev/{row['name']}"))

            uniq = {}
            for path, hint in payload_candidates:
                uniq[path] = hint
            payload_candidates = [(p, h) for p, h in uniq.items()]

            if len(payload_candidates) == 1:
                convert_dev, convert_hint = payload_candidates[0]
            elif len(payload_candidates) > 1:
                log("Target is LUKS with multiple open ext4 payload candidates; refusing ambiguous conversion.", 'ERROR')
                for p, h in payload_candidates:
                    log(f"  candidate: {h} ({p})", 'ERROR')
                log("Map and convert the exact payload target explicitly.", 'ERROR')
                return
            else:
                log("Target is crypto_LUKS but no open ext4 payload device was detected.", 'ERROR')
                log("Open the LUKS container and ensure its payload is ext4 before converting.", 'ERROR')
                return
        elif dev_type == 'disk':
            parts = _lsblk_partitions(real_target)
            ext_parts = [p for p in parts if (p.get('fstype') or '').strip().lower() == 'ext4']
            if len(ext_parts) == 1:
                convert_dev = os.path.realpath(f"/dev/{ext_parts[0]['name']}")
                convert_hint = f"single ext4 partition /dev/{ext_parts[0]['name']}"
            elif len(ext_parts) > 1:
                log("Disk has multiple ext4 partitions; map and convert a single partition target explicitly.", 'ERROR')
                return
            else:
                log(f"No ext4 filesystem found on {real_target}.", 'ERROR')
                return
        else:
            log(f"Unsupported target type/fstype for convert: type={dev_type or '-'}, fstype={fstype or '-'}", 'ERROR')
            return

        convert_dev = os.path.realpath(convert_dev)
        if not os.path.exists(convert_dev):
            log(f"Resolved convert target does not exist: {convert_dev}", 'ERROR')
            return

        if self._block_if_root_drive(convert_dev, f"convert {target_in}"):
            return

        # convert requires an unmounted source filesystem.
        targets = find_mount_targets(convert_dev)
        if targets:
            log(f"OPERATION BLOCKED: {convert_dev} is mounted at {', '.join(targets)}. Unmount/close it first.", 'ERROR')
            return

        final_fs = (_lsblk_fstype(convert_dev) or "").strip().lower()
        if final_fs != 'ext4':
            log(f"Resolved target is '{final_fs or 'unknown'}', but convert currently supports ext4 -> btrfs only.", 'ERROR')
            return

        print(f"Converting: {Colors.BOLD}{convert_dev}{Colors.ENDC} ({convert_hint})")
        if not self.extensive_confirm(f"convert {target_in} ({convert_dev})", destructive=False):
            return

        run_command(['sudo', '-v'])
        log_path = _cmd_log_open("convert") if (_CMD_LOG_FH is None) else _CMD_LOG_PATH
        if log_path:
            print(f"Log: {log_path}")
        start_ts = time.time()

        try:
            btrfs_convert_bin = _find_tool_or_common_paths('btrfs-convert', [
                '/usr/sbin/btrfs-convert',
                '/sbin/btrfs-convert',
                '/usr/local/sbin/btrfs-convert',
                '/usr/bin/btrfs-convert',
                '/bin/btrfs-convert',
            ])
            if btrfs_convert_bin is None:
                log("btrfs-convert not found. Install 'btrfs-progs' and retry.", 'ERROR')
                return

            e2fsck_bin = _find_tool_or_common_paths('e2fsck', [
                '/usr/sbin/e2fsck',
                '/sbin/e2fsck',
                '/usr/local/sbin/e2fsck',
                '/usr/bin/e2fsck',
                '/bin/e2fsck',
            ])
            if e2fsck_bin:
                log(f"Running pre-conversion fsck: {e2fsck_bin} -f -p {convert_dev}")
                res_ck = run_command([e2fsck_bin, '-f', '-p', convert_dev], sudo=True, capture_output=True, check=False)
                if (getattr(res_ck, 'stdout', '') or '').strip():
                    print((getattr(res_ck, 'stdout', '') or '').rstrip())
                if (getattr(res_ck, 'stderr', '') or '').strip():
                    print((getattr(res_ck, 'stderr', '') or '').rstrip(), file=sys.stderr)
                if getattr(res_ck, 'returncode', 0) not in (0, 1):
                    log(f"Pre-conversion fsck failed with exit code {res_ck.returncode}. Aborting conversion.", 'ERROR')
                    return

            log(f"Running in-place conversion (streaming output): {btrfs_convert_bin} {convert_dev}")
            _cmd_log_write(f"CMD: sudo {btrfs_convert_bin} {convert_dev}")
            proc = subprocess.Popen(
                ['sudo', btrfs_convert_bin, convert_dev],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        # Keep output live so conversions continue safely over flaky SSH.
                        print(line, end='', flush=True)
                        _cmd_log_write(line.rstrip("\n"))
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
            rc = proc.wait()
            _cmd_log_write(f"RC: {rc}")

            if rc != 0:
                log(f"Conversion failed with exit code {rc}.", 'ERROR')
                return

            log("Conversion complete: ext4 -> btrfs.")
            log("Rollback data is kept by btrfs-convert. Avoid btrfs balance if you may need rollback.", 'WARN')
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            _cmd_log_close()

    def do_format(self, arg):
        '''Format a superfloppy disk/partition volume: format <name/id> [options]

        Note: You must 'map' a disk first to give it a name before initializing it.

        NUANCES & SCOPE:
        1. Running format on a Partition (e.g., sda2)
           - Formats inside the existing partition boundary (plain or LUKS + payload FS).
           - Other partitions on the disk are untouched.

        2. Running format on a Whole Disk (e.g., sda)
           - Creates a superfloppy-style volume directly on the disk (plain or LUKS + payload FS).
           - Refuses if the disk already has a partition table (non-destructive policy).
           - To wipe partition metadata first, use: erase <name>

        Options:
          --fs <ext4|xfs|btrfs|fat32>   Filesystem type (default: ext4)
          --label <label>   Set a different internal filesystem label (other than <name>)
          --luks            Encrypt target first with LUKS2, then format payload filesystem.
                            PBKDF defaults: argon2id, memory=4GiB, threads=4, time=8.
          --detached-header [FILE]
                            Store LUKS header detached from the target device.
                            If FILE is omitted: ~/.local/share/diskmgr/<name>

        UNDER THE HOOD:
        1.  Safety: Refuses to run if anything is mounted on the target device tree.
        2.  Disk Type Policy:
            - If target is a whole disk, it must be unpartitioned (no GPT/MBR table present).
            - If target is a partition, format is applied directly within that partition.
        3.  LUKS Format (only when --luks is used):
            - Uses 'passgen' to generate a master key.
            - Runs 'cryptsetup luksFormat' with LUKS2 encryption
              (and --header FILE when --detached-header is used).
            - Opens the container as /dev/mapper/<name>.
        4.  Filesystem:
            - Plain mode (default): formats target directly with ext4, xfs, btrfs, or fat32.
            - --luks mode: formats the opened mapper payload with ext4, xfs, btrfs, or fat32.
            - (ext4 only): Reclaims the 5% reserved space for root using 'tune2fs -m 0'.
        5.  Persistence: Adds the new disk's PDP to diskmap.tsv automatically (best-effort).

        Note: This is a DESTRUCTIVE operation. You must type both the resolved device path and persistent path to proceed.
        '''
        parser = CmdArgumentParser(prog='format', add_help=False)
        parser.add_argument('args', nargs=1, help='<name>')
        parser.add_argument('--fs', default='ext4', choices=['ext4', 'xfs', 'btrfs', 'fat32'])
        parser.add_argument('--label', help='Filesystem label')
        parser.add_argument('--luks', action='store_true', help='Encrypt target with LUKS2 before mkfs')
        parser.add_argument(
            '--detached-header',
            nargs='?',
            const='__DEFAULT_DETACHED_HEADER__',
            metavar='[FILE]',
            help='Use detached LUKS header file (default: ~/.local/share/diskmgr/<name>)'
        )

        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        if args.detached_header and not args.luks:
            log("--detached-header requires --luks.", 'ERROR')
            return

        if args.fs == 'btrfs':
            mkfs_btrfs = _find_tool_or_common_paths('mkfs.btrfs', [
                '/usr/sbin/mkfs.btrfs',
                '/sbin/mkfs.btrfs',
                '/usr/local/sbin/mkfs.btrfs',
                '/usr/bin/mkfs.btrfs',
                '/bin/mkfs.btrfs',
            ])
            if mkfs_btrfs is None:
                log("btrfs not found. Install 'btrfs-progs' and retry.", 'ERROR')
                return
        elif args.fs == 'ext4':
            mkfs_ext4 = _find_tool_or_common_paths('mkfs.ext4', [
                '/usr/sbin/mkfs.ext4',
                '/sbin/mkfs.ext4',
                '/usr/local/sbin/mkfs.ext4',
                '/usr/bin/mkfs.ext4',
                '/bin/mkfs.ext4',
            ])
            if mkfs_ext4 is None:
                log("mkfs.ext4 not found. Install 'e2fsprogs' and retry.", 'ERROR')
                return
        elif args.fs == 'fat32':
            mkfs_vfat = (
                _find_tool_or_common_paths('mkfs.vfat', [
                    '/usr/sbin/mkfs.vfat',
                    '/sbin/mkfs.vfat',
                    '/usr/local/sbin/mkfs.vfat',
                    '/usr/bin/mkfs.vfat',
                    '/bin/mkfs.vfat',
                ]) or
                _find_tool_or_common_paths('mkfs.fat', [
                    '/usr/sbin/mkfs.fat',
                    '/sbin/mkfs.fat',
                    '/usr/local/sbin/mkfs.fat',
                    '/usr/bin/mkfs.fat',
                    '/bin/mkfs.fat',
                ])
            )
            if mkfs_vfat is None:
                log("mkfs.vfat/mkfs.fat not found. Install 'dosfstools' and retry.", 'ERROR')
                return

        name = args.args[0]
        input_token = name
        clean_input = input_token.strip('[]')
        input_is_id = (
            (clean_input.startswith('#') and clean_input[1:].isdigit()) or
            (clean_input.startswith('U') and clean_input[1:].isdigit())
        )
        luks_memory_kib = LUKS_PBKDF_DEFAULT_MEMORY_KIB
        target = self.resolve_target(name, allow_id=True)
        if not target:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        # Operation name used for mapper name and auto-added mapping key.
        # Never let discovery ID tokens like '#3' become persistent names.
        op_name = input_token
        if input_is_id:
            self.mappings = read_luks_map()
            existing_name = None
            for n, p in (self.mappings or {}).items():
                try:
                    if os.path.realpath(p) == os.path.realpath(target):
                        existing_name = n
                        break
                except Exception:
                    continue

            if existing_name:
                op_name = existing_name
            elif args.label:
                cand = str(args.label).strip()
                cand_clean = cand.strip('[]')
                if (
                    not cand or
                    (cand_clean.startswith('#') and cand_clean[1:].isdigit()) or
                    (cand_clean.startswith('U') and cand_clean[1:].isdigit()) or
                    cand_clean.isdigit()
                ):
                    log("Invalid --label for ID-based format. Use a non-ID name (e.g. 1a).", 'ERROR')
                    return
                op_name = cand
            else:
                log("ID-based format requires a stable name for LUKS mapper/mapping.", 'ERROR')
                log("Run: map #N <name> first, or pass --label <name>.", 'ERROR')
                return

        detached_header_path = None
        if args.luks and args.detached_header:
            raw_detached = str(args.detached_header or '').strip()
            if raw_detached == '__DEFAULT_DETACHED_HEADER__':
                detached_header_path = str(LUKS_HEADER_BACKUP_DIR / op_name)
            else:
                detached_header_path = os.path.abspath(os.path.expanduser(raw_detached))

            try:
                dh_parent = os.path.dirname(detached_header_path)
                if dh_parent:
                    os.makedirs(dh_parent, exist_ok=True)
            except Exception as e:
                log(f"Failed to prepare detached-header directory for {detached_header_path}: {e}", 'ERROR')
                return

            if os.path.exists(detached_header_path):
                log(f"Detached header path already exists: {detached_header_path}", 'ERROR')
                log("Refusing to overwrite existing detached header file. Choose another path or move/remove the old file.", 'ERROR')
                return

        # Wait/Verify target existence
        real_target = os.path.realpath(target)
        if not os.path.exists(real_target):
            log(f"Target device not found: {target} (resolved: {real_target})", 'ERROR')
            return

        if self.is_root_disk(real_target):
            log(f"OPERATION BLOCKED: {real_target} is part of the system root drive!", 'ERROR')
            return

        log(f"Target: {real_target}")
        log(f"Name: {op_name}")
        if op_name != input_token:
            log(f"Input token '{input_token}' resolved to operation name '{op_name}'.")

        # Safety checks
        if not self.extensive_confirm(f"{input_token} ({real_target})"):
            return

        run_command(['sudo', '-v'])

        # Refuse if anything on the target device tree is mounted.
        try:
            res_m = run_command(['lsblk', '-nr', '-o', 'MOUNTPOINT', real_target], check=False)
            mounts = [ln.strip() for ln in (getattr(res_m, 'stdout', '') or '').splitlines() if ln.strip()]
            if mounts:
                log(f"OPERATION BLOCKED: {real_target} has mounted filesystems ({', '.join(mounts)}). Unmount/close it first.", 'ERROR')
                return
        except Exception:
            pass

        crypt_target = real_target
        dev_type = _lsblk_type(real_target)

        if dev_type not in ('disk', 'part'):
            log(f"Unsupported target type for format: {dev_type or 'unknown'}. Map a disk or partition.", 'ERROR')
            return

        # Superfloppy mode only: whole disks must be unpartitioned.
        if dev_type == 'disk':
            try:
                res_pt = run_command(['lsblk', '-no', 'PTTYPE', real_target], check=False)
                pttype = (getattr(res_pt, 'stdout', '') or '').strip().lower()
            except Exception:
                pttype = ""
            if pttype:
                log(f"OPERATION BLOCKED: {real_target} is partitioned ({pttype}). Refusing to format a whole-disk superfloppy over an existing partition table.", 'ERROR')
                log("Use erase <name> first to wipe partition metadata, then run format again.", 'ERROR')
                return
            # Ensure stale child partitions from prior layouts are dropped before
            # whole-disk superfloppy/LUKS formatting.
            self._refresh_kernel_partition_state(real_target, drop_partitions=True)

        if args.luks:
            res_is_luks = run_command(['cryptsetup', 'isLuks', crypt_target], sudo=True, check=False)
            if getattr(res_is_luks, 'returncode', 1) == 0:
                log(f"OPERATION BLOCKED: {crypt_target} is already a LUKS container. Refusing to run luksFormat again.", 'ERROR')
                return

        if args.luks:
            # LUKS Format
            log(f"Formatting LUKS on {crypt_target}...")
            log(f"LUKS PBKDF: memory={LUKS_PBKDF_DEFAULT_MEMORY_LABEL} ({luks_memory_kib:,} KiB), argon2id, threads={LUKS_PBKDF_DEFAULT_THREADS}, time={LUKS_PBKDF_DEFAULT_TIME}")
            if detached_header_path:
                log(f"Using detached LUKS header file: {detached_header_path}")
            pg_cmd = subprocess.Popen([PASSGEN_BIN], stdout=subprocess.PIPE, text=True)
            try:
                luks_format_cmd = [
                    'cryptsetup', 'luksFormat',
                    '--type', 'luks2',
                    '--batch-mode',
                    '--pbkdf', 'argon2id',
                    '--pbkdf-memory', str(luks_memory_kib),
                    '--pbkdf-parallel', str(LUKS_PBKDF_DEFAULT_THREADS),
                    '--pbkdf-force-iterations', str(LUKS_PBKDF_DEFAULT_TIME),
                    '--key-file', '-',
                ]
                if detached_header_path:
                    luks_format_cmd.extend(['--header', detached_header_path])
                luks_format_cmd.append(crypt_target)
                run_command(
                    luks_format_cmd,
                    input_str=pg_cmd.communicate()[0],
                    sudo=True,
                    check=True
                )
            except Exception as e:
                log(f"LUKS Format failed: {e}", 'ERROR')
                return

            # Open
            log("Opening new LUKS volume...")
            pg_cmd = subprocess.Popen([PASSGEN_BIN], stdout=subprocess.PIPE, text=True)
            open_cmd = ['cryptsetup', 'open', '--key-file', '-']
            if detached_header_path:
                open_cmd.extend(['--header', detached_header_path])
            open_cmd.extend([crypt_target, op_name])
            run_command(
                open_cmd,
                input_str=pg_cmd.communicate()[0],
                sudo=True,
                check=True
            )
            if detached_header_path:
                user, _group = self._invoking_user_group()
                run_command(['chown', f'{user}:{user}', detached_header_path], sudo=True, check=False)
                run_command(['chmod', '600', detached_header_path], sudo=True, check=False)
            fs_target = f"/dev/mapper/{op_name}"
        else:
            log("Using plain format mode (no LUKS).")
            fs_target = crypt_target

        # Mkfs
        label = args.label if args.label else op_name
        log(f"Formatting filesystem {args.fs} (label={label}) on {fs_target}...")

        if args.fs == 'ext4':
            mkfs_ext4 = _find_tool_or_common_paths('mkfs.ext4', [
                '/usr/sbin/mkfs.ext4',
                '/sbin/mkfs.ext4',
                '/usr/local/sbin/mkfs.ext4',
                '/usr/bin/mkfs.ext4',
                '/bin/mkfs.ext4',
            ]) or 'mkfs.ext4'
            run_command([mkfs_ext4, '-F', '-L', label, fs_target], sudo=True)
            log("Reclaiming 5% reserved space (tune2fs -m 0)...")
            run_command(['tune2fs', '-m', '0', fs_target], sudo=True)
        elif args.fs == 'xfs':
            run_command(['mkfs.xfs', '-f', '-L', label, fs_target], sudo=True)
        elif args.fs == 'btrfs':
            mkfs_btrfs = _find_tool_or_common_paths('mkfs.btrfs', [
                '/usr/sbin/mkfs.btrfs',
                '/sbin/mkfs.btrfs',
                '/usr/local/sbin/mkfs.btrfs',
            ]) or 'mkfs.btrfs'
            # -f required because we just wipefs'ed; still safer to be explicit.
            run_command([mkfs_btrfs, '-f', '-L', label, fs_target], sudo=True)
        elif args.fs == 'fat32':
            mkfs_vfat = (
                _find_tool_or_common_paths('mkfs.vfat', [
                    '/usr/sbin/mkfs.vfat',
                    '/sbin/mkfs.vfat',
                    '/usr/local/sbin/mkfs.vfat',
                    '/usr/bin/mkfs.vfat',
                    '/bin/mkfs.vfat',
                ]) or
                _find_tool_or_common_paths('mkfs.fat', [
                    '/usr/sbin/mkfs.fat',
                    '/sbin/mkfs.fat',
                    '/usr/local/sbin/mkfs.fat',
                    '/usr/bin/mkfs.fat',
                    '/bin/mkfs.fat',
                ]) or
                'mkfs.vfat'
            )
            run_command([mkfs_vfat, '-F', '32', '-n', label, fs_target], sudo=True)

        # Reconcile kernel partition view again after whole-disk formatting so
        # list output does not retain stale child partition nodes.
        if dev_type == 'disk':
            self._refresh_kernel_partition_state(real_target, drop_partitions=True)

        # Mount (prefer /etc/fstab mountpoint when an entry exists for this filesystem).
        fallback_mountpoint = f"/media/{os.environ.get('USER', 'root')}/{label}"
        mountpoint, use_fstab_mount, fstab_entry = self._select_mountpoint_for_device(
            fs_target,
            fallback_mountpoint,
            preferred_label=label
        )
        if use_fstab_mount:
            log(f"Using fstab mount after format: {fstab_entry['spec']} -> {mountpoint}")

        # Safety Check: Is this mountpoint already in use by another device?
        res_check = run_command(['findmnt', '-rn', '-M', mountpoint], check=False)
        if res_check.returncode == 0:
            res_src = run_command(['findmnt', '-rn', '-M', mountpoint, '-o', 'SOURCE'], capture_output=True)
            current_src = os.path.realpath(res_src.stdout.strip())
            if current_src != os.path.realpath(fs_target):
                log(f"MOUNT BLOCKED: Path {mountpoint} is already in use by {current_src}.", 'ERROR')
                log("Disk was initialized successfully, but could not be mounted at the preferred path.", 'WARN')
                return

        self._mount_device(fs_target, mountpoint, use_fstab=use_fstab_mount)
        self._chown_new_filesystem_root(mountpoint)

        # Update map if needed
        self.mappings = read_luks_map()
        if op_name not in self.mappings:
            stable_path = crypt_target
            # Try to find by-id
            pdp = self.find_persistent_path(os.path.basename(crypt_target))
            if pdp != '-':
                stable_path = pdp

            self.mappings[op_name] = stable_path
            save_luks_map(self.mappings)
            log(f"Added mapping: {op_name} -> {stable_path}")

        log("Disk initialization complete.")
