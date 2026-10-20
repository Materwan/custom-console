"""PERF-1: a long answer streams smoothly over a long transcript. The budget is written in docs/DECISIONS.md.

The old console repainted the whole transcript for every token. Here the text is drawn 15 times a second, complete
paragraphs are frozen, and a finished answer is rendered once, so the cost of a frame must not grow with the answer or with
the transcript. These tests measure how late the event loop answers while 9,000 tokens arrive over a transcript of 120 messages.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import pytest

from custom_console.ui.agent_app import MAX_BLOCKS
from custom_console.ui.widgets.blocks import AnswerBlock, HistoryBlock, UserBlock
from tests.support.appworld import SIZE, make_app, settle

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
CID = "c0123456789abcdef"
# Coverage tracing slows every line of Python several times over, so under `--cov` (as CI runs) the budgets are scaled; the
# real numbers are the ones of a run without it. `CONSOLE_PERF_SCALE` sets the factor by hand (a slow machine).
SCALE = float(os.environ.get("CONSOLE_PERF_SCALE") or (8 if "coverage" in sys.modules else 1))
LAG_P95_MS = 60.0 * SCALE  # how late the event loop answers while text streams in (what a person feels as stutter), 95th percentile
LAG_MAX_MS = 500.0 * SCALE  # and the worst stall
FINISH_MS = 4000.0 * SCALE  # drawing a 100 KB answer as Markdown, once
# Under tracing the workload is a third of the size (the budgets are scaled, the scenario is the same in shape).
TOKENS = 9000 if SCALE == 1 else 3000
PAIRS = 120 if SCALE == 1 else 50
FINISH_WORDS = 15_000 if SCALE == 1 else 5000


def turn():
    return {"type": "turn", "id": "t1", "conversation": CID, "model": "fake:small", "incognito": False, "local_only": False}


def done(reply):
    return {"type": "done", "reply": reply, "conversation": CID, "tools": [], "message_id": 7, "versions": [7],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "context": {"tokens": 1, "window": 1000, "percent": 0.1},
            "model": "fake:small", "tainted": False}


def words(count: int) -> list[str]:
    out = []
    for i in range(count):
        out.append(f"word{i % 97} ")
        if i % 12 == 11:
            out.append("\n\n")
    return out


async def test_a_long_answer_streams_over_a_long_transcript_inside_the_budget(tmp_path, fake_clara):
    pieces = words(TOKENS)
    fake_clara.queue_turn(turn(), *({"type": "token", "text": p} for p in pieces), done("".join(pieces)))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        for number in range(PAIRS):  # a long transcript of finished messages
            app.add(UserBlock(f"question {number}"))
            app.show_answer("\n".join(f"- line {i} of answer {number} with **bold** text" for i in range(8)))
        # the app folds the old blocks by itself as the transcript grows, a little at a time
        await settle(app, pilot, until=lambda: bool(app.query(HistoryBlock)) and len(app.query("#transcript > *")) <= MAX_BLOCKS,
                     timeout=60)
        await pilot.pause(0.5)
        assert len(app.query(HistoryBlock)) == 1 and len(app.query("#transcript > *")) <= MAX_BLOCKS
        lags: list[float] = []
        running = True

        async def probe() -> None:
            """Sleeps 10 ms over and over; how much later than that it wakes up is the time the loop was busy."""
            while running:
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                lags.append(max((time.perf_counter() - started - 0.01) * 1000, 0.0))

        watcher = asyncio.ensure_future(probe())
        app.prompt_box.load_text("write a lot")
        await pilot.press("enter")
        started = time.perf_counter()
        await settle(app, pilot, until=lambda: not app.busy and app.turn_started > 0, timeout=90)
        streamed = time.perf_counter() - started
        running = False
        await watcher
        # the last answer is complete, and it holds everything that was sent
        last = list(app.query(AnswerBlock))[-1]
        assert last.complete and len(last.text) > TOKENS * 5
        assert len(app.query("#transcript > *")) <= MAX_BLOCKS  # and it stayed folded while the answer grew
    assert len(lags) > 50, "the probe barely ran: the measurement is broken"
    ordered = sorted(lags)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(f"\n(budget scale {SCALE:g})\nstreaming: {len(lags)} probes, median lag {statistics.median(lags):.1f} ms, p95 {p95:.1f} ms, "
          f"max {max(lags):.1f} ms, {streamed:.1f} s in all, {len(last.text):,} characters")
    assert p95 < LAG_P95_MS, f"p95 loop lag {p95:.1f} ms is over {LAG_P95_MS} ms"
    assert max(lags) < LAG_MAX_MS, f"worst stall {max(lags):.1f} ms is over {LAG_MAX_MS} ms"


async def test_a_finished_answer_is_rendered_once_inside_the_budget(tmp_path, fake_clara):
    text = "".join(words(FINISH_WORDS))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        started = time.perf_counter()
        app.show_answer(text)
        await pilot.pause(0.05)
        for _ in range(400):
            if app.query("Static.markdown"):
                break
            await pilot.pause(0.05)
        await pilot.pause(0.5)  # and drawn
        elapsed = (time.perf_counter() - started) * 1000
    print(f"\nfinish: {len(text):,} characters as Markdown in {elapsed:.0f} ms")
    assert elapsed < FINISH_MS, f"rendering took {elapsed:.0f} ms (budget {FINISH_MS:.0f} ms)"
