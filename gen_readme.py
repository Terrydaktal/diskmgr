import os
import re
import subprocess

def strip_ansi(text):
    """Strip ANSI escape sequences from text."""
    return re.sub(r'\x1b\[([0-9,;]*[mGJKHF])', '', text)

def get_help(cmd_name=None):
    """Run diskmgr help and return the cleaned output."""
    cmd = ["sudo", "./diskmgr"]
    input_str = f"help {cmd_name}\nexit\n" if cmd_name else "help\nexit\n"
    try:
        res = subprocess.run(cmd, input=input_str, capture_output=True, text=True, timeout=10)
        return strip_ansi(res.stdout)
    except Exception as e:
        return f"Error capturing help: {e}"

def get_example(cmd_name):
    """Run a command and return its output for documentation examples."""
    cmd = ["sudo", "./diskmgr"]
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
    commands = ['list', 'layout', 'map', 'unmap', 'open', 'close', 'label', 'luks', 'erase', 'create', 'clone', 'sync']
    
    readme_content = "# Disk Manager (diskmgr)\n\n"
    readme_content += "A utility designed to simplify the management of encrypted and plain removable media. It maps friendly labels to hardware-specific Persistent Device Paths (PDP), ensuring that disks are recognized reliably even if device nodes change.\n\n"

    # 1. Main Help / Overview
    main_help = clean_diskmgr_output(get_help())
    readme_content += "## Overview\n\n```text\n" + main_help + "\n```\n\n"

    # 2. Individual Command Reference
    for cmd in commands:
        help_text = clean_diskmgr_output(get_help(cmd))
        if help_text:
            readme_content += f"## Command Reference: `{cmd}`\n\n```text\n" + help_text + "\n```\n\n"
            
            # Add Example for list and layout
            if cmd in ['list', 'layout']:
                example_raw = get_example(cmd)
                if example_raw:
                    # Filter example to show only the relevant table/layout data
                    ex_lines = clean_diskmgr_output(example_raw).splitlines()
                    final_ex = []
                    trigger = "--- Disk Management" if cmd == 'list' else "Disk: /dev/"
                    saving = False
                    for line in ex_lines:
                        if trigger in line: saving = True
                        if saving: final_ex.append(line)
                    
                    if final_ex:
                        readme_content += "### Example Output\n\n```text\n" + "\n".join(final_ex).strip() + "\n```\n\n"

    readme_content += "## Configuration\n\nMappings are stored in `luksmap.tsv` in the same directory as the script. The file uses a simple Tab-Separated Values format:\n\n```text\n<friendly_name>\t<persistent_device_path>\n```\n"
    readme_content += "\n## Author\n\nTerrydaktal <9lewis9@gmail.com>\n"

    with open('README.md', 'w', encoding='utf-8') as f:
        f.write(readme_content)
    print("README.md has been successfully regenerated.")

if __name__ == "__main__":
    main()
