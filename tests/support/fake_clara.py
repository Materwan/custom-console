"""A stand-in for the Clara server: plays scripted turns and records what the console sent.

`FakeClara` is a real `ClaraClient` (so request bodies are built by the real code) whose network
methods are replaced. A turn is a list of events as the server streams them: build them with
`say(text)` (tokens, usage, done) and `ask_tools(("tool_name", {arguments}))` (a `tool_requests`
event, after which the console posts the results to `fake.results`).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional

from custom_console.agent.clara import ClaraClient, ClaraError, NothingToCompact


def say(
    text: str,
    prompt: int = 10,
    completion: int = 5,
    tokens: Optional[int] = None,
    window: int = 8192,
    model: str = "fake-model",
    provider: str = "local",
) -> List[Dict[str, Any]]:
    """The events of an answer: its text, the usage of the round, and the end of the turn."""
    tokens = prompt + completion if tokens is None else tokens
    return [
        {"type": "token", "text": text},
        {"type": "usage", "prompt_tokens": prompt, "completion_tokens": completion},
        {
            "type": "done",
            "reply": text,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            "context": {"tokens": tokens, "window": window, "percent": round(100 * tokens / window, 1)},
            "model": model,
            "provider": provider,
        },
    ]


def ask_tools(*requests: Any) -> Dict[str, Any]:
    """A `tool_requests` event for `(name, arguments)` pairs."""
    return {
        "type": "tool_requests",
        "turn": "turn-1",
        "calls": [{"id": f"call_0_{i}", "name": name, "arguments": arguments} for i, (name, arguments) in enumerate(requests)],
    }


class FakeClara(ClaraClient):
    def __init__(
        self,
        respond: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
        *,
        model: str = "fake-model",
        provider: str = "local",
        window: int = 8192,
        tokens: int = 0,
        admin_token: Optional[str] = "admin",
    ) -> None:
        super().__init__("http://fake-clara", "token", user_id="tester", admin_token=admin_token)
        self.respond = respond or (lambda body: say(f"Done: {body['message'][:20]}"))
        self.model, self.provider, self.window, self.tokens = model, provider, window, tokens
        self.summary, self.messages = "", 0
        self.bodies: List[Dict[str, Any]] = []  # the request of each turn
        self.results: List[List[Dict[str, str]]] = []  # what the console answered to tool requests
        self.forgotten: List[str] = []
        self.compactions: List[Any] = []
        self.admin_calls: List[str] = []
        self.admin_output: Dict[str, str] = {}
        self.down = False  # the server cannot be reached
        self.nothing_to_compact = False
        self.reminder_list: List[Dict[str, Any]] = []  # what /remind created
        self.announced: List[Dict[str, Any]] = []  # reminders the next connection of the stream delivers
        self.notify_after_value: Optional[int] = None  # what /notify-after set (None: the server's default)
        self.offered_models: List[Dict[str, Any]] = []  # what an administrator lets this user choose
        self.model_choice: Optional[str] = None  # what /model chose (None: the server's own)

    def _check(self) -> None:
        if self.down:
            raise ClaraError(f"Cannot reach the Clara server at {self.url}.")

    def health(self) -> Dict[str, Any]:
        self._check()
        return {"status": "ok", "provider": self.provider, "model": self.model}

    def stream_turn(self, body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        self._check()
        self.bodies.append(body)
        yield from self.respond(body)

    def send_results(self, turn_id: str, results: List[Dict[str, str]]) -> None:
        self.results.append(results)

    def context(self, conversation: str) -> Dict[str, Any]:
        self._check()
        return {
            "conversation": conversation,
            "tokens": self.tokens,
            "window": self.window,
            "percent": round(100 * self.tokens / self.window, 1),
            "summary": self.summary,
            "messages": self.messages,
        }

    def compact(self, conversation: str, focus: str = "") -> Dict[str, Any]:
        self._check()
        self.compactions.append((conversation, focus))
        if self.nothing_to_compact:
            raise NothingToCompact("the conversation is empty: nothing to compact")
        self.summary = "User wants the thing built; it is built."
        return {"before_percent": 60.0, "after_percent": 10.0, "summary": self.summary}

    def forget(self, conversation: str) -> None:
        self.forgotten.append(conversation)

    def add_reminder(self, at: str, text: str, repeat: str = "", targets: Optional[List[str]] = None) -> Dict[str, Any]:
        self._check()
        reminder = {"id": len(self.reminder_list) + 1, "text": text, "due_at": at, "repeat": repeat,
                    "targets": list(targets or [])}
        self.reminder_list.append(reminder)
        return reminder

    def notify(self, text: str, title: str = "", targets: Optional[List[str]] = None) -> Dict[str, Any]:
        self._check()
        return {"id": 1, "targets": list(targets or [])}

    def settings(self) -> Dict[str, Any]:
        self._check()
        own = self.notify_after_value
        return {"notify_after": own, "notify_after_default": 120,
                "notify_after_effective": 120 if own is None else own}

    def set_notify_after(self, seconds: Optional[int]) -> Dict[str, Any]:
        self._check()
        self.notify_after_value = seconds
        return self.settings()

    def models(self) -> Dict[str, Any]:
        self._check()
        default = {"ref": f"{self.provider}:{self.model}", "name": self.model, "provider": self.provider,
                   "provider_label": "Local host", "weight": 0.4}
        chosen = next((m for m in self.offered_models if m["ref"] == self.model_choice), None)
        return {"models": self.offered_models, "default": default, "current": chosen or default,
                "choices": {"console": chosen["ref"]} if chosen else {}}

    def choose_model(self, ref: Optional[str]) -> Dict[str, Any]:
        self._check()
        self.model_choice = ref
        return self.models()

    def reminders(self) -> List[Dict[str, Any]]:
        self._check()
        return list(self.reminder_list)

    def cancel_reminder(self, reminder_id: int) -> None:
        self._check()
        before = len(self.reminder_list)
        self.reminder_list[:] = [r for r in self.reminder_list if r["id"] != reminder_id]
        if len(self.reminder_list) == before:
            raise ClaraError("Clara server: No such reminder of yours (HTTP 404)")

    def reminder_events(self) -> Iterator[Dict[str, Any]]:
        """Delivers what was announced, then the connection ends (the listener reconnects)."""
        self._check()
        while self.announced:
            yield self.announced.pop(0)

    def admin(self, line: str) -> str:
        if not self.admin_token:
            return super().admin(line)
        self.admin_calls.append(line)
        return self.admin_output.get(line, f"ran {line}")

    def admin_commands(self) -> List[Dict[str, Any]]:
        if not self.admin_token:
            return []
        return [
            {"name": "provider", "usage": "[local|cloud]", "summary": "", "choices": ["local", "cloud"]},
            {"name": "model", "usage": "[name]", "summary": "", "choices": []},
        ]
