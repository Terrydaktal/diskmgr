"""Command-line entry point for diskmgr."""

import os
import shlex
import sys

from .shell import DiskMgrShell


def main():
    try:
        os.chmod(os.path.join(os.path.dirname(os.path.dirname(__file__)), "diskmgr"), 0o755)
    except OSError:
        pass
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
        else:
            shell.cmdloop()
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
