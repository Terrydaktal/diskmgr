"""Process execution, logging, configuration, and shared runtime utilities."""

import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path

# Configuration
MAP_FILENAME = 'diskmap.tsv'
PASSGEN_BIN = 'passgen'
VERSION = '3.6.6'
HISTORY_FILE_ENV = 'DISKMGR_HISTORY'
DEFAULT_HISTORY_FILE = Path.home() / '.local' / 'state' / 'diskmgr' / 'history'
MAX_HISTORY_ENTRIES = 5000
LUKS_PBKDF_DEFAULT_THREADS = 4
LUKS_PBKDF_DEFAULT_TIME = 8
LUKS_PBKDF_DEFAULT_MEMORY_KIB = 4 * 1024 * 1024
LUKS_PBKDF_DEFAULT_MEMORY_LABEL = '4GiB'
LUKS_HEADER_BACKUP_DIR = Path.home() / '.local' / 'share' / 'diskmgr'

_CMD_LOG_FH = None
_CMD_LOG_PATH = None
_COMMAND_ERROR_COUNT = 0
_COMMAND_ERROR_LOCK = threading.Lock()


class CommandExecutionError(RuntimeError):
    """Raised when diskmgr cannot execute a command safely."""

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[38;5;117m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def sanitize_terminal_text(value):
    """Make untrusted text safe to print in a terminal while preserving lines/tabs."""
    text = str(value if value is not None else "")
    out = []
    for char in text:
        code = ord(char)
        if char in ('\n', '\r', '\t'):
            out.append(char)
        elif code == 0x1B or code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(char)
    return ''.join(out)


def reset_command_status():
    global _COMMAND_ERROR_COUNT
    with _COMMAND_ERROR_LOCK:
        _COMMAND_ERROR_COUNT = 0


def command_failed():
    with _COMMAND_ERROR_LOCK:
        return _COMMAND_ERROR_COUNT > 0


def mark_command_failed():
    global _COMMAND_ERROR_COUNT
    with _COMMAND_ERROR_LOCK:
        _COMMAND_ERROR_COUNT += 1


def get_command_log_path():
    return _CMD_LOG_PATH


def command_log_is_open():
    return _CMD_LOG_FH is not None

def _cmd_log_write(text):
    global _CMD_LOG_FH
    if _CMD_LOG_FH is None:
        return
    try:
        text = sanitize_terminal_text(text)
        _CMD_LOG_FH.write(text)
        if not text.endswith("\n"):
            _CMD_LOG_FH.write("\n")
        _CMD_LOG_FH.flush()
    except Exception:
        # Best-effort logging: never break the tool because logs can't be written.
        pass

def _cmd_log_open(prefix):
    """Enable per-command logging in a private, non-predictable temporary file."""
    global _CMD_LOG_FH, _CMD_LOG_PATH
    try:
        fd, path = tempfile.mkstemp(prefix=f"diskmgr_{prefix}_", suffix='.log', dir='/tmp', text=True)
        os.fchmod(fd, 0o600)
        _CMD_LOG_FH = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
        _CMD_LOG_PATH = path
        _cmd_log_write(f"# diskmgr {VERSION}")
        _cmd_log_write(f"# started: {datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}")
        return path
    except Exception:
        _CMD_LOG_FH = None
        _CMD_LOG_PATH = None
        return None

def _cmd_log_close():
    global _CMD_LOG_FH, _CMD_LOG_PATH
    try:
        if _CMD_LOG_FH is not None:
            _cmd_log_write(f"# ended: {datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}")
            _CMD_LOG_FH.close()
    except Exception:
        pass
    _CMD_LOG_FH = None
    _CMD_LOG_PATH = None

def log(msg, level='INFO'):
    msg = sanitize_terminal_text(msg)
    _cmd_log_write(f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] {level}: {msg}")
    if level == 'ERROR':
        mark_command_failed()
        print(f"{Colors.FAIL}ERROR: {msg}{Colors.ENDC}", file=sys.stderr)
    elif level == 'WARN':
        print(f"{Colors.WARNING}WARNING: {msg}{Colors.ENDC}", file=sys.stderr)
    else:
        print(f"{Colors.OKBLUE}diskmgr: {msg}{Colors.ENDC}")

def _fmt_hms(total_seconds):
    s = int(max(total_seconds, 0))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"

def _command_environment(extra_env=None):
    """Return an environment suitable for machine-parsed command output."""
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    return env


def privileged_command(command):
    """Wrap a command in the configured privilege backend.

    The default is pkexec so authentication is handled by Polkit. Tests and
    already-privileged callers can set DISKMGR_PRIVILEGE_BACKEND=none.
    """
    command = [str(part) for part in command]
    if os.geteuid() == 0:
        return command
    backend = os.environ.get('DISKMGR_PRIVILEGE_BACKEND', 'pkexec').strip().lower()
    if backend in ('', 'pkexec'):
        binary = shutil.which('pkexec')
        if not binary:
            raise CommandExecutionError(
                "pkexec is required for privileged operations but was not found"
            )
        return [binary] + command
    if backend in ('none', 'direct'):
        return command
    raise CommandExecutionError(
        f"Unsupported DISKMGR_PRIVILEGE_BACKEND={backend!r}; use 'pkexec' or 'none'"
    )


def prepare_command(command, privileged=False):
    prepared = [str(part) for part in command]
    if not prepared or not prepared[0]:
        raise ValueError("command must contain an executable")
    return privileged_command(prepared) if privileged else prepared


def _logged_command(command):
    """Render a command for logs without leaking inline device credentials."""
    parts = [sanitize_terminal_text(part) for part in command]
    secret_after = {
        '--security-set-pass', '--security-erase', '--security-erase-enhanced',
        '--security-disable', '--user-master',
    }
    redacted = []
    redact_next = False
    for part in parts:
        if redact_next:
            redacted.append('<redacted>')
            redact_next = False
            continue
        redacted.append(part)
        if part in secret_after:
            # --user-master is not itself a password, but the next option in
            # the hdparm form is still logged normally. The actual password
            # flags above are what trigger redaction.
            redact_next = part != '--user-master'
    return shlex.join(redacted)


def run_command(
    command,
    check=True,
    input_str=None,
    capture_output=True,
    sudo=False,
    timeout=None,
    env=None,
):
    """Run a text-mode command.

    ``sudo`` is retained as a compatibility keyword and means "privileged";
    the implementation always delegates to the configured privilege backend.
    """
    command = prepare_command(command, privileged=sudo)

    try:
        start_ts = time.time()
        _cmd_log_write(
            f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] "
            f"CMD: {_logged_command(command)}"
        )
        result = subprocess.run(
            command,
            input=input_str,
            text=True,
            check=check,
            timeout=timeout,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            env=_command_environment(env),
        )
        _cmd_log_write(f"RC: {getattr(result, 'returncode', 0)}  elapsed={_fmt_hms(time.time() - start_ts)}")
        if capture_output:
            out = getattr(result, 'stdout', '') or ''
            err = getattr(result, 'stderr', '') or ''
            if out.strip():
                _cmd_log_write("--- STDOUT ---")
                _cmd_log_write(out.rstrip())
            if err.strip():
                _cmd_log_write("--- STDERR ---")
                _cmd_log_write(err.rstrip())
        return result
    except subprocess.CalledProcessError as e:
        if check:
            log(f"Command failed: {_logged_command(command)}", 'ERROR')
            if e.stderr:
                log(e.stderr.strip(), 'ERROR')
            raise
        return e
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode('utf-8', errors='replace') if e.stdout else '')
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode('utf-8', errors='replace') if e.stderr else '')
        msg = f"Command timed out after {timeout}s: {_logged_command(command)}"
        if check:
            log(msg, 'ERROR')
            raise
        _cmd_log_write(f"TIMEOUT: {msg}")
        if out.strip():
            _cmd_log_write("--- STDOUT (partial) ---")
            _cmd_log_write(out.rstrip())
        if err.strip():
            _cmd_log_write("--- STDERR (partial) ---")
            _cmd_log_write(err.rstrip())
        return subprocess.CompletedProcess(command, 124, out, err)


def run_command_bytes(command, check=True, capture_output=True, sudo=False, timeout=None, env=None):
    """Run a command without text decoding; use for block-device verification."""
    command = prepare_command(command, privileged=sudo)
    start_ts = time.time()
    _cmd_log_write(
        f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] "
        f"CMD(binary): {_logged_command(command)}"
    )
    try:
        result = subprocess.run(
            command,
            check=check,
            timeout=timeout,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            env=_command_environment(env),
        )
        _cmd_log_write(
            f"RC: {getattr(result, 'returncode', 0)}  "
            f"elapsed={_fmt_hms(time.time() - start_ts)}"
        )
        return result
    except subprocess.CalledProcessError as exc:
        if check:
            log(f"Command failed: {_logged_command(command)}", 'ERROR')
            detail = exc.stderr.decode('utf-8', errors='replace') if exc.stderr else ''
            if detail.strip():
                log(detail.strip(), 'ERROR')
            raise
        return exc


def popen_command(command, sudo=False, env=None, **kwargs):
    """Start a process through the same privilege and locale policy as run_command."""
    prepared = prepare_command(command, privileged=sudo)
    _cmd_log_write(
        f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] "
        f"POPEN: {_logged_command(prepared)}"
    )
    kwargs.setdefault('env', _command_environment(env))
    return subprocess.Popen(prepared, **kwargs)

def run_command_hard_timeout(command, seconds, check=True, input_str=None, capture_output=True, sudo=False):
    """
    Run a command with a bounded userspace timeout using coreutils ``timeout``.

    The timeout includes privilege authentication. No userspace timeout can kill
    a task that the kernel has placed in uninterruptible (D-state) I/O wait.
    """
    try:
        sec = float(seconds)
    except Exception:
        sec = 0.0
    if sec <= 0:
        return run_command(
            command,
            check=check,
            input_str=input_str,
            capture_output=capture_output,
            sudo=sudo,
        )

    timeout_bin = shutil.which('timeout')
    if timeout_bin:
        sec_s = f"{sec:g}s"
        # Build the privileged invocation first so authentication is inside the
        # timeout rather than preceding an unbounded probe.
        wrapped = ['timeout', '-k', '1s', sec_s] + prepare_command(command, privileged=sudo)
        return run_command(
            wrapped,
            check=check,
            input_str=input_str,
            capture_output=capture_output,
            sudo=False,
        )

    # Fallback to Python-level timeout when coreutils timeout is unavailable.
    return run_command(
        command,
        check=check,
        input_str=input_str,
        capture_output=capture_output,
        sudo=sudo,
        timeout=sec,
    )

def _split_nonempty_lines(s):
    if not s:
        return []
    out = []
    for line in str(s).splitlines():
        line = line.strip()
        if line and line not in out:
            out.append(line)
    return out

def _first_int_from_text(s):
    if s is None:
        return None
    m = re.search(r"([0-9][0-9,]*)", str(s))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""), 10)
    except ValueError:
        return None

def _find_tool_or_common_paths(tool_name, common_paths):
    """
    Find an executable by PATH, or fall back to common sbin locations.

    This avoids failures when running as a normal user with /usr/sbin not in PATH.
    Returns an absolute path or None.
    """
    p = shutil.which(tool_name)
    if p:
        return p
    for cp in common_paths:
        try:
            if cp and os.path.exists(cp) and os.access(cp, os.X_OK):
                return cp
        except Exception:
            continue
    return None
