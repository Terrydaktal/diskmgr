"""SMART output parsers and vendor-specific attribute decoding."""

import re


def _parse_smart_attr_raw(out, attr_id):
    """
    Parse a smartctl -a attribute table RAW_VALUE for a given attribute ID.
    Returns a string (raw value) or None.
    """
    if not out:
        return None
    for line in str(out).splitlines():
        s = line.strip()
        if not s:
            continue
        # Attribute table rows typically start with the numeric ID.
        if not s.startswith(str(attr_id) + " "):
            continue
        parts = s.split()
        if len(parts) < 2 or parts[0] != str(attr_id):
            continue
        # Old format: ID NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW...
        if len(parts) >= 10 and re.fullmatch(r"0x[0-9a-fA-F]+", parts[2]):
            return " ".join(parts[9:]).strip()
        # Brief format: ID NAME FLAGS VALUE WORST THRESH FAIL RAW...
        if len(parts) >= 8:
            return " ".join(parts[7:]).strip()
        # Fallback
        return parts[-1]
    return None

def _parse_smart_attr_row(out, attr_id):
    """
    Parse a smartctl -a attribute table row for a given attribute ID.

    Supports both smartctl ATA table formats:
      old:   ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
      brief: ID# ATTRIBUTE_NAME FLAGS VALUE WORST THRESH FAIL RAW_VALUE
    RAW_VALUE may contain spaces (e.g. temperatures with Min/Max), so we capture the tail.
    """
    if not out:
        return None
    for line in str(out).splitlines():
        s = line.strip()
        if not s or not s.startswith(str(attr_id) + " "):
            continue
        parts = s.split()
        if len(parts) < 8 or parts[0] != str(attr_id):
            continue

        name = parts[1]
        try:
            # Old format has a hex flag in column 3.
            if re.fullmatch(r"0x[0-9a-fA-F]+", parts[2]) and len(parts) >= 10:
                value = int(parts[3], 10)
                worst = int(parts[4], 10)
                thresh = int(parts[5], 10)
                raw = " ".join(parts[9:]).strip()
            else:
                # Brief format.
                value = int(parts[3], 10)
                worst = int(parts[4], 10)
                thresh = int(parts[5], 10)
                raw = " ".join(parts[7:]).strip()
        except ValueError:
            continue

        return {
            "id": int(attr_id),
            "name": name,
            "value": value,
            "worst": worst,
            "thresh": thresh,
            "raw": raw,
        }
    return None

def _parse_smart_error_log_count(out):
    if not out:
        return None
    lines = str(out).splitlines()
    for i, line in enumerate(lines):
        if "SMART Error Log" in line:
            # Fast-path: "No Errors Logged"
            for j in range(i, min(i + 40, len(lines))):
                if lines[j].strip() == "No Errors Logged":
                    return 0
            # Otherwise count "Error N occurred at" lines in the next chunk.
            cnt = 0
            for j in range(i, min(i + 300, len(lines))):
                if re.search(r"^\s*Error\s+[0-9]+\s+occurred\s+at\b", lines[j]):
                    cnt += 1
            return cnt
    return None

def _parse_smart_last_error_poh(out):
    """
    Parse the most recent ATA SMART Error Log entry's power-on lifetime (hours).

    smartctl typically formats the most recent entry as:
      "Error 667 occurred at disk power-on lifetime: 20140 hours (839 days + 4 hours)"
    Note: The error number is not necessarily "1" (it can be a running counter).

    Returns (error_number, power_on_hours) or (None, None) if not found / not an ATA SMART error log.
    """
    if not out:
        return (None, None)
    m = re.search(
        r"(?m)^\s*Error\s+([0-9]+)\s+occurred\s+at\s+disk\s+power-on\s+lifetime:\s*([0-9,]+)\s*(?:hours|h)\b",
        str(out),
    )
    if not m:
        return (None, None)
    try:
        n = int(m.group(1), 10)
        h = int(m.group(2).replace(",", ""), 10)
        return (n, h)
    except ValueError:
        return (None, None)

def _smartctl_looks_seagate(out):
    if not out:
        return False
    # Common smartctl identifiers for Seagate HDDs.
    if re.search(r"(?im)^Model Family:.*Seagate", out):
        return True
    if re.search(r"(?im)^(Device Model|Product):\s*Seagate", out):
        return True
    # Most Seagate HDDs report model starting with "ST".
    if re.search(r"(?im)^Device Model:\s*ST[0-9A-Z]", out):
        return True
    return False

def _decode_seagate_command_timeout(raw_val):
    """
    Seagate often packs SMART 188 into 6 bytes (3x 16-bit counters).

    smartctl prints the entire 48-bit value as a decimal integer, which can look huge.
    Decode it as:
      hi word:  >7.5s bucket (included in >5s)
      mid word: >5s bucket
      lo word:  total command timeouts
    """
    if raw_val is None:
        return None
    s = str(raw_val).strip().replace(",", "")
    if not s or not re.fullmatch(r"[0-9]+", s):
        return None
    try:
        v = int(s, 10)
    except ValueError:
        return None
    if v < 0 or v >= (1 << 48):
        return None
    hx = f"{v:012x}"  # 6 bytes
    hi = int(hx[0:4], 16)
    mid = int(hx[4:8], 16)
    lo = int(hx[8:12], 16)
    return {
        "raw_int": v,
        "hex": "0x" + hx,
        "timeouts": lo,
        "gt_5s": mid,
        "gt_7_5s": hi,
    }

def _decode_seagate_hi16_lo32(raw_val):
    """
    Common Seagate packing for some SMART RAW fields:
      RAW = (hi16_error_count << 32) | lo32_operation_count

    This is often seen for attribute 1 (Raw_Read_Error_Rate) and 7 (Seek_Error_Rate),
    where RAW is not "number of errors" in the intuitive sense.

    Returns dict with raw_int, hex, errors, ops; or None if not parseable.
    """
    if raw_val is None:
        return None
    s = str(raw_val).strip().replace(",", "")
    if not s or not re.fullmatch(r"[0-9]+", s):
        return None
    try:
        v = int(s, 10)
    except ValueError:
        return None
    if v < 0 or v >= (1 << 48):
        return None
    return {
        "raw_int": v,
        "hex": f"0x{v:012x}",
        "errors": (v >> 32) & 0xFFFF,
        "ops": v & 0xFFFFFFFF,
    }

def _parse_smart_long_selftest_failures(out):
    """
    Count non-success statuses in the SMART Self-test log for extended/long tests.
    Returns an int or None if the section isn't present.
    """
    if not out:
        return None
    lines = str(out).splitlines()
    start = None
    for i, line in enumerate(lines):
        if "SMART Self-test log" in line:
            start = i
            break
    if start is None:
        return None

    # Find the table header line with "#"
    header = None
    for i in range(start, min(start + 60, len(lines))):
        if lines[i].lstrip().startswith("#"):
            header = i
            break
        if "No self-tests have been logged" in lines[i]:
            return 0
    if header is None:
        # Section exists but we couldn't find the table.
        return None

    fail = 0
    for i in range(header + 1, min(header + 200, len(lines))):
        line = lines[i].strip()
        if not line:
            break
        if not line.startswith("#"):
            continue
        # Common format: "# 1  Extended offline  Completed without error  00%  1234  -"
        cols = line.split()
        if len(cols) < 4:
            continue
        desc = " ".join(cols[2:4])  # "Extended offline" or "Short offline"
        if "Extended offline" not in desc and "Long offline" not in desc:
            continue
        # Status begins after description; search the raw line for "Completed without error"
        if "Completed without error" in line:
            continue
        fail += 1
    return fail
