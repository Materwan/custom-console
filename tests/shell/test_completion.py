from __future__ import annotations

import pytest
from prompt_toolkit.document import Document

from custom_console.apps.finder import SavedApps
from custom_console.fs import FileManager
from custom_console.shell.commands import build_registry
from custom_console.shell.completion import ArgumentSpec, ShellCompleter


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "report.txt").write_text("x")
    (tmp_path / "My Folder").mkdir()
    (tmp_path / "My Folder" / "inner.txt").write_text("x")
    (tmp_path / "subdir").mkdir()
    return tmp_path


@pytest.fixture
def completer(tree, tmp_path):
    saved = SavedApps(tmp_path / "saved.json")
    saved.remember("Opera", str(tree / "report.txt"))
    return ShellCompleter(
        build_registry(),
        FileManager(start_dir=str(tree)),
        saved,
        model_names=lambda: ["gemma4:31b-cloud", "phi3:latest"],
        executables=lambda: {"python", "git"},
    )


def complete(completer, text):
    """(inserted text, start_position) pairs for `text` typed at the prompt."""
    return [(c.text, c.start_position) for c in completer.get_completions(Document(text), None)]


def texts(completer, text):
    return [t for t, _ in complete(completer, text)]


class TestCommandNames:
    def test_builtin_commands_and_path_executables(self, completer):
        assert "cd" in texts(completer, "c") and "clear" in texts(completer, "c")
        assert "git" in texts(completer, "gi")

    def test_builtins_are_not_duplicated_by_executables(self, tree, tmp_path):
        c = ShellCompleter(
            build_registry(), FileManager(start_dir=str(tree)), executables=lambda: {"cd", "git"}
        )
        assert texts(c, "cd") == ["cd"]

    def test_replaces_the_typed_prefix(self, completer):
        assert ("cat", -2) in complete(completer, "ca")


class TestPaths:
    def test_files_and_directories(self, completer):
        assert "report.txt" in texts(completer, "cat rep")
        assert "subdir/" in texts(completer, "cd sub")

    def test_names_with_spaces_are_quoted(self, completer):
        assert "'My Folder/'" in texts(completer, "cd My")

    def test_inside_an_unterminated_quote(self, completer):
        results = complete(completer, "cd 'My F")
        assert ("'My Folder/'", -len("'My F")) in results

    def test_nested_path(self, completer):
        assert "'My Folder/inner.txt'" in texts(completer, "cat 'My Folder/in")

    def test_positional_count_is_respected(self, completer):
        assert texts(completer, "cd subdir ") == []  # `cd` takes a single path
        assert texts(completer, "cat report.txt ")  # `cat` takes many

    def test_virtual_root(self, completer):
        assert "/reMarkable/" in texts(completer, "cd /")


class TestFlags:
    def test_flags_are_suggested_after_a_dash(self, completer):
        assert {"-a", "--all"} <= set(texts(completer, "ls -"))
        assert "--depth" in texts(completer, "tree --d")

    def test_help_flag_is_not_suggested(self, completer):
        assert "-h" not in texts(completer, "ls -")

    def test_value_of_a_flag_is_not_a_path(self, completer):
        assert texts(completer, "find -d ") == []

    def test_paths_after_a_boolean_flag(self, completer):
        assert "report.txt" in texts(completer, "cp -r rep")


class TestSubcommandsAndValues:
    def test_ai_subcommands(self, completer):
        assert set(texts(completer, "ai ")) == {"list", "start", "agent"}
        assert texts(completer, "ai li") == ["list"]

    def test_subcommand_flags(self, completer):
        assert {"-r", "--running", "-s", "--size"} <= set(texts(completer, "ai list -"))

    def test_model_names(self, completer):
        assert texts(completer, "ai start ge") == ["gemma4:31b-cloud"]
        assert texts(completer, "ai start ph") == ["phi3:latest"]

    def test_model_provider_failure_is_silent(self, tree):
        def broken():
            raise RuntimeError("ollama down")

        c = ShellCompleter(build_registry(), FileManager(start_dir=str(tree)), model_names=broken)
        assert texts(c, "ai start ") == []

    def test_saved_applications(self, completer):
        assert texts(completer, "launch op") == ["opera"]

    def test_unknown_command_and_bad_quote(self, completer):
        assert texts(completer, "nonsense ") == []
        assert texts(completer, 'cat "abc" "def') == []  # quote still open: nothing matches, nothing raises
        assert texts(completer, 'echo "abc') == []  # tokenizer failure path


class TestArgumentSpec:
    def test_reads_the_parser(self):
        spec = ArgumentSpec(build_registry().get("tree").parser)
        assert spec.flags["-d"].nargs is None and ArgumentSpec.takes_value(spec.flags["-d"])
        assert not ArgumentSpec.takes_value(spec.flags["-a"])
        assert spec.positional_at(0) is spec.positional_at(5)  # nargs="*"

    def test_single_positionals(self):
        spec = ArgumentSpec(build_registry().get("cp").parser)
        assert spec.positional_at(0).dest == "src"
        assert spec.positional_at(1).dest == "dst"
        assert spec.positional_at(2) is None
