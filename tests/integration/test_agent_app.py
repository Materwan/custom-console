"""The full-screen agent, driven with Textual's pilot against the fake Clara."""

from __future__ import annotations

import sys

import pytest
from textual.widgets import Collapsible, OptionList, Static

from custom_console.core.attachments import PNG_SIGNATURE
from custom_console.core.options import AgentOptions
from custom_console.ui.agent_app import MAX_BLOCKS, common_prefix
from custom_console.ui.widgets.blocks import (
    AnswerBlock,
    ApprovalBlock,
    HistoryBlock,
    NoteBlock,
    PendingTool,
    QuestionBlock,
    UserBlock,
)
from custom_console.ui.widgets.picker import PickerScreen
from custom_console.ui.widgets.status import shorten
from custom_console.ui.widgets.viewer import ViewerScreen
from tests.fake_clara.server import Hang, WaitResults
from tests.support.appworld import SIZE, make_app, settle, text_of, transcript_text, type_and_send
from tests.support.shelltest import Sh

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
CID = "c0123456789abcdef"


def turn(turn_id="t1", conversation=CID, **extra):
    return {"type": "turn", "id": turn_id, "conversation": conversation, "model": "fake:small", "incognito": False,
            "local_only": False, **extra}


def done(reply="ok", conversation=CID):
    return {"type": "done", "reply": reply, "conversation": conversation, "tools": [], "message_id": 7, "versions": [7],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "context": {"tokens": 100, "window": 1000, "percent": 10.0},
            "model": "fake:small", "tainted": False}


def token(text):
    return {"type": "token", "text": text}


def requests(turn_id, *calls):
    return {"type": "tool_requests", "turn": turn_id,
            "calls": [{"id": f"call_0_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]}


def test_common_prefix():
    assert common_prefix(["notes.txt", "nothing.md"]) == "not" and common_prefix(["a"]) == "a" and common_prefix([]) == ""
    assert common_prefix(["abc", "xyz"]) == ""


async def test_it_starts_with_a_focused_prompt_and_a_status_line(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot, until=lambda: "Talking to Clara in" in transcript_text(app))
        assert app.focused is app.prompt_box
        status = text_of(app.query_one("#status", Static))
        assert status.startswith(shorten(str(sh.work))) and "new conversation" in status and "default model" in status


async def test_a_message_streams_and_ends_as_markdown(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("Hel"), token("lo **bold**\n\nsecond"), done("Hello **bold**\n\nsecond"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hi there")
        await settle(app, pilot)
        blocks = list(app.query(UserBlock))
        assert len(blocks) == 1 and "hi there" in text_of(blocks[0])
        answer = app.query_one(AnswerBlock)
        assert answer.complete and [text_of(s) for s in answer.query("Static.markdown")] == ["Hello **bold**\n\nsecond"]
        assert app.prompt_box.text == "" and app.focused is app.prompt_box
        assert "ctx 10%" in text_of(app.query_one("#status", Static))
        body = fake_clara.last("/v1/chat/stream")["body"]
        assert body["message"] == "hi there" and body["instructions"].startswith("# The console")


async def test_the_session_gets_a_title_after_the_first_answer(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("x"), done("x"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hello")
        await settle(app, pilot)
        await settle(app, pilot, until=lambda: bool(app.agent.session.title))
        assert "A title" in text_of(app.query_one("#status", Static))


async def test_a_permission_prompt_opens_inline_and_one_key_answers_it(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_write", {"path": "new.txt", "content": "hi"})), WaitResults(),
                          token("done"), done("done"))
    app, sh = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "make a file")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        block = app.query_one(ApprovalBlock)
        assert app.focused is block
        shown = "\n".join(text_of(w) for w in block.query(Static))
        assert "Write" in shown and "new.txt" in shown and "[o]" in shown
        await pilot.press("o")
        await settle(app, pilot)
        assert (sh.work / "new.txt").read_text() == "hi"
        assert "allowed once" in transcript_text(app) and app.focused is app.prompt_box
        assert any("write" in line and "new.txt" in line for line in
                   [str(c.title) for c in app.query(Collapsible)])


@pytest.mark.parametrize(("key", "file_made", "rules"), [("r", False, 0), ("n", False, 1), ("a", True, 1), ("escape", False, 0)])
async def test_every_answer_key(tmp_path, fake_clara, key, file_made, rules):
    fake_clara.queue_turn(turn(), requests("t1", ("local_write", {"path": "x.txt", "content": "1"})), WaitResults(), done("end"))
    app, sh = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "write")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        await pilot.press(key)
        await settle(app, pilot)
    assert (sh.work / "x.txt").exists() is file_made and len(app.agent.rules.list()) == rules


@pytest.mark.parametrize(("key", "pattern", "exact", "tail"), [
    ("y", ("ruff",), False, "(pattern)"), ("e", (), True, "always:"), ("n", (), False, "once:"),
])
async def test_always_on_a_command_offers_a_pattern_to_confirm(tmp_path, fake_clara, key, pattern, exact, tail):
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "ruff --version"})), WaitResults(), done("end"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "run it")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        block = app.query_one(ApprovalBlock)
        await pilot.press("a")
        await settle(app, pilot, until=lambda: block.stage == "confirm")
        shown = "\n".join(text_of(w) for w in block.query(Static))
        assert "Always allow in this folder:" in shown and "ruff:*" in shown and "[y]" in shown and "[e]" in shown
        assert not block.future.done()
        await pilot.press(key)
        await settle(app, pilot)
        assert tail in transcript_text(app)
    rules = app.agent.rules.list()
    assert [r.pattern for r in rules if r.pattern] == ([pattern] if pattern else [])
    assert any(not r.pattern and r.key.command == "ruff --version" for r in rules) is exact


async def test_escape_in_the_pattern_question_goes_back_to_the_first_question(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "ruff --version"})), WaitResults(), done("end"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "run it")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        block = app.query_one(ApprovalBlock)
        await pilot.press("a")
        await settle(app, pilot, until=lambda: block.stage == "confirm")
        await pilot.press("escape")
        await settle(app, pilot, until=lambda: block.stage == "ask")
        assert not block.future.done() and "[o]" in "\n".join(text_of(w) for w in block.query(Static))
        await pilot.press("r")
        await settle(app, pilot)
    assert app.agent.rules.list() == []


async def test_a_model_proposes_the_pattern_for_a_command_the_table_does_not_know(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "mytool build --fast"})), WaitResults(), done("end"))
    fake_clara.queue_turn(turn("t9", conversation=None), token('["mytool build"]'), done('["mytool build"]', conversation=None))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "run it")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        block = app.query_one(ApprovalBlock)
        await pilot.press("a")
        await settle(app, pilot, until=lambda: block.stage == "confirm")
        assert "mytool build:*" in "\n".join(text_of(w) for w in block.query(Static))
        await pilot.press("y")
        await settle(app, pilot)
    asked = fake_clara.requests_to("/v1/chat/stream")[1]["body"]
    assert asked["incognito"] is True and "tools" not in asked and asked["message"] == '["mytool build --fast"]'
    assert [r.pattern for r in app.agent.rules.list()] == [("mytool", "build")]


async def test_without_a_usable_proposal_always_means_the_exact_command(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "mytool build"})), WaitResults(), done("end"))
    fake_clara.queue_turn(turn("t9", conversation=None), token("no idea"), done("no idea", conversation=None))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "run it")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        await pilot.press("a")
        await settle(app, pilot)
    assert [(r.pattern, r.key.command) for r in app.agent.rules.list()] == [((), "mytool build")]


async def test_prompts_queue_up_and_are_answered_one_after_the_other(tmp_path, fake_clara):
    calls = [("local_write", {"path": "a.txt", "content": "a"}), ("local_write", {"path": "b.txt", "content": "b"})]
    fake_clara.queue_turn(turn(), requests("t1", *calls), WaitResults(), done("end"))
    app, sh = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "write two")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        await pilot.press("o")
        await settle(app, pilot, until=lambda: len([b for b in app.query(ApprovalBlock) if not b.future.done()]) == 1)
        await pilot.press("r")
        await settle(app, pilot)
    assert (sh.work / "a.txt").exists() and not (sh.work / "b.txt").exists()


async def test_escape_stops_the_answer_and_closes_the_connection(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("partial"), Hang())
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "long one")
        await settle(app, pilot, until=lambda: "partial" in transcript_text(app))
        assert app.turn_running and "working" in text_of(app.query_one("#status", Static))
        await pilot.press("escape")
        await settle(app, pilot, until=lambda: "Stopped." in transcript_text(app) and fake_clara.disconnects == 1)
        assert not app.turn_running
        assert app.query_one(AnswerBlock).complete


async def test_a_second_message_while_one_runs_is_refused_and_kept(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("a"), Hang())
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "first")
        await settle(app, pilot, until=lambda: app.turn_running)
        await type_and_send(app, pilot, "second")
        await settle(app, pilot, until=lambda: "still running" in transcript_text(app))
        assert app.prompt_box.text == "second"
        await pilot.press("escape")
        await settle(app, pilot)


async def test_an_error_event_is_told_in_one_line(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "error", "status": 502, "code": "model_failed", "message": "The model failed."})
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hi")
        await settle(app, pilot)
        notes = [text_of(n) for n in app.query(NoteBlock) if n.has_class("error")]
        assert notes == ["The model failed. Retry, or choose another model with /model."]


async def test_the_model_asking_a_question_is_answered_in_the_prompt_box(tmp_path, fake_clara):
    ask = ("local_ask_user", {"question": "Which one?", "options": ["red", "blue"]})
    fake_clara.queue_turn(turn(), requests("t1", ask), WaitResults(), done("thanks"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "pick")
        await settle(app, pilot, until=lambda: bool(app.query(QuestionBlock)))
        assert "1. red" in transcript_text(app) and "2. blue" in transcript_text(app)
        await type_and_send(app, pilot, "2")
        await settle(app, pilot)
    assert fake_clara.results["t1"]["call_0_0"] == "The person answered: blue"


async def test_server_tools_show_one_line_and_open_to_their_result(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "tool_start", "name": "web_search", "arguments": {"query": "weather"}},
                          {"type": "tool", "name": "web_search", "arguments": {"query": "weather"}, "result": "Sunny", "truncated": False},
                          token("It is sunny."), done("It is sunny."))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "weather?")
        await settle(app, pilot)
        titles = [str(c.title) for c in app.query(Collapsible)]
        assert any("web_search weather" in t for t in titles) and not app.query("PendingTool")
        assert "Sunny" in transcript_text(app)


async def test_thinking_is_collapsed_and_counted(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "thinking", "text": "let me think about it"}, token("answer"), done("answer"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "think")
        await settle(app, pilot)
        assert any(str(c.title) == "Thought (5 words)" and c.collapsed for c in app.query(Collapsible))


# ---------------------------------------------------------------------- slash and shell commands
async def test_slash_commands_print_into_the_transcript(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "/model fake:big")
        await settle(app, pilot)
        assert "Model: fake:big" in transcript_text(app) and "fake:big" in text_of(app.query_one("#status", Static))
        await type_and_send(app, pilot, "/plan")
        await settle(app, pilot)
        assert "[plan]" in text_of(app.query_one("#status", Static))


async def test_model_picker_is_chosen_with_a_mouse_click(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "/model")
        await settle(app, pilot, until=lambda: isinstance(app.screen, PickerScreen))
        options = app.screen.query_one("#picker-list", OptionList)
        big = next(i for i in range(options.option_count) if "fake:big" in str(options.get_option_at_index(i).prompt))
        await pilot.click("#picker-list", offset=(4, big + 1))  # +1: the list's border
        await settle(app, pilot, until=lambda: "Model: fake:big" in transcript_text(app))
        assert not isinstance(app.screen, PickerScreen) and "fake:big" in text_of(app.query_one("#status", Static))


async def test_an_unknown_command_is_told_and_a_double_slash_is_a_message(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("ok"), done("ok"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "/nope")
        await settle(app, pilot)
        assert "Unknown command /nope" in transcript_text(app)
        await type_and_send(app, pilot, "//etc/hosts what is this")
        await settle(app, pilot)
        assert fake_clara.last("/v1/chat/stream")["body"]["message"] == "/etc/hosts what is this"


async def test_shell_commands_work_with_a_slash(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.write("notes.txt", "alpha\n")
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "/ls -1")
        await settle(app, pilot)
        assert "notes.txt" in transcript_text(app)
        await type_and_send(app, pilot, "/cd ..")
        await settle(app, pilot)
        assert "not available inside the agent console" in transcript_text(app)


async def test_a_bang_command_runs_and_its_output_goes_with_the_next_message(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("ok"), done("ok"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "!echo from-the-shell")
        await settle(app, pilot)
        assert "from-the-shell" in transcript_text(app) and "goes with your next message" in transcript_text(app)
        await type_and_send(app, pilot, "what was that?")
        await settle(app, pilot)
        assert "from-the-shell" in fake_clara.last("/v1/chat/stream")["body"]["message"]


async def test_exit_leaves_the_app(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "/exit")
        await pilot.pause(0.5)
    assert app.return_code == 0 and "Left the agent console" in app.summary()


async def test_clear_empties_the_screen(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("x"), done("x"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hello")
        await settle(app, pilot)
        await type_and_send(app, pilot, "/clear --incognito")
        await settle(app, pilot, until=lambda: "New conversation (incognito)" in transcript_text(app) and not app.query(UserBlock))
        assert not app.query(UserBlock) and not app.query(AnswerBlock)
        assert "New conversation (incognito)" in transcript_text(app)
        assert app.agent.session.incognito and "[incognito]" in text_of(app.query_one("#status", Static))


# ---------------------------------------------------------------------- the prompt box
async def test_enter_sends_and_shift_enter_or_ctrl_j_adds_a_line(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        for char in "one":
            await pilot.press(char)
        await pilot.press("ctrl+j")
        for char in "two":
            await pilot.press(char)
        assert app.prompt_box.text == "one\ntwo" and app.prompt_box.outer_size.height == 4
        await pilot.press("ctrl+c")
        assert app.prompt_box.text == ""
        app.prompt_box.load_text("line\\")
        app.prompt_box.move_cursor(app.prompt_box.document.end)
        await pilot.press("enter")
        assert app.prompt_box.text == "line\n"


async def test_up_and_down_walk_what_was_sent_and_it_is_saved(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("a"), done("a"))
    fake_clara.queue_turn(turn("t2"), token("b"), done("b"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "first\nline two")
        await settle(app, pilot)
        await type_and_send(app, pilot, "second")
        await settle(app, pilot)
        await pilot.press("up")
        assert app.prompt_box.text == "second"
        await pilot.press("up")
        assert app.prompt_box.text == "first\nline two"
        await pilot.press("down")
        assert app.prompt_box.text == "second"
        await pilot.press("down")
        assert app.prompt_box.text == ""
    saved = (tmp_path / "data" / "agent_history").read_text().splitlines()
    assert saved == ["first\\nline two", "second"]
    again, _ = make_app(tmp_path / "again", fake_clara)
    assert again.history.messages() == []


async def test_tab_completes_a_command_a_path_and_an_at_mention(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.write("notes.txt", "x")
    sh.write("nothing.md", "x")
    sh.write("src/main.py", "x")
    async with app.run_test(size=SIZE) as pilot:
        app.prompt_box.load_text("/mod")
        app.prompt_box.move_cursor(app.prompt_box.document.end)
        await pilot.press("tab")
        await settle(app, pilot, until=lambda: app.prompt_box.text == "/model")
        app.prompt_box.load_text("look at @src/ma")
        app.prompt_box.move_cursor(app.prompt_box.document.end)
        await pilot.press("tab")
        await settle(app, pilot, until=lambda: app.prompt_box.text == "look at @src/main.py")
        app.prompt_box.load_text("see @no")
        app.prompt_box.move_cursor(app.prompt_box.document.end)
        await pilot.press("tab")
        popup = app.query_one("#popup", OptionList)
        await settle(app, pilot, until=lambda: popup.has_class("open"))
        assert popup.option_count == 2 and app.prompt_box.text == "see @not"  # the common part was inserted
        await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot, until=lambda: app.prompt_box.text == "see @nothing.md")
        assert not popup.has_class("open")


async def test_escape_closes_the_completion_popup_without_stopping_anything(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.write("notes.txt", "x")
    sh.write("nothing.md", "x")
    async with app.run_test(size=SIZE) as pilot:
        app.prompt_box.load_text("@n")
        app.prompt_box.move_cursor(app.prompt_box.document.end)
        await pilot.press("tab")
        await settle(app, pilot, until=lambda: app.query_one("#popup", OptionList).has_class("open"))
        await pilot.press("escape")
        await settle(app, pilot, until=lambda: not app.query_one("#popup", OptionList).has_class("open"))
        assert app.prompt_box.text.startswith("@n")


# ---------------------------------------------------------------------- attachments
async def test_a_mentioned_file_goes_in_the_message(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("ok"), done("ok"))
    app, sh = make_app(tmp_path, fake_clara)
    sh.write("a.py", "print(1)\n")
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "explain @a.py")
        await settle(app, pilot)
        assert "print(1)" in fake_clara.last("/v1/chat/stream")["body"]["message"]
        assert "a.py" in text_of(app.query_one(UserBlock)) and "print(1)" not in text_of(app.query_one(UserBlock))


async def test_a_missing_mention_stops_the_message_and_keeps_nothing_sent(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "read @gone/x.txt")
        await settle(app, pilot)
        assert "Not sent. @gone/x.txt" in transcript_text(app) and fake_clara.requests_to("/v1/chat/stream") == []


async def test_ctrl_v_with_an_image_adds_a_chip_that_goes_as_an_attachment(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("ok"), done("ok"))
    app, sh = make_app(tmp_path, fake_clara)
    sh.desktop.image = PNG_SIGNATURE + b"fake"
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+v")
        await settle(app, pilot, until=lambda: bool(app.chips))
        assert "pasted-image-1.png" in text_of(app.query_one("#chips", Static))
        await type_and_send(app, pilot, "what is this?")
        await settle(app, pilot)
        body = fake_clara.last("/v1/chat/stream")["body"]
        assert [a["name"] for a in body["attachments"]] == ["pasted-image-1.png"]
        assert app.chips == [] and app.query_one("#chips", Static).has_class("empty")


async def test_ctrl_v_without_an_image_pastes_the_clipboard_text_and_ctrl_x_drops_a_chip(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.desktop.clipboard = "pasted words"
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+v")
        await settle(app, pilot, until=lambda: app.prompt_box.text == "pasted words")
        sh.desktop.image = PNG_SIGNATURE
        await pilot.press("ctrl+v")
        await settle(app, pilot, until=lambda: len(app.chips) == 1)
        await pilot.press("ctrl+x")
        assert app.chips == []


async def test_a_clipboard_with_no_tool_says_so(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.desktop.fail = "No clipboard tool found (install xclip, xsel or wl-clipboard)."
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+v")
        await settle(app, pilot, until=lambda: "Nothing pasted: No clipboard tool found" in transcript_text(app))


# ---------------------------------------------------------------------- trust, sub-agents, viewer
async def test_a_new_folder_asks_for_trust_and_o_grants_it(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara, trusted=False)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        assert "[untrusted]" in text_of(app.query_one("#status", Static))
        await pilot.press("o")
        await settle(app, pilot, until=lambda: app.agent.trusted)
        await settle(app, pilot, until=lambda: "[untrusted]" not in text_of(app.query_one("#status", Static)))


async def test_saying_no_to_trust_leaves_the_agent_read_only(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara, trusted=False)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        await pilot.press("r")
        await settle(app, pilot, until=lambda: "can only read here" in transcript_text(app))
        assert not app.agent.trusted


async def test_a_subagent_is_a_collapsed_block_that_ends_with_a_mark(tmp_path, fake_clara):
    sub = "c0fedcba987654321"
    fake_clara.queue_turn(turn(), requests("t1", ("local_task", {"prompt": "find the thing"})), WaitResults(), token("back"),
                          done("back"))
    fake_clara.queue_turn(turn("s1", sub, incognito=True), token("found"), done("found", sub))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "delegate")
        await settle(app, pilot)
        titles = [str(c.title) for c in app.query(Collapsible)]
        assert any(t.startswith("ok Sub-agent: find the thing") for t in titles)
    assert fake_clara.results["t1"]["call_0_0"] == "found"


async def test_the_journal_opens_in_a_viewer_with_search(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), token("x"), done("x"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hello")
        await settle(app, pilot)
        await type_and_send(app, pilot, "/journal")
        await settle(app, pilot, until=lambda: isinstance(app.screen, ViewerScreen))
        viewer = app.screen
        assert "Journal" in viewer.title_text and "turn" in viewer.body
        await pilot.press("slash")
        await pilot.press(*"turn")
        await pilot.press("enter")
        await settle(app, pilot, until=lambda: bool(viewer.matches))
        assert viewer.needle == "turn"
        await pilot.press("escape")
        await settle(app, pilot, until=lambda: not isinstance(app.screen, ViewerScreen))


async def test_restore_shows_the_old_conversation(tmp_path, fake_clara):
    fake_clara.conversations[CID] = {"id": CID, "title": "", "packs": ["web"], "messages": [
        {"id": 5, "role": "user", "content": "What is 2+2?", "versions": [5], "version": 1},
        {"id": 6, "role": "assistant", "content": "It is **4**."}]}
    fake_clara.queue_turn(turn(), token("x"), done("x"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hello")
        await settle(app, pilot)
        await type_and_send(app, pilot, "/clear")
        await settle(app, pilot)
        await type_and_send(app, pilot, "/restore 1")
        await settle(app, pilot, until=lambda: "Restored:" in transcript_text(app))
        await settle(app, pilot, until=lambda: "It is **4**." in transcript_text(app))
        text = transcript_text(app)
        assert "What is 2+2?" in text and "It is **4**." in text and "Restored:" in text


async def test_the_app_keeps_the_options_it_was_started_with(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara, options=AgentOptions(incognito=True, local_only=True, model="fake:big"))
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot, until=lambda: "[incognito]" in text_of(app.query_one("#status", Static)))
        status = text_of(app.query_one("#status", Static))
        assert "[incognito]" in status and "[local only]" in status and "fake:big" in status


# ---------------------------------------------------------------------- a long transcript stays cheap (PERF-1)
async def test_old_finished_blocks_are_folded_into_one_widget(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        for number in range(150):
            app.say(f"note {number}")
        # the app folds by itself while they arrive; whenever it does, the transcript stays at or under MAX_BLOCKS widgets
        await settle(app, pilot, until=lambda: "note 149" in transcript_text(app) and bool(app.query(HistoryBlock)))
        app.trim_transcript()
        await pilot.pause(0.3)
        children = list(app.query("#transcript > *"))
        assert isinstance(children[0], HistoryBlock) and len(children) <= MAX_BLOCKS
        folded = text_of(children[0])
        assert "note 0" in folded and "note 149" not in folded
        assert all(f"note {n}" in transcript_text(app) for n in range(150))      # nothing was lost
        await type_and_send(app, pilot, "/clear")
        await settle(app, pilot, until=lambda: not app.query(HistoryBlock))


async def test_a_block_still_at_work_is_never_folded(tmp_path, fake_clara):
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        app.say("first")
        app.add(PendingTool("web_search", {"query": "x"}))
        for number in range(150):
            app.say(f"note {number}")
        await settle(app, pilot, until=lambda: "note 149" in transcript_text(app) and bool(app.query(HistoryBlock)))
        app.trim_transcript()
        await pilot.pause(0.3)
        children = list(app.query("#transcript > *"))
        assert isinstance(children[0], HistoryBlock) and text_of(children[0]).endswith("\nfirst")  # the welcome line and "first"
        assert "note 0" not in text_of(children[0]) and isinstance(children[1], PendingTool) and len(children) > 100


async def test_a_prompt_that_is_still_open_is_never_folded(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_write", {"path": "x.txt", "content": "1"})), WaitResults(), done("end"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "write")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        for number in range(150):
            app.say(f"note {number}")
        await settle(app, pilot, until=lambda: "note 149" in transcript_text(app) and bool(app.query(HistoryBlock)))
        app.trim_transcript()
        await pilot.pause(0.3)
        assert app.query_one(ApprovalBlock).is_attached and not app.query_one(ApprovalBlock).future.done()
        await pilot.press("r")
        await settle(app, pilot)


async def test_a_very_long_change_is_cut_and_the_answer_keys_still_show(tmp_path, fake_clara):
    """N4: a long command or diff hid the answer keys."""
    content = "\n".join(f"row {i}" for i in range(500))
    fake_clara.queue_turn(turn(), requests("t1", ("local_write", {"path": "big.txt", "content": content})), WaitResults(), done("end"))
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "write a lot")
        await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)))
        block = app.query_one(ApprovalBlock)
        assert block.prompt.preview.rstrip().endswith("more lines") and block.prompt.preview.count("\n") < 60
        children = list(block.query(Static))
        assert "[o]" in text_of(children[-1]) and "[r]" in text_of(children[-1])  # the keys are the last thing, always
        await pilot.press("r")
        await settle(app, pilot)


async def test_a_long_agent_md_is_warned_about_at_start(tmp_path, fake_clara):
    app, sh = make_app(tmp_path, fake_clara)
    sh.write("AGENT.md", "x" * 9_000)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot, until=lambda: "only the start is sent to Clara" in transcript_text(app))


async def test_a_clara_tool_that_never_ended_does_not_stay_on_the_screen(tmp_path, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "tool_start", "name": "web_search", "arguments": {"query": "x"}}, Hang())
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "search")
        await settle(app, pilot, until=lambda: bool(app.query(PendingTool)))
        await pilot.press("escape")
        await settle(app, pilot, until=lambda: not app.query(PendingTool) and "Stopped." in transcript_text(app))


async def test_an_unexpected_error_while_preparing_a_message_does_not_leave_the_app_busy(tmp_path, fake_clara, monkeypatch):
    app, _ = make_app(tmp_path, fake_clara)

    async def broken(text, pasted=()):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app.agent, "prepare", broken)
    async with app.run_test(size=SIZE) as pilot:
        await type_and_send(app, pilot, "hello")
        await settle(app, pilot)
        assert "Unexpected error: RuntimeError: disk on fire" in transcript_text(app) and not app.busy
        assert app.focused is app.prompt_box


async def test_a_blocked_front_is_looked_at_again_only_after_a_while_and_then_folds(tmp_path, fake_clara, monkeypatch):
    monkeypatch.setattr("custom_console.ui.agent_app.TRIM_RETRY_SECONDS", 0.3)
    app, _ = make_app(tmp_path, fake_clara)
    async with app.run_test(size=SIZE) as pilot:
        pending = PendingTool("web_search", {})
        app.add(pending)                                 # not finished: blocks the fold behind the welcome line
        for number in range(120):
            app.say(f"note {number}")
        await settle(app, pilot, until=lambda: "note 119" in transcript_text(app) and app._trim_retry_at > 0)
        first_wait = app._trim_retry_at                  # the welcome line was folded, then the front was found blocked
        for _ in range(5):
            app.trim_transcript()                        # too soon: it does not even look
        assert app._trim_retry_at == first_wait and len(app.query("#transcript > *")) > 100
        pending.remove()                                 # the tool ended
        await settle(app, pilot, until=lambda: len(app.query("#transcript > *")) <= MAX_BLOCKS)   # the next look folds
        assert app._trim_retry_at == 0.0


def test_the_agent_refuses_to_start_without_a_terminal(tmp_path, fake_clara):
    """Piped input (a script, a test) cannot drive a full-screen app: say so instead of hanging."""
    from custom_console.core.runtime import Runtime
    from custom_console.settings import load
    from custom_console.ui.agent_app import run_agent

    sh = Sh(tmp_path)
    settings = load({"CONSOLE_DATA_DIR": str(tmp_path / "data"), "CLARA_URL": fake_clara.url}, env={}, home=sh.home,
                    config_dir=tmp_path / "cfg")
    run_agent(Runtime(settings, sh.shell.platform, fake_clara.token), sh.shell, AgentOptions())
    assert sh.err == ["ai: the agent console needs a terminal, and the input or the output of this program is not one"]
