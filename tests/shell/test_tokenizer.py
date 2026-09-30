import pytest

from custom_console.shell.tokenizer import (
    TokenizeError,
    current_word,
    quote_if_needed,
    split_command,
    unquote_word,
)


class TestSplitCommand:
    def test_windows_backslashes_are_preserved(self):
        assert split_command(r"cd C:\Users\erwan\Documents", posix=False) == ["cd", r"C:\Users\erwan\Documents"]

    def test_quotes_are_removed_in_windows_mode(self):
        assert split_command("cat 'a b.txt' \"c d.txt\"", posix=False) == ["cat", "a b.txt", "c d.txt"]

    def test_posix_mode(self):
        assert split_command("cat 'a b.txt' c\\ d", posix=True) == ["cat", "a b.txt", "c d"]

    def test_hash_is_not_a_comment(self):
        assert split_command("echo #tag", posix=False) == ["echo", "#tag"]

    def test_empty_line(self):
        assert split_command("   ", posix=False) == []

    @pytest.mark.parametrize("posix", [True, False])
    def test_unbalanced_quote(self, posix):
        with pytest.raises(TokenizeError):
            split_command('echo "abc', posix=posix)


class TestCurrentWord:
    def test_plain(self):
        assert current_word("cd sub") == "sub"
        assert current_word("cd ") == ""
        assert current_word("cd") == "cd"

    def test_open_quote_keeps_the_space_inside_the_word(self):
        assert current_word("cat 'My fo") == "'My fo"

    def test_closed_quote_then_new_word(self):
        assert current_word("cat 'a b' c") == "c"


def test_unquote_word():
    assert unquote_word("'My fo") == "My fo"
    assert unquote_word('"a b"') == "a b"
    assert unquote_word("plain") == "plain"
    assert unquote_word("") == ""


def test_quote_if_needed():
    assert quote_if_needed("plain") == "plain"
    assert quote_if_needed("a b") == "'a b'"
    assert quote_if_needed("it's here") == '"it\'s here"'
