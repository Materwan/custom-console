"""Spike C: an `rmapi` call with input closed and a timeout, against the fake rmapi (or the real one: --rmapi PATH).

    python scripts/spikes/spike_c.py [--rmapi /path/to/rmapi]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from custom_console.platform.process import run_capture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rmapi")
    args = parser.parse_args()
    fake = [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_rmapi.py")]
    base = [args.rmapi] if args.rmapi else fake
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as folder:
        tree = Path(folder) / "tree.json"
        tree.write_text(json.dumps({"/Notes": {"type": "d"}, "/Notes/Plan": {"type": "f"}}))
        env["FAKE_RMAPI_TREE"] = str(tree)
        results = []

        def check(label: str, ok: bool, detail: str) -> None:
            results.append(ok)
            print(f"{'ok  ' if ok else 'FAIL'} {label}: {detail}")

        done = run_capture([*base, "ls", "/Notes"], env=env, timeout=10)
        check("listing", done.ok and "[f]\tPlan" in done.output, repr(done.output.strip()))
        if not args.rmapi:
            env["FAKE_RMAPI_MODE"] = "unauth"
            begun = time.monotonic()
            done = run_capture([*base, "ls"], env=env, timeout=10)
            check("not signed in (input closed, no wait)", time.monotonic() - begun < 5 and "one-time code" in done.output,
                  f"{time.monotonic() - begun:.2f}s, output {done.output.strip()[:60]!r}")
            env["FAKE_RMAPI_MODE"] = "hang"
            begun = time.monotonic()
            done = run_capture([*base, "ls"], env=env, timeout=1.0)
            check("hang (timeout kills it)", done.timed_out and time.monotonic() - begun < 6,
                  f"timed_out={done.timed_out} after {time.monotonic() - begun:.2f}s")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
