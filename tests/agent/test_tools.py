from __future__ import annotations

import json

import pytest

from custom_console.agent.permissions import UserPermissionDenied
from custom_console.fs import virtual
from custom_console.agent.tools import build_tools
from custom_console.agent.tools import mail as mail_module
from custom_console.agent.tools import pdf as pdf_module
from custom_console.agent.tools import web as web_module
from custom_console.agent.tools.filesystem import filesystem_tools, python_outline
from custom_console.agent.tools.mail import build_message, mail_tools
from custom_console.agent.tools.moodle import (
    compact_announcements,
    compact_course_structure,
    compact_grades,
    moodle_tools,
)
from custom_console.agent.tools.pdf import pdf_tools
from custom_console.agent.tools.web import compact_weather, web_tools
from custom_console.agent.tools.workspace_tools import workspace_tools


MARKDOWN = "# Title\n\n$x^2$"


def by_name(tools):
    return {tool.__name__: tool for tool in tools}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


class TestBuildTools:
    def test_default_set(self, ctx):
        names = {t.__name__ for t in build_tools(ctx)}
        assert {"file_system_read", "workspace_file_write", "pdf_to_markdown", "get_weather"} <= names
        assert "moodle_list_courses" in names
        assert "send_email" not in names  # no SMTP server configured

    def test_every_tool_has_a_description(self, ctx):
        for tool in build_tools(ctx):
            assert (tool.__doc__ or "").strip(), tool.__name__

    def test_tool_names_are_unique(self, ctx):
        names = [t.__name__ for t in build_tools(ctx)]
        assert len(names) == len(set(names))

    def test_moodle_can_be_disabled(self, make_ctx):
        ctx, _ = make_ctx(MOODLE_ENABLED="false")
        assert not any(t.__name__.startswith("moodle_") for t in build_tools(ctx))

    def test_email_tool_needs_an_smtp_server(self, make_ctx):
        ctx, _ = make_ctx(SMTP_HOST="smtp.example.com")
        assert "send_email" in {t.__name__ for t in build_tools(ctx)}

    def test_instructions_cover_every_registered_tool_family(self, ctx):
        from pathlib import Path

        text = (Path(__file__).parents[2] / "config" / "agent_instructions.txt").read_text(encoding="utf-8")
        names = {tool.__name__ for tool in build_tools(ctx)}
        # The tools describe themselves; the instructions only say which family is for what.
        for family in ("file_system_", "workspace_file_"):
            assert any(name.startswith(family) for name in names) and f"`{family}*`" in text
        assert "pdf_to_markdown" in names and "pdf_to_markdown" in text


# --------------------------------------------------------------------------- #
# file_system_*
# --------------------------------------------------------------------------- #


@pytest.fixture
def fs(ctx, files_dir):
    (files_dir / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    (files_dir / "sub").mkdir()
    (files_dir / "sub" / "deep.py").write_text(
        '"""Module."""\nimport os\n\nclass Box(Base):\n    """A box."""\n    def open(self, lid):\n        pass\n\n'
        "async def fetch(url, *rest, flag=1, **kw):\n    pass\n"
    )
    return by_name(filesystem_tools(ctx))


class TestFileSystemTools:
    def test_pwd(self, fs, files_dir):
        assert fs["file_system_pwd"]().data == str(files_dir).replace("\\", "/")

    def test_list(self, fs):
        assert fs["file_system_list"]().data == ["a.txt", "sub/"]
        assert fs["file_system_list"](path="sub").data == ["deep.py"]

    def test_list_errors_are_results_not_exceptions(self, fs):
        result = fs["file_system_list"](path="nope")
        assert not result.success and isinstance(result.error, FileNotFoundError)

    def test_read_full(self, fs):
        assert fs["file_system_read"]("a.txt").data == "one\ntwo\nthree\nfour\n"

    def test_read_range_is_one_based_inclusive_and_keeps_newlines(self, fs):
        assert fs["file_system_read"]("a.txt", mode="range", start_line=2, end_line=3).data == "two\nthree"

    @pytest.mark.parametrize(
        "kwargs",
        [{"mode": "range"}, {"mode": "range", "start_line": 0, "end_line": 2}, {"mode": "range", "start_line": 3, "end_line": 2}, {"mode": "bogus"}],
    )
    def test_read_invalid_arguments(self, fs, kwargs):
        result = fs["file_system_read"]("a.txt", **kwargs)
        assert not result.success and isinstance(result.error, ValueError)

    def test_read_summary_of_a_python_file(self, fs):
        outline = fs["file_system_read"](str("sub/deep.py"), mode="summary").data
        assert "class Box(Base):" in outline and "def open(self, lid)" in outline

    def test_read_summary_of_other_files_is_the_head(self, fs, files_dir):
        (files_dir / "long.txt").write_text("\n".join(f"l{i}" for i in range(200)))
        summary = fs["file_system_read"]("long.txt", mode="summary").data
        assert summary.splitlines()[0] == "l0" and len(summary.splitlines()) == 50

    def test_read_truncation_notice(self, fs):
        data = fs["file_system_read"]("a.txt", max_chars=5).data
        assert data.startswith("one\nt") and "truncated at 5 chars" in data and 'mode="range"' in data

    def test_read_binary_file_fails_cleanly(self, fs, files_dir):
        (files_dir / "b.bin").write_bytes(b"\x00\x01")
        assert not fs["file_system_read"]("b.bin").success

    def test_stat(self, fs):
        assert set(fs["file_system_stat"]("a.txt").data) == {"readable", "writable", "executable"}
        assert fs["file_system_stat"]("a.txt").data["readable"] is True

    def test_find(self, fs):
        assert fs["file_system_find"]("deep").data == ["sub/deep.py"]
        assert fs["file_system_find"]("deep", depth=1).data == []

    def test_cd_changes_where_relative_paths_resolve(self, fs):
        assert "Changed directory" in fs["file_system_cd"]("sub").data
        assert fs["file_system_list"]().data == ["deep.py"]

    def test_tree(self, fs):
        text = fs["file_system_tree"](depth=2).data
        assert "sub/" in text and "deep.py" in text

    def test_copy(self, fs, files_dir):
        assert fs["file_system_copy"]("a.txt", "b.txt").success
        assert (files_dir / "b.txt").read_text() == "one\ntwo\nthree\nfour\n"
        assert not fs["file_system_copy"]("sub", "sub2").success  # needs recursive
        assert fs["file_system_copy"]("sub", "sub2", recursive=True).success

    def test_read_only_level_blocks_the_write_tools(self, make_ctx, files_dir):
        ctx, log = make_ctx(auto_level=1, answer=False)
        (files_dir / "a.txt").write_text("x")
        tools = by_name(filesystem_tools(ctx))
        assert tools["file_system_list"]().success  # auto-accepted read
        denied = tools["file_system_copy"]("a.txt", "b.txt")
        assert isinstance(denied.error, UserPermissionDenied) and not (files_dir / "b.txt").exists()
        assert len(log.asked) == 1 and "file system copy" in log.asked[0]


def test_python_outline():
    source = "class A(B):\n    def f(self, x): ...\n    async def g(self): ...\n\ndef top(a, *args, k=1, **kw): ..."
    assert python_outline(source).splitlines() == [
        "class A(B):  [L1]",
        "    def f(self, x)  [L2]",
        "    async def g(self)  [L3]",
        "def top(a, *args, k, **kw)  [L5]",
    ]
    assert python_outline("x = 1") == "(no class or function)"
    assert "Syntax error" in python_outline("def (:")


# --------------------------------------------------------------------------- #
# workspace_file_*
# --------------------------------------------------------------------------- #


@pytest.fixture
def wt(ctx):
    return by_name(workspace_tools(ctx))


class TestWorkspaceTools:
    def test_write_read_list_move_delete(self, wt):
        assert wt["workspace_file_write"]("tmp", "a/n.txt", "hello").data == "tmp/a/n.txt"
        assert wt["workspace_file_read"]("tmp", "a/n.txt").data == "hello"
        assert wt["workspace_file_list"]("tmp").data == ["a/"]
        assert wt["workspace_file_move"]("tmp", "a/n.txt", "result", "n.txt").data == "result/n.txt"
        assert wt["workspace_file_delete"]("result", "n.txt").data == "result/n.txt"
        assert wt["workspace_file_list"]("result").data == []

    def test_sandbox_escape_is_a_failed_result(self, wt):
        for result in (
            wt["workspace_file_read"]("tmp", "../../secret"),
            wt["workspace_file_write"]("tmp", "../x", "data"),
            wt["workspace_file_delete"]("tmp", ".."),
            wt["workspace_file_list"]("nowhere"),
        ):
            assert not result.success and isinstance(result.error, ValueError)

    def test_workspace_root_cannot_be_deleted(self, wt):
        assert "root" in str(wt["workspace_file_delete"]("tmp", ".").error)

    def test_large_reads_are_truncated(self, wt):
        wt["workspace_file_write"]("tmp", "big.txt", "x" * 60_000)
        data = wt["workspace_file_read"]("tmp", "big.txt").data
        assert len(data) < 51_000 and "truncated" in data

    def test_get_copies_files_and_folders_from_the_system(self, ctx, wt, files_dir):
        (files_dir / "doc.txt").write_text("content")
        (files_dir / "folder").mkdir()
        (files_dir / "folder" / "inner.txt").write_text("inner")

        assert wt["workspace_file_get"]("doc.txt", "result").success
        assert wt["workspace_file_get"]("folder", "result").success
        assert ctx.workspace.read("result", "doc.txt")[0] == "content"
        assert ctx.workspace.read("result", "folder/inner.txt")[0] == "inner"

    def test_get_works_even_after_navigating_to_the_remarkable(self, ctx, wt, files_dir):
        (files_dir / "doc.txt").write_text("content")
        ctx.files.mode = ctx.files.MODE_REMARKABLE  # relative paths would now mean "remote"
        result = wt["workspace_file_get"](virtual.local_to_virtual(str(files_dir / "doc.txt")), "result")
        assert result.success, result.error
        assert ctx.workspace.read("result", "doc.txt")[0] == "content"

    def test_read_does_not_depend_on_the_current_location(self, ctx, wt):
        wt["workspace_file_write"]("tmp", "x.txt", "data")
        ctx.files.mode = ctx.files.MODE_REMARKABLE
        assert wt["workspace_file_read"]("tmp", "x.txt").data == "data"
        assert wt["workspace_file_list"]("tmp").data == ["x.txt"]

    def test_workspace_reads_and_lists_are_read_level(self, make_ctx):
        ctx, log = make_ctx(auto_level=1, answer=False)
        tools = by_name(workspace_tools(ctx))
        assert tools["workspace_file_list"]("tmp").success
        assert not tools["workspace_file_write"]("tmp", "a", "b").success
        assert len(log.asked) == 1


# --------------------------------------------------------------------------- #
# pdf / web / mail / moodle helpers
# --------------------------------------------------------------------------- #


class TestPdfTool:
    def test_converts_a_workspace_pdf(self, ctx, monkeypatch):
        ctx.workspace.write("tmp", "docs/paper.pdf", "%PDF fake")
        seen = {}

        def fake_convert(path, pages=None):
            seen.update(path=path, pages=pages)
            return "# Title\n\n$x^2$"

        monkeypatch.setattr(pdf_module, "convert_pdf", fake_convert)
        result = by_name(pdf_tools(ctx))["pdf_to_markdown"]("tmp", "docs/paper.pdf", pages=[0, 1])

        assert result.data == {"output": "tmp/docs/paper.md", "characters": len(MARKDOWN)}
        assert ctx.workspace.read("tmp", "docs/paper.md")[0] == MARKDOWN
        assert seen["pages"] == [0, 1] and seen["path"].endswith("paper.pdf")

    def test_custom_output_path(self, ctx, monkeypatch):
        ctx.workspace.write("tmp", "p.pdf", "x")
        monkeypatch.setattr(pdf_module, "convert_pdf", lambda path, pages=None: "md")
        result = by_name(pdf_tools(ctx))["pdf_to_markdown"]("tmp", "p.pdf", output_path="out/p.md")
        assert result.data["output"] == "tmp/out/p.md"

    def test_rejects_missing_and_non_pdf_files(self, ctx):
        tool = by_name(pdf_tools(ctx))["pdf_to_markdown"]
        ctx.workspace.write("tmp", "n.txt", "x")
        assert isinstance(tool("tmp", "missing.pdf").error, FileNotFoundError)
        assert "not a PDF" in str(tool("tmp", "n.txt").error)
        assert isinstance(tool("tmp", "../x.pdf").error, ValueError)


class TestWebTools:
    def test_compact_weather_keeps_only_the_requested_block_and_rounds(self):
        payload = {
            "latitude": 1,
            "elevation": 2,
            "timezone": "Europe/Paris",
            "current": {"temperature_2m": 12.3456, "weather_code": 3, "time": "2026-01-01T00:00"},
            "hourly": {"temperature_2m": [1.25, 2.0]},
        }
        assert compact_weather(payload, "current") == {
            "current": {"temperature_2m": 12.3, "weather_code": 3, "time": "2026-01-01T00:00"},
            "timezone": "Europe/Paris",
        }
        assert compact_weather(payload, "hourly")["hourly"]["temperature_2m"] == [1.2, 2.0]
        assert compact_weather({"odd": 1}, "daily") == {"odd": 1}

    def test_get_weather_builds_the_request(self, ctx, monkeypatch):
        captured = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"daily": {"temperature_2m_max": [20.04]}, "timezone": "UTC"}

        def fake_get(url, params=None, timeout=None):
            captured.update(url=url, params=params, timeout=timeout)
            return Response()

        monkeypatch.setattr(web_module.requests, "get", fake_get)
        tools = by_name(web_tools(ctx))
        result = tools["get_weather"](48.85, 2.35, resolution="daily", forecast_days=99)

        assert result.data == {"daily": {"temperature_2m_max": [20.0]}, "timezone": "UTC"}
        assert captured["params"]["forecast_days"] == web_module.MAX_FORECAST_DAYS
        assert "temperature_2m_max" in captured["params"]["daily"] and captured["timeout"]

    def test_get_weather_rejects_unknown_resolution(self, ctx):
        result = by_name(web_tools(ctx))["get_weather"](0, 0, resolution="yearly")
        assert isinstance(result.error, ValueError)

    def test_get_weather_reports_http_failures(self, ctx, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("offline")

        monkeypatch.setattr(web_module.requests, "get", boom)
        assert not by_name(web_tools(ctx))["get_weather"](0, 0).success

    def test_get_location(self, ctx, monkeypatch):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"city": "Paris", "region": "IdF", "country": "FR", "loc": "48,2", "ip": "1.2.3.4"}

        monkeypatch.setattr(web_module.requests, "get", lambda url, timeout=None: Response())
        assert by_name(web_tools(ctx))["get_location"]().data == {
            "city": "Paris", "region": "IdF", "country": "FR", "loc": "48,2"
        }  # the IP address is not passed on to the model


class TestMailTool:
    def test_message(self):
        message = build_message("me@x.org", "you@y.org", "Hi", "Body")
        assert message["To"] == "you@y.org" and message["Subject"] == "Hi"
        assert message.get_content().strip() == "Body"

    def test_no_tool_without_smtp_host(self, ctx):
        assert mail_tools(ctx) == []

    def test_sends_through_smtp_with_starttls_and_login(self, make_ctx, monkeypatch):
        ctx, log = make_ctx(
            auto_level=0, SMTP_HOST="smtp.test", SMTP_PORT="587", SMTP_USER="user", SMTP_PASSWORD="pw", SMTP_FROM="me@test"
        )
        events = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                events.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                events.append("starttls")

            def login(self, user, password):
                events.append(("login", user, password))

            def send_message(self, message):
                events.append(("send", message["To"]))

        monkeypatch.setattr(mail_module.smtplib, "SMTP", FakeSMTP)
        result = by_name(mail_tools(ctx))["send_email"]("you@y.org", "Hi", "Body")

        assert result.success and log.asked  # sending always needs confirmation at level 0
        assert events == [("connect", "smtp.test", 587), "starttls", ("login", "user", "pw"), ("send", "you@y.org")]

    def test_port_465_uses_ssl(self, make_ctx, monkeypatch):
        ctx, _ = make_ctx(SMTP_HOST="smtp.test", SMTP_PORT="465", SMTP_FROM="me@test")
        used = []

        class FakeSSL:
            def __init__(self, host, port, timeout=None):
                used.append("ssl")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send_message(self, message):
                used.append("sent")

        monkeypatch.setattr(mail_module.smtplib, "SMTP_SSL", FakeSSL)
        assert by_name(mail_tools(ctx))["send_email"]("a@b.c", "s").success
        assert used == ["ssl", "sent"]

    def test_requires_a_sender(self, make_ctx):
        ctx, _ = make_ctx(SMTP_HOST="smtp.test")
        result = by_name(mail_tools(ctx))["send_email"]("a@b.c", "s")
        assert not result.success and "SMTP_FROM" in str(result.error)


class TestMoodleHelpers:
    def test_compact_course_structure(self):
        data = {
            "course_id": "42",
            "url": "https://m/course/view.php?id=42",
            "sections": [
                {"title": "Week 1", "resources": [
                    {"title": "Slides", "url": "u1", "kind": "resource", "due_date": None},
                    {"title": "TD1", "url": "u2", "kind": "assign", "due_date": "Friday"},
                ]},
                {"title": "Empty", "resources": []},
            ],
        }
        assert compact_course_structure(data).splitlines() == [
            "Course 42 - https://m/course/view.php?id=42",
            "## Week 1",
            "- [resource] Slides - u1",
            "- [assign] TD1 (due: Friday) - u2",
            "## Empty",
        ]

    def test_compact_announcements_and_grades(self):
        assert compact_announcements([{"title": "T", "url": "u", "text": "body"}]) == "### T - u\nbody"
        assert compact_grades([{"columns": ["Math", "15"]}, {"columns": ["Phys", "12"]}]) == "Math | 15\nPhys | 12"

    def test_courses_are_cached_and_the_browser_is_not_started_twice(self, make_ctx, monkeypatch):
        ctx, _ = make_ctx()
        tools = by_name(moodle_tools(ctx))
        calls = []

        from custom_console.agent.tools import moodle as moodle_module

        class FakeRunner:
            def __init__(self, *a, **k):
                pass

            def run(self, fn):
                calls.append(1)

                class Client:
                    def list_courses(self):
                        return [{"id": "1", "title": "Algo", "url": "u"}]

                return fn(Client())

            def close(self):
                calls.append("closed")

        monkeypatch.setattr(moodle_module, "MoodleRunner", FakeRunner)
        tools = by_name(moodle_module.moodle_tools(ctx))

        first = tools["moodle_list_courses"]()
        second = tools["moodle_list_courses"]()
        refreshed = tools["moodle_list_courses"](force_refresh=True)

        assert first.data == second.data == refreshed.data == [{"id": "1", "title": "Algo", "url": "u"}]
        assert calls == [1, 1]  # the second call was served from the cache
        ctx.close()
        assert calls[-1] == "closed"

    def test_download_goes_through_the_workspace_sandbox(self, make_ctx, monkeypatch):
        ctx, _ = make_ctx()
        from custom_console.agent.tools import moodle as moodle_module

        saved = {}

        class FakeRunner:
            def __init__(self, *a, **k):
                pass

            def run(self, fn):
                class Client:
                    def download_file(self, url, path):
                        saved.update(url=url, path=path)
                        return {"downloaded": True, "path": path, "suggested_filename": "f.pdf", "failure": None}

                return fn(Client())

            def close(self):
                pass

        monkeypatch.setattr(moodle_module, "MoodleRunner", FakeRunner)
        tool = by_name(moodle_module.moodle_tools(ctx))["moodle_download_file"]

        ok = tool("/pluginfile.php/1/f.pdf", "tmp", "dl/f.pdf")
        assert ok.data["path"] == "tmp/dl/f.pdf" and saved["path"].endswith("f.pdf")
        escaped = tool("/pluginfile.php/1/f.pdf", "tmp", "../../evil.pdf")
        assert isinstance(escaped.error, ValueError)
        assert json.loads(ok.to_llm())["downloaded"] is True
