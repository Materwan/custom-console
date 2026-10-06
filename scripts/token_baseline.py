"""Phase 0 of the token roadmap: what the console sends with EVERY request, and where the turns go.

    python scripts/token_baseline.py          # tool definitions + system prompt, tool by tool
    python scripts/token_baseline.py --log    # + calls and result sizes per tool, tokens per turn (journal)

Tokens are estimated as chars/4 (the same estimate as the console's /context); the real counts of
a turn are in the journal ("turn" entries) and in /usage.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from custom_console.agent.cache import JsonCache
from custom_console.agent.permissions import PermissionGate
from custom_console.agent.schema import tool_schema
from custom_console.agent.tools import ToolContext, build_tools
from custom_console.fs import FileManager
from custom_console.settings import load_settings


def est(chars: int) -> int:
    return round(chars / 4)


def static_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATA_DIR": tmp, "SMTP_HOST": "x", "MOODLE_ENABLED": "true"}
        settings = load_settings(env, root=Path(tmp), use_dotenv=False)
        ctx = ToolContext(
            settings=settings,
            files=FileManager(start_dir=tmp),
            gate=PermissionGate(2, lambda info: True, lambda info, status: None),
            cache=JsonCache(settings.agent_cache_path),
        )
        rows = []
        for tool in build_tools(ctx):
            rows.append((tool.__name__, len(json.dumps(tool_schema(tool), ensure_ascii=False, separators=(",", ":")))))
        close = getattr(ctx, "close", None)
        if close:
            close()
    instructions = len((Path(__file__).resolve().parents[1] / "config" / "agent_instructions.txt").read_text("utf-8"))

    print(f"{'tool definition':<34}{'chars':>7}{'~tokens':>9}")
    for name, chars in sorted(rows, key=lambda r: -r[1]):
        print(f"{name:<34}{chars:>7}{est(chars):>9}")
    total = sum(c for _, c in rows)
    print(f"{'TOOLS (' + str(len(rows)) + ')':<34}{total:>7}{est(total):>9}")
    print(f"{'SYSTEM PROMPT':<34}{instructions:>7}{est(instructions):>9}")
    print(f"{'STATIC PREFIX PER REQUEST':<34}{total + instructions:>7}{est(total + instructions):>9}")


def log_report(path: Path) -> None:
    calls, sizes, turns = defaultdict(int), defaultdict(int), []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "tool_call":
            calls[entry["tool"]] += 1
            sizes[entry["tool"]] += len(json.dumps(entry.get("result"), ensure_ascii=False))
        elif entry.get("type") == "turn":
            turns.append(entry)
    print("\ntool                              calls  result chars")
    for name in sorted(sizes, key=lambda n: -sizes[n])[:10]:
        print(f"{name:<34}{calls[name]:>5}{sizes[name]:>14}")
    if turns:
        done = [t for t in turns if not t.get("error") and not t.get("interrupted")]
        total = sum(t.get("total_tokens", 0) for t in done)
        print(f"\n{len(turns)} turns, {len(done)} completed, {total // max(1, len(done))} tokens per completed turn")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", action="store_true", help="also report the journal")
    args = parser.parse_args()
    static_report()
    if args.log:
        path = load_settings().agent_log_path
        log_report(path) if path.exists() else print(f"\n(no journal at {path})")


if __name__ == "__main__":
    main()
