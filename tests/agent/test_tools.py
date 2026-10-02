from __future__ import annotations

import json
import os



from custom_console.agent.tools import build_tools
from custom_console.agent.tools import mail as mail_module
from custom_console.agent.tools import pdf as pdf_module
from custom_console.agent.tools import web as web_module
from custom_console.agent.tools.mail import build_message, mail_tools
from custom_console.agent.tools.moodle import (
    compact_announcements,
    compact_course_structure,
    compact_grades,
    moodle_tools,
)
from custom_console.agent.tools.pdf import pdf_tools
from custom_console.agent.tools.web import compact_weather, web_tools


MARKDOWN = "# Title\n\n$x^2$"


def by_name(tools):
    return {tool.__name__: tool for tool in tools}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


class TestBuildTools:
    def test_default_set(self, ctx):
        names = {t.__name__ for t in build_tools(ctx)}
        assert {"file_system_read", "file_system_edit", "run_command", "todo_write", "pdf_to_markdown", "rmdoc_to_pdf", "get_weather"} <= names
        assert not any(name.startswith("workspace_file_") for name in names)
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

    def test_instructions_mention_every_registered_tool_family(self, ctx):
        from pathlib import Path

        text = (Path(__file__).parents[2] / "config" / "agent_instructions.txt").read_text(encoding="utf-8")
        for tool in build_tools(ctx):
            if tool.__name__.startswith("file_system_") or tool.__name__ in ("pdf_to_markdown", "rmdoc_to_pdf", "run_command", "todo_write"):
                assert tool.__name__ in text, tool.__name__


# --------------------------------------------------------------------------- #
# pdf / web / mail / moodle helpers
# --------------------------------------------------------------------------- #


class TestPdfTool:
    def test_converts_a_pdf_next_to_it(self, make_ctx, files_dir, monkeypatch):
        ctx, _ = make_ctx(zone=True, auto_level=0)
        (files_dir / "docs").mkdir()
        (files_dir / "docs" / "paper.pdf").write_text("%PDF fake")
        seen = {}

        def fake_convert(path, pages=None):
            seen.update(path=path, pages=pages)
            return MARKDOWN

        monkeypatch.setattr(pdf_module, "convert_pdf", fake_convert)
        result = by_name(pdf_tools(ctx))["pdf_to_markdown"]("docs/paper.pdf", pages=[0, 1])

        output = files_dir / "docs" / "paper.md"
        assert result.success and result.data["characters"] == len(MARKDOWN)
        assert output.read_text(encoding="utf-8") == MARKDOWN
        assert seen["pages"] == [0, 1] and seen["path"].endswith("paper.pdf")

    def test_custom_output_path(self, make_ctx, files_dir, monkeypatch):
        ctx, _ = make_ctx(zone=True, auto_level=0)
        (files_dir / "p.pdf").write_text("x")
        monkeypatch.setattr(pdf_module, "convert_pdf", lambda path, pages=None: "md")
        result = by_name(pdf_tools(ctx))["pdf_to_markdown"]("p.pdf", output_path="out/p.md")
        assert result.success and (files_dir / "out" / "p.md").read_text() == "md"

    def test_rejects_missing_and_non_pdf_files(self, ctx, files_dir):
        tool = by_name(pdf_tools(ctx))["pdf_to_markdown"]
        (files_dir / "n.txt").write_text("x")
        assert isinstance(tool("missing.pdf").error, FileNotFoundError)
        assert "not a PDF" in str(tool("n.txt").error)

    def test_conversion_in_the_free_zone_needs_no_permission_but_elsewhere_does(self, make_ctx, files_dir, tmp_path, monkeypatch):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        monkeypatch.setattr(pdf_module, "convert_pdf", lambda path, pages=None: "md")
        (files_dir / "in.pdf").write_text("x")
        (tmp_path / "out.pdf").write_text("x")
        tool = by_name(pdf_tools(ctx))["pdf_to_markdown"]
        assert tool("in.pdf").success and not log.asked
        assert not tool(str(tmp_path / "out.pdf")).success and len(log.asked) == 1


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

    def test_download_saves_to_a_local_path_and_marks_it_as_known(self, make_ctx, files_dir, monkeypatch):
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
                        os.makedirs(os.path.dirname(path), exist_ok=True)  # the real client does too
                        with open(path, "wb") as handle:
                            handle.write(b"pdf")
                        return {"downloaded": True, "path": path, "suggested_filename": "f.pdf", "failure": None}

                return fn(Client())

            def close(self):
                pass

        monkeypatch.setattr(moodle_module, "MoodleRunner", FakeRunner)
        tool = by_name(moodle_module.moodle_tools(ctx))["moodle_download_file"]

        ok = tool("/pluginfile.php/1/f.pdf", "dl/f.pdf")
        assert ok.data["path"].endswith("dl/f.pdf") and saved["path"] == ok.data["path"]
        assert (files_dir / "dl" / "f.pdf").read_bytes() == b"pdf"
        assert json.loads(ok.to_llm())["data"]["downloaded"] is True
        ctx.reads.check(saved["path"])  # the agent may edit what it downloaded
