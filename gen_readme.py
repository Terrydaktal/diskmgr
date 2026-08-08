import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENTRYPOINT = PROJECT_ROOT / "diskmgr"

def strip_ansi(text):
    """Strip ANSI escape sequences and invisible control characters from text."""
    # Robust ANSI sequence stripper
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    # Strip \x01 and \x02 (often used as prompt delimiters)
    text = re.sub(r'[\x01\x02]', '', text)
    return text

def get_help(cmd_name=None):
    """Run diskmgr help and return the cleaned output."""
    cmd = [str(ENTRYPOINT)]
    input_str = f"help {cmd_name}\nexit\n" if cmd_name else "help\nexit\n"
    try:
        env = os.environ.copy()
        env['LC_ALL'] = 'C'
        env['LANG'] = 'C'
        # Documentation generation must never open an authentication dialog or
        # depend on privileged hardware probes just to capture help text.
        env['DISKMGR_PRIVILEGE_BACKEND'] = 'none'
        env['HOME'] = '/tmp/diskmgr-readme-home'
        env['DISKMGR_MAP_FILE'] = '/tmp/diskmgr-readme-home/diskmap.tsv'
        res = subprocess.run(cmd, input=input_str, capture_output=True, text=True, timeout=10, env=env, cwd=PROJECT_ROOT)
        return strip_ansi(res.stdout)
    except Exception as e:
        return f"Error capturing help: {e}"

def clean_diskmgr_output(raw_content):
    """Remove interactive shell artifacts from the captured output."""
    lines = raw_content.splitlines()
    content_lines = []
    for line in lines:
        if "Welcome to diskmgr" in line or "[sudo] password for" in line:
            continue
        if "(diskmgr)" in line:
            # Strip everything before and including the prompt
            idx = line.find("(diskmgr)")
            clean_line = line[idx+len("(diskmgr)"):].strip()
            # If there's still a sudo prompt after the diskmgr prompt on the same line
            if "[sudo] password for" in clean_line:
                continue
            if clean_line:
                content_lines.append(clean_line)
        else:
            content_lines.append(line.rstrip())
    
    # Optional: replace common UTF-8 symbols with ASCII if they cause issues for the user
    result = "\n".join(content_lines).strip()
    result = result.replace('≈', '~')
    result = result.replace('├─', '|--')
    result = result.replace('└─', '`--')
    result = result.replace('│', '|')
    
    return result

def main():
    commands = [
        'list', 'boot', 'map', 'unmap', 'create', 'format', 'erase', 'nuke',
        'entropy', 'entropise', 'remove', 'selftest', 'health', 'clone',
        'open', 'close', 'luks', 'label', 'remount', 'sync', 'diff', 'defrag',
        'fshealth', 'scrub', 'version', 'convert'
    ]
    
    readme_content = "# Disk Manager (diskmgr)\n\n"
    readme_content += "A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.\n\n"

    # Project Structure
    readme_content += "## Project Structure\n\n"
    readme_content += "```text\n"
    readme_content += ".\n"
    readme_content += "|-- diskmap.tsv                # Configuration file storing disk mappings\n"
    readme_content += "|-- diskmgr                    # Thin executable compatibility wrapper\n"
    readme_content += "|-- diskmgrlib/                # Modular application package\n"
    readme_content += "|   |-- app.py                 # CLI entry point\n"
    readme_content += "|   |-- shell.py               # Composed interactive shell\n"
    readme_content += "|   |-- shell_core.py          # History, prompts, and shell lifecycle\n"
    readme_content += "|   |-- shell_helpers.py       # Shared discovery, mount, and formatting helpers\n"
    readme_content += "|   |-- common.py              # Backward-compatible re-exports for older callers\n"
    readme_content += "|   |-- runtime.py             # Privileged execution, logging, output, and timing\n"
    readme_content += "|   |-- devices.py             # lsblk/sysfs device resolution helpers\n"
    readme_content += "|   |-- inventory.py            # Device inventory and formatted list data\n"
    readme_content += "|   |-- mappings.py             # Atomic persistent friendly-name mappings\n"
    readme_content += "|   |-- mount_policy.py         # Mount, fstab, ownership, and compression policy\n"
    readme_content += "|   |-- mounts.py               # Mountpoint discovery and cleanup helpers\n"
    readme_content += "|   |-- rawio.py                # Erase, sanitize, and overwrite primitives\n"
    readme_content += "|   |-- safety.py               # Destructive-operation preflight and identity checks\n"
    readme_content += "|   |-- smart.py                # SMART parsing and vendor decoding\n"
    readme_content += "|   `-- commands/               # Workflow-focused command mixins\n"
    readme_content += "|       |-- listing.py         # list\n"
    readme_content += "|       |-- boot.py            # layout and boot\n"
    readme_content += "|       |-- mapping.py         # map and unmap\n"
    readme_content += "|       |-- mounting.py        # open, close, label, remount\n"
    readme_content += "|       |-- luks.py            # LUKS management\n"
    readme_content += "|       |-- destructive.py     # erase, nuke, entropise, clone\n"
    readme_content += "|       |-- entropy.py         # entropy sampling\n"
    readme_content += "|       |-- health.py          # SMART and filesystem health commands\n"
    readme_content += "|       |-- transfer.py        # sync and diff\n"
    readme_content += "|       |-- filesystem.py      # Compatibility composition for maintenance/provisioning\n"
    readme_content += "|       |-- filesystem_maintenance.py # defrag, fshealth, and scrub\n"
    readme_content += "|       |-- provisioning.py    # format, convert, create, and remove\n"
    readme_content += "|       |-- block.py           # erase, nuke, entropise, and clone\n"
    readme_content += "|       `-- partition.py       # create and remove partitions\n"
    readme_content += "|-- tests/                     # Import and command-dispatch regression tests\n"
    readme_content += "|-- DEPENDENCIES.md            # External command and capability requirements\n"
    readme_content += "|-- gen_readme.py              # Script to regenerate this documentation\n"
    readme_content += "`-- README.md                  # This file\n"
    readme_content += "```\n\n"

    # File Descriptions
    readme_content += "## File Descriptions\n\n"
    readme_content += "### `diskmgr` and `diskmgrlib/` (Application)\n"
    readme_content += "- **Description**: The executable wrapper delegates to a composed interactive shell. Shared behavior is separated into runtime, device, inventory, mapping, mount-policy, safety, raw-I/O, and SMART modules; command groups live under `diskmgrlib/commands/`.\n"
    readme_content += "- **Inputs**: User commands via interactive shell or one-shot CLI invocation; system hardware information via `lsblk`, `udevadm`, `cryptsetup`, and related tools.\n"
    readme_content += "- **Outputs**: Formatted tables, system state changes (mounts, encryption status), command logs, and updates to `diskmap.tsv`.\n\n"
    
    readme_content += "### `diskmap.tsv` (Configuration)\n"
    readme_content += "- **Description**: Tab-separated values file that stores the mapping between user-defined friendly names and persistent device paths (e.g., `/dev/disk/by-id/...`).\n"
    readme_content += "- **Format**: `<friendly_name>\\t<persistent_device_path>`\n\n"

    readme_content += "### `gen_readme.py` (Documentation Generator)\n"
    readme_content += "- **Description**: Automates the generation of `README.md` by querying `diskmgr`'s help system and examples.\n"
    readme_content += "- **Inputs**: `diskmgr` help output and command examples.\n"
    readme_content += "- **Outputs**: An updated `README.md` file.\n\n"

    readme_content += "### `DEPENDENCIES.md` (Capability Manifest)\n"
    readme_content += "- **Description**: Lists Python/runtime requirements and optional command-line capabilities by workflow.\n"
    readme_content += "- **Inputs**: None.\n"
    readme_content += "- **Outputs**: Documentation only; the application does not install packages.\n\n"

    # 1. Main Help / Overview
    main_help = clean_diskmgr_output(get_help())
    readme_content += "## Overview\n\n```text\n" + main_help + "\n```\n\n"

    # 2. Individual Command Reference
    for cmd in commands:
        help_text = clean_diskmgr_output(get_help(cmd))
        if help_text:
            readme_content += f"## Command Reference: `{cmd}`\n\n```text\n" + help_text + "\n```\n\n"
            
    readme_content += "## Configuration\n\nMappings are stored in `diskmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:\n\n```text\n<friendly_name>\t<persistent_device_path>\n```\n"
    readme_content += "\n## Author\n\nTerrydaktal <9lewis9@gmail.com>\n"

    with (PROJECT_ROOT / 'README.md').open('w', encoding='utf-8') as f:
        f.write(readme_content)
    print("README.md has been successfully regenerated.")

if __name__ == "__main__":
    main()
