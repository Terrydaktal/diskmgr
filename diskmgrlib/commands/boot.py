"""BootCommands command implementations."""

from pathlib import Path
import cmd
import os
import re
import shutil
import subprocess
import tempfile
import time
from ..runtime import Colors, log, run_command, run_command_bytes
from ..devices import _lsblk_pttype, _sysfs_block_name, _sysfs_to_parent_disk_name
from ..mappings import get_map_file_path
from ..shell_core import CmdArgumentParser


class BootCommands:

    def do_layout(self, arg):
        '''(Deprecated) Alias for list: layout

        This command was renamed to 'list'. Prefer: list
        '''
        log("layout was renamed to list; running list ...", 'WARN')
        return self.do_list(arg)

        # Missing mappings section: show friendly names and their configured persistent IDs.
        if missing_mappings:
            print(f"{Colors.HEADER}--- Missing Mappings ({get_map_file_path()}) ---{Colors.ENDC}")
            rows = []
            missing_idx = idx
            for friendly, target in missing_mappings:
                rows.append({
                    "#": str(missing_idx),
                    "NAME": str(friendly),
                    "PERSISTENT ID": str(target),
                })
                missing_idx += 1

            cols = ["#", "NAME", "PERSISTENT ID"]
            widths = {c: len(c) for c in cols}
            for r in rows:
                for c in cols:
                    widths[c] = max(widths[c], len(str(r.get(c, "") or "")))
            # 1-space padding each side.
            for c in cols:
                widths[c] += 2

            def _cell(text, width):
                inner = max(width - 2, 0)
                return f" {text:<{inner}} "

            header = "".join([_cell(c, widths[c]) for c in cols]).rstrip()
            print(f"{Colors.BOLD}{header}{Colors.ENDC}")
            for r in rows:
                line = "".join([_cell(str(r.get(c, "") or ""), widths[c]) for c in cols]).rstrip()
                print(line)
            print("")

    def do_boot(self, arg):
        '''Display boot entries from GRUB and fstab detection for each partition.

        UNDER THE HOOD:
        Scans partition devices. If mounted, it parses /boot/grub/grub.cfg
        and checks for /etc/fstab inside that mounted partition.
        If unmounted or encrypted, it explains why it cannot yet read the config.
        '''
        all_devs = self.get_disk_info()
        flat_disks = self.flatten_disks(all_devs)

        def _sector_int(tok):
            m = re.match(r'^\s*([0-9]+)s\s*$', str(tok or ''))
            if not m:
                return None
            try:
                return int(m.group(1))
            except Exception:
                return None

        def _parse_parted_parts(disk_dev):
            """
            Parse `parted -m -s <disk> unit s print` and return part rows:
            [{num,start_s,end_s,flags}, ...]
            """
            out = []
            try:
                res = run_command(['parted', '-m', '-s', disk_dev, 'unit', 's', 'print'], sudo=True, check=False)
                txt = (getattr(res, 'stdout', '') or '')
                for raw in txt.splitlines():
                    line = (raw or '').strip()
                    if not line or line in ('BYT;',) or line.startswith('/dev/'):
                        continue
                    parts = line.split(':')
                    if len(parts) < 7:
                        continue
                    if not str(parts[0]).isdigit():
                        continue
                    num = parts[0].strip()
                    start_s = _sector_int(parts[1])
                    end_s = _sector_int(parts[2])
                    flags = parts[6].strip().strip(';')
                    out.append({
                        'num': num,
                        'start_s': start_s,
                        'end_s': end_s,
                        'flags': flags,
                    })
            except Exception:
                return []
            return out

        print(f"\n{Colors.HEADER}{Colors.BOLD}--- GRUB core.img Source Scan ---{Colors.ENDC}")
        firmware_mode = 'UEFI' if os.path.isdir('/sys/firmware/efi') else 'BIOS'
        print(f"Firmware mode: {firmware_mode}")

        boot_src = ""
        boot_mp = ""
        for mp in ('/boot', '/'):
            try:
                res_b = run_command(['findmnt', '-nro', 'SOURCE', mp], check=False)
                src = (getattr(res_b, 'stdout', '') or '').strip()
                if src:
                    boot_src = src
                    boot_mp = mp
                    break
            except Exception:
                continue
        if boot_src:
            print(f"/boot source ({boot_mp}): {boot_src} [{os.path.realpath(boot_src)}]")
        else:
            print(f"{Colors.WARNING}Could not resolve /boot mount source via findmnt.{Colors.ENDC}")

        if firmware_mode == 'UEFI':
            print(f"{Colors.OKCYAN}UEFI mode detected: BIOS embedded core.img scan is not primary boot path in this mode.{Colors.ENDC}")
            print(f"{Colors.OKCYAN}Inspect ESP grub config/EFI binaries for active boot chain.{Colors.ENDC}")
        else:
            boot_real = os.path.realpath(boot_src) if boot_src else ""
            boot_disk = ""
            if boot_real:
                try:
                    bname = _sysfs_block_name(boot_real)
                    dname = _sysfs_to_parent_disk_name(bname)
                    if dname:
                        boot_disk = f"/dev/{dname}"
                except Exception:
                    boot_disk = ""

            if not boot_disk:
                print(f"{Colors.WARNING}Could not infer boot disk for BIOS core.img scan.{Colors.ENDC}")
            else:
                print(f"Inferred boot disk for BIOS embedding scan: {boot_disk}")

            disk_devs = []
            seen_disks = set()
            for d in flat_disks:
                if (d.get('type') or '') != 'disk':
                    continue
                k = (d.get('kname') or d.get('name') or '').strip()
                if not k:
                    continue
                p = f"/dev/{k}"
                # Skip pseudo block devices that are not firmware boot media.
                if re.match(r'^/dev/(zram|loop|ram)\d+$', p):
                    continue
                if p in seen_disks:
                    continue
                seen_disks.add(p)
                if os.path.exists(p):
                    disk_devs.append(p)

            if not disk_devs:
                print(f"{Colors.WARNING}No local whole-disk block devices found for BIOS core scan.{Colors.ENDC}")
            else:
                ts = int(time.time())

                def _read_bytes(path):
                    try:
                        with open(path, 'rb') as f:
                            return f.read()
                    except Exception:
                        return b""

                def _token_hits(blob, token_bytes):
                    # Return first-match offsets per token: [(token_str, offset_bytes), ...]
                    hits = []
                    for t in token_bytes:
                        try:
                            pos = blob.find(t)
                            if pos >= 0:
                                hits.append((t.decode('ascii', errors='ignore'), pos))
                        except Exception:
                            continue
                    return hits

                def _stage1_sig(blob):
                    toks = [b'GRUB', b'Geom', b'Read', b' Error', b'Hard Disk']
                    hits = _token_hits(blob, toks)
                    if (blob.find(b'GRUB') >= 0) and (
                        blob.find(b'Geom') >= 0 or blob.find(b'Read') >= 0 or blob.find(b'Hard Disk') >= 0
                    ):
                        return "FOUND", hits
                    if len(hits) >= 2:
                        return "POSSIBLE", hits
                    return "NOT FOUND", hits

                def _stage15_sig(blob):
                    toks = [b'loading', b'Geom', b'Read', b' Error', b'GRUB']
                    hits = _token_hits(blob, toks)
                    if (blob.find(b'loading') >= 0) and (blob.find(b'Geom') >= 0 or blob.find(b'Read') >= 0):
                        return "FOUND", hits
                    if (blob.find(b'loading') >= 0) or (blob.find(b'GRUB') >= 0 and len(hits) >= 2):
                        return "POSSIBLE", hits
                    return "NOT FOUND", hits

                def _report_raw_scan(raw_label, raw_path, stage_kind):
                    if not os.path.exists(raw_path):
                        print(f"  {Colors.WARNING}{raw_label}: dump missing ({raw_path}).{Colors.ENDC}")
                        return None
                    blob = _read_bytes(raw_path)
                    if not blob:
                        print(f"  {Colors.WARNING}Could not read dumped bytes for signature analysis.{Colors.ENDC}")
                        return None

                    def _fmt_hits(hits):
                        if not hits:
                            return ""
                        out = []
                        for sig, off in hits:
                            sig_clean = (sig or '').strip()
                            out.append(f"{sig_clean} @ byte {off} (0x{off:x})")
                        return " ".join(out)

                    if stage_kind == 'stage1':
                        print(f"  Raw scan source: {raw_label} (dump: {raw_path})")
                        st, hits = _stage1_sig(blob[:512])
                        color = Colors.OKGREEN if st == "FOUND" else (Colors.WARNING if st == "POSSIBLE" else Colors.FAIL)
                        print(f"  Stage 1/boot.img signature (MBR sector 0): {color}{st}{Colors.ENDC}")
                        if hits:
                            print(f"    signature matches: {_fmt_hits(hits)}")
                        return st
                    else:
                        print(f"  Raw scan source: {raw_label} (dump: {raw_path})")
                        st, hits = _stage15_sig(blob)
                        color = Colors.OKGREEN if st == "FOUND" else (Colors.WARNING if st == "POSSIBLE" else Colors.FAIL)
                        print(f"  Stage 1.5/core.img signature: {color}{st}{Colors.ENDC}")
                        if hits:
                            print(f"    signature matches: {_fmt_hits(hits)}")
                        return st

                def _extract_embedded_core(input_img, out_core, use_lzma2=False):
                    marker = b"sector sizes of %d bytes aren't supported yet"
                    max_output_bytes = 64 * 1024 * 1024
                    max_probe_output_bytes = 8 * 1024 * 1024
                    if not os.path.exists(input_img):
                        return False, f"input image does not exist: {input_img}", None
                    if shutil.which('xz') is None:
                        return False, "xz is required for embedded core extraction but was not found.", None

                    lzma_arg = '--lzma2=dict=65535,lc=3,lp=0,pb=2' if use_lzma2 else '--lzma1=dict=65535,lc=3,lp=0,pb=2'
                    skip_support = getattr(_extract_embedded_core, '_dd_skip_bytes', None)
                    if skip_support is None:
                        try:
                            dd_probe = subprocess.run(
                                ['dd', 'if=/dev/zero', 'of=/dev/null', 'skip=1', 'count=1', 'iflag=skip_bytes', 'status=none'],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=False
                            )
                            skip_support = (dd_probe.returncode == 0)
                        except Exception:
                            skip_support = False
                        _extract_embedded_core._dd_skip_bytes = skip_support

                    def _spawn_xz(off):
                        dd_proc = None
                        try:
                            timeout_bin = shutil.which('timeout')
                            timeout_prefix = [timeout_bin, '--kill-after=1s', '3s'] if timeout_bin else []
                            xz_command = timeout_prefix + ['xz', '--decompress', '--stdout', '--format=raw', lzma_arg]
                            if off == 0:
                                xz_proc = subprocess.Popen(
                                    xz_command + [input_img],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL
                                )
                                return dd_proc, xz_proc, None
                            if skip_support:
                                dd_cmd = ['dd', f'if={input_img}', f'skip={off}', 'iflag=skip_bytes', 'status=none']
                            else:
                                dd_cmd = ['dd', f'if={input_img}', f'bs={off}', 'skip=1', 'status=none']
                            dd_proc = subprocess.Popen(dd_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                            xz_proc = subprocess.Popen(
                                xz_command,
                                stdin=dd_proc.stdout,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL
                            )
                            if dd_proc.stdout:
                                dd_proc.stdout.close()
                            return dd_proc, xz_proc, None
                        except Exception as e:
                            if dd_proc:
                                try:
                                    dd_proc.kill()
                                except Exception:
                                    pass
                            return None, None, str(e)

                    def _cleanup_procs(dd_proc, xz_proc):
                        if xz_proc:
                            try:
                                xz_proc.wait(timeout=1.0)
                            except Exception:
                                try:
                                    xz_proc.kill()
                                except Exception:
                                    pass
                                try:
                                    xz_proc.wait(timeout=0.5)
                                except Exception:
                                    pass
                        if dd_proc:
                            try:
                                dd_proc.wait(timeout=1.0)
                            except Exception:
                                try:
                                    dd_proc.kill()
                                except Exception:
                                    pass
                                try:
                                    dd_proc.wait(timeout=0.5)
                                except Exception:
                                    pass

                    def _probe_marker(off):
                        dd_proc, xz_proc, err = _spawn_xz(off)
                        if err:
                            return False, err
                        if not xz_proc or not xz_proc.stdout:
                            _cleanup_procs(dd_proc, xz_proc)
                            return False, None
                        found = False
                        tail = b''
                        output_bytes = 0
                        try:
                            while True:
                                chunk = xz_proc.stdout.read(65536)
                                if not chunk:
                                    break
                                output_bytes += len(chunk)
                                if output_bytes > max_probe_output_bytes:
                                    break
                                window = tail + chunk
                                if marker in window:
                                    found = True
                                    break
                                if len(marker) > 1:
                                    tail = window[-(len(marker) - 1):]
                        except Exception:
                            found = False
                        finally:
                            if found:
                                try:
                                    xz_proc.kill()
                                except Exception:
                                    pass
                                if dd_proc:
                                    try:
                                        dd_proc.kill()
                                    except Exception:
                                        pass
                            _cleanup_procs(dd_proc, xz_proc)
                        return found, None

                    def _extract_full(off):
                        dd_proc, xz_proc, err = _spawn_xz(off)
                        if err:
                            return False, err
                        if not xz_proc or not xz_proc.stdout:
                            _cleanup_procs(dd_proc, xz_proc)
                            return False, None
                        try:
                            with open(out_core, 'wb') as f_out:
                                output_bytes = 0
                                while True:
                                    chunk = xz_proc.stdout.read(65536)
                                    if not chunk:
                                        break
                                    output_bytes += len(chunk)
                                    if output_bytes > max_output_bytes:
                                        return False, f"extracted core exceeded {max_output_bytes} bytes"
                                    f_out.write(chunk)
                        except Exception as e:
                            _cleanup_procs(dd_proc, xz_proc)
                            return False, f"failed writing extracted core: {e}"
                        _cleanup_procs(dd_proc, xz_proc)
                        try:
                            if os.path.getsize(out_core) <= 0:
                                return False, None
                        except Exception:
                            return False, None
                        return True, None

                    # Prefer offsets commonly seen in BIOS MBR-gap embeddings.
                    # 3344 is the gap-relative form of the previously common 3856
                    # first-2048 offset (minus 512-byte MBR sector).
                    common = [3344, 3328, 3392, 3584, 3840, 3856, 4096]
                    offsets = []
                    for c in common:
                        for off in range(max(0, c - 512), c + 513, 16):
                            if off not in offsets:
                                offsets.append(off)

                    for off in offsets:
                        found, probe_err = _probe_marker(off)
                        if probe_err:
                            return False, f"failed probing embedded core: {probe_err}", None
                        if not found:
                            continue
                        ok, extract_err = _extract_full(off)
                        if not ok:
                            if extract_err:
                                return False, extract_err, None
                            continue
                        if off == 0:
                            note = f"\"{input_img}\" is a compressed core image."
                        else:
                            note = f"Found a compressed core image in \"{input_img}\" at offset {off} bytes."
                        return True, note, off

                    return False, f"Nothing was found in \"{input_img}\"!", None

                def _report_core_hints(core_path, note_text, off_bytes):
                    if off_bytes is not None:
                        print(f"  Recovered core image: {core_path} at offset {off_bytes} bytes (from start of gap).")
                    else:
                        print(f"  Recovered core image: {core_path}")

                    try:
                        sres = subprocess.run(['strings', '-a', core_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                        lines = (sres.stdout or '').splitlines()
                    except Exception:
                        lines = []

                    interesting = []
                    patterns = (
                        '/boot/grub',
                        '/grub',
                        'set prefix=',
                        'search.fs_uuid',
                        'search.fs_label',
                        'prefix',
                    )
                    for ln in lines:
                        t = (ln or '').strip()
                        if not t:
                            continue
                        if any(p in t for p in patterns):
                            if t not in interesting:
                                interesting.append(t)
                    search_uuid_lines = []
                    uuid_hits = []
                    uuid_re = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
                    for ln in lines:
                        t = (ln or '').strip()
                        if not t:
                            continue
                        if 'search.fs_uuid' in t and t not in search_uuid_lines:
                            search_uuid_lines.append(t)
                        for m in uuid_re.findall(t):
                            if m not in uuid_hits:
                                uuid_hits.append(m)

                    if interesting:
                        print("  Embedded core hints (first 12):")
                        for ln in interesting[:12]:
                            m_cur_boot = re.match(r'^\(,([^)]+)\)(.*)$', ln)
                            if m_cur_boot:
                                part = m_cur_boot.group(1)
                                print(
                                    f"    {Colors.OKGREEN}{ln}{Colors.ENDC} "
                                    f"(use current boot disk passed from MBR, partition {part} as the root to find grub.cfg)"
                                )
                            else:
                                print(f"    {ln}")
                    else:
                        print(f"  {Colors.WARNING}No readable prefix/search hints found in extracted core image.{Colors.ENDC}")

                    if search_uuid_lines or uuid_hits:
                        print("  search.fs_uuid / UUID matches:")
                        for ln in search_uuid_lines[:6]:
                            print(f"    {ln}")
                        if uuid_hits:
                            print(f"    UUIDs: {', '.join(uuid_hits[:6])}")
                    else:
                        print("  No embedded search.fs_uuid / UUID match found in extracted core image.")

                for i, disk_dev in enumerate(disk_devs, start=1):
                    is_boot_disk = bool(boot_disk and os.path.realpath(disk_dev) == os.path.realpath(boot_disk))
                    mark = "  [BOOT SOURCE DISK]" if is_boot_disk else ""
                    print(f"\nDisk {i}/{len(disk_devs)}: {disk_dev}{mark}")
                    pttype = (_lsblk_pttype(disk_dev) or "").strip().lower()
                    print(f"  Partition table type: {pttype or 'unknown'}")
                    parts = _parse_parted_parts(disk_dev)
                    first_start = None
                    for p in parts:
                        s = p.get('start_s')
                        if s is None:
                            continue
                        if first_start is None or s < first_start:
                            first_start = s

                    base = os.path.basename(disk_dev)
                    dump_dir = tempfile.mkdtemp(prefix=f"diskmgr_coreimg_{os.getpid()}_{ts}_{base}_", dir='/tmp')
                    os.chmod(dump_dir, 0o700)
                    stage1_state = None
                    stage15_state = None
                    raw_gap = None

                    # Method 0: MBR/sector0 signature (Stage 1).
                    raw_mbr = os.path.join(dump_dir, f"{base}_mbr.bin")
                    res_mbr = run_command_bytes(
                        ['dd', f'if={disk_dev}', 'bs=512', 'count=1', 'status=none'],
                        sudo=True,
                        check=False,
                    )
                    if getattr(res_mbr, 'returncode', 1) == 0 and getattr(res_mbr, 'stdout', b''):
                        Path(raw_mbr).write_bytes(res_mbr.stdout)
                        stage1_state = _report_raw_scan(f"MBR sector 0 on {disk_dev}", raw_mbr, 'stage1')
                    else:
                        print(f"  {Colors.WARNING}Failed sector0/MBR dump on {disk_dev}.{Colors.ENDC}")

                    # Method 1: post-MBR gap (sector 1 .. first partition start-1).
                    if first_start is not None and first_start > 1:
                        # A malformed partition table must not turn this diagnostic
                        # into an unbounded read of a whole disk. Normal BIOS gaps
                        # are far below this limit.
                        max_gap_sectors = 131072  # 64 MiB at 512-byte sectors
                        gap_count = min(max(1, first_start - 1), max_gap_sectors)
                        gap_end = gap_count
                        raw_gap = os.path.join(dump_dir, f"{base}_gap.bin")
                        res_gap = run_command_bytes(
                            ['dd', f'if={disk_dev}', 'bs=512', 'skip=1', f'count={gap_count}', 'status=none'],
                            sudo=True,
                            check=False,
                        )
                        if getattr(res_gap, 'returncode', 1) == 0 and getattr(res_gap, 'stdout', b''):
                            Path(raw_gap).write_bytes(res_gap.stdout)
                            stage15_state = _report_raw_scan(
                                f"MBR/post-MBR gap on {disk_dev} (sectors 1..{gap_end})",
                                raw_gap,
                                'stage15'
                            )
                        else:
                            print(f"  {Colors.WARNING}Failed MBR-gap dump on {disk_dev}.{Colors.ENDC}")
                    else:
                        print(f"  {Colors.WARNING}Could not determine a usable MBR embedding gap on {disk_dev}.{Colors.ENDC}")

                    # Method 2: Attempt built-in grub-unlzma style embedded core extraction from post-MBR gap.
                    if stage1_state not in ("FOUND", "POSSIBLE") and stage15_state not in ("FOUND", "POSSIBLE"):
                        print("  Skipping embedded core extraction (no Stage 1/1.5 GRUB signatures detected).")
                    elif not raw_gap or not os.path.exists(raw_gap):
                        print(f"  {Colors.WARNING}Skipping embedded core extraction (no usable MBR gap dump).{Colors.ENDC}")
                    else:
                        out_lzma1 = os.path.join(dump_dir, f"{base}_core_lzma1.bin")
                        ok1, msg1, off1 = _extract_embedded_core(raw_gap, out_lzma1, use_lzma2=False)
                        if ok1:
                            print("  Embedded core extraction (LZMA1): SUCCESS")
                            _report_core_hints(out_lzma1, msg1, off1)
                        else:
                            print(f"  Embedded core extraction (LZMA1): {Colors.WARNING}not found{Colors.ENDC}")
                            out_lzma2 = os.path.join(dump_dir, f"{base}_core_lzma2.bin")
                            ok2, msg2, off2 = _extract_embedded_core(raw_gap, out_lzma2, use_lzma2=True)
                            if ok2:
                                print("  Embedded core extraction (LZMA2): SUCCESS")
                                _report_core_hints(out_lzma2, msg2, off2)
                            else:
                                if msg1:
                                    print(f"    LZMA1 note: {msg1}")
                                if msg2:
                                    print(f"    LZMA2 note: {msg2}")

        # AWK script parses using ASCII Unit Separator (\x1f) to avoid collisions
        # with tabs/spaces present in grub.cfg command lines:
        #   Submenu <US> Entry <US> SEARCH_UUID <US> ROOT_UUID <US> SEARCH_LINE <US> LINUX_LINE <US> INITRD_LINE
        awk_script = r"""
  BEGIN { SEP=sprintf("%c",31) }
  /search[[:space:]].*--fs-uuid/ {g_search=$NF}
  /search[[:space:]].*--fs-uuid/ {g_search_line=$0}

  function trim(s) {
    sub(/^[[:space:]]+/, "", s)
    sub(/[[:space:]]+$/, "", s)
    return s
  }

  /^[[:space:]]*submenu / {
    submenu_title=$2
    next
  }

  /^[[:space:]]*menuentry / {
    e=$2; search_u=""; root_u=""; search_line=""; linux_line=""; initrd_line=""; in_entry=1
    next
  }

  in_entry && search_u=="" && /search[[:space:]].*--fs-uuid/ {search_u=$NF}
  in_entry && search_line=="" && /search[[:space:]].*--fs-uuid/ {search_line=trim($0)}
  in_entry && linux_line=="" && /(linux|linuxefi)[[:space:]]/ {linux_line=trim($0)}
  in_entry && initrd_line=="" && /(initrd|initrdefi)[[:space:]]/ {initrd_line=trim($0)}

  in_entry && root_u=="" && /(linux|linuxefi)[[:space:]].*root=UUID=/ {
    match($0,/root=UUID=[0-9a-fA-F-]+/)
    if (RSTART) root_u=substr($0,RSTART+10,RLENGTH-10)
  }

  in_entry && /^[[:space:]]*}/ {
    if (submenu_title=="") submenu_title="Top-level"
    s = (search_u!="" ? search_u : g_search)
    r = (root_u!=""   ? root_u   : "-")
    sl = (search_line!="" ? search_line : trim(g_search_line))
    ll = linux_line
    il = initrd_line
    if (e ~ /UEFI Firmware Settings/) { s="(firmware)"; r="(firmware)"; sl=""; ll=""; il="" }

    print submenu_title SEP e SEP s SEP r SEP sl SEP ll SEP il
    in_entry=0
  }
"""
        processed_devs = set()

        print(f"\n{Colors.HEADER}{Colors.BOLD}--- Partition Boot Configuration Scan ---{Colors.ENDC}")

        for dev in flat_disks:
            d_name = dev.get('name')
            d_kname = dev.get('kname', d_name)
            d_path = f"/dev/{d_kname}"
            d_type = dev.get('type')
            fstype = dev.get('fstype')
            mountpoint = dev.get('mountpoint')

            if d_type != 'part':
                continue

            if d_path in processed_devs:
                continue
            processed_devs.add(d_path)

            print(f"\n{Colors.OKBLUE}Partition: {d_path} ({fstype or 'unknown FS'}){Colors.ENDC}")

            if fstype == 'crypto_LUKS':
                print(f"  {Colors.WARNING}Result: LUKS container is LOCKED. Please 'open' this partition to scan for boot entries.{Colors.ENDC}")
                print(f"  {Colors.WARNING}Result: No fstab detected (locked encrypted partition).{Colors.ENDC}")
            elif not mountpoint:
                if fstype and fstype != '-':
                    print(f"  {Colors.WARNING}Result: Partition is UNMOUNTED. Please 'open' or mount the partition to scan for boot entries.{Colors.ENDC}")
                else:
                    print(f"  {Colors.WARNING}Result: No recognizable filesystem found.{Colors.ENDC}")
                print(f"  {Colors.WARNING}Result: No fstab detected (partition is not mounted).{Colors.ENDC}")
            else:
                cfg_path = Path(mountpoint) / "boot/grub/grub.cfg"
                fstab_path = Path(mountpoint) / "etc/fstab"
                display_path = str(cfg_path).replace("//", "/")
                try:
                    if cfg_path.exists():
                        print(f"  {Colors.OKGREEN}Result: Found GRUB config at {display_path}{Colors.ENDC}")
                        cmd = ['awk', '-F', "'", awk_script, str(cfg_path)]
                        res = run_command(cmd, sudo=True, capture_output=True)

                        if res.stdout.strip():
                            entries = {}
                            uuid_re = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
                            uuid_dev_cache = {}

                            def _normalize_whitespace(s):
                                return " ".join((s or "").split()).strip()

                            def _normalize_boot_line(line, kind):
                                line = _normalize_whitespace(line)
                                if not line:
                                    return ""
                                if kind == 'search':
                                    if re.match(r'^search\b', line):
                                        return re.sub(r'^search\b', 'search', line, count=1)
                                    # Bare UUID fallback.
                                    if re.match(r'^[0-9a-fA-F-]{36}$', line):
                                        return f"search --fs-uuid --set=root {line}"
                                    return f"search {line}"
                                if kind == 'linux':
                                    if re.match(r'^(linuxefi|linux)\b', line):
                                        return re.sub(r'^(linuxefi|linux)\b', 'linux', line, count=1)
                                    return f"linux {line}"
                                if kind == 'initrd':
                                    if re.match(r'^(initrdefi|initrd)\b', line):
                                        return re.sub(r'^(initrdefi|initrd)\b', 'initrd', line, count=1)
                                    return f"initrd {line}"
                                return line

                            def _annotate_uuids(line):
                                def _repl(m):
                                    u = m.group(0)
                                    if u not in uuid_dev_cache:
                                        uuid_dev_cache[u] = self.resolve_uuid_to_dev(u) or "-"
                                    dev = uuid_dev_cache[u]
                                    return f"{Colors.OKBLUE}{u}{Colors.ENDC} {Colors.OKCYAN}[{dev}]{Colors.ENDC}"
                                return uuid_re.sub(_repl, line or "")

                            for line in res.stdout.strip().splitlines():
                                parts = [x.strip() for x in line.split('\x1f', 6)]
                                if len(parts) >= 4:
                                    sub = parts[0]
                                    title = parts[1]
                                    s_uuid = parts[2]
                                    r_uuid = parts[3]
                                    s_line = parts[4] if len(parts) > 4 else ""
                                    l_line = parts[5] if len(parts) > 5 else ""
                                    i_line = parts[6] if len(parts) > 6 else ""
                                    if sub not in entries: entries[sub] = []
                                    entries[sub].append((title, s_uuid, r_uuid, s_line, l_line, i_line))

                            for sub, items in entries.items():
                                print(f"\n{sub}")
                                for i, (title, s_uuid, r_uuid, s_line, l_line, i_line) in enumerate(items):
                                    connector = "  └─" if i == len(items) - 1 else "  ├─"
                                    child_prefix = "     " if i == len(items) - 1 else "  │  "

                                    print(f"{connector} {title}")

                                    if s_uuid and s_uuid not in ('-', '(firmware)'):
                                        search_line = f"search --fs-uuid --set=root {s_uuid}"
                                    else:
                                        search_line = _normalize_boot_line(s_line, 'search')

                                    linux_line = _normalize_boot_line(l_line, 'linux')
                                    if not linux_line and r_uuid and r_uuid not in ('-', '(firmware)'):
                                        linux_line = f"linux /vmlinuz root=UUID={r_uuid} ro quiet"

                                    initrd_line = _normalize_boot_line(i_line, 'initrd') or "initrd /initrd.img"

                                    if search_line:
                                        annotated_search = _annotate_uuids(search_line)
                                        if re.search(r'^search\s+--fs-uuid\s+--set=root\s+', search_line):
                                            annotated_search += " (UUID of the FS to use to resolve /boot and hence which kernel and initramfs to use)"
                                        print(f"{child_prefix}  {annotated_search}")
                                    if linux_line:
                                        linux_head = linux_line
                                        linux_opts = ""
                                        if " ro " in linux_line:
                                            pre, post = linux_line.split(" ro ", 1)
                                            linux_head = pre.strip()
                                            linux_opts = f"ro {post.strip()}"
                                        annotated_linux_head = _annotate_uuids(linux_head)
                                        if re.search(r'\broot=UUID=[0-9a-fA-F-]{36}\b', linux_head):
                                            annotated_linux_head += " (UUID of the FS to use as root of the operating system; passed to kernel)"
                                        print(f"{child_prefix}  {annotated_linux_head}")
                                        if linux_opts:
                                            print(f"{child_prefix}  {_annotate_uuids(linux_opts)}")
                                    if initrd_line:
                                        print(f"{child_prefix}  {_annotate_uuids(initrd_line)}")
                        else:
                            print("  (No menu entries found in config)")
                    else:
                        print(f"  {Colors.OKCYAN}Result: Mounted at {mountpoint}, but no GRUB configuration found.{Colors.ENDC}")
                    self._render_fstab_file(fstab_path, indent="  ")
                except PermissionError:
                    print(f"  {Colors.FAIL}Result: Permission denied scanning {mountpoint} (System protected path).{Colors.ENDC}")
                    print(f"  {Colors.WARNING}Result: No fstab detected (permission denied).{Colors.ENDC}")
                except Exception as e:
                    print(f"  {Colors.FAIL}Result: Error checking path: {e}{Colors.ENDC}")
                    print(f"  {Colors.WARNING}Result: No fstab detected (scan error).{Colors.ENDC}")

            print("-" * 60)
