"""EntropyCommands command implementations."""

import argparse
import os
import re
import shlex
import subprocess
import time
from ..runtime import _find_tool_or_common_paths, _first_int_from_text, log, popen_command, run_command
from ..shell_core import CmdArgumentParser


class EntropyCommands:

    def do_entropy(self, arg):
        '''Plot entropy profile over a raw device range or stitched random samples.

        Range mode (explicit flags required):
          entropy <name/id> --begin 0 --end 1GiB [--step 1MiB] [--window 1MiB]

        Random stitched mode:
          entropy <name/id> <span> --samples N
          Example: entropy 1a 1GiB --samples 1000
          - window size is derived as: span / N (integer bytes)
          - N random windows are sampled across the whole device
          - graph X axis is stitched as one contiguous span (not physical offsets)
        '''
        parser = CmdArgumentParser(prog='entropy', add_help=False)
        parser.add_argument('target', help='Mapped target name or discovery ID (#N)')
        parser.add_argument('span', nargs='?', help='Stitched span with units for random mode (used with --samples)')
        parser.add_argument('--begin', default=None, help='Range begin offset (units required; 0 allowed)')
        parser.add_argument('--end', default=None, help='Range end offset with units (e.g. 1GiB)')
        parser.add_argument('--step', default=None, help='Range mode step size with units (default: 10MiB)')
        parser.add_argument('--window', default=None, help='Range mode read window size with units (default: --step)')
        parser.add_argument('--samples', type=int, default=None, help='Enable random stitched mode with N samples')
        try:
            split_args = shlex.split(arg)
            args = parser.parse_args(split_args)
        except argparse.ArgumentError as e:
            log(str(e), 'ERROR')
            return
        except SystemExit:
            return

        _SIZE_UNIT_MAP = {
            'kib': 1024,
            'mib': 1024 ** 2,
            'gib': 1024 ** 3,
            'tib': 1024 ** 4,
        }

        def _parse_size_to_bytes(spec, field_name, allow_plain_zero=False):
            txt = str(spec or '').strip()
            if allow_plain_zero and txt == '0':
                return 0
            m = re.fullmatch(r'(?i)\s*([0-9]+(?:\.[0-9]+)?)\s*(kib|mib|gib|tib)\s*', txt)
            if not m:
                raise ValueError(
                    f"{field_name} must include a unit (KiB/MiB/GiB/TiB). "
                    f"Example: {field_name}=1MiB"
                )
            val = float(m.group(1))
            unit = (m.group(2) or '').strip().lower()
            mul = _SIZE_UNIT_MAP.get(unit)
            if mul is None:
                raise ValueError(f"Unsupported unit for {field_name}: {unit}")
            total_bytes = int(round(val * mul))
            if total_bytes < 0:
                raise ValueError(f"{field_name} must be non-negative")
            return total_bytes

        mode = None
        begin_bytes = None
        end_bytes = None
        step_bytes = None
        window_bytes = None
        span_bytes = None
        samples = None
        begin_spec = None
        end_spec = None
        step_spec = None
        window_spec = None

        if args.samples is not None:
            # Random stitched mode.
            if args.begin is not None or args.end is not None:
                log("Random mode (--samples) cannot be combined with --begin/--end.", 'ERROR')
                return
            if args.step is not None or args.window is not None:
                log("Random mode (--samples) does not accept --step/--window; window is derived from <span>/samples.", 'ERROR')
                return
            if not args.span:
                log("Random mode usage: entropy <name/id> <span> --samples N", 'ERROR')
                return
            if int(args.samples) <= 0:
                log("--samples must be > 0.", 'ERROR')
                return
            try:
                span_bytes = _parse_size_to_bytes(args.span, 'span', allow_plain_zero=False)
            except ValueError as e:
                log(str(e), 'ERROR')
                return
            if span_bytes <= 0:
                log("span must be > 0.", 'ERROR')
                return
            samples = int(args.samples)
            window_bytes = span_bytes // samples
            if window_bytes <= 0:
                log("span/samples is < 1 byte. Increase span or reduce --samples.", 'ERROR')
                return
            mode = 'rand'
        else:
            # Sequential range mode.
            if args.span:
                log("Range mode does not accept a positional span. Use: entropy <name/id> --begin <X> --end <Y>", 'ERROR')
                return
            if args.begin is None or args.end is None:
                log("Range mode requires both --begin and --end. Example: entropy 1a --begin 0 --end 1GiB", 'ERROR')
                return
            begin_spec = args.begin
            end_spec = args.end
            step_spec = args.step or '10MiB'
            window_spec = args.window or step_spec
            try:
                begin_bytes = _parse_size_to_bytes(begin_spec, '--begin', allow_plain_zero=True)
                end_bytes = _parse_size_to_bytes(end_spec, '--end', allow_plain_zero=True)
                step_bytes = _parse_size_to_bytes(step_spec, '--step', allow_plain_zero=False)
                window_bytes = _parse_size_to_bytes(window_spec, '--window', allow_plain_zero=False)
            except ValueError as e:
                log(str(e), 'ERROR')
                return
            if end_bytes <= begin_bytes:
                log("--end must be greater than --begin.", 'ERROR')
                return
            if step_bytes <= 0:
                log("--step must be > 0.", 'ERROR')
                return
            if window_bytes <= 0:
                log("--window must be > 0.", 'ERROR')
                return
            mode = 'seq'

        target = args.target
        resolved = self.resolve_target(target, allow_id=True)
        if not resolved:
            log(f"Unknown target: '{target}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return
        devnode = os.path.realpath(resolved)
        if not os.path.exists(devnode):
            log(f"Target not found: {devnode}", 'ERROR')
            return

        gnuplot_bin = _find_tool_or_common_paths('gnuplot', [
            '/usr/bin/gnuplot',
            '/bin/gnuplot',
            '/usr/local/bin/gnuplot',
        ])
        if gnuplot_bin is None:
            log("Dependency missing: 'gnuplot' not found.", 'ERROR')
            return

        # Device size guard for sequential bounds and random-sample offset generation.
        size_bytes = None
        try:
            res_sz = run_command(['lsblk', '-bno', 'SIZE', devnode], check=False)
            size_bytes = _first_int_from_text(getattr(res_sz, 'stdout', '') or '')
        except Exception:
            size_bytes = None

        if mode == 'seq':
            if size_bytes is not None and end_bytes > int(size_bytes):
                max_gib = float(size_bytes) / (1024.0 ** 3)
                log(f"Requested --end {end_spec} exceeds device size (~{max_gib:.3f} GiB).", 'ERROR')
                return
            total_points = (end_bytes - begin_bytes + step_bytes - 1) // step_bytes
        else:
            if size_bytes is None:
                log("Could not determine device size for random mode. Aborting.", 'ERROR')
                return
            if window_bytes > int(size_bytes):
                log("Derived random window is larger than the device size.", 'ERROR')
                return
            total_points = samples

        log(f"Target resolved: {devnode}")
        if mode == 'seq':
            log(
                f"Entropy range mode: begin={begin_spec}, end={end_spec} "
                f"(step={step_spec}, window={window_spec})"
            )
        else:
            stitched_bytes = window_bytes * samples
            log(
                f"Entropy random-stitch mode: span={args.span}, samples={samples}, "
                f"window={self._format_bytes_binary(str(window_bytes), decimals=2)}"
            )
            if stitched_bytes != span_bytes:
                log(
                    f"Requested span {self._format_bytes_binary(str(span_bytes), decimals=2)}; "
                    f"effective stitched span is {self._format_bytes_binary(str(stitched_bytes), decimals=2)} "
                    f"(integer window rounding).",
                    'WARN'
                )
        stamp = f"{os.getpid()}_{int(time.time())}"
        data_file = f"/tmp/diskmgr_entropy_{stamp}.txt"
        plot_file = f"/tmp/diskmgr_entropy_{stamp}.png"
        points_written = 0

        helper_code = r'''
import math
import os
import random
import sys

BYTES_PER_GIB = 1024.0 * 1024.0 * 1024.0

def entropy_bits_per_byte(buf):
    total = len(buf)
    if total <= 0:
        return 0.0
    counts = [0] * 256
    for b in buf:
        counts[b] += 1
    ent = 0.0
    inv_total = 1.0 / float(total)
    for c in counts:
        if c:
            p = c * inv_total
            ent -= p * math.log2(p)
    return ent

def read_chunk(fd, off, need):
    chunks = []
    got = 0
    while got < need:
        chunk = os.pread(fd, need - got, off + got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    if got <= 0:
        return b""
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)

def main():
    if len(sys.argv) != 7:
        print('usage: <dev> <mode> <p1> <p2> <p3> <p4>', file=sys.stderr)
        return 2

    dev = sys.argv[1]
    mode = sys.argv[2]
    p1 = int(sys.argv[3])
    p2 = int(sys.argv[4])
    p3 = int(sys.argv[5])
    p4 = int(sys.argv[6])

    fd = os.open(dev, os.O_RDONLY)
    try:
        if mode == 'seq':
            start_bytes = p1
            end_bytes = p2
            step_bytes = p3
            window_bytes = p4
            idx = 0
            for skip_bytes in range(start_bytes, end_bytes, step_bytes):
                count_bytes = min(window_bytes, end_bytes - skip_bytes)
                if count_bytes <= 0:
                    break
                data = read_chunk(fd, skip_bytes, count_bytes)
                if not data:
                    continue
                ent_val = entropy_bits_per_byte(data)
                x_gib = skip_bytes / BYTES_PER_GIB
                idx += 1
                sys.stdout.write(f"{idx}\t{x_gib:.6f}\t{ent_val:.6f}\t{skip_bytes}\n")
                sys.stdout.flush()
        elif mode == 'rand':
            device_size = p1
            samples = p2
            window_bytes = p3
            max_start = device_size - window_bytes
            if max_start < 0:
                return 2
            rng = random.Random()
            for i in range(samples):
                if max_start == 0:
                    skip_bytes = 0
                else:
                    skip_bytes = rng.randrange(0, max_start + 1)
                data = read_chunk(fd, skip_bytes, window_bytes)
                if not data:
                    continue
                ent_val = entropy_bits_per_byte(data)
                stitched_off = i * window_bytes
                x_gib = stitched_off / BYTES_PER_GIB
                idx = i + 1
                sys.stdout.write(f"{idx}\t{x_gib:.6f}\t{ent_val:.6f}\t{skip_bytes}\n")
                sys.stdout.flush()
        else:
            print(f"unknown mode: {mode}", file=sys.stderr)
            return 2
    finally:
        os.close(fd)

    return 0

raise SystemExit(main())
'''

        if mode == 'seq':
            sample_cmd = [
                'python3', '-c', helper_code,
                devnode, 'seq',
                str(begin_bytes),
                str(end_bytes),
                str(step_bytes),
                str(window_bytes),
            ]
        else:
            sample_cmd = [
                'python3', '-c', helper_code,
                devnode, 'rand',
                str(int(size_bytes)),
                str(samples),
                str(window_bytes),
                '0',
            ]

        proc = popen_command(
            sample_cmd,
            sudo=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        try:
            with open(data_file, 'w', encoding='utf-8') as f_data:
                f_data.write("# offset_gib entropy_bits_per_byte\n")
                if proc.stdout is None:
                    log("Entropy sampler failed to start stdout pipe.", 'ERROR')
                    return

                for raw_line in proc.stdout:
                    line = (raw_line or '').strip()
                    if not line:
                        continue
                    parts = line.split('\t')
                    if len(parts) != 4:
                        continue
                    try:
                        idx = int(parts[0])
                        x_gib = float(parts[1])
                        ent_val = float(parts[2])
                    except Exception:
                        continue

                    f_data.write(f"{x_gib:.6f} {ent_val:.6f}\n")
                    points_written += 1
                    if idx < 1:
                        idx = points_written
                    if idx > total_points:
                        idx = total_points
                    print(f"\rSampling entropy: {idx}/{total_points} at {x_gib:.3f} GiB", end="", flush=True)
        finally:
            if proc.poll() is None:
                proc.terminate()
            rc = proc.wait()
        stderr_txt = (proc.stderr.read() if proc.stderr else '') or ''
        print("")

        if rc != 0:
            err_line = (stderr_txt.strip().splitlines() or ['unknown error'])[-1]
            if points_written == 0:
                log(f"Entropy sampler failed: {err_line}", 'ERROR')
                return
            log(f"Entropy sampler ended with warnings: {err_line}", 'WARN')

        if points_written == 0:
            log(f"No entropy samples were written. Data file: {data_file}", 'ERROR')
            return

        def _gp_escape(s):
            return str(s).replace("\\", "\\\\").replace("'", "\\'")

        gp_data = _gp_escape(data_file)
        gp_plot = _gp_escape(plot_file)
        if mode == 'seq':
            title = f"Entropy Profile: {devnode} [--begin {begin_spec}, --end {end_spec}]"
            x_label = "Offset (GiB)"
        else:
            title = f"Entropy Profile (stitched random): {devnode} [span={args.span}, samples={samples}]"
            x_label = "Stitched Offset (GiB)"
        gp_title = _gp_escape(title)

        gp_png_script = (
            "set terminal pngcairo size 1600,900;"
            f"set output '{gp_plot}';"
            f"set title '{gp_title}';"
            f"set xlabel '{x_label}';"
            "set ylabel 'Entropy (bits/byte)';"
            "set grid;"
            "set yrange [0:8];"
            f"plot '{gp_data}' using 1:2 with lines linewidth 2 title 'Entropy';"
        )
        run_command([gnuplot_bin, '-e', gp_png_script], check=True)
        log(f"Entropy data saved: {data_file}")
        log(f"Entropy plot saved: {plot_file}")

        displayed = False
        if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            gp_live_script = (
                f"set title '{gp_title}';"
                f"set xlabel '{x_label}';"
                "set ylabel 'Entropy (bits/byte)';"
                "set grid;"
                "set yrange [0:8];"
                f"plot '{gp_data}' using 1:2 with lines linewidth 2 title 'Entropy';"
            )
            res_live = run_command([gnuplot_bin, '-p', '-e', gp_live_script], check=False, capture_output=False)
            displayed = getattr(res_live, 'returncode', 1) == 0

        if not displayed:
            res_open = run_command(['xdg-open', plot_file], check=False, capture_output=False)
            displayed = getattr(res_open, 'returncode', 1) == 0
        if not displayed:
            log(f"Could not auto-display plot. Open manually: {plot_file}", 'WARN')
