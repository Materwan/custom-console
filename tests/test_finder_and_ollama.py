from __future__ import annotations

import json

import pytest
import requests

from custom_console.apps import finder
from custom_console.apps.finder import SavedApps, clean_name, find_application
from custom_console.llm import ollama as ollama_module
from custom_console.llm.ollama import (
    ModelInfo,
    OllamaClient,
    OllamaUnavailableError,
    format_size,
    search_model,
)

# --------------------------------------------------------------------------- #
# Application finder
# --------------------------------------------------------------------------- #


def test_clean_name():
    assert clean_name("  Opera.EXE ") == "Opera"
    assert clean_name("code") == "code"


class TestSavedApps:
    def test_roundtrip_is_case_insensitive(self, tmp_path):
        exe = tmp_path / "app.exe"
        exe.write_text("x")
        saved = SavedApps(tmp_path / "saved.json")
        saved.remember("Opera.exe", str(exe))
        assert saved.get("OPERA") == str(exe)
        assert saved.names() == ["opera"]

    def test_stale_entries_are_pruned(self, tmp_path):
        path = tmp_path / "saved.json"
        path.write_text(json.dumps({"gone": str(tmp_path / "missing.exe")}))
        saved = SavedApps(path)
        assert saved.get("Gone") is None
        assert json.loads(path.read_text()) == {}

    def test_corrupt_or_missing_file(self, tmp_path):
        path = tmp_path / "saved.json"
        assert SavedApps(path).get("x") is None
        path.write_text("not json")
        assert SavedApps(path).names() == []
        path.write_text("[1, 2]")
        assert SavedApps(path).names() == []


class TestFindApplication:
    @pytest.fixture
    def calls(self, monkeypatch):
        """Replace the four strategies by recorders returning nothing."""
        calls = []

        def make(label):
            def strategy(name):
                calls.append(label)
                return None

            return strategy

        monkeypatch.setattr(
            finder,
            "STAGES",
            [(label, make(label)) for label in ("path", "registry", "install", "recursive")],
        )
        return calls

    def test_each_level_runs_one_more_stage(self, calls):
        assert find_application("x", level=2) is None
        assert calls == ["path", "registry"]

    def test_install_folders_stage_is_really_reached(self, calls):
        find_application("x", level=3)
        assert calls == ["path", "registry", "install"]  # regression: stage 3 used to repeat the registry

    def test_minus_one_means_all_stages(self, calls):
        find_application("x", level=-1)
        assert len(calls) == 4

    def test_invalid_level(self, calls):
        for level in (0, 5, -2):
            with pytest.raises(ValueError):
                find_application("x", level=level)

    def test_first_hit_stops_the_search_and_is_remembered(self, monkeypatch, tmp_path):
        visited = []
        monkeypatch.setattr(
            finder,
            "STAGES",
            [
                ("a", lambda n: visited.append("a")),
                ("b", lambda n: "C:/found.exe"),
                ("c", lambda n: visited.append("c")),
            ],
        )
        saved = SavedApps(tmp_path / "saved.json")
        stages = []
        assert find_application("Tool.exe", saved=saved, on_stage=stages.append) == "C:/found.exe"
        assert visited == ["a"] and stages == ["a", "b"]
        assert json.loads((tmp_path / "saved.json").read_text()) == {"tool": "C:/found.exe"}

    def test_saved_application_short_circuits_the_search(self, calls, tmp_path):
        exe = tmp_path / "x.exe"
        exe.write_text("x")
        saved = SavedApps(tmp_path / "saved.json")
        saved.remember("x", str(exe))
        assert find_application("x", saved=saved) == str(exe)
        assert calls == []


def test_search_path_finds_an_executable_on_the_path(tmp_path, monkeypatch):
    exe = tmp_path / "mytool.exe"
    exe.write_text("x")
    monkeypatch.setenv("PATH", str(tmp_path))
    import os
    assert os.path.normcase(finder.search_path("mytool")) == os.path.normcase(str(exe))
    assert finder.search_path("absent") is None


def test_search_install_folders_and_recursive(tmp_path, monkeypatch):
    (tmp_path / "Vendor" / "bin").mkdir(parents=True)
    (tmp_path / "Direct").mkdir()
    (tmp_path / "Direct" / "direct.exe").write_text("x")
    (tmp_path / "Vendor" / "bin" / "nested.exe").write_text("x")
    (tmp_path / "Vendor" / "a" / "b").mkdir(parents=True)
    (tmp_path / "Vendor" / "a" / "b" / "deep.exe").write_text("x")
    monkeypatch.setattr(finder, "_base_directories", lambda: [tmp_path])

    assert finder.search_install_folders("direct").endswith("direct.exe")
    assert finder.search_install_folders("nested").endswith("nested.exe")
    assert finder.search_install_folders("deep") is None  # only two levels
    assert finder.search_recursive("DEEP").endswith("deep.exe")


# --------------------------------------------------------------------------- #
# Ollama client
# --------------------------------------------------------------------------- #

MODELS = [ModelInfo("llama3:8b"), ModelInfo("llama3:70b"), ModelInfo("phi3:latest")]


class TestSearchModel:
    def test_exact_match_when_a_tag_is_given(self):
        assert search_model("llama3:70b", MODELS) == "llama3:70b"
        assert search_model("llama3:1b", MODELS) is None

    def test_bare_name_matches_first_model_of_that_family(self):
        assert search_model("llama3", MODELS) == "llama3:8b"
        assert search_model("phi3", MODELS) == "phi3:latest"
        assert search_model("phi", MODELS) is None

    def test_no_models(self):
        assert search_model("x", []) is None


def test_format_size():
    assert format_size(None) == "?"
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024**3) == "5.0 GB"


class FakeResponse:
    def __init__(self, payload, error=None):
        self.payload, self.error = payload, error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class TestOllamaClient:
    @pytest.fixture
    def server(self, monkeypatch):
        routes = {
            "/api/tags": {
                "models": [
                    {"model": "llama3:8b", "size": 100, "capabilities": ["completion", "tools"]},
                    {"name": "phi3:latest", "size": 50},
                ]
            },
            "/api/ps": {"models": [{"name": "phi3:latest"}]},
        }
        requested = []

        def fake_get(url, timeout):
            requested.append(url)
            return FakeResponse(routes[url[url.index("/api"):]])

        monkeypatch.setattr(ollama_module.requests, "get", fake_get)
        return requested

    def test_installed_running_and_find(self, server):
        client = OllamaClient("http://host:1/")
        installed = client.installed()
        assert [m.name for m in installed] == ["llama3:8b", "phi3:latest"]
        assert installed[0].capabilities == ["completion", "tools"] and installed[1].capabilities == []
        assert [m.name for m in client.running()] == ["phi3:latest"]
        assert client.find("llama3") == "llama3:8b"
        assert client.is_running("phi3") and not client.is_running("llama3")
        assert server[0] == "http://host:1/api/tags"

    @pytest.mark.parametrize(
        "error", [requests.ConnectionError("refused"), requests.Timeout("slow")]
    )
    def test_unreachable_server_raises_a_dedicated_error(self, monkeypatch, error):
        def boom(url, timeout):
            raise error

        monkeypatch.setattr(ollama_module.requests, "get", boom)
        with pytest.raises(OllamaUnavailableError, match="cannot reach Ollama"):
            OllamaClient().installed()

    def test_invalid_json(self, monkeypatch):
        class BadJson(FakeResponse):
            def json(self):
                raise ValueError("no json")

        monkeypatch.setattr(ollama_module.requests, "get", lambda url, timeout: BadJson({}))
        with pytest.raises(OllamaUnavailableError):
            OllamaClient().running()
