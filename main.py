"""
main.py

PiScout — entry point and main loop.

Monitors Ethernet carrier state, runs the discovery race on link-up,
and updates the e-paper display with switch port information.

Discovery is handled entirely by race.py, which runs passive
LLDP/CDP capture in parallel and returns the fastest result.

What this file does:
- Initialize the display
- Monitor link state on the configured interface
- Show appropriate screens (waiting, scanning, result, stale)
- Call race.run() on link-up and feed results to the display
- Handle SIGTERM gracefully for clean shutdown

What this file does NOT do:
- Implement any discovery logic (that belongs in discover_*.py and race.py)
- Parse LLDP or CDP frames directly
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import config
import race
import trigger
import discover_passive
import history
import port_aliases
from parse_utils import shorten_interface_name

# Configure logging before importing display or discovery modules
# so their loggers inherit the correct level.
_log_level_str = (getattr(config, "LOG_LEVEL", "WARNING") or "WARNING").upper()
_log_level     = getattr(logging, _log_level_str, logging.WARNING)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)
log.info("Logging initialized at level %s", _log_level_str)


# ============================================================
# -------------------- DISPLAY FACTORY -----------------------
# ============================================================

def _get_display_type() -> str:
    return str(getattr(config, "DISPLAY_TYPE", "epaper")).lower().strip()


def _create_display():
    """
    Instantiate the correct display driver based on config.DISPLAY_TYPE.
    """
    display_type = _get_display_type()
    font_path    = getattr(config, "DISPLAY_FONT_PATH", None)

    if display_type == "epaper":
        from display_epaper import EPaperDisplay
        return EPaperDisplay(
            font_path=font_path,
            min_refresh_interval=int(getattr(config, "EPAPER_MIN_REFRESH_INTERVAL", 10)),
            auto_sleep=bool(getattr(config, "EPAPER_AUTO_SLEEP", True)),
            startup_mode=True,
            partial_refresh_limit=int(getattr(config, "EPAPER_PARTIAL_REFRESH_LIMIT", 8)),
            sleep_delay=float(getattr(config, "EPAPER_SLEEP_DELAY", 60)),
        )

    if display_type == "lcd":
        from display_lcd import LCDDisplay
        return LCDDisplay(
            font_path=font_path,
            rotate_180=bool(getattr(config, "LCD_ROTATE_180", True)),
            clear_on_start=bool(getattr(config, "LCD_CLEAR_ON_START", True)),
            background_color=getattr(config, "LCD_BACKGROUND_COLOR", (0, 0, 0)),
            text_color=getattr(config, "LCD_TEXT_COLOR", (255, 255, 255)),
            backlight_brightness=int(getattr(config, "LCD_BACKLIGHT_BRIGHTNESS", 100)),
        )

    log.warning(
        "Unknown DISPLAY_TYPE '%s' — defaulting to epaper.",
        display_type,
    )
    from display_epaper import EPaperDisplay
    return EPaperDisplay(font_path=font_path)


# ============================================================
# -------------------- DISPLAY HELPERS -----------------------
# ============================================================

def _truncate(text: str, max_len: int) -> str:
    text = str(text).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _merge_result(prev: Optional[dict], fresh: dict) -> dict:
    """
    Fill gaps in a fresh advertisement with data already known for the
    SAME port.

    Some switches alternate between a full LLDP frame (sent on trigger,
    with System Description etc.) and a lean periodic one — the SG500's
    30s frame carries no model, which used to blank the MODEL line on a
    passive refresh. When the fresh result clearly describes the same
    switch port, empty fields inherit the previously known values;
    non-empty fresh fields always win.
    """
    if not prev or not isinstance(fresh, dict):
        return fresh

    prev_port  = shorten_interface_name(str(prev.get("port", "")).strip()).lower()
    fresh_port = shorten_interface_name(str(fresh.get("port", "")).strip()).lower()
    # An EMPTY fresh port does not disqualify the merge: a lean periodic
    # frame that omits the Port ID is exactly the case merging exists for
    # (same rule as same_switch below, where a missing name is not a
    # mismatch). The cable has not moved — the port is inherited.
    same_port = (not prev_port) or (not fresh_port) or (prev_port == fresh_port)

    prev_name  = str(prev.get("switch_name", "")).strip().lower()
    fresh_name = str(fresh.get("switch_name", "")).strip().lower()
    same_switch = (not prev_name or not fresh_name or prev_name == fresh_name)

    if not same_port or not same_switch:
        return fresh

    for key in (
        "switch_name", "switch_ip", "switch_mac", "switch_model",
        "port", "port_desc", "vlan", "voice_vlan",
    ):
        if not str(fresh.get(key, "")).strip() and str(prev.get(key, "")).strip():
            fresh[key] = prev[key]
            if key == "port_desc":
                fresh["port_desc_source"] = prev.get("port_desc_source", "")

    # Inherited fields can turn a partial result into a complete one
    # (e.g. a lean frame missing the port becomes complete once the port
    # is filled from prev), so the flag must be recomputed — otherwise a
    # complete merged result would still be treated as partial downstream.
    fresh["is_partial"] = not discover_passive._is_complete(fresh)
    return fresh


def _flush_interface_addresses(interface: str) -> None:
    """
    Remove all IPv4 addresses from the interface.

    Called on every link-down transition. Without this, NetworkManager
    can briefly keep the previous DHCP lease on eth0 after the cable is
    moved to another port, and the DHCP line would then show a stale
    address from the PREVIOUS port's VLAN — actively misleading for a
    technician. Flushing guarantees the next port starts from a clean
    "DHCP: no" state until a fresh lease actually arrives (NM renegotiates
    DHCP on link-up regardless).

    Failures are logged at debug level and never propagate.
    """
    try:
        subprocess.run(
            ["ip", "-4", "addr", "flush", "dev", interface],
            capture_output=True,
            timeout=3,
        )
        log.debug("Flushed IPv4 addresses on %s after link down", interface)
    except Exception as exc:
        log.debug("Could not flush addresses on %s: %s", interface, exc)


def _kick_fresh_dhcp(interface: str) -> None:
    """
    Force a fresh DHCP transaction at link-up.

    Why this exists: when the cable is moved quickly between switch
    ports, NetworkManager treats the short carrier loss as a link flap
    and keeps the connection active — after carrier returns it silently
    RE-APPLIES the cached lease from the PREVIOUS port's VLAN. Our
    link-down address flush gets undone by NM, and the display shows a
    ghost address until the DHCP server on the new VLAN NAKs the stale
    lease seconds later.

    Reactivating the dedicated profile ("nmcli connection up") tears the
    assumed state down immediately and starts a clean DHCP negotiation
    on the new port. Fire-and-forget with -w 0: if NM is already mid-
    activation the command fails harmlessly and NM continues on its own.
    Errors (nmcli absent, profile missing) are ignored by design.
    """
    try:
        subprocess.Popen(
            ["nmcli", "-w", "0", "connection", "up", "piscout-eth0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.debug("Requested fresh DHCP via NM profile reactivation (%s)", interface)
    except Exception as exc:
        log.debug("Could not reactivate NM profile: %s", exc)


def _get_interface_ipv4(interface: str) -> str:
    """
    Read the current IPv4 address of an interface directly from the kernel
    (SIOCGIFADDR ioctl). Uses only the standard library, no subprocess.

    Returns "" when the interface has no IPv4 address.
    """
    import fcntl
    import socket
    import struct

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(
                sock.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack("256s", interface[:15].encode("ascii")),
            )
            return socket.inet_ntoa(packed[20:24])
        finally:
            sock.close()
    except OSError:
        return ""


def _get_interface_prefix_len(interface: str) -> int:
    """
    Read the IPv4 netmask of an interface from the kernel
    (SIOCGIFNETMASK ioctl) and return it as a prefix length (0-32).

    Returns -1 when the netmask cannot be read.
    """
    import fcntl
    import socket
    import struct

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(
                sock.fileno(),
                0x891B,  # SIOCGIFNETMASK
                struct.pack("256s", interface[:15].encode("ascii")),
            )
            mask_int = struct.unpack("!I", packed[20:24])[0]
            return bin(mask_int).count("1")
        finally:
            sock.close()
    except OSError:
        return -1


def _format_ip_line(interface: str) -> str:
    """
    Build the IP line from the live eth0 state.

    Shows the address PiScout's OWN eth0 obtained from DHCP, with the
    prefix length — the most useful address for a technician at the
    socket. The switch management IP is intentionally NOT shown (it
    usually lives on an unreachable management VLAN); it remains stored
    as switch_ip in history and JSON snapshots.

    Returns e.g. "IP: 192.168.200.5/28" or "IP: --".
    """
    ip = _get_interface_ipv4(interface)
    if not ip or ip.startswith("169.254."):
        return "IP: --"

    prefix = _get_interface_prefix_len(interface)
    if prefix < 0:
        return f"IP: {ip}"
    return f"IP: {ip}/{prefix}"


def _format_dhcp_line(interface: str) -> str:
    """
    DHCP status: "DHCP: yes" when a lease is held on eth0, "DHCP: no"
    otherwise (a 169.254.x.x link-local fallback counts as no lease).
    The address itself is shown on the IP line.
    """
    ip = _get_interface_ipv4(interface)
    if not ip or ip.startswith("169.254."):
        return "DHCP: no"
    return "DHCP: yes"


def build_display_lines(neighbor: dict, interface: str) -> list[str]:
    """
    Build the 8 body lines for a valid neighbor result.

    Layout for the 3.7" 480x280 e-paper panel, ordered from the most to
    the least needed information in the field:
        SW / PORT / VLAN / DESC / IP / DHCP / MAC / MODEL

    IP shows the address obtained by PiScout's own eth0 (with prefix);
    DHCP is a yes/no lease status. The switch management IP is not
    displayed (kept as switch_ip in history and JSON snapshots).

    Character budgets are generous on purpose: the renderer first steps
    the font down to MIN_FONT_SIZE before text is ever cut, so the "…"
    ellipsis appears only for extreme lengths.
    """
    port_desc = str(neighbor.get("port_desc",    "")).strip()
    model     = str(neighbor.get("switch_model", "")).strip()
    mac       = str(neighbor.get("switch_mac",   "")).strip()

    # Descriptions from the LOCAL inventory map are prefixed with "*" so
    # the technician can tell them apart from live device data.
    if port_desc and neighbor.get("port_desc_source") == "LOCAL":
        port_desc = "*" + port_desc

    return [
        f"SW: {_truncate(str(neighbor.get('switch_name') or neighbor.get('switch_mac') or 'Unknown'), 34)}",
        f"PORT: {_truncate(neighbor.get('port', 'Unknown'), 30)}",
        f"VLAN: {neighbor.get('vlan', 'Unknown')}",
        f"DESC: {_truncate(port_desc, 48) if port_desc else '--'}",
        _format_ip_line(interface),
        _format_dhcp_line(interface),
        f"MAC: {mac if mac else '--'}",
        f"MODEL: {_truncate(model, 48) if model else '--'}",
    ]


def build_scanning_lines() -> list[str]:
    """Shown immediately after link-up while discovery is running."""
    return ["", "", "Scanning...", "", ""]


def build_waiting_for_link_lines() -> list[str]:
    """Shown when no Ethernet carrier is detected."""
    return ["", "", "Waiting for", "link...", ""]


def build_stale_lines() -> list[str]:
    """Shown when a previously seen neighbor has not re-advertised."""
    return ["", "No active", "neighbor data.", "", ""]


def _show(display, lines: list[str], force: bool = False, protocol: str = "") -> bool:
    """
    Show lines on whichever display is connected.

    Wraps the display's show_lines method and passes the optional
    protocol string for the top-corner indicator. Falls back gracefully
    if the display driver does not support the protocol parameter.
    """
    try:
        return display.show_lines(lines, force=force, protocol=protocol)
    except TypeError:
        return display.show_lines(lines, force=force)
    except Exception as exc:
        log.error("Display update failed: %s", exc)
        return False


# ============================================================
# -------------------- CARRIER DETECTION ---------------------
# ============================================================

def _read_carrier(interface: str) -> bool:
    """
    Read the Ethernet carrier state from the kernel sysfs.

    Returns True if link is up, False if down or the file cannot be read.
    """
    carrier_path = Path(f"/sys/class/net/{interface}/carrier")
    try:
        return carrier_path.read_text(encoding="ascii").strip() == "1"
    except Exception:
        return False


def _interface_exists(interface: str) -> bool:
    return Path(f"/sys/class/net/{interface}").exists()


def _read_link_speed(interface: str) -> int:
    """
    Read the negotiated link speed from sysfs.

    Returns the speed in Mbps (100 or 1000 on typical ports).
    Returns -1 if the link has not yet negotiated or the file cannot be read.
    Auto-negotiation on 1GbE takes 1-3 seconds after physical link-up.
    """
    speed_path = Path(f"/sys/class/net/{interface}/speed")
    try:
        val = int(speed_path.read_text(encoding="ascii").strip())
        return val if val > 0 else -1
    except Exception:
        return -1


def _wait_for_negotiation(
    interface:      str,
    shutdown_event: threading.Event,
    timeout:        float = 5.0,
) -> bool:
    """
    Wait for Ethernet auto-negotiation to complete.

    During the 1-3 seconds after physical link-up, the NIC and switch
    are negotiating speed and duplex. No frames can be sent or received
    during this window. This function blocks until negotiation completes
    or the timeout expires.

    Returns True if negotiation completed, False if it timed out.
    """
    deadline = time.monotonic() + timeout
    while not shutdown_event.is_set() and time.monotonic() < deadline:
        if _read_link_speed(interface) > 0:
            return True
        time.sleep(0.05)
    return _read_link_speed(interface) > 0


# ============================================================
# -------------------- MAIN LOOP -----------------------------
# ============================================================

def run() -> None:
    """
    Main loop. Never returns unless a fatal error occurs or SIGTERM fires.
    """
    # --- Shutdown event (set by SIGTERM/SIGINT handler) ---
    shutdown_event = threading.Event()

    def _sigterm_handler(signum, frame):
        log.info("Signal %d received — initiating graceful shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT,  _sigterm_handler)

    # --- Configuration ---
    interface            = str(getattr(config, "NETWORK_INTERFACE",     "eth0"))
    disc_timeout         = float(getattr(config, "DISCOVERY_TIMEOUT",   120.0))
    reveal_delay         = float(getattr(config, "RESULT_REVEAL_DELAY",   1.5))
    partial_display_delay = float(getattr(config, "PARTIAL_DISPLAY_DELAY", 30.0))

    log.info("Starting PiScout")
    log.info("DISPLAY_TYPE=%s",           _get_display_type())
    log.info("NETWORK_INTERFACE=%s",      interface)
    log.info("DISCOVERY_TIMEOUT=%s",      disc_timeout)
    log.info("RESULT_REVEAL_DELAY=%s",    reveal_delay)
    log.info("PARTIAL_DISPLAY_DELAY=%s",  partial_display_delay)

    # --- Verify interface exists ---
    if not _interface_exists(interface):
        log.error(
            "Network interface '%s' not found. Check NETWORK_INTERFACE in config.py.",
            interface,
        )
        sys.exit(1)

    log.info("Network interface '%s' found.", interface)

    # --- Get local MAC for self-frame filtering ---
    local_mac = trigger.get_interface_mac(interface)
    if local_mac is None:
        log.warning("Could not read MAC address for %s — self-frame filtering disabled", interface)

    # --- Create display ---
    log.info("Creating %s display object", _get_display_type())
    display = _create_display()
    display.initialize()

    # --- Initial screen ---
    _show(display, build_waiting_for_link_lines(), force=True)

    # --- Main event loop ---
    while not shutdown_event.is_set():

        carrier = _read_carrier(interface)

        if not carrier:
            _show(display, build_waiting_for_link_lines())
            _wait_or_shutdown(shutdown_event, 0.5)
            continue

        # ---- Link is up — start a discovery session ----
        log.info("Link up on %s — starting discovery", interface)

        # Defeat NM's link-flap grace: without this, a quick cable move
        # re-applies the previous port's cached lease (see helper docs).
        _kick_fresh_dhcp(interface)

        # Wait for auto-negotiation to complete before transmitting.
        # During the 1-3 second negotiation window no frames can be sent
        # or received. Polling here prevents wasted trigger frames being
        # dropped by the NIC before the link is ready.
        negotiated = _wait_for_negotiation(interface, shutdown_event, timeout=5.0)
        if not negotiated:
            log.debug("Link speed did not negotiate on %s — proceeding anyway", interface)

        if shutdown_event.is_set():
            break

        if not _read_carrier(interface):
            # Link dropped during negotiation.
            _show(display, build_waiting_for_link_lines(), force=True)
            continue

        # Send the LLDP trigger immediately after negotiation, before the
        # display starts drawing "Scanning...". The e-paper takes ~400ms
        # for a partial refresh — every millisecond of head start matters
        # on LLDP switches that respond almost immediately.
        trigger.send_lldp_trigger(interface, local_mac)
        log.debug("Early LLDP trigger sent on %s before display draw", interface)

        # Show "Scanning..." and note when it finishes drawing.
        # The reveal delay timer starts AFTER the display finishes drawing
        # so the user always sees the screen for at least reveal_delay seconds.
        _show(display, build_scanning_lines(), force=True)
        scan_drawn_at = time.monotonic()

        log.debug(
            "Scanning screen shown. Reveal delay: %.2fs after screen draw.",
            reveal_delay,
        )

        # cancel_event is set when link goes down or shutdown fires.
        cancel_event = threading.Event()

        # Run the discovery race in a background thread so we can
        # simultaneously monitor carrier state.
        result_holder: list[Optional[dict]] = [None]

        def _race_thread():
            result_holder[0] = race.run(
                interface=interface,
                local_mac=local_mac,
                cancel_event=cancel_event,
                timeout=disc_timeout,
            )

        race_thread = threading.Thread(
            target=_race_thread,
            name="rf-race",
            daemon=True,
        )
        race_thread.start()

        # Monitor carrier while the race runs.
        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s — cancelling discovery", interface)
                cancel_event.set()
                _flush_interface_addresses(interface)
                break

            if not race_thread.is_alive():
                break  # Race finished

            _wait_or_shutdown(shutdown_event, 0.5)

        # Signal cancellation BEFORE joining. The monitor loop above exits
        # on three paths: link lost (cancel_event already set), race
        # finished (join returns immediately), or shutdown_event fired —
        # and on that last path cancel_event was NOT yet set, so joining
        # first would block a SIGTERM shutdown for the full 5s timeout
        # while the race kept running. Setting the event first lets the
        # race thread exit within one receive timeout.
        cancel_event.set()
        race_thread.join(timeout=5.0)

        if shutdown_event.is_set():
            break

        result = result_holder[0]

        if result is not None:
            result = port_aliases.apply(result)

        if not _read_carrier(interface):
            # Link went down while we were waiting — go back to top of loop.
            _flush_interface_addresses(interface)
            _show(display, build_waiting_for_link_lines(), force=True)
            continue

        if result is None:
            # Discovery timed out with link still up.
            log.info("Discovery timed out on %s", interface)
            _show(display, build_stale_lines(), force=True)
            # Stay on stale screen until link drops.
            while _read_carrier(interface) and not shutdown_event.is_set():
                _wait_or_shutdown(shutdown_event, 1.0)
            continue

        # ---- We have a result (may be partial) ----
        is_partial   = result.get("is_partial", False)
        result_at    = time.monotonic()
        displayed    = False

        if is_partial:
            log.info(
                "Partial result from race | protocol=%s switch=%s port=%s vlan=%s",
                result.get("protocol"),
                result.get("switch_name"),
                result.get("port"),
                result.get("vlan"),
            )
        else:
            # Complete result — enforce normal reveal delay then show immediately.
            elapsed = time.monotonic() - scan_drawn_at
            if elapsed < reveal_delay:
                _wait_or_shutdown(shutdown_event, reveal_delay - elapsed)

            if not shutdown_event.is_set():
                protocol = result.get("protocol", "")
                display.set_startup_mode(False)
                lines = build_display_lines(result, interface)
                _show(display, lines, force=True, protocol=protocol)
                displayed = True
                history.record(result)
                history.save_port_snapshot(result, lines)
                log.info(
                    "Display updated | protocol=%s switch=%s ip=%s port=%s desc=%s vlan=%s",
                    protocol,
                    result.get("switch_name"),
                    result.get("switch_ip"),
                    result.get("port"),
                    result.get("port_desc"),
                    result.get("vlan"),
                )

        if shutdown_event.is_set():
            break

        # ---- Monitor link while showing result ----
        # Background passive listener serves three purposes:
        # 1. Resets the stale timer on every switch re-advertisement
        # 2. Upgrades display from partial to complete when more data arrives
        # 3. Updates display if switch data changes (e.g. VLAN reassignment)
        refresh_cancel = threading.Event()
        # Shared state between this monitor loop and the background passive
        # refresh thread. The background thread is the sole PRODUCER (it
        # updates result/last_success and bumps data_gen only when the
        # displayed data actually changes); this loop is the sole CONSUMER
        # (it renders to the panel and writes history). One lock keeps the
        # two from racing on the panel or the snapshot files — the cause of
        # double renders and double history entries.
        state_lock = threading.Lock()
        state = {
            "result":       result,
            "last_success": time.monotonic(),
            "data_gen":     0,      # bumps only when the displayed result changes
            "was_upgrade":  False,  # sticky: a partial->complete upgrade is pending
        }
        stale_shown       = False
        last_rendered_gen = 0

        def _passive_refresh():
            """
            Background producer: listen for switch re-advertisements and
            publish updated state. It NEVER renders or writes history — the
            monitor loop is the sole consumer that does that. It only
            refreshes last_success (stale timer) and bumps data_gen when the
            displayed data actually changes.
            """
            while not shutdown_event.is_set() and not refresh_cancel.is_set():
                fresh = discover_passive.discover(
                    interface=interface,
                    local_mac=local_mac,
                    cancel_event=refresh_cancel,
                    timeout=disc_timeout,
                )
                if not fresh or refresh_cancel.is_set():
                    continue

                # This thread is the only writer of state["result"], so
                # reading the previous value for the merge is safe; the lock
                # is held only for the brief state publish below.
                with state_lock:
                    prev = state["result"]
                fresh = _merge_result(prev, fresh)
                fresh = port_aliases.apply(fresh)

                is_upgrade   = prev.get("is_partial") and not fresh.get("is_partial")
                data_changed = fresh != prev

                with state_lock:
                    state["last_success"] = time.monotonic()
                    if is_upgrade or data_changed:
                        state["result"]   = fresh
                        state["data_gen"] += 1
                        if is_upgrade:
                            state["was_upgrade"] = True

                log.debug(
                    "Passive refresh | protocol=%s switch=%s port=%s partial=%s changed=%s",
                    fresh.get("protocol"),
                    fresh.get("switch_name"),
                    fresh.get("port"),
                    fresh.get("is_partial"),
                    is_upgrade or data_changed,
                )

        refresh_thread = threading.Thread(
            target=_passive_refresh,
            name="rf-passive-refresh",
            daemon=True,
        )
        refresh_thread.start()

        # Track the DHCP line so a lease that arrives AFTER the result was
        # shown (typical when CDP/LLDP wins before DHCP completes) updates
        # the display without waiting for the next switch advertisement.
        last_ip_line = [_format_ip_line(interface)]

        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s", interface)
                refresh_cancel.set()
                refresh_thread.join(timeout=3.0)
                _flush_interface_addresses(interface)
                display.set_startup_mode(True)
                _show(display, build_waiting_for_link_lines(), force=True)
                break

            # Snapshot the shared state once per pass, then act on it. This
            # loop is the ONLY place that renders and writes history.
            with state_lock:
                best            = state["result"]
                cur_gen         = state["data_gen"]
                last_success    = state["last_success"]
                pending_upgrade = state["was_upgrade"]

            now           = time.monotonic()
            stale_elapsed = now - last_success

            # (1) Render a data change produced by the background refresh
            #     (partial->complete upgrade, or changed data such as a VLAN
            #     reassignment). Only after the first result is on screen.
            if displayed and cur_gen != last_rendered_gen:
                lines = build_display_lines(best, interface)
                _show(display, lines, force=True, protocol=best.get("protocol", ""))
                if pending_upgrade:
                    history.record(best)
                    with state_lock:
                        state["was_upgrade"] = False
                history.save_port_snapshot(best, lines)
                last_rendered_gen = cur_gen
                last_ip_line[0]   = _format_ip_line(interface)
                stale_shown       = False
                log.info(
                    "Display refreshed | protocol=%s switch=%s port=%s desc=%s vlan=%s",
                    best.get("protocol"), best.get("switch_name"),
                    best.get("port"), best.get("port_desc"), best.get("vlan"),
                )

            # (2) DHCP state change (only while a real result is on screen).
            #     A lease obtained/lost re-renders and refreshes the snapshot.
            if displayed and not stale_shown:
                new_ip = _format_ip_line(interface)
                if new_ip != last_ip_line[0]:
                    last_ip_line[0] = new_ip
                    lines = build_display_lines(best, interface)
                    _show(display, lines, force=True, protocol=best.get("protocol", ""))
                    history.save_port_snapshot(best, lines)
                    log.info("DHCP state changed — display updated | %s", new_ip)

            # (3) First display of a result: a partial shown after the delay
            #     (something is better than nothing), or a complete result
            #     that arrived during the partial wait.
            if not displayed:
                partial_elapsed = now - result_at
                reason = ""
                if partial_elapsed >= partial_display_delay:
                    reason = "after %.0fs partial-wait" % partial_elapsed
                elif cur_gen != last_rendered_gen and not best.get("is_partial"):
                    reason = "complete arrived during partial wait"
                if reason:
                    display.set_startup_mode(False)
                    lines = build_display_lines(best, interface)
                    _show(display, lines, force=True, protocol=best.get("protocol", ""))
                    displayed         = True
                    last_rendered_gen = cur_gen
                    last_ip_line[0]   = _format_ip_line(interface)
                    history.record(best)
                    with state_lock:
                        state["was_upgrade"] = False
                    history.save_port_snapshot(best, lines)
                    log.info(
                        "Result displayed (%s) | switch=%s port=%s vlan=%s",
                        reason, best.get("switch_name"),
                        best.get("port"), best.get("vlan"),
                    )

            # (4) Restore the real result after a stale screen: a fresh
            #     success has arrived (timer no longer stale) while the stale
            #     screen is still up. Fires even if the data is unchanged, so
            #     the panel never gets stuck on "stale" once the switch is back.
            if stale_shown and stale_elapsed <= disc_timeout:
                lines = build_display_lines(best, interface)
                _show(display, lines, force=True, protocol=best.get("protocol", ""))
                stale_shown       = False
                last_rendered_gen = cur_gen
                last_ip_line[0]   = _format_ip_line(interface)
                log.info(
                    "Neighbor data restored — cleared stale screen | switch=%s port=%s",
                    best.get("switch_name"), best.get("port"),
                )

            # (5) Show stale warning if no successful refresh within the window.
            if not stale_shown and stale_elapsed > disc_timeout:
                log.info(
                    "Neighbor data stale on %s (%.0fs since last success)",
                    interface, stale_elapsed,
                )
                _show(display, build_stale_lines(), force=True)
                stale_shown = True

            _wait_or_shutdown(shutdown_event, 1.0)

        refresh_cancel.set()

    # ---- Graceful shutdown ----
    log.info("Shutting down PiScout")
    try:
        # The e-paper panel keeps its image after power-off, so blank it
        # on shutdown (configurable) instead of leaving stale port info
        # frozen on screen. LCD clears on power loss, so it needs nothing.
        if _get_display_type() == "epaper":
            display.shutdown(
                clear_before_sleep=bool(
                    getattr(config, "EPAPER_CLEAR_ON_SHUTDOWN", True)
                )
            )
        else:
            display.shutdown()
    except Exception as exc:
        log.debug("Display shutdown error: %s", exc)


def _wait_or_shutdown(shutdown_event: threading.Event, seconds: float) -> None:
    """Sleep for up to `seconds` but wake immediately if shutdown fires."""
    shutdown_event.wait(timeout=seconds)


# ============================================================
# -------------------- ENTRY POINT ---------------------------
# ============================================================

if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Fatal error in main loop")
        sys.exit(1)
