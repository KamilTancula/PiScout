"""
test_merge_result.py

Tests for main._merge_result().

Covers the lean-frame merge fix: a periodic advertisement that omits
the Port ID entirely used to fail the same_port comparison, skipping
the merge and re-rendering the display with a BLANK port line. Two
things were wrong: an empty fresh port disqualified the merge, and
"port" was not on the list of inherited fields, so even a successful
merge would have left it empty. The merged result must also have its
is_partial flag recomputed, since inherited fields can complete it.

Run:
    python3 test_merge_result.py
Exit code 0 = all checks passed.
"""

from main import _merge_result


def _prev_full() -> dict:
    """A complete previously displayed result (SG500 via LLDP)."""
    return {
        "protocol":     "LLDP",
        "switch_name":  "T085",
        "switch_ip":    "192.168.1.254",
        "port":         "Gi1/1/3",
        "port_desc":    "Stanowisko IT",
        "port_desc_source": "LLDP",
        "switch_mac":   "C4:72:95:3C:45:5D",
        "switch_model": "SG500-28 28-Port Gigabit Stackable Managed Switch",
        "vlan":         "150",
        "voice_vlan":   "",
        "is_partial":   False,
    }


def _fresh(**overrides) -> dict:
    """A lean fresh advertisement; fields default to empty."""
    base = {
        "protocol":     "LLDP",
        "switch_name":  "T085",
        "switch_ip":    "",
        "port":         "Gi1/1/3",
        "port_desc":    "",
        "switch_mac":   "C4:72:95:3C:45:5D",
        "switch_model": "",
        "vlan":         "150",
        "voice_vlan":   "",
        "is_partial":   False,
    }
    base.update(overrides)
    return base


FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    # --- 1. Regression: lean frame missing model inherits it -----------
    merged = _merge_result(_prev_full(), _fresh())
    check(
        "lean frame inherits model (SG500 30s frame)",
        merged["switch_model"].startswith("SG500-28"),
        f"model={merged['switch_model']!r}",
    )
    check(
        "lean frame inherits port_desc + source",
        merged["port_desc"] == "Stanowisko IT"
        and merged.get("port_desc_source") == "LLDP",
    )

    # --- 2. THE FIX: fresh frame with EMPTY port still merges ----------
    merged = _merge_result(_prev_full(), _fresh(port="", vlan="", is_partial=True))
    check(
        "empty fresh port does not skip the merge",
        merged["switch_model"].startswith("SG500-28"),
    )
    check(
        "port itself is inherited from prev",
        merged["port"] == "Gi1/1/3",
        f"port={merged['port']!r}",
    )
    check(
        "vlan inherited alongside",
        merged["vlan"] == "150",
    )
    check(
        "is_partial recomputed to False after inheriting port",
        merged["is_partial"] is False,
    )

    # --- 3. Different port -> NO merge (cable moved) -------------------
    merged = _merge_result(_prev_full(), _fresh(port="Gi1/1/4", vlan="200"))
    check(
        "different port skips the merge",
        merged["switch_model"] == "" and merged["vlan"] == "200",
    )

    # --- 4. Different switch name -> NO merge --------------------------
    merged = _merge_result(_prev_full(), _fresh(switch_name="T283"))
    check(
        "different switch name skips the merge",
        merged["switch_model"] == "",
    )

    # --- 5. Non-empty fresh fields always win --------------------------
    merged = _merge_result(_prev_full(), _fresh(vlan="250"))
    check(
        "non-empty fresh VLAN wins over prev",
        merged["vlan"] == "250",
    )

    # --- 6. Case-insensitive port comparison (SG500 lowercase) ---------
    merged = _merge_result(_prev_full(), _fresh(port="gigabitethernet1/1/3"))
    check(
        "case/format-insensitive port match still merges",
        merged["switch_model"].startswith("SG500-28"),
    )

    # --- 7. No prev -> fresh returned unchanged ------------------------
    fresh = _fresh(port="", is_partial=True)
    merged = _merge_result(None, fresh)
    check(
        "no prev: fresh returned as-is",
        merged is fresh and merged["port"] == "",
    )

    if FAILS:
        print(f"\nFAILED: {len(FAILS)} case(s): {', '.join(FAILS)}")
        return 1

    print("\nPASSED: lean-frame merge inherits port and recomputes is_partial.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
