"""HealthCommands command implementations."""

import argparse
import cmd
import os
import re
import shlex
import sys
import time
from ..runtime import Colors, _cmd_log_close, _cmd_log_open, _cmd_log_write, _find_tool_or_common_paths, _first_int_from_text, _fmt_hms, log, run_command
from ..devices import _sysfs_block_name, _sysfs_to_parent_disk_name
from ..smart import _decode_seagate_command_timeout, _decode_seagate_hi16_lo32, _parse_smart_attr_raw, _parse_smart_attr_row, _parse_smart_error_log_count, _parse_smart_last_error_poh, _parse_smart_long_selftest_failures, _smartctl_looks_seagate
from ..shell_core import CmdArgumentParser


class HealthCommands:

    def do_health(self, arg):
        '''Display SMART health for a mapped disk: health <name/id>

        Runs smartctl against the underlying DISK device for the mapping.
        - If the mapping points to a partition, diskmgr automatically targets the parent disk.
        - If the disk transport is USB and the device is /dev/sdX, diskmgr uses:
              smartctl -d sat -x /dev/sdX
          (common for USB-SATA bridges).
        '''
        args = arg.split()
        if len(args) != 1:
            log("Usage: health <name/id>", 'ERROR')
            return

        name = args[0]
        src = self.resolve_target(name, allow_id=True)
        if not src:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        smartctl_bin = _find_tool_or_common_paths('smartctl', [
            '/usr/sbin/smartctl',
            '/sbin/smartctl',
            '/usr/local/sbin/smartctl',
        ])
        if smartctl_bin is None:
            log("smartctl not found. Install 'smartmontools' and retry.", 'ERROR')
            return

        mapped_dev = os.path.realpath(src)

        # Always query SMART on the underlying whole-disk device (SMART is not partition-scoped).
        disk_dev = mapped_dev
        try:
            mapped_name = _sysfs_block_name(mapped_dev)
            disk_name = _sysfs_to_parent_disk_name(mapped_name)
            candidate = os.path.realpath(f"/dev/{disk_name}")
            if os.path.exists(candidate):
                disk_dev = candidate
        except Exception:
            # Fall back to the mapped device (best-effort) if sysfs probing fails.
            disk_dev = mapped_dev

        tran = ""
        try:
            res_tran = run_command(['lsblk', '-no', 'TRAN', disk_dev], check=False)
            tran = (getattr(res_tran, 'stdout', '') or '').strip().lower()
        except Exception:
            tran = ""

        use_sat = (tran == 'usb' and os.path.basename(disk_dev).startswith('sd'))
        cmd = [smartctl_bin, '-x', disk_dev]
        if use_sat:
            cmd = [smartctl_bin, '-d', 'sat', '-x', disk_dev]

        res = run_command(cmd, sudo=True, capture_output=True, check=False)
        out = (res.stdout or "")
        err = (res.stderr or "")

        # If SAT probing fails on a USB bridge, retry without -d sat as a best-effort fallback.
        if use_sat and res.returncode != 0 and ("Unknown USB bridge" in (out + err) or "Please specify device type" in (out + err)):
            log("smartctl -d sat failed on this USB bridge; retrying without '-d sat'...", 'WARN')
            cmd = [smartctl_bin, '-x', disk_dev]
            res = run_command(cmd, sudo=True, capture_output=True, check=False)
            out = (res.stdout or "")
            err = (res.stderr or "")

        def _find_first(patterns):
            for p in patterns:
                m = re.search(p, out, re.MULTILINE)
                if m:
                    return m.group(1).strip()
            return None

        overall = _find_first([
            r"^SMART overall-health self-assessment test result:\s*(.+)$",
            r"^SMART Health Status:\s*(.+)$",
        ])
        temp = _find_first([
            r"^Current Temperature:\s*([0-9]+)\s*C",
            r"^Current Drive Temperature:\s*([0-9]+)\s*C",
            r"^Temperature:\s*([0-9]+)\s*C",
            r"^\s*194\s+Temperature_Celsius\s+.*\s([0-9]+)\s*$",
            r"^\s*190\s+Airflow_Temperature_Cel\s+.*\s([0-9]+)\s*$",
        ])
        poh = _find_first([
            r"^Power On Hours:\s*([0-9,]+)",
            r"^\s*9\s+Power_On_Hours\s+.*\s([0-9]+)\s*$",
        ])
        realloc = _find_first([r"^\s*5\s+Reallocated_Sector_Ct\s+.*\s([0-9]+)\s*$"])
        pending = _find_first([r"^\s*197\s+Current_Pending_Sector\s+.*\s([0-9]+)\s*$"])
        offline_unc = _find_first([r"^\s*198\s+Offline_Uncorrectable\s+.*\s([0-9]+)\s*$"])

        # Prefer SCT lifetime min/max (from -x) over attribute 194 Min/Max.
        temp_life_min = None
        temp_life_max = None
        temp_life_src = None
        m_sct = re.search(r"(?im)^\s*Lifetime\s+Min/Max\s+Temperature:\s*([0-9]+)\s*/\s*([0-9]+)\s*Celsius\s*$", out)
        if m_sct:
            temp_life_min = m_sct.group(1)
            temp_life_max = m_sct.group(2)
            temp_life_src = "SCT"
        else:
            m_194 = re.search(r"(?im)^\s*194\s+Temperature_Celsius\s+.*\(\s*Min/Max\s*([0-9]+)\s*/\s*([0-9]+)\s*\)\s*$", out)
            if m_194:
                temp_life_min = m_194.group(1)
                temp_life_max = m_194.group(2)
                temp_life_src = "attr194"

        mode = "-d sat -x" if ('-d' in cmd) else "-x"
        extra = f"{tran}" if tran else "unknown transport"
        print(f"\n{Colors.HEADER}{Colors.BOLD}=== SMART health: {name} ({extra}, smartctl {mode}) ==={Colors.ENDC}")
        print(f"{Colors.BOLD}Mapped device:{Colors.ENDC} {mapped_dev}")
        if mapped_dev != disk_dev:
            print(f"{Colors.BOLD}SMART queried on:{Colors.ENDC} {disk_dev} (SMART is disk-level, not partition-level)")
        else:
            print(f"{Colors.BOLD}SMART queried on:{Colors.ENDC} {disk_dev}")

        # NVMe drives don't expose ATA SMART attributes/logs in the same format. For NVMe, just show smartctl output.
        is_nvme = (
            os.path.basename(disk_dev).startswith('nvme')
            or ('SMART/Health Information (NVMe Log' in out)
            or ('NVMe Version' in out)
        )
        if is_nvme:
            def _nv_field(label_patterns):
                for p in label_patterns:
                    m = re.search(p, out, re.MULTILINE)
                    if m:
                        return m.group(1).strip()
                return None

            def _nv_int(v, base=10):
                if v is None:
                    return None
                s = str(v).strip()
                try:
                    if base == 16:
                        return int(s, 16)
                    return int(s.replace(',', ''), 10)
                except Exception:
                    return _first_int_from_text(s)

            def _ok_watch_bad(val, ok_when=None, watch_when=None, bad_when=None, default_color=Colors.OKGREEN):
                if val is None:
                    return "-"
                text = str(val)
                ival = _nv_int(val)
                if ival is None:
                    return text
                if bad_when is not None and bad_when(ival):
                    return f"{Colors.FAIL}{text}{Colors.ENDC}"
                if watch_when is not None and watch_when(ival):
                    return f"{Colors.WARNING}{text}{Colors.ENDC}"
                if ok_when is not None:
                    if ok_when(ival):
                        return f"{Colors.OKGREEN}{text}{Colors.ENDC}"
                    return f"{Colors.WARNING}{text}{Colors.ENDC}"
                return f"{default_color}{text}{Colors.ENDC}"

            nv_critical_warning_raw = _nv_field([r"^Critical Warning:\s*(0x[0-9a-fA-F]+|[0-9]+)\s*$"])
            nv_media_err_raw = _nv_field([r"^Media and Data Integrity Errors:\s*([0-9,]+)\s*$"])
            nv_avail_spare_raw = _nv_field([r"^Available Spare:\s*([0-9]+)%\s*$", r"^Available Spare:\s*([0-9,]+)\s*$"])
            nv_pct_used_raw = _nv_field([r"^Percentage Used:\s*([0-9]+)%\s*$", r"^Percentage Used:\s*([0-9,]+)\s*$"])
            nv_errlog_raw = _nv_field([r"^Error Information Log Entries:\s*([0-9,]+)\s*$"])
            nv_unsafe_raw = _nv_field([r"^Unsafe Shutdowns:\s*([0-9,]+)\s*$"])
            nv_temp_crit_time_raw = _nv_field([r"^Critical Comp\.\s+Temperature Time:\s*([0-9,]+)\s*$"])
            nv_temp_warn_time_raw = _nv_field([r"^Warning\s+Comp\.\s+Temperature Time:\s*([0-9,]+)\s*$"])
            nv_temp_raw = _nv_field([r"^Temperature:\s*([0-9]+)\s+Celsius\s*$", r"^Temperature:\s*([0-9]+)\s*C\s*$"])
            nv_duw_raw = _nv_field([r"^Data Units Written:\s*(.+)$"])
            nv_poh_raw = _nv_field([r"^Power On Hours:\s*([0-9,]+)\s*$"])
            nv_pcycles_raw = _nv_field([r"^Power Cycles:\s*([0-9,]+)\s*$"])
            nv_dur_raw = _nv_field([r"^Data Units Read:\s*(.+)$"])
            nv_busy_raw = _nv_field([r"^Controller Busy Time:\s*([0-9,]+)\s*$"])

            nv_unsafe_int = _nv_int(nv_unsafe_raw)
            nv_pcycles_int = _nv_int(nv_pcycles_raw)

            unsafe_ratio = None
            if nv_unsafe_int is not None and nv_pcycles_int not in (None, 0):
                unsafe_ratio = float(nv_unsafe_int) / float(nv_pcycles_int)

            print(f"\n{Colors.BOLD}NVMe attributes ranked by severity (Tier 1 -> Tier 4):{Colors.ENDC}")
            print(f"\n{Colors.BOLD}Tier 1: Replace Immediately Zone{Colors.ENDC}")
            cw_text = nv_critical_warning_raw if nv_critical_warning_raw is not None else "-"
            print(f"  Critical Warning: {_ok_watch_bad(cw_text, ok_when=lambda v: v == 0, bad_when=lambda v: v != 0)} - Master alarm; non-zero indicates controller-level fault/overheat/read-only condition.")
            me_text = nv_media_err_raw if nv_media_err_raw is not None else "-"
            print(f"  Media and Data Integrity Errors: {_ok_watch_bad(me_text, ok_when=lambda v: v == 0, bad_when=lambda v: v > 0)} - Counts real data-integrity failures.")
            as_text = f"{nv_avail_spare_raw}%" if nv_avail_spare_raw is not None else "-"
            print(f"  Available Spare: {_ok_watch_bad(as_text, ok_when=lambda v: v >= 10, watch_when=lambda v: 10 <= v < 20, bad_when=lambda v: v < 10)} - Spare block reserve; low values are critical.")

            print(f"\n{Colors.BOLD}Tier 2: Wear and Tear Zone{Colors.ENDC}")
            pu_text = f"{nv_pct_used_raw}%" if nv_pct_used_raw is not None else "-"
            print(f"  Percentage Used: {_ok_watch_bad(pu_text, watch_when=lambda v: 70 <= v < 100, bad_when=lambda v: v >= 100)} - Wear-life consumed.")
            el_text = nv_errlog_raw if nv_errlog_raw is not None else "-"
            print(f"  Error Information Log Entries: {_ok_watch_bad(el_text, ok_when=lambda v: v == 0, watch_when=lambda v: 0 < v < 100, bad_when=lambda v: v >= 100)} - Internal error history (controller stability signal).")
            us_text = nv_unsafe_raw if nv_unsafe_raw is not None else "-"
            if unsafe_ratio is not None:
                us_text = f"{us_text} ({unsafe_ratio*100:.1f}% of power cycles)"
            us_col = Colors.OKGREEN
            if nv_unsafe_int is not None and nv_unsafe_int > 0:
                us_col = Colors.WARNING
            if unsafe_ratio is not None and unsafe_ratio >= 0.5:
                us_col = Colors.FAIL
            print(f"  Unsafe Shutdowns: {us_col}{us_text}{Colors.ENDC} - High counts increase corruption risk.")

            print(f"\n{Colors.BOLD}Tier 3: Abuse History Zone{Colors.ENDC}")
            ctt_text = nv_temp_crit_time_raw if nv_temp_crit_time_raw is not None else "-"
            print(f"  Critical Composite Temperature Time: {_ok_watch_bad(ctt_text, ok_when=lambda v: v == 0, bad_when=lambda v: v > 0)} - Time spent at critical thermal level.")
            wtt_text = nv_temp_warn_time_raw if nv_temp_warn_time_raw is not None else "-"
            print(f"  Warning Composite Temperature Time: {_ok_watch_bad(wtt_text, ok_when=lambda v: v == 0, watch_when=lambda v: v > 0)} - Time spent in warning thermal range.")
            t_text = f"{nv_temp_raw} C" if nv_temp_raw is not None else "-"
            print(f"  Temperature: {_ok_watch_bad(t_text, ok_when=lambda v: v < 70, watch_when=lambda v: 70 <= v < 80, bad_when=lambda v: v >= 80)} - Current temperature status.")

            print(f"\n{Colors.BOLD}Tier 4: Biography Zone (Informational){Colors.ENDC}")
            print(f"  Data Units Written: {Colors.OKCYAN}{nv_duw_raw or '-'}{Colors.ENDC} - Total data written (wear context).")
            print(f"  Power On Hours: {Colors.OKCYAN}{nv_poh_raw or '-'}{Colors.ENDC} - Device age in hours.")
            print(f"  Power Cycles: {Colors.OKCYAN}{nv_pcycles_raw or '-'}{Colors.ENDC} - Number of power cycles.")
            print(f"  Data Units Read: {Colors.OKCYAN}{nv_dur_raw or '-'}{Colors.ENDC} - Total data read (informational).")
            print(f"  Controller Busy Time: {Colors.OKCYAN}{nv_busy_raw or '-'}{Colors.ENDC} - Controller active time (informational).")

            print("")
            if out.strip():
                print(out.rstrip())
            if err.strip():
                print(err.rstrip(), file=sys.stderr)

            # Also show nvme-cli smart-log output (often includes vendor-specific counters smartctl doesn't).
            nvme_bin = _find_tool_or_common_paths('nvme', [
                '/usr/sbin/nvme',
                '/sbin/nvme',
                '/usr/local/sbin/nvme',
                '/usr/bin/nvme',
                '/bin/nvme',
            ])
            if nvme_bin is None:
                log("nvme tool not found. Install 'nvme-cli' to show 'nvme smart-log'.", 'WARN')
            else:
                # Prefer the controller device (/dev/nvmeX) when available; fall back to the namespace node.
                nvme_target = disk_dev
                m = re.match(r"^(/dev/nvme[0-9]+)n[0-9]+$", disk_dev)
                if m:
                    ctrl = m.group(1)
                    if os.path.exists(ctrl):
                        nvme_target = ctrl

                print(f"\n{Colors.HEADER}{Colors.BOLD}--- nvme smart-log {nvme_target} ---{Colors.ENDC}")
                res_nv = run_command([nvme_bin, 'smart-log', nvme_target], sudo=True, capture_output=True, check=False)
                nv_out = (res_nv.stdout or "").rstrip()
                nv_err = (res_nv.stderr or "").rstrip()
                if nv_out:
                    print(nv_out)
                if nv_err:
                    print(nv_err, file=sys.stderr)
                if getattr(res_nv, 'returncode', 0) != 0:
                    log(f"nvme smart-log exit status: {res_nv.returncode}", 'WARN')

            if getattr(res, 'returncode', 0) != 0:
                log(f"smartctl exit status: {res.returncode} (non-zero may indicate SMART warnings).", 'WARN')
            return

        def _print_smartctl_info_block():
            """
            Print the smartctl prolog + information section, but not the full attribute tables/logs.
            """
            if not out:
                return
            lines = str(out).splitlines()
            if not lines:
                return
            info_idx = None
            read_idx = None
            for i, line in enumerate(lines):
                if line.strip() == "=== START OF INFORMATION SECTION ===":
                    info_idx = i
                if line.strip() == "=== START OF READ SMART DATA SECTION ===":
                    read_idx = i
                    break
            if info_idx is None:
                return
            end = read_idx if read_idx is not None else len(lines)
            # Print from the beginning through the end of the information section header block.
            for line in lines[:end]:
                print(line)

        def _print_smartctl_remaining_block():
            """
            Print the remainder of the smartctl output (everything after the information section),
            so the user can still see the attribute tables and logs without duplicating the header/info.
            """
            if not out:
                return
            lines = str(out).splitlines()
            if not lines:
                return
            read_idx = None
            for i, line in enumerate(lines):
                if line.strip() == "=== START OF READ SMART DATA SECTION ===":
                    read_idx = i
                    break
            if read_idx is None:
                # Fall back to the entire output if we can't locate the split point.
                print(out.rstrip())
                return
            tail = lines[read_idx:]
            if not tail:
                return
            print(f"{Colors.HEADER}{Colors.BOLD}--- smartctl details ---{Colors.ENDC}")
            for line in tail:
                print(line)

        _print_smartctl_info_block()

        # Time since last SMART error (if the drive has an ATA SMART Error Log).
        poh_row = _parse_smart_attr_row(out, 9)
        cur_poh_h = _first_int_from_text((poh_row or {}).get("raw")) or _first_int_from_text(poh)
        last_err_no, last_err_poh_h = _parse_smart_last_error_poh(out)
        errlog_cnt = _parse_smart_error_log_count(out)
        if last_err_poh_h is not None:
            delta_h = cur_poh_h - last_err_poh_h if (cur_poh_h is not None) else None
            if delta_h is not None and delta_h >= 0:
                delta_days = delta_h / 24.0
                # Color: green if very old, yellow if recent.
                if delta_h < 72:
                    delta_disp = f"{Colors.WARNING}{delta_h}h{Colors.ENDC} (~{delta_days:.1f} days)"
                else:
                    delta_disp = f"{Colors.OKGREEN}{delta_h}h{Colors.ENDC} (~{delta_days:.1f} days)"
                eno = f"Error {last_err_no}" if last_err_no is not None else "Last error"
                print(f"\n{Colors.BOLD}Time since last SMART error:{Colors.ENDC} {delta_disp} ({eno} at POH {last_err_poh_h}h)\n")
            else:
                eno = f"Error {last_err_no}" if last_err_no is not None else "Last error"
                print(f"\n{Colors.BOLD}Last SMART error logged at:{Colors.ENDC} {eno} at POH {last_err_poh_h}h\n")
        elif errlog_cnt == 0:
            print(f"\n{Colors.BOLD}SMART Error Log:{Colors.ENDC} {Colors.OKGREEN}No Errors Logged{Colors.ENDC}\n")

        # Key HDD indicators (earliest -> most severe), with current values if present.
        crc199 = _parse_smart_attr_raw(out, 199)
        cmd188 = _parse_smart_attr_raw(out, 188)
        errlog = errlog_cnt
        pend197 = _parse_smart_attr_raw(out, 197)
        off198 = _parse_smart_attr_raw(out, 198)
        rep187 = _parse_smart_attr_raw(out, 187)
        realloc5 = _parse_smart_attr_raw(out, 5)
        relocev196 = _parse_smart_attr_raw(out, 196)
        long_fail = _parse_smart_long_selftest_failures(out)

        def _to_int(x):
            if x is None:
                return None
            if isinstance(x, int):
                return x
            s = str(x).strip().replace(",", "")
            if s.isdigit() or (s.startswith('-') and s[1:].isdigit()):
                try:
                    return int(s, 10)
                except ValueError:
                    return None
            return None

        def _v(x):
            return x if x is not None else "-"

        def _color_val(kind, val):
            """
            Colorize values: OK=green, WATCH=yellow, BAD=red.
            """
            ival = _to_int(val)
            raw = str(_v(val))

            # Unknown/unavailable
            if val is None:
                return raw

            # Severity rules (conservative; trend is unknown).
            if kind in ("pending", "unc", "reported", "selftest"):
                if ival is not None and ival > 0:
                    return f"{Colors.FAIL}{raw}{Colors.ENDC}"
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}"
            if kind in ("crc", "timeout", "errlog", "realloc"):
                if ival is not None and ival > 0:
                    return f"{Colors.WARNING}{raw}{Colors.ENDC}"
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}"
            if kind == "temp":
                if ival is None:
                    return raw
                if ival >= 60:
                    return f"{Colors.FAIL}{raw}{Colors.ENDC}"
                if ival >= 50:
                    return f"{Colors.WARNING}{raw}{Colors.ENDC}"
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}"
            return raw

        def _color_overall(s):
            if not s:
                return "-"
            t = str(s).strip()
            if "PASSED" in t.upper() or "OK" == t.upper():
                return f"{Colors.OKGREEN}{t}{Colors.ENDC}"
            return f"{Colors.FAIL}{t}{Colors.ENDC}"

        is_seagate = _smartctl_looks_seagate(out)

        # Fix Seagate SMART 188 formatting: decode packed 48-bit value into real counters.
        cmd188_disp = cmd188
        cmd188_for_color = cmd188
        d188 = None
        if is_seagate:
            d188 = _decode_seagate_command_timeout(cmd188)
            if d188 and d188["raw_int"] > 0xFFFF:
                cmd188_for_color = d188["timeouts"]
                cmd188_disp = f'{d188["timeouts"]} (packed: >5s={d188["gt_5s"]}, >7.5s={d188["gt_7_5s"]}; raw={d188["raw_int"]} {d188["hex"]})'

        # SMART pretext block (fixed ranking 1=most critical -> 24=least critical).
        rows = {
            1: _parse_smart_attr_row(out, 1),
            3: _parse_smart_attr_row(out, 3),
            4: _parse_smart_attr_row(out, 4),
            5: _parse_smart_attr_row(out, 5),
            7: _parse_smart_attr_row(out, 7),
            9: _parse_smart_attr_row(out, 9),
            10: _parse_smart_attr_row(out, 10),
            12: _parse_smart_attr_row(out, 12),
            184: _parse_smart_attr_row(out, 184),
            187: _parse_smart_attr_row(out, 187),
            188: _parse_smart_attr_row(out, 188),
            189: _parse_smart_attr_row(out, 189),
            190: _parse_smart_attr_row(out, 190),
            191: _parse_smart_attr_row(out, 191),
            192: _parse_smart_attr_row(out, 192),
            193: _parse_smart_attr_row(out, 193),
            197: _parse_smart_attr_row(out, 197),
            198: _parse_smart_attr_row(out, 198),
            199: _parse_smart_attr_row(out, 199),
            240: _parse_smart_attr_row(out, 240),
            241: _parse_smart_attr_row(out, 241),
            242: _parse_smart_attr_row(out, 242),
            254: _parse_smart_attr_row(out, 254),
            194: _parse_smart_attr_row(out, 194),
        }

        # Seagate packed SMART 188 decode already applied to cmd188_disp/cmd188_for_color above.
        cmd188_timeouts = _first_int_from_text(cmd188_for_color)

        def _tb_from_lbas(lbas):
            try:
                n = int(lbas)
            except Exception:
                return None
            # 512-byte LBAs
            return (n * 512) / (1024 ** 4)

        def _rank_color(rank):
            if rank <= 5:
                return Colors.FAIL
            if rank <= 15:
                return Colors.WARNING
            if rank <= 21:
                return Colors.OKCYAN
            return Colors.OKGREEN

        def _norm_suffix(row, include_worst=False):
            """Append normalized VALUE/THRESH (and WORST when requested)."""
            if not row:
                return ""
            value = row.get('value', '-')
            worst = row.get('worst', '-')
            thresh = row.get('thresh', '-')
            t_i = _first_int_from_text(thresh)
            if include_worst:
                if t_i is not None and t_i > 0:
                    return f" (VALUE {value}, WORST {worst}, THRESH {thresh})"
                return f" (VALUE {value}, WORST {worst})"
            if t_i is not None and t_i > 0:
                return f" (VALUE {value}, THRESH {thresh})"
            return f" (VALUE {value})"

        def _fmt_smart_value(aid):
            # Single-line value formatting (no multi-line blocks).
            if aid == 188 and is_seagate and d188 and d188.get("raw_int") is not None and d188["raw_int"] > 0xFFFF:
                v = f"{d188['timeouts']} (decoded: >5s={d188['gt_5s']}, >7.5s={d188['gt_7_5s']}; raw={d188['hex']})"
                return _color_val('timeout', d188['timeouts']) + v[len(str(d188['timeouts'])):]

            row = rows.get(aid)
            if not row:
                return "-"

            raw = row.get("raw", "-")
            if aid in (1, 7) and is_seagate:
                # Common Seagate packing: hi16=error_count, lo32=op_count.
                d = _decode_seagate_hi16_lo32(raw)
                if d:
                    err_col = f"{Colors.OKGREEN}{d['errors']}{Colors.ENDC}" if d["errors"] == 0 else f"{Colors.FAIL}{d['errors']}{Colors.ENDC}"
                    if aid == 1:
                        return f"read_ops={d['ops']:,}, read_errors={err_col} (raw={d['hex']}; VALUE {row.get('value', '-')}, THRESH {row.get('thresh', '-')})"
                    return f"seek_ops={d['ops']:,}, seek_errors={err_col} (raw={d['hex']}; VALUE {row.get('value', '-')}, THRESH {row.get('thresh', '-')})"

            if aid in (5, 1, 7, 10):
                kind = 'realloc' if aid == 5 else ('reported' if aid == 10 else 'errlog')
                if aid == 10:
                    kind = 'reported'
                v = f"{raw} (VALUE {row.get('value', '-')}, THRESH {row.get('thresh', '-')})"
                # Colorize the RAW count where non-zero is meaningful.
                raw_i = _first_int_from_text(raw)
                if aid == 5:
                    return (_color_val('realloc', raw_i if raw_i is not None else raw)) + v[len(str(raw_i if raw_i is not None else raw)):]
                if aid == 10:
                    return (_color_val('reported', raw_i if raw_i is not None else raw)) + v[len(str(raw_i if raw_i is not None else raw)):]
                # 1/7 fall through above for Seagate; otherwise keep as-is (vendor-specific).
                return v
            if aid == 187:
                raw_i = _first_int_from_text(raw)
                v = f"{raw}{_norm_suffix(row)}"
                return (_color_val('reported', raw_i if raw_i is not None else raw)) + v[len(str(raw_i if raw_i is not None else raw)):]
            if aid == 188:
                raw_i = cmd188_timeouts or _first_int_from_text(raw)
                v = f"{raw}{_norm_suffix(row, include_worst=True)}"
                return (_color_val('timeout', raw_i if raw_i is not None else raw)) + v[len(str(raw_i if raw_i is not None else raw)):]
            if aid == 3:
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"
            if aid == 193:
                rv = _first_int_from_text(raw)
                if rv is not None:
                    col = Colors.WARNING if rv > 300000 else Colors.OKGREEN
                    return f"{col}{rv:,}{Colors.ENDC}{_norm_suffix(row)}"
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"
            if aid in (241, 242):
                rv = _first_int_from_text(raw)
                tb = _tb_from_lbas(rv) if rv is not None else None
                if tb is not None:
                    return f"{Colors.OKGREEN}{raw}{Colors.ENDC} (~{tb:.2f} TiB){_norm_suffix(row)}"
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"
            if aid in (197, 198, 184):
                raw_i = _first_int_from_text(raw)
                v = str(raw)
                kind = 'pending' if aid == 197 else 'unc'
                if aid == 184:
                    kind = 'reported'
                return _color_val(kind, raw_i if raw_i is not None else raw) + _norm_suffix(row)
            if aid in (199, 191, 254, 189, 192, 4, 12, 9, 240):
                raw_i = _first_int_from_text(raw)
                if aid in (199, 191, 254, 189, 192):
                    kind = 'crc' if aid == 199 else 'timeout'
                    # treat these as medium signals when non-zero
                    return _color_val('timeout', raw_i if raw_i is not None else raw) + _norm_suffix(row)
                return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"
            if aid in (190, 194):
                raw_i = _first_int_from_text(raw)
                if raw_i is None:
                    return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"
                s = str(raw)
                head = str(raw_i)
                # Preserve the full raw field (including Min/Max etc) but color the leading temperature.
                if s.startswith(head):
                    return _color_val('temp', raw_i) + s[len(head):] + _norm_suffix(row)
                return _color_val('temp', raw_i) + _norm_suffix(row)
            return f"{Colors.OKGREEN}{raw}{Colors.ENDC}{_norm_suffix(row)}"

        rr_meaning = (
            "Seagate RAW is often packed; treat normalized VALUE trend as primary. diskmgr shows decoded ops+errors when possible."
            if is_seagate else
            "Vendor-specific RAW encoding is common; prioritize normalized VALUE/WORST/THRESH trend."
        )
        seek_meaning = (
            "Seagate RAW is often packed; treat normalized VALUE trend as primary. diskmgr shows decoded ops+errors when possible."
            if is_seagate else
            "Vendor-specific RAW encoding is common; prioritize normalized VALUE/WORST/THRESH trend."
        )

        ranked = [
            (1, 187, "Reported_Uncorrect", "Uncorrectable errors reached the OS/host; corruption has already happened."),
            (2, 198, "Offline_Uncorrectable", "Unrecoverable sectors found during offline scan/self-test; physical defects confirmed."),
            (3, 5, "Reallocated_Sector_Ct", "Bad sectors remapped to spares; increasing suggests real media degradation."),
            (4, 197, "Current_Pending_Sector", "Unstable sectors awaiting re-read/rewrite; data at risk until resolved."),
            (5, 184, "End-to-End_Error", "Internal data-path corruption (cache/buffer <-> media); logic/RAM path issues."),
            (6, 10, "Spin_Retry_Count", "Spin-up retries; motor/bearing/power trouble can trap data permanently."),
            (7, 1, "Raw_Read_Error_Rate", rr_meaning),
            (8, 7, "Seek_Error_Rate", seek_meaning),
            (9, 3, "Spin_Up_Time", "Drive taking longer to become ready; can indicate wear (trend matters)."),
            (10, 188, "Command_Timeout", "Drive commands timing out/hanging; often link/power/bridge issues (trend matters)."),
            (11, 199, "UDMA_CRC_Error_Count", "Interface CRC errors; usually cable/port/bridge noise, not platter damage."),
            (12, 191, "G-Sense_Error_Rate", "Shock/vibration events while running; can cause (not just reflect) damage."),
            (13, 254, "Free_Fall_Sensor", "Recorded free-fall/drop events (history of dangerous handling)."),
            (14, 193, "Load_Cycle_Count", "Head parking cycles; very high counts increase mechanical wear risk."),
            (15, 189, "High_Fly_Writes", "Head flying height anomalies during writes; risk weak writes/data fade."),
            (16, 194, "Temperature_Celsius", "Internal temperature; only critical when extreme (e.g. >60C)."),
            (17, 190, "Airflow_Temperature_Cel", "Alternate temperature sensor; only critical when extreme."),
            (18, 9, "Power_On_Hours", "Lifetime hours; context only (older drives have higher baseline risk)."),
            (19, 4, "Start_Stop_Count", "Spindle start/stop cycles; wear context."),
            (20, 12, "Power_Cycle_Count", "Power cycles; wear/environment context."),
            (21, 192, "Power-Off_Retract_Count", "Emergency retracts (power loss/unplug); environment context."),
            (22, 241, "Total_LBAs_Written", "Cumulative writes; statistics/context."),
            (23, 242, "Total_LBAs_Read", "Cumulative reads; statistics/context."),
            (24, 240, "Head_Flying_Hours", "Head flying time; informational."),
        ]

        zone_headers = {
            1: "The \"Data is Already Gone\" Zone",
            6: "The \"Mechanical Failure Imminent\" Zone",
            11: "The \"Environmental & Usage Stress\" Zone",
            16: "The \"Old Age & Thermometer\" Zone",
            22: "The \"Pure Statistics\" Zone (Least Critical)",
        }

        print(f"\n{Colors.BOLD}SMART attributes ranked strictly from 1 (most critical) to 24 (least critical):{Colors.ENDC}")
        print("Each attribute is printed on one line (ID + name + current value + meaning).")
        for rank, aid, nm, meaning in ranked:
            if rank in zone_headers:
                print(f"\n{Colors.BOLD}{zone_headers[rank]}:{Colors.ENDC}")
            c = _rank_color(rank)
            v = _fmt_smart_value(aid)
            print(f"{c}{rank:2d}.{Colors.ENDC} {Colors.BOLD}{aid}{Colors.ENDC} {nm}: {v} - {meaning}")

        if overall or temp or poh or realloc or pending or offline_unc:
            if overall:
                print(f"\n{Colors.BOLD}Overall:{Colors.ENDC} {_color_overall(overall)}")
            if temp:
                print(f"{Colors.BOLD}Temp:{Colors.ENDC} {_color_val('temp', temp)} C")
            if temp_life_min is not None and temp_life_max is not None:
                print(f"{Colors.BOLD}Lifetime temp min/max:{Colors.ENDC} {temp_life_min}/{temp_life_max} C ({temp_life_src})")
            if poh:
                print(f"{Colors.BOLD}Power-on hours:{Colors.ENDC} {poh}")
            if realloc:
                print(f"{Colors.BOLD}Reallocated sectors:{Colors.ENDC} {_color_val('realloc', realloc)}")
            if pending:
                print(f"{Colors.BOLD}Pending sectors:{Colors.ENDC} {_color_val('pending', pending)}")
            if offline_unc:
                print(f"{Colors.BOLD}Offline uncorrectable:{Colors.ENDC} {_color_val('unc', offline_unc)}")
            print("")

        _print_smartctl_remaining_block()
        if err.strip():
            print(err.rstrip(), file=sys.stderr)

        # smartctl uses a bitmask exit code; non-zero can mean "drive has issues" and is still useful output.
        if getattr(res, 'returncode', 0) != 0:
            log(f"smartctl exit status: {res.returncode} (non-zero may indicate SMART warnings).", 'WARN')

    def do_smart(self, arg):
        '''Alias for health: smart <name/id>'''
        return self.do_health(arg)

    def do_selftest(self, arg):
        '''Start a SMART long self-test: selftest <name/id>

        Runs smartctl long test against the underlying DISK device for the mapping.
        - If the mapping points to a partition, diskmgr targets the parent disk.
        - If the disk transport is USB and the device is /dev/sdX, diskmgr uses:
              smartctl -d sat -t long /dev/sdX
          (common for USB-SATA bridges).
        '''
        parser = CmdArgumentParser(prog='selftest', add_help=False)
        parser.add_argument('name')
        parser.add_argument('--watch', action='store_true', help='Poll SMART self-test progress until complete')
        parser.add_argument('--interval', type=int, default=60, help='Polling interval seconds for --watch (default: 60)')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        name = args.name
        watch = bool(getattr(args, 'watch', False))
        interval = int(getattr(args, 'interval', 60) or 60)
        if interval < 5:
            interval = 5
        src = self.resolve_target(name, allow_id=True)
        if not src:
            log(f"Unknown target: '{name}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        smartctl_bin = _find_tool_or_common_paths('smartctl', [
            '/usr/sbin/smartctl',
            '/sbin/smartctl',
            '/usr/local/sbin/smartctl',
        ])
        if smartctl_bin is None:
            log("smartctl not found. Install 'smartmontools' and retry.", 'ERROR')
            return

        mapped_dev = os.path.realpath(src)

        # Always run SMART commands on the underlying whole-disk device (SMART is not partition-scoped).
        disk_dev = mapped_dev
        try:
            mapped_name = _sysfs_block_name(mapped_dev)
            disk_name = _sysfs_to_parent_disk_name(mapped_name)
            candidate = os.path.realpath(f"/dev/{disk_name}")
            if os.path.exists(candidate):
                disk_dev = candidate
        except Exception:
            disk_dev = mapped_dev

        tran = ""
        try:
            res_tran = run_command(['lsblk', '-no', 'TRAN', disk_dev], check=False)
            tran = (getattr(res_tran, 'stdout', '') or '').strip().lower()
        except Exception:
            tran = ""

        use_sat = (tran == 'usb' and os.path.basename(disk_dev).startswith('sd'))
        cmd = [smartctl_bin, '-t', 'long', disk_dev]
        if use_sat:
            cmd = [smartctl_bin, '-d', 'sat', '-t', 'long', disk_dev]

        mode = "-d sat" if ('-d' in cmd) else ""
        print(f"Starting SMART long self-test: {Colors.BOLD}{name}{Colors.ENDC} -> {disk_dev} {mode}".strip())

        # Confirmation (high-impact; not directly destructive but stresses the drive).
        if not self.extensive_confirm(f"selftest {name} ({disk_dev})", destructive=False):
            return

        log_path = _cmd_log_open("selftest")
        if log_path:
            print(f"Log: {log_path}")
        start_ts = time.time()
        try:
            res = run_command(cmd, sudo=True, capture_output=True, check=False)
            out = (res.stdout or "").rstrip()
            err = (res.stderr or "").rstrip()

            if out:
                print(out)
            if err:
                print(err, file=sys.stderr)

            if getattr(res, 'returncode', 0) != 0:
                log(f"smartctl exit status: {res.returncode} (non-zero may indicate it could not start the test).", 'WARN')
                return

            if not watch:
                print(f"\nTo check progress/results: run {Colors.BOLD}health {name}{Colors.ENDC} and look at the SMART Self-test log.")
                return

            def _parse_remaining_pct(text):
                # ATA: "Self-test execution status: ... 90% of test remaining."
                m = re.search(r"(?im)\b([0-9]{1,3})%\s+of\s+test\s+remaining\b", text)
                if m:
                    try:
                        return int(m.group(1), 10)
                    except ValueError:
                        return None
                return None

            print(f"\nWatching SMART self-test progress (interval={interval}s). Ctrl+C to stop watching.")
            last_line = None
            parse_fail = 0
            while True:
                res_p = run_command([smartctl_bin, '-a', disk_dev] if not use_sat else [smartctl_bin, '-d', 'sat', '-a', disk_dev],
                                    sudo=True, capture_output=True, check=False)
                txt = ((res_p.stdout or "") + "\n" + (res_p.stderr or "")).strip()
                rem = _parse_remaining_pct(txt)
                in_progress = False
                if rem is not None:
                    in_progress = True
                    done = max(0, min(100, 100 - rem))
                    line = f"SMART self-test: {done}% complete ({rem}% remaining)"
                else:
                    # If we can't parse remaining%, fall back to detecting the "in progress" phrase.
                    if re.search(r"(?im)self-test\s+routine\s+in\s+progress", txt):
                        in_progress = True
                        line = "SMART self-test: in progress (unable to parse % remaining)"
                    else:
                        in_progress = False
                        line = "SMART self-test: not in progress (completed or not running)"

                if line != last_line:
                    print(line)
                    _cmd_log_write(line)
                    last_line = line

                if not in_progress:
                    break

                if rem is None:
                    parse_fail += 1
                    if parse_fail >= 3:
                        print("SMART self-test is in progress but % remaining could not be parsed; use `health` to inspect the self-test log.")
                        break
                else:
                    parse_fail = 0

                time.sleep(interval)
        finally:
            print(f"Duration: {_fmt_hms(time.time() - start_ts)}")
            _cmd_log_close()
