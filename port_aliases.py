"""
port_aliases.py

Local, passive port-description inventory.

An administrator can prepare port descriptions OFFLINE (e.g. generated
from switch configuration backups) and copy them to the device — PiScout
never connects to any switch to obtain this data. When a discovered port
has no description from the device itself (LLDP TLV 4 / SNMP ifAlias),
the local map fills it in, clearly marked as locally sourced.

File location:
    <PORT_HISTORY_PATH>/devices/*.json     (default /data/piscout/devices/)

File format (one or many devices per file, UTF-8):
    {
      "T085": {
        "gi1/1/1": "Gniazdo A-101",
        "gi1/1/2": "Telefon sekretariat"
      },
      "C4:72:95:3C:45:5D": {
        "gi1/1/5": "Kamera parking"
      }
    }

Device keys may be either:
  - the switch hostname (matched case-insensitively against the
    discovered switch name), or
  - the chassis MAC address (any common format: colons, dashes, dots
    or bare hex). NOTE: CDP results carry the per-port source MAC, not
    the chassis MAC, so MAC-keyed entries reliably match only LLDP and
    SNMP results — prefer hostname keys, they work with every protocol.

Port keys are normalized the same way the display normalizes them, so
"gi1/1/1", "Gi1/1/1" and "gigabitethernet1/1/1" all match.

Priority of the description shown on screen (applied in apply()):
    1. Real description from the device (LLDP TLV 4 or SNMP ifAlias)
    2. Local map entry (marked with "*" on the display)
    3. "--"

What this file does NOT do:
- Query switches over the network
- Write anything to disk
- Decide how the description is rendered (main.py's job)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import config
from parse_utils import sanitize_display_string, shorten_interface_name


log = logging.getLogger(__name__)


# Loaded map cache. Reloaded automatically whenever any *.json file in
# the devices directory is added, removed, or modified (mtime change) —
# the admin can scp a new map onto the device without restarting.
_cache: dict = {"stamp": None, "by_name": {}, "by_mac": {}}


def _devices_dir() -> Path:
    base = str(getattr(config, "PORT_HISTORY_PATH", "/data/piscout"))
    return Path(base) / "devices"


def _normalize_port(port: str) -> str:
    """Canonical port key: vendor prefix shortened, lowercase."""
    return shorten_interface_name(str(port or "").strip()).lower()


def _normalize_mac(value: str) -> str:
    """
    Canonical MAC: 12 lowercase hex chars, or "" if the value does not
    look like a MAC. Accepts colons, dashes, dots or bare hex.
    """
    hex_chars = "".join(
        c for c in str(value or "").lower() if c in "0123456789abcdef"
    )
    return hex_chars if len(hex_chars) == 12 else ""


def _merge_ports(
    target: dict,
    sources: dict,
    device_key: str,
    ports: dict,
    source_file: str,
) -> None:
    """
    Merge one device's normalized {port: desc} entries into a target map
    (by_name or by_mac), warning when a later file overrides a port
    description already set by an earlier one.

    Policy: last write wins — deterministic, because files load in sorted
    order — but every real override is logged as a WARNING so a silent
    clobber can't hide a wrong description on screen. `sources` maps each
    (device, port) to the file that last set it, so the warning can name
    both the previous and the new file. Identical redefinitions are not
    conflicts and stay silent (no data is lost, so no noise).
    """
    bucket = target.setdefault(device_key, {})
    for port_clean, desc_clean in ports.items():
        skey = (device_key, port_clean)
        existing = bucket.get(port_clean)
        if existing is not None and existing != desc_clean:
            log.warning(
                "Duplicate port alias overwritten | device=%s port=%s "
                "old=%r new=%r old_file=%s new_file=%s",
                device_key, port_clean, existing, desc_clean,
                sources.get(skey, "?"), source_file,
            )
        bucket[port_clean] = desc_clean
        sources[skey] = source_file


def _load() -> None:
    """(Re)load all map files if anything changed on disk."""
    directory = _devices_dir()
    try:
        files = sorted(directory.glob("*.json"))
        stamp = tuple((str(f), f.stat().st_mtime, f.stat().st_size) for f in files)
    except OSError:
        files, stamp = [], ()

    if stamp == _cache["stamp"]:
        return

    by_name: dict = {}
    by_mac:  dict = {}
    # Track which file last set each (device, port) in each space, so an
    # override across files can be reported with both filenames.
    name_sources: dict = {}
    mac_sources:  dict = {}

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Aliases: cannot parse %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            log.warning("Aliases: %s is not a JSON object — skipped", path.name)
            continue

        for device_key, ports in data.items():
            if not isinstance(ports, dict):
                continue
            normalized = {}
            for port_key, desc in ports.items():
                desc_clean = sanitize_display_string(str(desc)).strip()
                port_clean = _normalize_port(port_key)
                if port_clean and desc_clean:
                    normalized[port_clean] = desc_clean
            if not normalized:
                continue

            mac = _normalize_mac(device_key)
            if mac:
                _merge_ports(by_mac, mac_sources, mac, normalized, path.name)
            else:
                name_key = str(device_key).strip().lower()
                _merge_ports(by_name, name_sources, name_key, normalized, path.name)

    _cache["stamp"]   = stamp
    _cache["by_name"] = by_name
    _cache["by_mac"]  = by_mac
    log.info(
        "Aliases: loaded %d device(s) by name, %d by MAC from %s",
        len(by_name), len(by_mac), directory,
    )


def lookup(switch_name: str, switch_mac: str, port: str) -> str:
    """
    Return the locally mapped description for a port, or "".

    Hostname match is attempted first (works with every protocol),
    chassis MAC second. Never raises.
    """
    try:
        _load()

        port_key = _normalize_port(port)
        if not port_key:
            return ""

        name = str(switch_name or "").strip().lower()
        if name:
            entry = _cache["by_name"].get(name)
            if entry:
                desc = entry.get(port_key, "")
                if desc:
                    return desc

        mac = _normalize_mac(switch_mac)
        if mac:
            entry = _cache["by_mac"].get(mac)
            if entry:
                return entry.get(port_key, "")
    except Exception as exc:
        log.warning("Aliases: lookup failed: %s", exc)
    return ""


def apply(result: dict) -> dict:
    """
    Annotate a discovery result with the description source and fill in
    the local description when the device provided none.

    port_desc_source values:
        "LLDP" / "SNMP" / "CDP" — real description from the device
        "LOCAL"                 — filled from the local map
        "NONE"                  — no description available
    """
    if not isinstance(result, dict):
        return result

    if str(result.get("port_desc", "")).strip():
        result.setdefault(
            "port_desc_source",
            str(result.get("protocol") or result.get("source") or "DEVICE"),
        )
        return result

    desc = lookup(
        result.get("switch_name", ""),
        result.get("switch_mac", ""),
        result.get("port", ""),
    )
    if desc:
        result["port_desc"]        = desc
        result["port_desc_source"] = "LOCAL"
    else:
        result["port_desc_source"] = "NONE"
    return result
