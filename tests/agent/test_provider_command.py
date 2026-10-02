"""/provider in the agent console: the table, keys asked once and masked, the model
menu, carrying the conversation over, /model with a remote provider, /restore."""

from __future__ import annotations

import json
from types import SimpleNamespace

from test_console import Session, wait_for

from custom_console.llm.keys import SERVICE

DOWN, ENTER, ESCAPE = "\x1b[B", "\r", "\x1b"


def key_question(s):
    return s.console_.screen._text


def model_menu(s):
    return s.console_.screen._choice


def saved_key(s, variable="OPENAI_API_KEY"):
    return s.created["keyring"].saved.get((SERVICE, variable))


class TestProviderCommand:
    def test_the_table_shows_the_providers_their_keys_and_the_current_one(self, tmp_path):
        session = Session(tmp_path, OLLAMA_API_KEY="ol-good")

        def driver(s):
            s.send("/provider")
            s.wait_output("LLM providers")

        out = session.run(driver)
        rows = [[cell.strip() for cell in line.split("│")] for line in out.splitlines() if "│" in line]
        lines = {row[2]: " ".join(row) for row in rows if len(row) > 2 and row[2] in ("ollama", "ollama-cloud", "chatgpt")}
        assert "●" in lines["ollama"] and "not needed" in lines["ollama"] and "gemma4:test" in lines["ollama"]
        assert "from OLLAMA_API_KEY" in lines["ollama-cloud"]
        assert "missing" in lines["chatgpt"]

    def test_switching_to_chatgpt_asks_the_key_masked_then_the_model(self, tmp_path):
        session = Session(tmp_path)
        seen = {}

        def driver(s):
            s.send("/provider chatgpt")
            wait_for(lambda: key_question(s) is not None)
            seen["question"] = key_question(s).info
            seen["prompt"] = "".join(text for _, text in s.console_.screen._prompt_fragments())
            assert key_question(s).secret
            s.send("sk-good")
            wait_for(lambda: model_menu(s) is not None)
            seen["options"] = [option.label for option in model_menu(s).options]
            seen["cursor"] = model_menu(s).cursor
            s.pipe.send_text(DOWN + ENTER)  # gpt-5
            s.wait_output("Provider: ChatGPT")

        out = session.run(driver)
        app = session.console_
        assert "platform.openai.com" in seen["question"] and "key >" in seen["prompt"]
        assert seen["options"] == ["gpt-5-mini", "gpt-5", "gpt-4.1"] and seen["cursor"] == 0  # the default model
        assert "Key saved" in out and saved_key(session) == "sk-good"
        assert (app.provider.name, app.model, app.api_key) == ("chatgpt", "gpt-5", "sk-good")
        assert session.created["switched_to"] == ("gpt-5", None)  # no window to request from a remote model
        assert session.created["switched_provider"] == ("chatgpt", "sk-good")
        assert app.context.window == 128_000 and "(chatgpt)" in app.screen.title
        memory = json.loads(app.settings.agent_provider_path.read_text(encoding="utf-8"))
        assert memory == {"provider": "chatgpt", "models": {"ollama": "gemma4:test", "chatgpt": "gpt-5"}}
        assert "sk-good" not in app.settings.agent_history_path.read_text(encoding="utf-8")  # never in the history

    def test_a_refused_key_changes_nothing_and_is_not_saved(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/provider chatgpt")
            wait_for(lambda: key_question(s) is not None)
            s.send("sk-bad")
            s.wait_output("the key was not saved")

        session.run(driver)
        assert session.console_.provider.name == "ollama" and saved_key(session) is None

    def test_escape_on_the_key_or_the_menu_changes_nothing(self, tmp_path):
        session = Session(tmp_path, OPENAI_API_KEY="sk-good")

        def driver(s):
            s.send("/provider chatgpt")  # the key comes from the environment: not asked
            wait_for(lambda: model_menu(s) is not None)
            assert key_question(s) is None
            s.pipe.send_text(ESCAPE)
            s.wait_output("Provider unchanged.")
            s.send("/provider ollama-cloud")
            wait_for(lambda: key_question(s) is not None)
            s.pipe.send_text(ESCAPE)
            s.wait_output("No key given")

        session.run(driver)
        assert session.console_.provider.name == "ollama" and session.created["keyring"].saved == {}

    def test_the_menu_starts_on_the_model_used_last_with_the_provider(self, tmp_path):
        session = Session(tmp_path, OPENAI_API_KEY="sk-good")
        session.console_.provider_memory.remember("chatgpt", "gpt-4.1")
        seen = {}

        def driver(s):
            s.send("/provider openai")
            wait_for(lambda: model_menu(s) is not None)
            seen["cursor"] = model_menu(s).cursor
            s.pipe.send_text(ENTER)
            s.wait_output("Provider: ChatGPT")

        session.run(driver)
        assert seen["cursor"] == 2 and session.console_.model == "gpt-4.1"

    def test_forget_deletes_a_saved_key(self, tmp_path):
        session = Session(tmp_path)
        session.created["keyring"].saved[(SERVICE, "OPENAI_API_KEY")] = "sk-old"

        def driver(s):
            s.send("/provider forget chatgpt")
            s.wait_output("Saved key of ChatGPT (OpenAI API) deleted.")
            s.send("/provider forget chatgpt")
            s.wait_output("No saved key")
            s.send("/provider forget ollama")
            s.wait_output("needs no key")
            s.send("/provider mistral")
            s.wait_output("unknown provider")

        session.run(driver)
        assert session.created["keyring"].saved == {}

    def test_completion(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        session = Session(tmp_path)
        completer = SlashCompleter(session.console_.screen.commands)

        def complete(text):
            return [c.text for c in completer.get_completions(Document(text), None)]

        names, keyed = complete("/provider "), complete("/provider forget ")
        session.pipe_ctx.__exit__(None, None, None)
        assert names == ["ollama", "ollama-cloud", "chatgpt", "forget"] and keyed == ["ollama-cloud", "chatgpt"]


class TestWithARemoteProvider:
    def test_starting_on_chatgpt_with_a_saved_key(self, tmp_path):
        session = Session(tmp_path, provider="chatgpt", model="gpt-5-mini", OPENAI_API_KEY="sk-good")

        def driver(s):
            s.send("/model")
            s.wait_output("Models of ChatGPT")
            s.send("/model gpt-4.1")
            s.wait_output("Model: gpt-4.1 (ChatGPT (OpenAI API))")
            s.send("/model gemma4:test")
            s.wait_output("not available from ChatGPT")

        out = session.run(driver)
        assert (session.created["provider"], session.created["api_key"]) == ("chatgpt", "sk-good")
        assert "OpenAI" in out and "ChatGPT (OpenAI API)" in out  # banner and table
        assert session.console_.context.window == 128_000 and session.console_.num_ctx is None

    def test_tool_calls_made_with_ollama_are_summarised_before_going_to_chatgpt(self, tmp_path):
        session = Session(tmp_path, OPENAI_API_KEY="sk-good")
        agent = session.created["agent"]
        agent.get_chat_history = lambda session_id=None, last_n_runs=None: [
            SimpleNamespace(role="user", content="list the files", tool_calls=None),
            SimpleNamespace(role="assistant", content="", tool_calls=[{"function": {"name": "file_system_list"}}]),
            SimpleNamespace(role="tool", content="[...]", tool_calls=None, tool_name="file_system_list"),
        ]
        summaries = []
        session.console_.session.summarize = lambda transcript, previous, focus: summaries.append(transcript) or "Files listed."

        def driver(s):
            s.send("/provider chatgpt")
            wait_for(lambda: model_menu(s) is not None)
            s.pipe.send_text(ENTER)
            s.wait_output("Provider: ChatGPT")

        out = session.run(driver)
        assert len(summaries) == 1 and "list the files" in summaries[0]
        assert "summarised to carry it over" in out and session.console_.context.summary == "Files listed."

    def test_a_plain_conversation_is_carried_over_as_is(self, tmp_path):
        session = Session(tmp_path, OPENAI_API_KEY="sk-good")
        summaries = []
        session.console_.session.summarize = lambda *args: summaries.append(args) or "x"

        def driver(s):
            s.send("/provider chatgpt")
            wait_for(lambda: model_menu(s) is not None)
            s.pipe.send_text(ENTER)
            s.wait_output("Provider: ChatGPT")

        session.run(driver)
        assert summaries == []  # the fake history has no tool call

    def test_the_summary_uses_the_current_provider(self, tmp_path):
        session = Session(tmp_path, provider="chatgpt", model="gpt-5", OPENAI_API_KEY="sk-good")
        session.pipe_ctx.__exit__(None, None, None)
        app = session.console_
        calls = []
        app.provider = SimpleNamespace(chat=lambda settings, name, key, messages, num_ctx: calls.append((name, key)) or "ok")
        assert app._chat([{"role": "user", "content": "x"}]) == "ok" and calls == [("gpt-5", "sk-good")]


class TestRestoreAcrossProviders:
    def first_session_on_chatgpt(self, tmp_path):
        first = Session(tmp_path, memory=True, provider="chatgpt", model="gpt-5", OPENAI_API_KEY="sk-good")

        def driver(s):
            s.send("remember me")
            s.wait_output("Done: remember me")

        first.run(driver)
        return first

    def test_restoring_goes_back_to_the_provider_of_the_session(self, tmp_path):
        self.first_session_on_chatgpt(tmp_path)
        second = Session(tmp_path, memory=True, OPENAI_API_KEY="sk-good")

        def driver(s):
            s.send("/restore")
            s.wait_output("Restored:")

        out = second.run(driver)
        assert (second.console_.provider.name, second.console_.model) == ("chatgpt", "gpt-5")
        assert "model gpt-5 (chatgpt)" in out

    def test_without_its_key_the_current_provider_is_kept(self, tmp_path):
        self.first_session_on_chatgpt(tmp_path)
        second = Session(tmp_path, memory=True)  # no OPENAI_API_KEY this time

        def driver(s):
            s.send("/restore")
            s.wait_output("Restored:")

        out = second.run(driver)
        assert second.console_.provider.name == "ollama" and "no API key for it" in out
