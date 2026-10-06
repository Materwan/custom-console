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


def take_when(words: List[str], now: datetime) -> Tuple[datetime, List[str]]:
    """The moment the words start with (+30m, 09:30, tomorrow 09:30, 2026-10-05 09:30) and the words after it.
    `now` is the local time, with its offset."""
    head = words[0].lower() if words else ""
    relative = re.fullmatch(r"\+(\d+)([mhd])", head)
    if relative:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[relative[2]]
        return now + timedelta(**{unit: int(relative[1])}), words[1:]
    if head == "tomorrow" and len(words) > 1:
        hour, minute = _clock(words[1])
        return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0), words[2:]
    if re.fullmatch(r"\d{4}-\d\d-\d\d", head) and len(words) > 1:
        hour, minute = _clock(words[1])
        try:
            return datetime.fromisoformat(head).replace(hour=hour, minute=minute).astimezone(), words[2:]
        except ValueError:
            raise ValueError(f"Not a date: {head!r}.") from None
    if ":" in head:
        hour, minute = _clock(head)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (due if due > now else due + timedelta(days=1)), words[1:]
    raise ValueError(f"Cannot read the time {head!r}. {WHEN_HELP}")


def parse_remind(argument: str, now: datetime) -> Tuple[datetime, str, str]:
    """``(when, repeat, text)`` from the arguments of /remind. `now` is the local time, with its offset."""
    words = argument.split()
    repeat = words.pop(0).lower() if words and words[0].lower() in REPEATS else ""
    if not words:
        raise ValueError(f"Usage: /remind [daily|weekly|monthly] WHEN TEXT   ({WHEN_HELP})")
    due, rest = take_when(words, now)
    text = " ".join(rest)
    if not text:
        raise ValueError("A reminder needs a text.")
    return due, repeat, text


TASK_HELP = (
    "Usage: /task add [@SURFACES] [due WHEN] [remind WHEN]... TITLE [| DESCRIPTION]\n"
    "       /task sub ID [@SURFACES] [due WHEN] [remind WHEN]... TITLE [| DESCRIPTION]   (a sub task of task ID)"
)


def parse_task(argument: str, now: datetime) -> Tuple[str, str, Optional[datetime], List[datetime]]:
    """``(title, description, due, reminders)`` from the arguments of /task add: ``[due WHEN] [remind WHEN]...
    TITLE [| DESCRIPTION]``."""
    words = argument.split()
    due: Optional[datetime] = None
    reminders: List[datetime] = []
    while words and words[0].lower() in ("due", "remind"):
        keyword = words.pop(0).lower()
        if not words:
            raise ValueError(f"{TASK_HELP}   ({keyword} needs a time: {WHEN_HELP})")
        moment, words = take_when(words, now)
        if keyword == "due":
            due = moment
        else:
            reminders.append(moment)
    title, _, description = " ".join(words).partition("|")
    if not title.strip():
        raise ValueError("A task needs a title.")
    return title.strip(), description.strip(), due, reminders


def parse_when_list(text: str, now: datetime) -> List[datetime]:
    """The moments of a comma-separated list (``+1h, tomorrow 09:00``); ``none`` is an empty list."""
    if text.strip().lower() in ("none", "off", "-"):
        return []
    moments = []
    for part in text.split(","):
        moment, rest = take_when(part.split(), now)
        if rest:
            raise ValueError(f"Unexpected after the time: {' '.join(rest)!r} (separate several times with commas).")
        moments.append(moment)
    return moments


def local_time(moment: str) -> str:
    """An ISO moment from the server, as local ``YYYY-MM-DD HH:MM``."""
    return datetime.fromisoformat(moment).astimezone().strftime("%Y-%m-%d %H:%M")


def describe_task(task: Dict[str, Any]) -> str:
    """One line: the number, title, deadline, reminders sent and the next reminder."""
    parts = [f"[{task['id']}] {task['title']}"]
    if task["status"] == "done":
        parts.append("done")
    if task.get("parent_id"):
        parts.append(f"sub task of [{task['parent_id']}]")
    if task.get("subtasks", {}).get("total"):
        parts.append(f"{task['subtasks']['done']}/{task['subtasks']['total']} sub tasks done")
    if task.get("due_at"):
        parts.append(f"due {local_time(task['due_at'])}")
    sent = task["reminders_sent"]
    parts.append(f"{sent} reminder{'s' if sent != 1 else ''} sent")
    if task["status"] == "open":
        parts.append(f"next reminder {local_time(task['next_reminder'])}" if task["next_reminder"] else "no reminder to come")
    return "  ·  ".join(parts)


def describe_task_detail(task: Dict[str, Any]) -> str:
    lines = [describe_task(task), f"Description: {task['description'] or '(none)'}"]
    if len(task["reminders"]) > 1:
        lines.append("Reminders to come: " + ", ".join(local_time(at) for at in task["reminders"]))
    if task.get("due_limit"):
        lines.append(f"Nothing of it may be later than {local_time(task['due_limit'])} (the deadline of the task it is part of).")
    if task["targets"]:
        lines.append("Shown on: " + ", ".join(task["targets"]))
    return "\n".join(lines)


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
