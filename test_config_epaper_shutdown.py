"""
test_config_epaper_shutdown.py

Tests the PS_EPAPER_CLEAR_ON_SHUTDOWN env-var parsing in config.py
(feature/adaptation-for-huawei, correction #4).

The e-paper panel keeps its image after power-off, so PiScout blanks it
on graceful shutdown by default; this flag lets a deployment turn that
off. Verifies the default (on) and the accepted falsy/truthy values.
Note: the actual panel-clear-on-SIGTERM behavior is verified on the Pi
(no e-paper here) — this only locks the config parsing.

Run:
    python3 test_config_epaper_shutdown.py
"""

import importlib
import os

import config

CASES = [
    # (env value or None to unset, expected bool)
    (None,    True),   # default: clear on shutdown
    ("1",     True),
    ("true",  True),
    ("TRUE",  True),
    ("yes",   True),
    ("on",    True),
    ("0",     False),
    ("false", False),
    ("False", False),
    ("no",    False),
    ("off",   False),
]


def _reload_with(value):
    if value is None:
        os.environ.pop("PS_EPAPER_CLEAR_ON_SHUTDOWN", None)
    else:
        os.environ["PS_EPAPER_CLEAR_ON_SHUTDOWN"] = value
    importlib.reload(config)
    return config.EPAPER_CLEAR_ON_SHUTDOWN


def main():
    fails = []
    for value, expected in CASES:
        got = _reload_with(value)
        ok = got is expected
        shown = "<unset>" if value is None else repr(value)
        print(f"  [{'ok ' if ok else 'XX '}] {shown:>9} -> {got}"
              f"{'' if ok else f' (expected {expected})'}")
        if not ok:
            fails.append((value, expected, got))

    # restore clean env for any later imports
    os.environ.pop("PS_EPAPER_CLEAR_ON_SHUTDOWN", None)
    importlib.reload(config)

    print()
    if fails:
        print(f"FAILED: {len(fails)} case(s).")
        return 1
    print("PASSED: EPAPER_CLEAR_ON_SHUTDOWN parses correctly (default on).")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
