import os
import re
import subprocess

def strip_ansi(text):
    """Strip ANSI escape sequences from text."""
    return re.sub(r'\x1b\[([0-9,;]*[mGJKHF])', '', text)

def get_help(cmd_name=None):
    """Run diskmgr help and return the cleaned output."""
    cmd = ["./diskmgr"]
    input_str = f"help {cmd_name}\nexit\n" if cmd_name else "help\nexit\n"
    try:
        res = subprocess.run(cmd, input=input_str, capture_output=True, text=True, timeout=10)
        return strip_ansi(res.stdout)
    except Exception as e:
        return f"Error capturing help: {e}"

def get_example(cmd_name):
    """Run a command and return its output for documentation examples."""
    cmd = ["./diskmgr"]
    input_str = f"{cmd_name}\nexit\n"
    try:
        res = subprocess.run(cmd, input=input_str, capture_output=True, text=True, timeout=15)
        return strip_ansi(res.stdout)
    except Exception as e:
        return ""

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
    return "\n".join(content_lines).strip()

def main():
    commands = [
        'list', 'boot', 'map', 'unmap', 'create', 'format', 'erase', 'nuke',
        'remove', 'selftest', 'health', 'clone', 'open', 'close', 'luks',
        'label', 'remount', 'sync', 'defrag', 'fshealth', 'scrub', 'version',
        'convert'
    ]
    
    readme_content = "# Disk Manager (diskmgr)\n\n"
    readme_content += "A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.\n\n"

    # Project Structure
    readme_content += "## Project Structure\n\n"
    readme_content += "```text\n"
    readme_content += ".\n"
    readme_content += "├── diskmap.tsv      # Configuration file storing disk mappings\n"
    readme_content += "├── diskmgr          # Main Python-based interactive CLI tool\n"
    readme_content += "├── gen_readme.py    # Script to regenerate this documentation\n"
    readme_content += "└── README.md        # This file\n"
    readme_content += "```\n\n"

    # File Descriptions
    readme_content += "## File Descriptions\n\n"
    readme_content += "### `diskmgr` (Main Application)\n"
    readme_content += "- **Description**: A comprehensive interactive shell for managing disks, LUKS containers, and filesystems.\n"
    readme_content += "- **Inputs**: User commands via interactive shell or pipe; system hardware information via `lsblk`, `udevadm`, `cryptsetup`, etc.\n"
    readme_content += "- **Outputs**: Formatted tables, system state changes (mounts, encryption status), and updates to `diskmap.tsv`.\n\n"
    
    readme_content += "### `diskmap.tsv` (Configuration)\n"
    readme_content += "- **Description**: Tab-separated values file that stores the mapping between user-defined friendly names and persistent device paths (e.g., `/dev/disk/by-id/...`).\n"
    readme_content += "- **Format**: `<friendly_name>\\t<persistent_device_path>`\n\n"

    readme_content += "### `gen_readme.py` (Documentation Generator)\n"
    readme_content += "- **Description**: Automates the generation of `README.md` by querying `diskmgr`'s help system and examples.\n"
    readme_content += "- **Inputs**: `diskmgr` help output and command examples.\n"
    readme_content += "- **Outputs**: An updated `README.md` file.\n\n"

    # 1. Main Help / Overview
    main_help = clean_diskmgr_output(get_help())
    readme_content += "## Overview\n\n```text\n" + main_help + "\n```\n\n"

    # 2. Individual Command Reference
    for cmd in commands:
        help_text = clean_diskmgr_output(get_help(cmd))
        if help_text:
            readme_content += f"## Command Reference: `{cmd}`\n\n```text\n" + help_text + "\n```\n\n"
            
            # Add Example for list and boot
            if cmd in ['list', 'boot']:
                example_raw = get_example(cmd)
                if example_raw:
                    # Filter example to show only the relevant table/layout data
                    ex_lines = clean_diskmgr_output(example_raw).splitlines()
                    final_ex = []
                    
                    if cmd == 'list': trigger = "Disk: "
                    elif cmd == 'boot': trigger = "--- System Boot"
                    
                    saving = False
                    for line in ex_lines:
                        if trigger in line: saving = True
                        if saving: final_ex.append(line)
                    
                    if final_ex:
                        readme_content += "### Example Output\n\n```text\n" + "\n".join(final_ex).strip() + "\n```\n\n"

    readme_content += "## Configuration\n\nMappings are stored in `diskmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:\n\n```text\n<friendly_name>\t<persistent_device_path>\n```\n"
    readme_content += "\n## Author\n\nTerrydaktal <9lewis9@gmail.com>\n"

    with open('README.md', 'w', encoding='utf-8') as f:
        f.write(readme_content)
    print("README.md has been successfully regenerated.")

if __name__ == "__main__":
    main()
