"""Model providers: names, API keys, the remembered choice, connecting, the OpenAI
model list, the agno models they build, and the shell's `ai --provider`."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from custom_console.agent.context import summarize_with
from custom_console.agent.sessions import SessionRecord
from custom_console.fs import FileManager
from custom_console.llm import providers as providers_module
from custom_console.llm.errors import ProviderUnavailableError
from custom_console.llm.keys import FROM_ENVIRONMENT, SAVED, SERVICE, KeyStore
from custom_console.llm.ollama import ModelInfo
from custom_console.llm.providers import (
    PROVIDERS,
    OpenAICatalog,
    ProviderMemory,
    connect,
    describe_model,
    find_model,
    get_provider,
    initial_provider,
    is_chat_model,
    openai_context_window,
    preferred_model,
)
from custom_console.settings import load_settings
from custom_console.shell.commands import CommandError, ShellContext, build_registry
from custom_console.shell.commands import ai as ai_module
from custom_console.shell.printer import Printer


class FakeKeyring:
    def __init__(self, broken=False):
        self.saved, self.broken = {}, broken

    def get_password(self, service, name):
        if self.broken:
            raise RuntimeError("no credential store")
        return self.saved.get((service, name))

    def set_password(self, service, name, value):
        if self.broken:
            raise RuntimeError("no credential store")
        self.saved[(service, name)] = value

    def delete_password(self, service, name):
        del self.saved[(service, name)]


class FakeCatalog:
    def __init__(self, names, key=None, valid_key=None):
        self.names, self.key, self.valid_key = names, key, valid_key
        self.listed = 0

    def installed(self):
        self.listed += 1
        if self.valid_key is not None and self.key != self.valid_key:
            raise ProviderUnavailableError("refused the API key (HTTP 401)")
        return [ModelInfo(name, context_length=128_000, remote=True) for name in self.names]

    def info(self, name):
        return next((model for model in self.installed() if model.name == name), None)


@pytest.fixture
def settings(tmp_path):
    return load_settings({}, root=tmp_path, use_dotenv=False)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


class TestNames:
    @pytest.mark.parametrize(
        "name, expected",
        [("ollama", "ollama"), ("local", "ollama"), ("OLLAMA-CLOUD", "ollama-cloud"), ("cloud", "ollama-cloud"),
         ("chatgpt", "chatgpt"), ("openai", "chatgpt"), (" gpt ", "chatgpt")],
    )
    def test_names_and_aliases(self, name, expected):
        assert get_provider(name).name == expected

    def test_unknown_provider(self):
        with pytest.raises(LookupError, match="choose ollama, ollama-cloud, chatgpt"):
            get_provider("mistral")

    def test_which_need_a_key(self):
        assert [p.name for p in PROVIDERS.values() if p.needs_key] == ["ollama-cloud", "chatgpt"]
        assert get_provider("chatgpt").key_variable == "OPENAI_API_KEY"
        assert get_provider("ollama-cloud").key_variable == "OLLAMA_API_KEY"


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


class TestKeyStore:
    def test_the_environment_wins_over_a_saved_key(self):
        backend = FakeKeyring()
        backend.saved[(SERVICE, "OPENAI_API_KEY")] = "saved-key"
        keys = KeyStore(env={"OPENAI_API_KEY": " env-key "}, backend=backend)
        assert keys.get("OPENAI_API_KEY") == "env-key" and keys.source("OPENAI_API_KEY") == FROM_ENVIRONMENT
        assert KeyStore(env={"OPENAI_API_KEY": ""}, backend=backend).get("OPENAI_API_KEY") == "saved-key"

    def test_save_source_and_forget(self):
        keys = KeyStore(env={}, backend=FakeKeyring())
        assert keys.get("X") is None and keys.source("X") == ""
        keys.save("X", " k1 ")
        assert keys.get("X") == "k1" and keys.source("X") == SAVED
        assert keys.forget("X") and keys.get("X") is None
        assert not keys.forget("X")

    def test_a_broken_credential_store_means_no_saved_key(self):
        keys = KeyStore(env={}, backend=FakeKeyring(broken=True))
        assert keys.get("X") is None and keys.source("X") == ""
        with pytest.raises(RuntimeError):
            keys.save("X", "k")


# --------------------------------------------------------------------------- #
# The remembered choice
# --------------------------------------------------------------------------- #


class TestMemory:
    def test_roundtrip(self, tmp_path):
        memory = ProviderMemory(tmp_path / "provider.json")
        memory.remember("chatgpt", "gpt-5")
        memory.remember("ollama", "gemma4:e2b")
        again = ProviderMemory(tmp_path / "provider.json")
        assert again.provider == "ollama" and again.last_model("chatgpt") == "gpt-5"

    def test_damaged_or_unknown_is_ignored(self, tmp_path):
        path = tmp_path / "provider.json"
        path.write_text("{broken")
        assert ProviderMemory(path).provider is None
        path.write_text(json.dumps({"provider": "mistral", "models": {"x": "y"}}))
        assert ProviderMemory(path).provider is None and ProviderMemory(path).last_model("x") == "y"

    def test_initial_provider_and_preferred_model(self, tmp_path):
        env_settings = load_settings({"AGENT_PROVIDER": "chatgpt"}, root=tmp_path, use_dotenv=False)
        memory = ProviderMemory(tmp_path / "p.json")
        assert initial_provider(env_settings, memory).name == "chatgpt"  # nothing remembered: AGENT_PROVIDER
        assert preferred_model(get_provider("chatgpt"), env_settings, memory) == "gpt-5-mini"
        memory.remember("ollama-cloud", "glm-5.3")
        assert initial_provider(env_settings, memory).name == "ollama-cloud"
        assert preferred_model(get_provider("ollama-cloud"), env_settings, memory) == "glm-5.3"
        assert preferred_model(get_provider("ollama"), env_settings, memory) == env_settings.default_model

    def test_a_bad_agent_provider_falls_back_to_ollama(self, tmp_path):
        bad = load_settings({"AGENT_PROVIDER": "nope"}, root=tmp_path, use_dotenv=False)
        assert initial_provider(bad, ProviderMemory(None)).name == "ollama"


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #


class TestConnect:
    def setup(self, env=None, answer="", valid="sk-good"):
        keys = KeyStore(env=env or {}, backend=FakeKeyring())
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer

        catalogs = []

        def make_catalog(provider, key):
            catalogs.append(FakeCatalog(["gpt-5"], key, valid))
            return catalogs[-1]

        return keys, asked, ask, make_catalog

    def test_no_key_needed(self):
        keys, asked, ask, _ = self.setup()
        connection = connect(get_provider("ollama"), keys, ask, lambda p, key: FakeCatalog(["llama3:8b"]))
        assert connection.key is None and [m.name for m in connection.models] == ["llama3:8b"] and asked == []

    def test_a_key_from_the_environment_is_not_asked_nor_saved(self):
        keys, asked, ask, make = self.setup(env={"OPENAI_API_KEY": "sk-good"})
        connection = connect(get_provider("chatgpt"), keys, ask, make)
        assert connection.key == "sk-good" and asked == [] and connection.notes == []
        assert keys.source("OPENAI_API_KEY") == FROM_ENVIRONMENT

    def test_a_typed_key_is_saved_once_it_worked(self):
        keys, asked, ask, make = self.setup(answer=" sk-good ")
        connection = connect(get_provider("chatgpt"), keys, ask, make)
        assert "platform.openai.com" in asked[0] and "Credential Manager" in asked[0]
        assert connection.key == "sk-good" and keys.source("OPENAI_API_KEY") == SAVED
        assert "Key saved" in connection.notes[0]

    def test_a_refused_key_is_not_saved(self):
        keys, _, ask, make = self.setup(answer="sk-bad")
        with pytest.raises(ProviderUnavailableError, match="the key was not saved"):
            connect(get_provider("chatgpt"), keys, ask, make)
        assert keys.get("OPENAI_API_KEY") is None

    def test_no_key_given(self):
        keys, _, ask, make = self.setup(answer="  ")
        assert connect(get_provider("ollama-cloud"), keys, ask, make) is None

    def test_a_key_that_cannot_be_saved_still_works_for_this_run(self):
        keys = KeyStore(env={}, backend=FakeKeyring(broken=True))
        connection = connect(get_provider("chatgpt"), keys, lambda p: "sk-good", lambda p, k: FakeCatalog(["gpt-5"], k, "sk-good"))
        assert connection.key == "sk-good" and "could not be saved" in connection.notes[0]

    def test_a_provider_without_models(self):
        with pytest.raises(ProviderUnavailableError, match="offers no model"):
            connect(get_provider("ollama"), KeyStore(env={}), lambda p: "", lambda p, k: FakeCatalog([]))

    def test_find_and_describe(self):
        models = [ModelInfo("llama3:8b", size=2_000_000_000, context_length=8192), ModelInfo("gpt-5", context_length=400_000, remote=True)]
        assert find_model(models, "llama3").name == "llama3:8b" and find_model(models, "nope") is None
        assert describe_model(models[0]) == "1.9 GB · 8k context" and describe_model(models[1]) == "400k context"


# --------------------------------------------------------------------------- #
# The OpenAI model list
# --------------------------------------------------------------------------- #


class FakeHTTP:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


OPENAI_MODELS = {
    "data": [
        {"id": "gpt-4o", "created": 1},
        {"id": "gpt-5-mini", "created": 5},
        {"id": "gpt-5", "created": 4},
        {"id": "gpt-4o-2024-08-06", "created": 2},
        {"id": "gpt-4o-realtime-preview", "created": 3},
        {"id": "gpt-5-codex", "created": 6},
        {"id": "text-embedding-3-small", "created": 3},
        {"id": "o3-pro", "created": 3},
        {"id": "o4-mini", "created": 3},
        {"id": "dall-e-3", "created": 3},
    ]
}


class TestOpenAICatalog:
    @pytest.fixture
    def http(self, monkeypatch):
        calls = []

        def fake_get(url, headers, timeout):
            calls.append((url, headers))
            return FakeHTTP(calls_payload[0], calls_status[0])

        calls_payload, calls_status = [OPENAI_MODELS], [200]
        monkeypatch.setattr(providers_module.requests, "get", fake_get)
        return SimpleNamespace(calls=calls, payload=calls_payload, status=calls_status)

    def test_chat_models_newest_first_without_snapshots(self, http):
        catalog = OpenAICatalog("https://api.openai.com/v1/", "sk-1")
        names = [m.name for m in catalog.installed()]
        assert names == ["gpt-5-mini", "gpt-5", "o4-mini", "gpt-4o"]
        assert http.calls[0] == ("https://api.openai.com/v1/models", {"Authorization": "Bearer sk-1"})
        assert all(m.remote for m in catalog.installed())

    def test_a_snapshot_is_still_accepted_by_name(self, http):
        catalog = OpenAICatalog("https://api.openai.com/v1", "sk-1")
        assert catalog.info("gpt-4o-2024-08-06").context_length == 128_000
        assert catalog.info("gpt-9") is None

    def test_a_refused_key(self, http):
        http.status[0] = 401
        with pytest.raises(ProviderUnavailableError, match="refused the API key"):
            OpenAICatalog("https://api.openai.com/v1", "bad").installed()

    def test_an_unreachable_server(self, monkeypatch):
        import requests

        def boom(url, headers, timeout):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(providers_module.requests, "get", boom)
        with pytest.raises(ProviderUnavailableError, match="cannot reach"):
            OpenAICatalog("http://localhost:1/v1", "k").installed()

    def test_a_compatible_server_with_other_names_lists_them_all(self, http):
        http.payload[0] = {"data": [{"id": "mistral-large"}, {"id": "llama-3.3-70b"}]}
        assert [m.name for m in OpenAICatalog("http://x/v1", "k").installed()] == ["mistral-large", "llama-3.3-70b"]

    @pytest.mark.parametrize(
        "name, window",
        [("gpt-5", 400_000), ("gpt-5.1-mini", 400_000), ("gpt-4.1-nano", 1_047_576), ("gpt-4o-mini", 128_000),
         ("gpt-4", 8_192), ("o1-mini", 128_000), ("o3", 200_000), ("something-new", 128_000)],
    )
    def test_context_windows(self, name, window):
        assert openai_context_window(name) == window

    @pytest.mark.parametrize("name", ["gpt-5", "chatgpt-4o-latest", "o3-mini", "o4-mini"])
    def test_chat_models(self, name):
        assert is_chat_model(name)

    @pytest.mark.parametrize("name", ["whisper-1", "gpt-image-1", "gpt-4o-mini-tts", "gpt-5-codex", "o1-pro", "omni-moderation"])
    def test_not_chat_models(self, name):
        assert not is_chat_model(name)


# --------------------------------------------------------------------------- #
# The agno models
# --------------------------------------------------------------------------- #


class TestModels:
    def test_the_local_ollama_never_goes_to_ollama_com(self, settings, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "should-not-be-used")
        model = get_provider("ollama").model(settings, "gemma4:e2b", None, 8192)
        assert model.id == "gemma4:e2b" and model.host == settings.ollama_host and model.api_key is None
        assert model.options == {"num_ctx": 8192}
        assert "authorization" not in str(model._get_client_params()).lower()

    def test_ollama_cloud_sends_the_key_to_ollama_com(self, settings):
        model = get_provider("ollama-cloud").model(settings, "gpt-oss:120b", "ol-key", 8192)
        params = model._get_client_params()
        assert params["host"] == "https://ollama.com" and params["headers"] == {"authorization": "Bearer ol-key"}
        assert model.options is None  # the window is not ours to set there

    def test_chatgpt_is_an_openai_chat_model(self, settings):
        from agno.models.openai import OpenAIChat

        model = get_provider("chatgpt").model(settings, "gpt-5-mini", "sk-key")
        assert isinstance(model, OpenAIChat) and model.id == "gpt-5-mini" and model.api_key == "sk-key"
        assert model.base_url == "https://api.openai.com/v1"

    def test_the_factory_routes_to_the_provider(self, settings):
        from custom_console.agent.factory import build_model

        assert type(build_model(settings, "gpt-5", None, "chatgpt", "k")).__name__ == "OpenAIChat"
        assert type(build_model(settings, "llama3", 4096)).__name__ == "Ollama"

    def test_the_summary_goes_through_the_given_chat(self):
        seen = []

        def chat(messages):
            seen.append(messages)
            return "  the summary  "

        assert summarize_with(chat, "User: hi", "before", "files") == "the summary"
        assert seen[0][0]["role"] == "system" and "before" in seen[0][1]["content"] and "files" in seen[0][1]["content"]
        with pytest.raises(RuntimeError, match="empty summary"):
            summarize_with(lambda messages: "", "User: hi")


def test_a_saved_session_keeps_its_provider():
    record = SessionRecord("1", "/d", model="gpt-5", provider="chatgpt")
    assert SessionRecord.from_dict(json.loads(json.dumps(record.to_dict()))).provider == "chatgpt"
    assert SessionRecord.from_dict({"id": "2"}).provider == "ollama"  # sessions of older versions


def test_settings_of_the_providers(tmp_path):
    s = load_settings({}, root=tmp_path, use_dotenv=False)
    assert (s.agent_provider, s.ollama_cloud_host, s.openai_base_url) == ("ollama", "https://ollama.com", "https://api.openai.com/v1")
    assert s.agent_provider_path == s.agent_dir / "provider.json"
    custom = load_settings({"AGENT_PROVIDER": "ChatGPT", "OPENAI_BASE_URL": "http://x/v1/"}, root=tmp_path, use_dotenv=False)
    assert custom.agent_provider == "chatgpt" and custom.openai_base_url == "http://x/v1"


# --------------------------------------------------------------------------- #
# The shell: ai list / ai agent --provider
# --------------------------------------------------------------------------- #


class ShellHarness:
    def __init__(self, tmp_path, monkeypatch, answer="sk-good", env=None):
        self.buffer = io.StringIO()
        self.settings = load_settings(env or {}, root=tmp_path, use_dotenv=False)
        self.asked = []
        self.keys = KeyStore(env=env or {}, backend=FakeKeyring())
        self.consoles = []

        def ask_secret(prompt):
            self.asked.append(prompt)
            return answer

        self.ctx = ShellContext(
            settings=self.settings,
            printer=Printer(Console(file=self.buffer, force_terminal=False, width=200)),
            files=FileManager(start_dir=str(tmp_path)),
            registry=build_registry(),
            ollama=SimpleNamespace(installed=lambda: [ModelInfo("gemma4:e2b")], find=lambda name: "gemma4:e2b", is_running=lambda name: True),
            saved_apps=None,
            confirm=lambda question: True,
            keys=self.keys,
            ask_secret=ask_secret,
        )
        monkeypatch.setattr(PROVIDERS["chatgpt"], "catalog", lambda settings, key: FakeCatalog(["gpt-5-mini", "gpt-5"], key, "sk-good"), raising=False)
        harness = self

        class FakeAgentConsole:
            def __init__(self, **kwargs):
                harness.consoles.append(kwargs)
                self.provider, self.model = get_provider(kwargs["provider"]), kwargs["model"]

            def run(self):
                pass

        import custom_console.agent.console as console_module

        monkeypatch.setattr(console_module, "AgentConsole", FakeAgentConsole)

    def run(self, line):
        command, *rest = line.split()
        parsed = self.ctx.registry.get(command).parser.parse_args(rest)
        self.ctx.registry.get(command).handler(self.ctx, parsed)
        return self.buffer.getvalue()


class TestShell:
    def test_ai_list_of_chatgpt_asks_the_key_once(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch)
        out = h.run("ai list -P chatgpt")
        assert "Models of ChatGPT" in out and "gpt-5-mini" in out and "128,000" in out
        assert len(h.asked) == 1 and h.keys.source("OPENAI_API_KEY") == SAVED
        h.run("ai list --provider openai")
        assert len(h.asked) == 1  # saved: not asked again

    def test_a_wrong_key_is_an_error_and_not_saved(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch, answer="sk-bad")
        with pytest.raises(CommandError, match="refused the API key"):
            h.run("ai list -P chatgpt")
        assert h.keys.get("OPENAI_API_KEY") is None

    def test_running_is_for_the_local_ollama(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch)
        with pytest.raises(CommandError, match="--running"):
            h.run("ai list -P chatgpt -r")

    def test_ai_agent_with_chatgpt(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch)
        h.run("ai agent -P chatgpt")
        options = h.consoles[0]
        assert (options["provider"], options["model"], options["api_key"]) == ("chatgpt", "gpt-5-mini", "sk-good")
        with pytest.raises(CommandError, match="not available from ChatGPT"):
            h.run("ai agent -P chatgpt -m gpt-9")

    def test_ai_agent_uses_the_remembered_provider_and_model(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch)
        ProviderMemory(h.settings.agent_provider_path).remember("chatgpt", "gpt-5")
        h.run("ai agent")
        assert (h.consoles[0]["provider"], h.consoles[0]["model"]) == ("chatgpt", "gpt-5")

    def test_ai_agent_local_by_default(self, tmp_path, monkeypatch):
        h = ShellHarness(tmp_path, monkeypatch)
        h.run("ai agent")
        assert (h.consoles[0]["provider"], h.consoles[0]["model"], h.consoles[0]["api_key"]) == ("ollama", "gemma4:e2b", None)
        assert h.asked == []

    def test_an_unknown_provider(self, tmp_path, monkeypatch):
        with pytest.raises(CommandError, match="unknown provider"):
            ShellHarness(tmp_path, monkeypatch).run("ai agent -P mistral")
