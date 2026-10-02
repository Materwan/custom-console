"""The HTTP client of the Clara server and the tool round trip, against a small fake server
that speaks the real protocol (SSE stream, tool requests answered by a second request)."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from custom_console.agent.clara import ClaraClient, ClaraError, NothingToCompact
from custom_console.agent.remote import run_remote_turn

CHAT, ADMIN = "chat-token", "admin-token"


class State:
    def __init__(self):
        self.bodies = []
        self.results = {}
        self.answered = threading.Event()
        self.admin_lines = []
        self.deleted = []
        self.closed_early = threading.Event()


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *args):
        pass

    # -- helpers ------------------------------------------------------------------
    def json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, token):
        if self.headers.get("Authorization") == f"Bearer {token}":
            return True
        self.reply(401, {"detail": "Missing or invalid token"})
        return False

    def event(self, payload):
        self.wfile.write(f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- routes -------------------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "provider": "local", "model": "fake"})
        if self.path == "/v1/admin/commands":
            return self.reply(200, [{"name": "provider", "choices": ["local", "cloud"]}]) if self.allowed(ADMIN) else None
        if self.path.startswith("/v1/conversations/"):
            if not self.allowed(CHAT):
                return
            return self.reply(200, {"tokens": 100, "window": 1000, "percent": 10.0, "summary": "", "messages": 2})
        self.reply(404, {"detail": "no such route"})

    def do_DELETE(self):
        if self.allowed(CHAT):
            self.state.deleted.append(self.path)
            self.reply(200, {"deleted_messages": 2})

    def do_POST(self):
        self.body = self.json_body()  # always read it: answering with the request unread resets the connection
        if self.path == "/v1/chat/stream":
            return self.stream() if self.allowed(CHAT) else None
        if self.path.startswith("/v1/turns/") and self.path.endswith("/tool-results"):
            if not self.allowed(CHAT):
                return
            self.state.results = {item["id"]: item["content"] for item in self.body["results"]}
            self.state.answered.set()
            return self.reply(200, {"accepted": len(self.state.results)})
        if self.path.endswith("/compact"):
            if not self.allowed(CHAT):
                return
            if "/empty/" in self.path:
                return self.reply(409, {"detail": "the conversation is empty: nothing to compact"})
            return self.reply(200, {"before_percent": 80.0, "after_percent": 5.0, "summary": "short"})
        if self.path == "/v1/admin/command":
            if self.allowed(ADMIN):
                line = self.body["line"]
                self.state.admin_lines.append(line)
                self.reply(200, {"output": f"ran {line}", "quit": False})
            return
        self.reply(404, {"detail": "no such route"})

    def stream(self):
        self.state.bodies.append(self.body)
        message = self.body.get("message")
        if message == "invalid":
            return self.reply(422, {"detail": "Tool name used twice, or reserved by the server: remember"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.event({"type": "turn", "id": "t1"})
        self.wfile.write(b": keepalive\n\n")
        try:
            if message == "tools":
                self.event(
                    {
                        "type": "tool_requests",
                        "turn": "t1",
                        "calls": [
                            {"id": "call_0_0", "name": "read", "arguments": {"path": "é.txt"}},
                            {"id": "call_0_1", "name": "list", "arguments": {}},
                        ],
                    }
                )
                if not self.state.answered.wait(1):
                    return self.event({"type": "error", "message": "client did not answer"})
                text = " | ".join(self.state.results[key] for key in sorted(self.state.results))
                self.event({"type": "token", "text": f"got: {text}"})
            elif message == "error":
                self.event({"type": "token", "text": "partial"})
                return self.event({"type": "error", "message": "The language model failed"})
            else:
                self.event({"type": "token", "text": "Héllo 🌍"})
            self.event({"type": "usage", "prompt_tokens": 3, "completion_tokens": 4})
            self.event({"type": "done", "reply": "ok", "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
        except (BrokenPipeError, ConnectionResetError):
            self.state.closed_early.set()


@pytest.fixture
def server():
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


def client_for(url, token=CHAT, **options):
    return ClaraClient(url, token, user_id="erwan", user_name="Erwan", admin_token=ADMIN, **options)


class TestBody:
    def test_who_speaks_and_where(self):
        body = client_for("http://x").body("hello", "conv-1")
        assert body == {"surface": "console", "user_id": "erwan", "user_name": "Erwan", "message": "hello", "conversation": "conv-1"}

    def test_empty_extras_are_left_out(self):
        body = client_for("http://x").body("hi", "c", tools=[], instructions="", prefix=None, ephemeral=False)
        assert set(body) == {"surface", "user_id", "user_name", "message", "conversation"}
        body = client_for("http://x").body("hi", "c", tools=[{"t": 1}], ephemeral=True, prefix="p")
        assert body["tools"] == [{"t": 1}] and body["ephemeral"] is True and body["prefix"] == "p"

    def test_no_user_name_when_there_is_none(self):
        assert "user_name" not in ClaraClient("http://x", "t", user_id="u").body("hi", "c")


class TestStream:
    def test_events_arrive_decoded_and_keepalives_are_skipped(self, server):
        url, _ = server
        events = list(client_for(url).stream_turn(client_for(url).body("hello", "c")))
        assert [e["type"] for e in events] == ["turn", "token", "usage", "done"]
        assert events[1]["text"] == "Héllo 🌍"  # UTF-8, whatever the headers say

    def test_a_refused_request_is_an_error_with_the_servers_reason(self, server):
        url, _ = server
        with pytest.raises(ClaraError, match="used twice.*HTTP 422"):
            list(client_for(url).stream_turn(client_for(url).body("invalid", "c")))

    def test_a_wrong_token_is_refused(self, server):
        url, _ = server
        with pytest.raises(ClaraError, match="Missing or invalid token.*401"):
            list(client_for(url, token="nope").stream_turn({"message": "hi"}))

    def test_no_token_fails_before_any_network_use(self):
        with pytest.raises(ClaraError, match="CLARA_TOKEN"):
            list(client_for("http://127.0.0.1:9", token=None).stream_turn({}))

    def test_an_unreachable_server(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # nothing listens here
        with pytest.raises(ClaraError, match="Cannot reach the Clara server"):
            list(client_for(f"http://127.0.0.1:{port}").stream_turn({"message": "hi"}))


class TestRemoteTurn:
    def test_a_plain_answer(self, server):
        url, _ = server
        client = client_for(url)
        text, usage = [], []
        done = run_remote_turn(client, client.body("hello", "c"), lambda n, a: "", on_text=text.append, on_usage=lambda p, c: usage.append((p, c)))
        assert "".join(text) == "Héllo 🌍" and usage == [(3, 4)] and done["reply"] == "ok"

    def test_tools_run_here_and_their_results_go_back_in_order(self, server):
        url, state = server
        client = client_for(url)
        ran = []

        def execute(name, arguments):
            ran.append((name, arguments))
            return f"{name}:{json.dumps(arguments, ensure_ascii=False)}"

        text = []
        done = run_remote_turn(client, client.body("tools", "c"), execute, on_text=text.append)
        assert ran == [("read", {"path": "é.txt"}), ("list", {})]
        assert state.results == {"call_0_0": 'read:{"path": "é.txt"}', "call_0_1": "list:{}"}
        assert "".join(text) == 'got: read:{"path": "é.txt"} | list:{}' and done["type"] == "done"

    def test_an_error_event_raises(self, server):
        url, _ = server
        client = client_for(url)
        text = []
        with pytest.raises(ClaraError, match="The language model failed"):
            run_remote_turn(client, client.body("error", "c"), lambda n, a: "", on_text=text.append)
        assert text == ["partial"]

    def test_cancelling_stops_and_returns_nothing(self, server):
        url, _ = server
        client = client_for(url)
        cancel = threading.Event()
        seen = []

        def on_text(chunk):
            seen.append(chunk)
            cancel.set()

        assert run_remote_turn(client, client.body("hello", "c"), lambda n, a: "", on_text=on_text, cancel=cancel) is None
        assert seen == ["Héllo 🌍"]

    def test_a_cancel_during_tools_does_not_send_the_results(self, server):
        url, state = server
        client = client_for(url)
        cancel = threading.Event()

        def execute(name, arguments):
            cancel.set()
            return "interrupted"

        assert run_remote_turn(client, client.body("tools", "c"), execute, cancel=cancel) is None
        assert state.results == {} and not state.answered.is_set()

    def test_a_stream_without_a_done_event_is_reported(self):
        class Ends:
            def stream_turn(self, body):
                yield {"type": "token", "text": "x"}

        with pytest.raises(ClaraError, match="closed the stream"):
            run_remote_turn(Ends(), {}, lambda n, a: "")

    def test_the_stream_is_closed_even_when_the_turn_fails(self):
        closed = []

        class Boom:
            def stream_turn(self, body):
                try:
                    yield {"type": "error", "message": "bad"}
                finally:
                    closed.append(True)

        with pytest.raises(ClaraError):
            run_remote_turn(Boom(), {}, lambda n, a: "")
        assert closed == [True]


class TestOtherCalls:
    def test_health_needs_no_token(self, server):
        url, _ = server
        assert client_for(url, token=None).health() == {"status": "ok", "provider": "local", "model": "fake"}

    def test_context_compact_and_forget(self, server):
        url, state = server
        client = client_for(url)
        assert client.context("c:1")["messages"] == 2
        assert client.compact("c:1", "tests") == {"before_percent": 80.0, "after_percent": 5.0, "summary": "short"}
        client.forget("c:1")
        assert state.deleted == ["/v1/conversations/c:1"]

    def test_nothing_to_compact_is_a_lookup_error(self, server):
        url, _ = server
        with pytest.raises(NothingToCompact) as caught:
            client_for(url).compact("empty")
        assert isinstance(caught.value, LookupError) and isinstance(caught.value, ClaraError)

    def test_admin_commands_use_the_admin_token(self, server):
        url, state = server
        client = client_for(url)
        assert client.admin("/provider cloud") == "ran /provider cloud"
        assert state.admin_lines == ["/provider cloud"]
        assert client.admin_commands() == [{"name": "provider", "choices": ["local", "cloud"]}]

    def test_the_chat_token_is_not_an_admin_token(self, server):
        url, _ = server
        client = ClaraClient(url, CHAT, user_id="u", admin_token=CHAT)
        with pytest.raises(ClaraError, match="401"):
            client.admin("/status")

    def test_without_an_admin_token_the_commands_explain(self):
        client = ClaraClient("http://x", CHAT, user_id="u")
        with pytest.raises(ClaraError, match="CLARA_ADMIN_TOKEN"):
            client.admin("/status")
        assert client.admin_commands() == []
