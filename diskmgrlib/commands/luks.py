"""LuksCommands command implementations."""

import argparse
import cmd
import os
import re
import shlex
import tempfile
from pathlib import Path
from ..runtime import LUKS_HEADER_BACKUP_DIR, LUKS_PBKDF_DEFAULT_THREADS, LUKS_PBKDF_DEFAULT_TIME, PASSGEN_BIN, log, run_command
from ..shell_core import CmdArgumentParser
from ..mappings import validate_mapping_name
from ..runtime import sanitize_terminal_text
from ..safety import validate_absolute_path


class LuksCommands:

    def do_luks(self, arg):
        '''LUKS encryption management: luks <passwd|params|backup|restore|header|wipe> [options]

        Subcommands:
          passwd <name>
                                 Change the LUKS passphrase (old from passgen, new passphrase confirmed via passgen twice).
          params <name> time=<N> memory=<VALUE> parallelism=<N>
                                 Change PBKDF parameters without changing passphrase.
                                 Example: luks params 7a time=8 memory=4GiB parallelism=4
          backup <name> [file]    Save the LUKS header to a file.
                                 Default file when omitted: ~/.local/share/diskmgr/<name>
          restore <name> <file>   Restore the LUKS header from a file (Destructive).
          header <name>           Print the current LUKS header (cryptsetup luksDump).
          wipe <name>             Overwrite LUKS header/keyslots with random data (Destructive; test helper).
        '''
        args = shlex.split(arg) if arg else []
        if not args:
            self.do_help('luks')
            return

        subcmd = args[0]
        sub_args = args[1:]
        if subcmd in ('help', '-h', '--help'):
            self.do_help('luks')
            return

        def _parse_memory_to_kib(spec):
            s = str(spec or '').strip()
            if not s:
                return None
            m = re.fullmatch(r'(?i)\s*([0-9]+(?:\.[0-9]+)?)\s*(kib|mib|gib|tib)\s*', s)
            if not m:
                return None
            try:
                val = float(m.group(1))
            except Exception:
                return None
            unit = (m.group(2) or '').strip().lower()

            if unit == 'kib':
                kib = val
            elif unit == 'mib':
                kib = val * 1024.0
            elif unit == 'gib':
                kib = val * (1024.0 ** 2)
            elif unit == 'tib':
                kib = val * (1024.0 ** 3)
            else:
                return None
            try:
                out = int(round(kib))
            except Exception:
                return None
            if out <= 0:
                return None
            return out

        def _write_temp_key(prefix, payload):
            tmp_dir = '/dev/shm' if os.path.isdir('/dev/shm') else None
            fd, path = tempfile.mkstemp(prefix=prefix, dir=tmp_dir, text=True)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f_key:
                    f_key.write(payload)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return path

        def _cleanup_temp_files(*paths):
            for p in paths:
                if not p:
                    continue
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

        if subcmd == 'passwd':
            parser = CmdArgumentParser(prog='luks passwd', add_help=False)
            parser.add_argument('name')
            try:
                parsed = parser.parse_args(sub_args)
            except argparse.ArgumentError as e:
                log(str(e), 'ERROR')
                return
            except SystemExit:
                return

            name = parsed.name
            try:
                name = validate_mapping_name(name)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            src = self.resolve_target(name, allow_id=False)
            if not src:
                log(f"Unknown mapping: '{name}'.", 'ERROR')
                return
            devnode = os.path.realpath(src)
            if self._block_if_root_drive(devnode, f"luks passwd {name}"):
                return
            detached_header = str(LUKS_HEADER_BACKUP_DIR / name)
            use_detached_header = False
            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode != 0:
                if os.path.isfile(detached_header):
                    use_detached_header = True
                    log(
                        f"On-disk LUKS signature not found for {name} ({devnode}); "
                        f"using detached header for passphrase update: {detached_header}"
                    )
                else:
                    log(f"Device {name} ({devnode}) is not a LUKS encrypted device.", 'ERROR')
                    return
            log(f"Starting passphrase change for {name} ({devnode})...")
            old_key_file = None
            new_key_file = None
            try:
                log("Generating CURRENT passphrase via passgen (any prompt like 'Site:' is from passgen).")
                old_key = run_command([PASSGEN_BIN], capture_output=True).stdout
                log("Generating NEW passphrase via passgen (first entry; prompt like 'Site:' is from passgen).")
                new_key = run_command([PASSGEN_BIN], capture_output=True).stdout
                log("Confirming NEW passphrase via passgen (second entry; prompt like 'Site:' is from passgen).")
                new_key_confirm = run_command([PASSGEN_BIN], capture_output=True).stdout
                if not str(old_key or '').strip():
                    log("passgen returned an empty current passphrase.", 'ERROR')
                    return
                if not str(new_key or '').strip():
                    log("passgen returned an empty new passphrase.", 'ERROR')
                    return
                if not str(new_key_confirm or '').strip():
                    log("passgen returned an empty new-passphrase confirmation.", 'ERROR')
                    return
                if str(new_key) != str(new_key_confirm):
                    log("New passphrase confirmation mismatch. Aborting without changing the LUKS key.", 'ERROR')
                    return
                if str(new_key) == str(old_key):
                    log("New passphrase equals current passphrase. No change applied.", 'WARN')
                    return

                old_key_file = _write_temp_key('diskmgr_luks_old_', old_key)
                new_key_file = _write_temp_key('diskmgr_luks_new_', new_key)

                cmd_add = ['cryptsetup', 'luksAddKey']
                if use_detached_header:
                    cmd_add.extend(['--header', detached_header])
                cmd_add.extend([
                    devnode,
                    '--key-file', old_key_file,
                    '--new-keyfile', new_key_file,
                ])
                run_command(cmd_add, sudo=True, capture_output=False)

                cmd_remove = ['cryptsetup', 'luksRemoveKey']
                if use_detached_header:
                    cmd_remove.extend(['--header', detached_header])
                cmd_remove.extend([
                    devnode,
                    '--key-file', old_key_file,
                ])
                run_command(cmd_remove, sudo=True, capture_output=False)
                log("Passphrase updated successfully (added new key and removed old key).")
            except Exception as e:
                log(f"Failed to change passphrase: {e}", 'ERROR')
            finally:
                _cleanup_temp_files(old_key_file, new_key_file)

        elif subcmd in ('params', 'memory'):
            if subcmd == 'memory':
                log("Subcommand 'luks memory' was renamed to 'luks params'. Continuing in compatibility mode.", 'WARN')

            if len(sub_args) < 2:
                log("Usage: luks params <name> time=<N> memory=<VALUE> parallelism=<N>", 'ERROR')
                return

            name = sub_args[0]
            try:
                name = validate_mapping_name(name)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            kv_tokens = sub_args[1:]
            opts = {
                'time': str(LUKS_PBKDF_DEFAULT_TIME),
                'parallelism': str(LUKS_PBKDF_DEFAULT_THREADS),
                'memory': None,
            }
            for tok in kv_tokens:
                if '=' not in tok:
                    log(f"Invalid parameter token: {tok}. Expected key=value.", 'ERROR')
                    return
                k, v = tok.split('=', 1)
                key = (k or '').strip().lower()
                val = (v or '').strip()
                if key not in ('time', 'memory', 'parallelism'):
                    log(f"Unknown parameter key: {k}. Allowed: time, memory, parallelism", 'ERROR')
                    return
                if not val:
                    log(f"Empty value for key: {k}", 'ERROR')
                    return
                opts[key] = val

            if opts['memory'] is None:
                log("Missing required parameter: memory=<VALUE>", 'ERROR')
                return

            mem_kib = _parse_memory_to_kib(opts['memory'])
            if mem_kib is None:
                log(f"Invalid memory value: {opts['memory']}. Use KiB/MiB/GiB/TiB (e.g. 4GiB, 512MiB).", 'ERROR')
                return
            try:
                time_cost = int(str(opts['time']).strip())
            except Exception:
                log(f"Invalid time value: {opts['time']}. Use a positive integer.", 'ERROR')
                return
            try:
                parallelism = int(str(opts['parallelism']).strip())
            except Exception:
                log(f"Invalid parallelism value: {opts['parallelism']}. Use a positive integer.", 'ERROR')
                return
            if time_cost <= 0:
                log("time must be a positive integer.", 'ERROR')
                return
            if parallelism <= 0:
                log("parallelism must be a positive integer.", 'ERROR')
                return

            src = self.resolve_target(name, allow_id=False)
            if not src:
                log(f"Unknown mapping: '{name}'.", 'ERROR')
                return
            devnode = os.path.realpath(src)

            if self._block_if_root_drive(devnode, f"luks params {name} time={time_cost} memory={opts['memory']} parallelism={parallelism}"):
                return

            detached_header = str(LUKS_HEADER_BACKUP_DIR / name)
            use_detached_header = False
            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode != 0:
                if os.path.isfile(detached_header):
                    use_detached_header = True
                    log(
                        f"On-disk LUKS signature not found for {name} ({devnode}); "
                        f"using detached header for PBKDF parameter update: {detached_header}"
                    )
                else:
                    log(f"Device {name} ({devnode}) is not a LUKS encrypted device.", 'ERROR')
                    return

            log(f"Starting PBKDF parameter update for {name} ({devnode})...")
            key_file = None
            try:
                log("Generating CURRENT passphrase via passgen (any prompt like 'Site:' is from passgen).")
                cur_key = run_command([PASSGEN_BIN], capture_output=True).stdout
                if not str(cur_key or '').strip():
                    log("passgen returned an empty current passphrase.", 'ERROR')
                    return
                key_file = _write_temp_key('diskmgr_luks_cur_', cur_key)
                log(
                    f"Setting PBKDF params: memory={opts['memory']} ({mem_kib:,} KiB), "
                    f"argon2id, parallelism={parallelism}, time={time_cost}"
                )
                cmd = ['cryptsetup', 'luksConvertKey']
                if use_detached_header:
                    cmd.extend(['--header', detached_header])
                cmd.extend([
                    devnode,
                    '--key-file', key_file,
                    '--pbkdf', 'argon2id',
                    '--pbkdf-memory', str(mem_kib),
                    '--pbkdf-parallel', str(parallelism),
                    '--pbkdf-force-iterations', str(time_cost),
                ])
                run_command(cmd, sudo=True, capture_output=False)
                log("PBKDF parameters updated successfully.")
            except Exception as e:
                log(f"Failed to update PBKDF parameters: {e}", 'ERROR')
            finally:
                _cleanup_temp_files(key_file)

        elif subcmd == 'backup':
            if not sub_args:
                log("Usage: luks backup <name> [filename]", 'ERROR')
                return
            name = sub_args[0]
            try:
                name = validate_mapping_name(name)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            if len(sub_args) > 1:
                filename = validate_absolute_path(
                    os.path.abspath(os.path.expanduser(sub_args[1])), 'header backup path'
                )
            else:
                try:
                    LUKS_HEADER_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    log(f"Failed to create default backup directory {LUKS_HEADER_BACKUP_DIR}: {e}", 'ERROR')
                    return
                filename = validate_absolute_path(
                    str(LUKS_HEADER_BACKUP_DIR / name), 'header backup path'
                )
            try:
                Path(filename).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except Exception as exc:
                log(f"Could not prepare header backup directory: {exc}", 'ERROR')
                return
            src = self.resolve_target(name)
            if not src:
                log(f"Unknown target: '{name}'", 'ERROR')
                return
            devnode = os.path.realpath(src)
            if self._block_if_root_drive(devnode, f"luks backup {name}"):
                return
            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode != 0:
                log(f"Device {devnode} is not a valid LUKS device.", 'ERROR')
                return
            log(f"Backing up LUKS header from {name} ({devnode}) to {filename}...")
            try:
                run_command(['cryptsetup', 'luksHeaderBackup', devnode, '--header-backup-file', filename], sudo=True)
                log(f"Header backup successful: {filename}")
            except Exception as e:
                log(f"Backup failed: {e}", 'ERROR')

        elif subcmd == 'restore':
            if len(sub_args) < 2:
                log("Usage: luks restore <name> <filename>", 'ERROR')
                return
            name, filename = sub_args[0], sub_args[1]
            try:
                name = validate_mapping_name(name)
                filename = validate_absolute_path(
                    os.path.abspath(os.path.expanduser(filename)), 'header backup path',
                    allow_missing=False,
                )
            except ValueError as exc:
                log(f"Invalid LUKS restore path: {exc}", 'ERROR')
                return
            if not os.path.isfile(filename):
                log(f"Backup file not found: {filename}", 'ERROR')
                return
            if os.path.getsize(filename) <= 0 or os.path.getsize(filename) > 128 * 1024 * 1024:
                log("Backup file size is outside the safe LUKS-header range.", 'ERROR')
                return
            src = self.resolve_target(name)
            if not src:
                log(f"Unknown target: '{name}'", 'ERROR')
                return
            devnode = os.path.realpath(src)
            if self._block_if_root_drive(devnode, f"luks restore {name}"):
                return
            preflight = self._destructive_safety_preflight(devnode)
            if not preflight.get('ok'):
                for error in preflight.get('errors') or ['unknown safety probe failure']:
                    log(f"RESTORE BLOCKED: {error}", 'ERROR')
                return
            self._format_print_preflight(preflight)
            if self._active_use_present(preflight.get('active')):
                log("RESTORE BLOCKED: target or a child is active. Close/unmount it first.", 'ERROR')
                self._format_release_device_lock(preflight.get('lock_fd'))
                return
            log(f"RESTORE WARNING: About to overwrite LUKS header on {name} ({devnode}) using {filename}")
            if not self.extensive_confirm(name):
                self._format_release_device_lock(preflight.get('lock_fd'))
                return
            stable, postflight = self._destructive_revalidate(preflight, preflight.get('lock_fd'))
            if not stable:
                for error in postflight.get('errors') or ['target changed after confirmation']:
                    log(f"RESTORE BLOCKED after confirmation: {error}", 'ERROR')
                self._format_release_device_lock(preflight.get('lock_fd'))
                return
            try:
                run_command(['cryptsetup', 'luksHeaderRestore', devnode, '--header-backup-file', filename], sudo=True)
                verify = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
                if getattr(verify, 'returncode', 1) != 0:
                    raise RuntimeError("restored header is not recognized as a LUKS device")
                log("Header restore completed successfully.")
            except Exception as e:
                log(f"Restore failed: {e}", 'ERROR')
            finally:
                self._format_release_device_lock(preflight.get('lock_fd'))
        elif subcmd == 'header':
            if not sub_args:
                log("Usage: luks header <name>", 'ERROR')
                return
            name = sub_args[0]
            try:
                name = validate_mapping_name(name)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            src = self.resolve_target(name)
            if not src:
                log(f"Unknown target: '{name}'", 'ERROR')
                return
            devnode = os.path.realpath(src)
            if self._block_if_root_drive(devnode, f"luks header {name}"):
                return
            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode != 0:
                log(f"Device {devnode} is not a valid LUKS device.", 'ERROR')
                return
            log(f"Printing LUKS header for {name} ({devnode})...")
            try:
                dump = run_command(['cryptsetup', 'luksDump', devnode], sudo=True, capture_output=True)
                print(sanitize_terminal_text(getattr(dump, 'stdout', '') or ""))
            except Exception as e:
                log(f"Failed to print LUKS header: {e}", 'ERROR')
        elif subcmd == 'wipe':
            if len(sub_args) != 1:
                log("Usage: luks wipe <name>", 'ERROR')
                return
            name = sub_args[0]
            try:
                name = validate_mapping_name(name)
            except ValueError as exc:
                log(f"Invalid mapping name: {exc}", 'ERROR')
                return
            src = self.resolve_target(name)
            if not src:
                log(f"Unknown target: '{name}'", 'ERROR')
                return
            devnode = os.path.realpath(src)
            if self._block_if_root_drive(devnode, f"luks wipe {name}"):
                return

            res = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
            if res.returncode != 0:
                log(f"Device {devnode} is not a valid LUKS device.", 'ERROR')
                return

            preflight = self._destructive_safety_preflight(devnode)
            if not preflight.get('ok'):
                for error in preflight.get('errors') or ['unknown safety probe failure']:
                    log(f"WIPE BLOCKED: {error}", 'ERROR')
                return
            self._format_print_preflight(preflight)
            if self._active_use_present(preflight.get('active')):
                log("WIPE BLOCKED: target or a child is active. Close/unmount it first.", 'ERROR')
                self._format_release_device_lock(preflight.get('lock_fd'))
                return

            # Determine how much of the start of the device to wipe:
            # use data-segment offset from luksDump when available; fallback to 16 MiB.
            try:
                dump = run_command(['cryptsetup', 'luksDump', devnode], sudo=True, capture_output=True, check=False)
                if getattr(dump, 'returncode', 1) != 0:
                    raise RuntimeError("cryptsetup luksDump failed")
                out = getattr(dump, 'stdout', '') or ''
                offsets = [int(value) for value in re.findall(
                    r'^\s*offset:\s*([0-9]+)\s+\[bytes\]', out, flags=re.MULTILINE
                )]
                if not offsets or offsets[0] <= 0 or offsets[0] > 128 * 1024 * 1024:
                    raise RuntimeError("could not determine a bounded LUKS data offset")
                wipe_bytes = offsets[0]
            except Exception as exc:
                log(f"WIPE BLOCKED: refusing to guess the LUKS header span: {exc}", 'ERROR')
                self._format_release_device_lock(preflight.get('lock_fd'))
                return
            bs = 4096
            blocks = max(1, (wipe_bytes + bs - 1) // bs)
            wipe_len = blocks * bs
            wipe_len_pretty = self._format_bytes_binary(str(wipe_len), decimals=2)

            log(
                f"WIPE WARNING: About to overwrite first {wipe_len_pretty} "
                f"({wipe_len:,} bytes) of {name} ({devnode}) with random data."
            )
            log("This is destructive and intended only for detached-header/restore testing.", 'WARN')
            if not self.extensive_confirm(f"luks wipe {name} ({devnode})"):
                self._format_release_device_lock(preflight.get('lock_fd'))
                return

            try:
                stable, postflight = self._destructive_revalidate(preflight, preflight.get('lock_fd'))
                if not stable:
                    for error in postflight.get('errors') or ['target changed after confirmation']:
                        log(f"WIPE BLOCKED after confirmation: {error}", 'ERROR')
                    return
                run_command(
                    ['dd', 'if=/dev/urandom', f'of={devnode}', f'bs={bs}', f'count={blocks}',
                     'iflag=fullblock', 'conv=notrunc,fsync', 'status=progress'],
                    sudo=True,
                    capture_output=False
                )
                run_command(['sync'], sudo=True, capture_output=False)
                verify = run_command(['cryptsetup', 'isLuks', devnode], sudo=True, check=False)
                if getattr(verify, 'returncode', 0) == 0:
                    raise RuntimeError("LUKS signature is still recognized after header wipe")
                log(f"LUKS header wipe completed: wrote {wipe_len:,} bytes of random data to {devnode}.")
                log("Device should now fail on-disk-header unlock until restored or opened with detached header.", 'WARN')
            except Exception as e:
                log(f"LUKS header wipe failed: {e}", 'ERROR')
            finally:
                self._format_release_device_lock(preflight.get('lock_fd'))
        else:
            log(f"Unknown LUKS subcommand: {subcmd}", 'ERROR')
            self.do_help('luks')
