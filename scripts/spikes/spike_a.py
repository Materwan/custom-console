"""Spike A: a prompt_toolkit prompt (Tab menu + history ghost text), a full-screen Textual app, the prompt again.

Run through scripts/spikes/spike_a_driver.py, which feeds keys through a pseudo-terminal.
"""

from __future__ import annotations

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from textual.app import App, ComposeResult
from textual.widgets import Static

WORDS = ["help", "history", "hello", "ai agent", "alias"]


class Words(Completer):
    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        word = document.text_before_cursor
        for w in WORDS:
            if w.startswith(word):
                yield Completion(w, start_position=-len(word))


class Ghost(AutoSuggest):
    def __init__(self) -> None:
        self.lines = ["ls /C:/Users"]

    def get_suggestion(self, buffer, document):  # type: ignore[no-untyped-def]
        text = document.text
        for line in reversed(self.lines):
            if text and line.startswith(text) and line != text:
                return Suggestion(line[len(text):])
        return None


class Agent(App[str]):
    def compose(self) -> ComposeResult:
        yield Static("AGENT-SCREEN press q")

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "q":
            self.exit("agent-done")


def main() -> None:
    session: PromptSession[str] = PromptSession(completer=Words(), auto_suggest=Ghost(), complete_while_typing=False)
    for _ in range(3):
        line = session.prompt(HTML("<b>$ </b>"))
        print(f"LINE={line!r}", flush=True)
        if line == "ai agent":
            result = Agent().run()
            print(f"AGENT-RESULT={result!r}", flush=True)
        if line == "exit":
            break
    print("SPIKE-A-OK", flush=True)


if __name__ == "__main__":
    sys.exit(main())
