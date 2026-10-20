from __future__ import annotations

import logging

from custom_console.core.ledger import Ledger
from custom_console.core.log import LOGGER, setup_logging

TOKEN = "clara_ab12cd_0123456789abcdefghijklmnopqrstuv"


def test_every_usage_event_counts_even_when_the_turn_fails(tmp_path):
    now = [1_000_000.0]
    ledger = Ledger(tmp_path / "usage.jsonl", clock=lambda: now[0])
    ledger.record("s1", prompt=100, completion=20, cached=50, model="m:a", conversation="c1")
    ledger.record("s1", prompt=10, completion=5, model="m:a", agent="sub:1")
    ledger.end_turn("s1", "failed")
    now[0] += 2 * 86_400
    ledger.record("s2", prompt=7, completion=3, model="m:b")
    ledger.end_turn("s2", "ok")
    ledger.end_turn("s2", "cancelled")
    first = ledger.window("session", "s1")
    assert (first.prompt, first.completion, first.cached, first.calls) == (110, 25, 50, 2)
    assert first.by_agent == {"main": 120, "sub:1": 15} and first.failed_turns == 1 and first.total == 135
    assert ledger.window("24h", "s2").total == 10
    assert ledger.window("7d", "s2").total == 145
    everything = ledger.window("all", "s2")
    assert everything.turns == 3 and everything.cancelled_turns == 1 and everything.by_model == {"m:a": 135, "m:b": 10}


def test_a_missing_or_damaged_ledger_is_empty_not_an_error(tmp_path):
    ledger = Ledger(tmp_path / "none.jsonl")
    assert ledger.totals().total == 0
    (tmp_path / "bad.jsonl").write_text('{"kind":"usage","prompt":1,"completion":1,"t":1}\n{"kind":"us')
    assert Ledger(tmp_path / "bad.jsonl").totals().total == 2


def test_the_log_redacts_secrets_in_messages_and_errors(tmp_path):
    path = tmp_path / "logs" / "console.log"
    logger = setup_logging(path, "info")
    logger.info("connected with %s", TOKEN)
    try:
        raise RuntimeError(f"Authorization: Bearer {TOKEN}")
    except RuntimeError:
        logger.exception("failed")
    logging.shutdown()
    text = path.read_text()
    assert "connected with [redacted]" in text and TOKEN not in text and "0123456789abcdef" not in text
    assert logging.getLogger(LOGGER).propagate is False


def test_debug_adds_a_level(tmp_path):
    logger = setup_logging(tmp_path / "a.log", "warning")
    assert logger.level == logging.WARNING
    assert setup_logging(tmp_path / "a.log", "warning", debug=True).level == logging.DEBUG
    assert setup_logging(None).handlers
