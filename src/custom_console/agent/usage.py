"""Token usage: this session, and a persistent ledger across sessions.

Ollama has no account-wide usage API, so the "global" figures are the sum of
every turn run through this console, appended to a JSON Lines file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from rich.table import Table

from .turn import TurnStats


@dataclass
class UsageTotals:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int, duration: float) -> None:
        self.turns += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.duration += duration


@dataclass
class UsageSummary:
    session: UsageTotals = field(default_factory=UsageTotals)
    today: UsageTotals = field(default_factory=UsageTotals)
    week: UsageTotals = field(default_factory=UsageTotals)
    everything: UsageTotals = field(default_factory=UsageTotals)
    by_model: Dict[str, UsageTotals] = field(default_factory=dict)  # all time


class UsageLedger:
    def __init__(self, path: Optional[Path]):
        """`path` None keeps the figures of this session only."""
        self.path = Path(path) if path else None
        self._session = UsageTotals()
        self._session_models: Dict[str, UsageTotals] = {}

    def record(self, model: str, stats: TurnStats) -> None:
        self._session.add(stats.input_tokens, stats.output_tokens, stats.duration)
        self._session_models.setdefault(model, UsageTotals()).add(
            stats.input_tokens, stats.output_tokens, stats.duration
        )
        if self.path is None:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input": stats.input_tokens,
            "output": stats.output_tokens,
            "duration": round(stats.duration, 2),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # losing a statistic must not break the agent

    def _entries(self):
        if self.path is None:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                entry = json.loads(line)
                yield (
                    datetime.fromisoformat(entry["timestamp"]),
                    str(entry.get("model", "?")),
                    int(entry.get("input", 0)),
                    int(entry.get("output", 0)),
                    float(entry.get("duration", 0)),
                )
            except (ValueError, KeyError, TypeError):
                continue  # a damaged line is skipped

    def summary(self, now: Optional[datetime] = None) -> UsageSummary:
        now = (now or datetime.now(timezone.utc)).astimezone()
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        result = UsageSummary(session=self._session)
        for when, model, tokens_in, tokens_out, duration in self._entries():
            when = when.astimezone()
            result.everything.add(tokens_in, tokens_out, duration)
            result.by_model.setdefault(model, UsageTotals()).add(tokens_in, tokens_out, duration)
            if when >= start_of_today:
                result.today.add(tokens_in, tokens_out, duration)
            if when >= week_ago:
                result.week.add(tokens_in, tokens_out, duration)
        return result

    @property
    def session_models(self) -> Dict[str, UsageTotals]:
        return dict(self._session_models)


def format_count(value: int) -> str:
    """12345 -> "12.3k", 2_500_000 -> "2.5M"."""
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.2f}M"


def usage_tables(summary: UsageSummary, session_models: Dict[str, UsageTotals]) -> List[Table]:
    """Two tables: usage per period, and per model (all time)."""
    periods = Table(title="Token usage", title_justify="left")
    periods.add_column("Period", style="cyan")
    for column in ("Turns", "Prompt", "Completion", "Total"):
        periods.add_column(column, justify="right")
    periods.add_column("Time", justify="right")
    for label, totals in (
        ("This session", summary.session),
        ("Today", summary.today),
        ("Last 7 days", summary.week),
        ("All time", summary.everything),
    ):
        periods.add_row(
            label,
            str(totals.turns),
            format_count(totals.input_tokens),
            format_count(totals.output_tokens),
            format_count(totals.total_tokens),
            f"{totals.duration / 60:.1f} min" if totals.duration >= 60 else f"{totals.duration:.0f} s",
        )

    models = Table(title="By model", title_justify="left")
    models.add_column("Model", style="green")
    models.add_column("This session", justify="right")
    models.add_column("All time", justify="right")
    names = sorted(set(summary.by_model) | set(session_models))
    for name in names:
        models.add_row(
            name,
            format_count(session_models[name].total_tokens) if name in session_models else "-",
            format_count(summary.by_model[name].total_tokens) if name in summary.by_model else "-",
        )
    return [periods, models] if names else [periods]
