"""Command-line entry point for diskmgr."""

import shlex
import sys

from .shell import DiskMgrShell
from .runtime import log


def main():
    try:
        shell = DiskMgrShell()
        if len(sys.argv) > 1:
            cmd_name = str(sys.argv[1] or "").strip()
            cmd_args = [str(arg) for arg in sys.argv[2:]]
            line = cmd_name
            if cmd_args:
                line += " " + " ".join(shlex.quote(arg) for arg in cmd_args)
            shell.onecmd(line)
            shell._save_history()
            return shell.last_command_status
        else:
            shell.cmdloop()
            return 0
    except KeyboardInterrupt:
        print("\nExiting...")
        return 130
    except Exception as exc:
        log(f"Fatal diskmgr error: {type(exc).__name__}: {exc}", 'ERROR')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
