"""
test_history_concurrency.py

Concurrency test for history.save_port_snapshot() and history.record()
(feature/adaptation-for-huawei, correction #7).

Several discovery paths can write the same port's snapshot and append to
history.jsonl from different threads at almost the same time. Without
serialization this races: interleaved temp files, FileNotFoundError on
rename, mismatched TXT/JSON, or lost history entries. This test hammers
both entry points concurrently and asserts:
  - no exception propagates
  - no *.tmp / *.jtmp left behind
  - the .txt / .json snapshot pair is present and the .json is valid
  - every record() append survives (no lost entries) under a high limit

Run:
    python3 test_history_concurrency.py
"""

import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
import history

N = 50


def _result(i: int) -> dict:
    # Same (switch, port) -> same snapshot file, so every call contends
    # for the same path. Vary payload to exercise the RMW of history.jsonl.
    return {
        "protocol": "LLDP",
        "switch_name": "T283",
        "switch_ip": "10.0.0.1",
        "port": "Gi0/0/37",
        "port_desc": f"A2.C{i:03d}",
        "switch_mac": "58:25:75:E6:0E:1D",
        "switch_model": "Huawei Switch S5735-L48T4X-A",
        "vlan": str(100 + (i % 5)),
        "voice_vlan": "",
    }


def _lines(i: int) -> list:
    r = _result(i)
    return [f"SW: {r['switch_name']}", f"PORT: {r['port']}",
            f"VLAN: {r['vlan']}", f"DESC: {r['port_desc']}"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="piscout-hist-"))
    config.PORT_HISTORY_MODE = 1
    config.PORT_HISTORY_PATH = str(tmp)
    config.PORT_HISTORY_LIMIT = 1000  # high, so all N appends must survive

    # Capture any WARNING+ from history (swallowed write failures log here).
    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, rec): records.append(rec)

    hlog = logging.getLogger("history")
    hlog.addHandler(_Cap())
    hlog.setLevel(logging.WARNING)

    fails: list[str] = []

    # 1) Hammer snapshots for the same port.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(history.save_port_snapshot, _result(i), _lines(i))
                for i in range(N)]
        for f in futs:
            f.result()  # re-raise anything that escaped

    # 2) Hammer history.jsonl appends for the same port.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(history.record, _result(i), _lines(i))
                for i in range(N)]
        for f in futs:
            f.result()

    # --- Assertions ---
    leftovers = list(tmp.rglob("*.tmp")) + list(tmp.rglob("*.jtmp"))
    if leftovers:
        fails.append(f"leftover temp files: {[p.name for p in leftovers]}")

    txt = tmp / "ports" / "t283_gi0-0-37.txt"
    js  = tmp / "ports" / "t283_gi0-0-37.json"
    if not txt.exists():
        fails.append(f"missing snapshot txt: {txt}")
    if not js.exists():
        fails.append(f"missing snapshot json: {js}")
    else:
        try:
            json.loads(js.read_text(encoding="utf-8"))
        except Exception as exc:
            fails.append(f"snapshot json invalid: {exc}")

    hist = tmp / "history.jsonl"
    if not hist.exists():
        fails.append("missing history.jsonl")
    else:
        good = 0
        for ln in hist.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                json.loads(ln); good += 1
            except Exception as exc:
                fails.append(f"corrupt history line: {exc}")
        if good != N:
            fails.append(f"lost history entries: got {good}, expected {N}")

    if records:
        fails.append(f"unexpected WARNING logs: {[r.getMessage() for r in records]}")

    print(f"tmp dir: {tmp}")
    print(f"snapshot .txt exists : {txt.exists()}")
    print(f"snapshot .json exists: {js.exists()} (valid JSON checked)")
    print(f"history.jsonl entries: expected {N}")
    print(f"leftover temp files  : {len(leftovers)}")
    print()
    if fails:
        for msg in fails:
            print(f"  XX {msg}")
        print(f"\nFAILED: {len(fails)} issue(s).")
        return 1
    print("PASSED: concurrent writes are serialized, atomic, and lossless.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
