from __future__ import annotations

import pytest

from custom_console.core.permissions.redact import (
    MASK,
    is_secret_name,
    looks_secret,
    redact,
    redact_value,
    scrub_environment,
)

TOKEN = "clara_ab12cd_0123456789abcdefghijklmnopqrstuv"


@pytest.mark.parametrize("text", [
    f"token is {TOKEN} ok",
    f"Authorization: Bearer {TOKEN}",
    "curl -H 'Authorization: Bearer abcdefghijklmnop12345' https://x",
    "API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
    "export MY_SECRET='top secret value'",
    'DB_PASSWORD="p@ss word"',
    "--password=hunter2hunter2",
    "--api-key abcdef123456 --verbose",
    '{"password": "hunter2", "user": "me"}',
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow-cut-off-here",
    "ghp_" + "a" * 36,
    "AKIAABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP",
    "postgres://admin:s3cretpass@db.example/app",
])
def test_secrets_are_hidden(text):
    redacted = redact(text)
    assert MASK in redacted
    for secret in ("0123456789abcdef", "hunter2", "top secret", "p@ss", "abcdef123456", "b3BlbnNzaC1rZXk", "MIIEow",
                   "s3cretpass", "abcdefghijklmnop12345", "dBjftJeZ4CVP", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in redacted
    assert looks_secret(text)


def test_the_text_around_a_secret_is_kept():
    assert redact(f"before {TOKEN} after") == f"before {MASK} after"
    assert redact("DB_PASSWORD=abc run --fast") == f"DB_PASSWORD={MASK} run --fast"
    assert redact('{"password": "x", "user": "me"}') == f'{{"password": "{MASK}", "user": "me"}}'


@pytest.mark.parametrize("text", [
    "ls -la /home/me", "git commit -m 'fix the token parser'", "keyboard layout", "monkey=banana", "echo hello world",
    "cat notes.txt | grep todo", "the secretary called", "turkey sandwich", "",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == text and not looks_secret(text)


def test_environment_scrub():
    env = {"PATH": "/bin", "HOME": "/h", "SystemRoot": "C:\\Windows", "CLARA_URL": "x", "CLARA_TOKEN_FILE": "y",
           "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "k", "DB_PASSWORD": "p", "ssh_key_path": "q",
           "MY_CREDENTIALS": "c", "LANG": "C"}
    assert scrub_environment(env) == {"PATH": "/bin", "HOME": "/h", "SystemRoot": "C:\\Windows", "LANG": "C"}
    assert is_secret_name("openai_api_key") and not is_secret_name("PATHEXT")


def test_redact_value_keeps_the_shape():
    value = {"a": [f"x {TOKEN}", 3, {"b": "PASSWORD=zzz"}], "c": None, "d": ("t",)}
    assert redact_value(value) == {"a": [f"x {MASK}", 3, {"b": f"PASSWORD={MASK}"}], "c": None, "d": ["t"]}


def test_a_value_under_a_secret_named_key_is_hidden_whole():
    value = {"env": {"API_KEY": "k", "apiKey": "k2", "auth_token": 5, "password": "hunter2", "user": "me"}, "list": [{"secret": "s"}]}
    assert redact_value(value) == {"env": {"API_KEY": MASK, "apiKey": MASK, "auth_token": MASK, "password": MASK, "user": "me"},
                                   "list": [{"secret": MASK}]}
    assert redact_value({"monkey": "banana", "keyboard": "us"}) == {"monkey": "banana", "keyboard": "us"}
