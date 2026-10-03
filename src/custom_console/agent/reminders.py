"""Reminders and notifications: reminders are set from the console with /remind; the server announces
them, and its notifications (from Clara, another client, or the server itself), to this user's clients.

The server keeps the reminders and decides when they are due. A background thread holds a stream
open to it; what came while the console was closed arrives as soon as it connects.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clara import ClaraClient, ClaraError

REPEATS = ("daily", "weekly", "monthly")
LATE_SECONDS = 120  # a reminder shown this long after it fired is announced as missed
RECONNECT_SECONDS = 5.0

WHEN_HELP = "WHEN: +30m, +2h, +3d | 09:30 | tomorrow 09:30 | 2026-10-05 09:30"
TARGETS_HELP = "@SURFACES: only on those clients, e.g. @app or @app,discord (default: all of yours)"


def _clock(text: str) -> Tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d\d)", text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"Not a time of day: {text!r} (use HH:MM).")
    return int(match[1]), int(match[2])


def take_targets(argument: str) -> Tuple[List[str], str]:
    """``(surfaces, the rest)``: an ``@app,discord`` word among the first two says where to show it."""
    words = argument.split()
    for index, word in enumerate(words[:2]):
        if word.startswith("@"):
            del words[index]
            return [name for name in word[1:].lower().split(",") if name], " ".join(words)
    return [], argument


def parse_remind(argument: str, now: datetime) -> Tuple[datetime, str, str]:
    """``(when, repeat, text)`` from the arguments of /remind. `now` is the local time, with its offset."""
    words = argument.split()
    repeat = words.pop(0).lower() if words and words[0].lower() in REPEATS else ""
    if not words:
        raise ValueError(f"Usage: /remind [daily|weekly|monthly] WHEN TEXT   ({WHEN_HELP})")
    head = words[0].lower()
    relative = re.fullmatch(r"\+(\d+)([mhd])", head)
    if relative:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[relative[2]]
        due, rest = now + timedelta(**{unit: int(relative[1])}), words[1:]
    elif head == "tomorrow" and len(words) > 1:
        hour, minute = _clock(words[1])
        due = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        rest = words[2:]
    elif re.fullmatch(r"\d{4}-\d\d-\d\d", head) and len(words) > 1:
        hour, minute = _clock(words[1])
        try:
            due = datetime.fromisoformat(head).replace(hour=hour, minute=minute).astimezone()
        except ValueError:
            raise ValueError(f"Not a date: {head!r}.") from None
        rest = words[2:]
    elif ":" in head:
        hour, minute = _clock(head)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        due, rest = (due if due > now else due + timedelta(days=1)), words[1:]
    else:
        raise ValueError(f"Cannot read the time {head!r}. {WHEN_HELP}")
    text = " ".join(rest)
    if not text:
        raise ValueError("A reminder needs a text.")
    return due, repeat, text


def local_time(moment: str) -> str:
    """An ISO moment from the server, as local ``YYYY-MM-DD HH:MM``."""
    return datetime.fromisoformat(moment).astimezone().strftime("%Y-%m-%d %H:%M")


def notice(event: Dict[str, Any], now: datetime) -> str:
    """The line shown for an announced reminder (always this user's own) or a notification."""
    if event.get("type") == "notification":
        late = (now - datetime.fromisoformat(event["sent_at"])).total_seconds() > LATE_SECONDS
        when = f"  (sent {local_time(event['sent_at'])})" if late else ""
        title = f"{event['title']}: " if event.get("title") else ""
        return f"🔔 {title}{event['text']}{when}"
    missed = (now - datetime.fromisoformat(event["fired_at"])).total_seconds() > LATE_SECONDS
    late = f"  (missed, it was due {local_time(event['due_at'])})" if missed else ""
    return f"⏰ {event.get('message') or event['text']}{late}"


SERVER_SAYS = {
    "stopping": "Clara is stopping: she finishes what is running and takes nothing new.",
    "down": "Clara is not running.",
    "again": "Clara is running again.",
}


class ReminderListener:
    """Calls `on_event(event)` for every reminder and notification the server announces, and
    `on_status(text)` when the server stops, is gone or is back, from a background thread. When the connection drops (the server
    restarts, the network goes) it tries again."""

    def __init__(
        self,
        client: ClaraClient,
        on_event: Callable[[Dict[str, Any]], None],
        pause: Optional[float] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self.on_event = on_event
        self.on_status = on_status
        self.pause = RECONNECT_SECONDS if pause is None else pause
        self.state = ""  # "running", "stopping" or "down"; "" before the first answer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop listening. The thread may still be waiting on the connection: it is a daemon."""
        self._stop.set()

    def _change(self, new: str) -> None:
        old, self.state = self.state, new
        if new == old or (new == "running" and old == "") or self.on_status is None:
            return
        self.on_status(SERVER_SAYS["again" if new == "running" else new])

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for event in self.client.reminder_events():
                    if self._stop.is_set():
                        return
                    if event.get("type") == "server":
                        self._change({"stopped": "down"}.get(event["state"], event["state"]))
                    else:
                        self.on_event(event)
            except ClaraError:
                pass  # server down, token refused...: try again, what was missed arrives then
            if not self._stop.is_set():
                self._change("down")
            self._stop.wait(self.pause)
