"""Permissions answered "always" or with a reason, the draft kept during a question, !commands, @files,
plan mode, /undo told to the model, server tools and reasoning in the turn, an instant stop, git and
desktop tools."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time

import pytest
from fake_clara import FakeClara, ask_tools, say
from outcome import outcome
from test_console import Session, wait_for
from test_ui import Harness, simple_runner

from custom_console.agent.clara import ClaraError
from custom_console.agent.mentions import attach, mentioned
from custom_console.agent.permissions import Decision, SESSION
from custom_console.agent.remote import run_remote_turn
from custom_console.agent.tools import desktop as desktop_module
from custom_console.agent.tools.git import git_tools
from custom_console.agent.tools.mail import mail_tools
from custom_console.agent.tools.state import ReadTracker


def by_name(tools):
    return {tool.__name__: tool for tool in tools}


# --------------------------------------------------------------------------- #
# Permission questions in the console
# --------------------------------------------------------------------------- #


def two_writes(session_dir):
    targets = [str(session_dir / "elsewhere" / name) for name in ("a.txt", "b.txt")]

    def respond(body):
        if body["message"] == "write twice":
            return [
                ask_tools(("file_system_write", {"path": targets[0], "content": "a"})),
                ask_tools(("file_system_write", {"path": targets[1], "content": "b"})),
                *say("Done: twice"),
            ]
        return say(f"Done: {body['message'][:20]}")

    return FakeClara(respond)


class TestPermissionAnswers:
    def test_always_for_the_session_stops_asking_for_that_folder(self, tmp_path):
        session = Session(tmp_path, permission_level=1, clara=two_writes(tmp_path))
        asked = []

        def driver(s):
            s.send("write twice")
            wait_for(lambda: s.console_.screen._question is not None)
            asked.append(s.console_.screen._question.prompt())
            s.send("a")
            s.wait_output("Done: twice")

        out = session.run(driver)
        assert asked == ["Allow? (y|a|n) > "]  # --no-memory: no project to remember it in
        assert (tmp_path / "elsewhere" / "a.txt").exists() and (tmp_path / "elsewhere" / "b.txt").exists()
        assert "accepted, always (session)" in out and "accepted (always, session)" in out

    def test_a_refusal_can_say_why_and_the_model_is_told(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("write a note")
            wait_for(lambda: s.console_.screen._question is not None)
            s.send("n put it in the work folder")
            s.wait_output("Done: write a note")

        out = session.run(driver)
        result = outcome(session.clara.results[0][0]["content"])
        assert not result.success and "They said: put it in the work folder" in result.error
        assert "refused: put it in the work folder" in out

    def test_an_empty_answer_answers_nothing_and_the_draft_comes_back(self):
        answers = []
        holder = {}

        def driver(h):
            holder["h"] = h
            h.send("half a message")
            wait_for(lambda: h.screen._buffer.text == "half a message")
            threading.Thread(
                target=lambda: answers.append(h.screen.ask_tool_permission("Agent wants to run x", "run_command:x")),
                daemon=True,
            ).start()
            wait_for(lambda: h.screen._question is not None)
            assert h.screen._buffer.text == ""  # the draft is put aside, not taken as the answer
            h.send("\r")
            time.sleep(0.2)
            assert answers == [] and h.screen._question is not None
            h.send("a\r")
            wait_for(lambda: answers)
            wait_for(lambda: h.screen._buffer.text == "half a message")
            h.send("\x15")  # clear it before the harness leaves

        Harness(simple_runner).run(driver)
        assert answers == [Decision(True, SESSION)]


# --------------------------------------------------------------------------- #
# What goes with a message
# --------------------------------------------------------------------------- #


class TestWhatGoesWithAMessage:
    def test_a_bang_command_is_shown_and_told_once_with_the_next_message(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("!echo hello from me")
            s.wait_output("the agent sees this output")
            s.send("what did I run?")
            s.wait_output("Done: what did I run?")
            s.send("and now?")
            s.wait_output("Done: and now?")

        out = session.run(driver)
        assert "$ echo hello from me" in out and "hello from me" in out
        first, second = session.clara.bodies
        assert "The user ran a command themselves" in first["prefix"] and "hello from me" in first["prefix"]
        assert "ran a command" not in second["prefix"]

    def test_a_mentioned_file_is_attached_and_counts_as_read(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("look at @visible.txt please")
            s.wait_output("Done: look at")

        session.run(driver)
        prefix = session.clara.bodies[0]["prefix"]
        assert "[File attached by the user:" in prefix and "visible.txt]\n```\nhi\n```" in prefix
        session.console_.tool_context.reads.check(str(session.created["work"] / "visible.txt"))

    def test_plan_mode_offers_only_the_reading_tools(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("/plan")
            s.wait_output("Plan mode on")
            s.send("plan it")
            s.wait_output("Done: plan it")
            s.send("/plan off")
            s.wait_output("Plan mode off")

        session.run(driver)
        body = session.clara.bodies[0]
        sent = {tool["function"]["name"] for tool in body["tools"]}
        assert {"file_system_read", "file_system_grep", "ask_user", "todo_write"} <= sent
        assert not sent & {"file_system_write", "file_system_edit", "run_command"}
        assert "## Plan mode" in body["instructions"]
        assert not session.console_.plan_mode

    def test_an_undo_is_told_to_the_model(self, tmp_path):
        session = Session(tmp_path, permission_level=1, write_inside_zone=True)

        def driver(s):
            s.send("write a note")
            s.wait_output("Done: write a note")
            s.send("/undo")
            s.wait_output("removed")
            s.send("what now?")
            s.wait_output("Done: what now?")

        session.run(driver)
        assert "The user undid the file changes" in session.clara.bodies[-1]["prefix"]


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #


class TestStream:
    def test_server_tools_and_reasoning_are_shown(self, tmp_path):
        events = [
            {"type": "thinking", "text": "The user wants tea noted."},
            {"type": "tool", "name": "remember", "arguments": {"fact": "Likes tea"}, "result": "Remembered."},
            *say("Noted."),
        ]
        session = Session(tmp_path, permission_level=1, clara=FakeClara(lambda body: events))

        def driver(s):
            s.send("I like tea")
            s.wait_output("Noted.")

        out = session.run(driver)
        assert "✻ thought for" in out and "✔ remember(fact='Likes tea') · on the server · Remembered." in out
        turn = session.console_.screen._finished[-1].snapshot()
        assert turn[0].detail == "The user wants tea noted."  # unfolds with Ctrl+O

    def test_a_stop_closes_the_connection_at_once(self):
        released = threading.Event()

        class Stuck(FakeClara):
            def stream_turn(self, body):
                yield {"type": "token", "text": "partial"}
                released.wait(30)  # a read blocked until the connection is closed
                raise ClaraError("connection closed")

            def abort_stream(self):
                released.set()

        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        texts = []
        assert run_remote_turn(Stuck(), {}, lambda name, arguments: "", on_text=texts.append, cancel=cancel) is None
        assert time.monotonic() - started < 5 and texts == ["partial"]


# --------------------------------------------------------------------------- #
# @mentions
# --------------------------------------------------------------------------- #


class TestMentions:
    def test_what_counts_as_a_mention(self):
        assert mentioned('see @src/a.py, and @"my notes.md" or @src/a.py.') == ["src/a.py", "my notes.md"]
        assert mentioned("mail me at me@example.com") == []

    def test_attach_within_a_budget(self, ctx, files_dir):
        (files_dir / "small.txt").write_text("small")
        (files_dir / "big.txt").write_text("x" * 5_000)
        (files_dir / "folder").mkdir()
        (files_dir / "folder" / "inside.txt").write_text("")
        reads = ReadTracker()
        blocks = attach("@small.txt @folder @big.txt @nothing.txt @someone", ctx.files, reads, budget=2_000)
        assert blocks[0].endswith("```\nsmall\n```") and "inside.txt" in blocks[1]
        assert "[... cut: read the rest" in blocks[2] and len(blocks) == 3
        reads.check(str(files_dir / "small.txt"))
        with pytest.raises(PermissionError):  # attached in part: it must be read before an edit
            reads.check(str(files_dir / "big.txt"))


# --------------------------------------------------------------------------- #
# Git, desktop and mail tools
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
class TestGitTools:
    def repo(self, files_dir):
        def git(*arguments):
            subprocess.run(["git", "-C", str(files_dir), *arguments], check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "Tester")
        (files_dir / "a.txt").write_text("one\n")
        git("add", "a.txt")
        git("commit", "-q", "-m", "first commit")
        (files_dir / "a.txt").write_text("one\ntwo\n")
        (files_dir / "new.txt").write_text("new\n")

    def test_status_diff_and_log_need_no_question_in_the_zone(self, make_ctx, files_dir):
        self.repo(files_dir)
        ctx, log = make_ctx(zone=True, auto_level=0)
        tools = by_name(git_tools(ctx))
        status = tools["git_status"]().data
        assert " M a.txt" in status and "?? new.txt" in status
        diff = tools["git_diff"]()
        assert "+two" in diff.data and "1 file changed" in diff.summary
        assert "Tester: first commit" in tools["git_log"]().data
        assert log.asked == []

    def test_options_cannot_be_slipped_in(self, make_ctx, files_dir):
        self.repo(files_dir)
        ctx, _ = make_ctx(zone=True)
        result = by_name(git_tools(ctx))["git_diff"](revision="--output=x.txt")
        assert not result.success and "must not start with '-'" in str(result.error)
        assert not (files_dir / "x.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop")
class TestDesktopTools:
    def test_programs_are_always_asked_documents_may_be_allowed_for_good(self, make_ctx, files_dir, monkeypatch):
        opened = []
        monkeypatch.setattr(desktop_module.os, "startfile", opened.append, raising=False)
        (files_dir / "doc.pdf").write_bytes(b"%PDF")
        (files_dir / "run.bat").write_text("echo")
        ctx, log = make_ctx(auto_level=1, answer=True)
        tools = by_name(desktop_module.desktop_tools(ctx))
        assert tools["open_path"]("doc.pdf").success and tools["open_path"]("run.bat").success
        assert tools["open_path"]("https://example.org").success
        assert log.rules == ["open_path:.pdf files", None, "open_path:URLs"]
        assert opened[-1] == "https://example.org"

    def test_the_clipboard(self, make_ctx, monkeypatch):
        calls = []
        monkeypatch.setattr(desktop_module, "_powershell", lambda script, env=None: calls.append(env) or "copied\r\n")
        ctx, log = make_ctx(auto_level=1, answer=True)
        tools = by_name(desktop_module.desktop_tools(ctx))
        assert tools["clipboard_write"]("héllo").success and calls[0] == {"CLARA_CLIPBOARD": "héllo"}
        assert tools["clipboard_read"]().data == "copied"
        assert len(log.asked) == 2  # both ask at level 1: replacing it loses what was there, reading it may leak

    def test_an_unknown_application(self, make_ctx, monkeypatch):
        monkeypatch.setattr(desktop_module, "find_application", lambda *a, **k: None)
        ctx, _ = make_ctx(auto_level=2)
        result = by_name(desktop_module.desktop_tools(ctx))["launch_app"]("nothing-like-this")
        assert not result.success and "No application named" in str(result.error)


def test_an_email_is_shown_in_full_before_it_is_sent(make_ctx):
    ctx, log = make_ctx(auto_level=1, answer=False, SMTP_HOST="smtp.example.com")
    send = by_name(mail_tools(ctx))["send_email"]
    assert not send("bob@example.com", "Hello", "line one\nline two").success
    assert log.asked[0] == "Agent wants to send an email to bob@example.com\nSubject: Hello\n\nline one\nline two"
    assert log.rules == ["send_email:bob@example.com"]
