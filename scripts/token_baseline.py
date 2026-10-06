"""Phase 0 of the token roadmap: what the agent sends on EVERY request, and where the turns go.

    python scripts/token_baseline.py              # static prompt: tool definitions + system prompt
    python scripts/token_baseline.py --log        # + per-turn usage from <DATA_DIR>/logs/agent.jsonl

Tokens are estimated as chars/4 (no tokenizer needed); the real counts per turn come from the
journal ("turn" entries) and from `/tokens` in the agent console.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from custom_console.agent.cache import JsonCache
from custom_console.agent.permissions import PermissionGate
from custom_console.agent.tools import ToolContext, build_tools
from custom_console.agent.workspace import Workspace
from custom_console.fs import FileManager
from custom_console.settings import load_settings


def est(chars: int) -> int:
    return round(chars / 4)


def static_report() -> None:
    from agno.tools.function import Function

    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATA_DIR": tmp, "SMTP_HOST": "x", "MOODLE_ENABLED": "true"}
        settings = load_settings(env, root=Path(tmp), use_dotenv=False)
        ctx = ToolContext(
            settings=settings,
            files=FileManager(start_dir=tmp),
            gate=PermissionGate(2, lambda info: True, lambda info, status: None),
            workspace=Workspace(settings.workspace_roots),
            cache=JsonCache(settings.agent_cache_path),
        )
        rows = []
        for tool in build_tools(ctx):
            schema = Function.from_callable(tool).to_dict()
            rows.append((tool.__name__, len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))))
        ctx.close()
    instructions = len(Path(__file__).resolve().parents[1].joinpath("config/agent_instructions.txt").read_text("utf-8"))

    print(f"{'tool definition':<34}{'chars':>7}{'~tokens':>9}")
    for name, chars in sorted(rows, key=lambda r: -r[1]):
        print(f"{name:<34}{chars:>7}{est(chars):>9}")
    tools_total = sum(c for _, c in rows)
    print(f"{'TOOLS (' + str(len(rows)) + ')':<34}{tools_total:>7}{est(tools_total):>9}")
    print(f"{'SYSTEM PROMPT':<34}{instructions:>7}{est(instructions):>9}")
    print(f"{'STATIC PREFIX PER REQUEST':<34}{tools_total + instructions:>7}{est(tools_total + instructions):>9}")


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
        ok = [t for t in turns if not t.get("error") and not t.get("interrupted")]
        total = sum(t["total_tokens"] for t in ok)
        print(f"\n{len(turns)} turns, {len(ok)} completed, {total // max(1, len(ok))} tokens per completed turn, "
              f"{sum(t['tool_calls'] for t in ok) / max(1, len(ok)):.1f} tool calls per turn")


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
