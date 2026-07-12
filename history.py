"""
history.py

Port discovery history logging for PiScout.

Three modes controlled by PORT_HISTORY_MODE in config.py:

  Mode 0 — Off (default)
    No history is recorded. Zero disk activity. Fully compatible with
    read-only filesystem with no special considerations.

  Mode 1 — Port History
    Records the last PORT_HISTORY_LIMIT port discovery results as JSON
    lines in PORT_HISTORY_PATH/history.jsonl. Each entry contains a
    timestamp plus all discovered switch data. Oldest entries are dropped
    when the limit is reached. Uses atomic writes (temp file + rename) to
    protect against data corruption on hard power cuts.

  Mode 2 — Debug Log
    Records verbose log entries to PORT_HISTORY_PATH/debug.log using a
    rotating file handler (max 5MB per file, 3 rotations kept). This is
    additive — it runs alongside the systemd journal, not instead of it.
    Useful for field troubleshooting without a live SSH session.

What this file does:
  - Read PORT_HISTORY_MODE, PORT_HISTORY_LIMIT, PORT_HISTORY_PATH from config
  - Provide a single record(result) function that main.py calls
  - Handle all file I/O, rotation, and atomic writes internally
  - Fail silently so a history write error never affects discovery or display

What this file does NOT do:
  - Talk to the display
  - Affect discovery logic
  - Raise exceptions to the caller
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config


log = logging.getLogger(__name__)


# ============================================================
# Configuration helpers
# ============================================================

def _get_mode() -> int:
    try:
        return max(0, min(2, int(getattr(config, "PORT_HISTORY_MODE", 0))))
    except (TypeError, ValueError):
        return 0


def _get_limit() -> int:
    try:
        return max(1, int(getattr(config, "PORT_HISTORY_LIMIT", 50)))
    except (TypeError, ValueError):
        return 50


def _get_path() -> Path:
    raw = str(getattr(config, "PORT_HISTORY_PATH", "/data/piscout")).strip()
    return Path(raw)


# ============================================================
# Path helpers
# ============================================================

def _ensure_dir(path: Path) -> bool:
    """
    Ensure the history directory exists.

    Returns True if the directory is ready to use, False if it could
    not be created (e.g. read-only filesystem without a writable partition).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        log.warning(
            "History: could not create directory %s: %s. "
            "Is the writable partition mounted?",
            path,
            exc,
        )
        return False


# ============================================================
# Entry builder
# ============================================================

def _build_entry(result: dict) -> dict:
    """
    Build a history entry dict from a discovery result.

    Includes a human-readable timestamp using the system clock.
    The Pi Zero 2W has no RTC — the clock syncs via NTP after boot.
    On networks without internet access the timestamp may be approximate.
    """
    return {
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "protocol":    result.get("protocol",    ""),
        "switch_name": result.get("switch_name", ""),
        "switch_ip":   result.get("switch_ip",   ""),
        "port":        result.get("port",        ""),
        "port_desc":   result.get("port_desc",   ""),
        "switch_mac":  result.get("switch_mac",  ""),
        "switch_model": result.get("switch_model", ""),
        "vlan":        result.get("vlan",        ""),
        "voice_vlan":  result.get("voice_vlan",  ""),
    }


# ============================================================
# Mode 1 — Port History
# ============================================================

def _record_port_history(result: dict, history_dir: Path, limit: int) -> None:
    """
    Append a JSON entry to history.jsonl, enforcing the entry limit.

    Uses an atomic write pattern:
      1. Read existing entries
      2. Append new entry
      3. Enforce limit (drop oldest if needed)
      4. Write to a temp file
      5. Atomically rename temp file over the real file

    If power is cut between steps 4 and 5, the rename never completes
    and the existing file is untouched. If power is cut during step 4,
    only the temp file is affected — the real file is intact.
    """
    history_file = history_dir / "history.jsonl"
    tmp_file     = history_dir / "history.jsonl.tmp"

    # Read existing entries.
    entries: list[dict] = []
    if history_file.exists():
        try:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("History: could not read %s: %s — starting fresh", history_file, exc)
            entries = []

    # Append new entry.
    entries.append(_build_entry(result))

    # Enforce limit — keep the most recent entries.
    if len(entries) > limit:
        entries = entries[-limit:]

    # Write to temp file then atomically rename.
    try:
        content = "\n".join(json.dumps(e) for e in entries) + "\n"
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.rename(history_file)
        log.debug(
            "History: recorded entry (%d/%d) | switch=%s port=%s",
            len(entries),
            limit,
            result.get("switch_name"),
            result.get("port"),
        )
    except OSError as exc:
        log.warning("History: could not write %s: %s", history_file, exc)
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================
# Mode 2 — Debug Log
# ============================================================

# Module-level rotating file handler — created once on first use.
_debug_handler: Optional[logging.handlers.RotatingFileHandler] = None
_debug_logger:  Optional[logging.Logger]                        = None


def _get_debug_logger(history_dir: Path) -> Optional[logging.Logger]:
    """
    Return a logger that writes to PORT_HISTORY_PATH/debug.log.

    The logger is created once and reused. Uses a RotatingFileHandler
    with a 5MB limit and 3 backup files kept.

    Returns None if the log file cannot be created.
    """
    global _debug_handler, _debug_logger

    if _debug_logger is not None:
        return _debug_logger

    debug_log = history_dir / "debug.log"

    try:
        handler = logging.handlers.RotatingFileHandler(
            filename=str(debug_log),
            maxBytes=5 * 1024 * 1024,   # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

        # Create a dedicated logger that does not propagate to the root logger
        # so debug entries go to file only, not to the systemd journal.
        debug_log_logger = logging.getLogger("piscout.debug_file")
        debug_log_logger.propagate = False
        debug_log_logger.setLevel(logging.DEBUG)
        debug_log_logger.addHandler(handler)

        _debug_handler = handler
        _debug_logger  = debug_log_logger

        log.debug("History: debug log handler initialized at %s", debug_log)
        return _debug_logger

    except OSError as exc:
        log.warning("History: could not create debug log at %s: %s", debug_log, exc)
        return None


def _record_debug_log(result: dict, history_dir: Path) -> None:
    """
    Write a debug log entry for a discovery result.

    Entries are written to PORT_HISTORY_PATH/debug.log via a rotating
    file handler. The file rotates at 5MB and 3 backups are kept.
    """
    logger = _get_debug_logger(history_dir)
    if logger is None:
        return

    entry = _build_entry(result)
    logger.info(
        "Discovery result | protocol=%s switch=%s ip=%s port=%s desc=%s vlan=%s voice=%s",
        entry["protocol"],
        entry["switch_name"],
        entry["switch_ip"],
        entry["port"],
        entry["port_desc"],
        entry["vlan"],
        entry["voice_vlan"],
    )


# ============================================================
# Per-port snapshot files
# ============================================================

def _safe_filename_part(text: str, fallback: str) -> str:
    """
    Turn a switch/port name into a filesystem-safe filename fragment.
    Keeps letters, digits, dots and dashes; everything else becomes "-".
    """
    text = str(text or "").strip()
    if not text:
        return fallback
    cleaned = "".join(
        ch if (ch.isalnum() or ch in ".-") else "-" for ch in text
    )
    return cleaned.strip("-") or fallback


def _save_port_snapshot(result: dict, display_lines: list, history_dir: Path) -> None:
    """
    Write a human-readable snapshot of what the display shows for this
    port to PORT_HISTORY_PATH/ports/<switch>_<port>.txt.

    One file per (switch, port) pair, ALWAYS overwritten — each file
    holds exactly one entry: the most recent state of that port. This
    gives a readable per-port record when walking through ports during
    testing or documentation.
    """
    ports_dir = history_dir / "ports"
    if not _ensure_dir(ports_dir):
        return

    switch_part = _safe_filename_part(result.get("switch_name", ""), "unknown-switch")
    port_part   = _safe_filename_part(result.get("port", ""),        "unknown-port")
    snapshot    = ports_dir / f"{switch_part}_{port_part}.txt"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body_lines = [str(line) for line in (display_lines or []) if str(line).strip()]

    content = (
        "PiScout port snapshot\n"
        f"Updated:  {timestamp}\n"
        f"Protocol: {result.get('protocol', '')}\n"
        "\n"
        + "\n".join(body_lines)
        + "\n"
    )

    try:
        snapshot.write_text(content, encoding="utf-8")
        log.debug("History: port snapshot written to %s", snapshot)
    except OSError as exc:
        log.warning("History: could not write port snapshot %s: %s", snapshot, exc)


# ============================================================
# Public entry point
# ============================================================

def save_port_snapshot(result: dict, display_lines: Optional[list] = None) -> None:
    """
    Write a human-readable snapshot file for a single switch port.

    One file per (switch, port) pair under PORT_HISTORY_PATH/ports/,
    e.g. "t085_gi1-1-3.txt". The file always contains exactly ONE
    entry — the latest display content for that port — and is
    overwritten on every new discovery, so it stays readable at a
    glance. Useful for walking a switch port-by-port and reviewing
    the collected results afterwards.

    Enabled whenever PORT_HISTORY_MODE >= 1 (same gate as history).
    Failures are logged but never propagate — a snapshot write error
    must never affect discovery or display behavior.
    """
    try:
        if _get_mode() == 0 or not display_lines:
            return

        ports_dir = Path(_get_path()) / "ports"
        if not _ensure_dir(ports_dir):
            return

        switch = _safe_filename(result.get("switch_name") or "unknown-switch")
        port   = _safe_filename(result.get("port")        or "unknown-port")
        path   = ports_dir / f"{switch}_{port}.txt".lower()

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        protocol  = str(result.get("protocol", "")).strip() or "?"

        body = "\n".join(str(line) for line in display_lines)
        content = (
            "# PiScout port snapshot — latest result for this port\n"
            f"# Updated : {timestamp}\n"
            f"# Protocol: {protocol}\n"
            "\n"
            f"{body}\n"
        )

        # Atomic write: never leave a half-written file behind if power
        # is cut mid-write (this device is unplugged without warning).
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)

        log.debug("Snapshot written: %s", path)
    except Exception as exc:
        log.warning("History: snapshot write failed: %s", exc)


def _safe_filename(name: str) -> str:
    """
    Make a string safe for use as a filename: keep letters, digits,
    dot, dash and underscore; replace everything else (slashes in
    port names, spaces, colons in MACs) with a dash.
    """
    cleaned = "".join(
        c if (c.isalnum() or c in "._-") else "-" for c in str(name).strip()
    )
    return cleaned.strip("-") or "unknown"


def record(result: dict, display_lines: Optional[list] = None) -> None:
    """
    Record a discovery result according to PORT_HISTORY_MODE.

    This is the only function main.py needs to call. All mode logic,
    file I/O, and error handling is handled internally. Failures are
    logged as warnings but never propagate to the caller — a history
    write error must never affect discovery or display behavior.

    Parameters:
        result : the neighbor dict returned by race.run(), containing
                 protocol, switch_name, switch_ip, port, vlan, voice_vlan

    Modes:
        0 — Off:          returns immediately, no disk activity
        1 — Port History: appends JSON entry to history.jsonl
        2 — Debug Log:    writes to rotating debug.log file
    """
    mode = _get_mode()

    if mode == 0:
        return

    history_dir = _get_path()

    if not _ensure_dir(history_dir):
        return

    try:
        if mode == 1:
            _record_port_history(result, history_dir, _get_limit())
        elif mode == 2:
            _record_debug_log(result, history_dir)
    except Exception as exc:
        # Belt-and-suspenders catch — specific errors are handled above.
        log.warning("History: unexpected error in record(): %s", exc)
