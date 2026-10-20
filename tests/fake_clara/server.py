"""A fake Clara server, over real HTTP, for the contract tests and the screens.

A test queues the turns it wants (`fake.queue_turn(...)`), each a list of steps: an event (a dict), `Keepalive()`,
`Sleep(s)`, `WaitResults()` (blocks until the client posts the results of the last `tool_requests`), `WaitDecision(id)`,
`Raw(text)`, `Hang()` or `Drop()`. The server records what it received, so a test can check the headers, the bodies,
the cancel (`disconnects`) and the results.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

TOKEN = "clara_fake01_0123456789abcdefghijklmnopqrstuv"


@dataclass
class Keepalive:
    pass


@dataclass
class Sleep:
    seconds: float


@dataclass
class Raw:
    text: str


@dataclass
class Hang:
    pass


@dataclass
class Drop:
    pass


@dataclass
class WaitResults:
    pass


@dataclass
class WaitDecision:
    approval: int


@dataclass
class BeforeHeaders:
    """Wait (polling for a disconnect) before the response starts."""

    seconds: float


@dataclass
class HttpError:
    status: int
    code: str
    message: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


class FakeClara:
    def __init__(self) -> None:
        self.token = TOKEN
        self.url = ""
        self.requests: list[dict[str, Any]] = []
        self.turns: deque[list[Any]] = deque()
        self.results: dict[str, dict[str, str]] = {}  # turn id -> call id -> content
        self.decisions: dict[int, str] = {}
        self.disconnects = 0  # streams the client closed before they ended
        self.cancelled_before_headers = 0
        self.conversations: dict[str, dict[str, Any]] = {}
        self.approvals: dict[int, dict[str, Any]] = {}
        self.rules: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = [
            {"ref": "fake:small", "provider": "fake", "name": "small", "local": False, "window": 32000,
             "capabilities": {"vision": False}},
            {"ref": "fake:big", "provider": "fake", "name": "big", "local": False, "window": 128000,
             "capabilities": {"vision": True}},
        ]
        self.projects = [{"id": 1, "name": "Demo", "description": "", "files": 2}]
        self.next_turn = 0
        self._results_events: dict[str, asyncio.Event] = {}
        self._decision_events: dict[int, asyncio.Event] = {}
        self._turn_tokens: dict[str, str] = {}
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ test API
    def queue_turn(self, *steps: Any) -> None:
        self.turns.append(list(steps))

    def last(self, path: str) -> dict[str, Any]:
        return next(r for r in reversed(self.requests) if r["path"] == path)

    def requests_to(self, path: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == path]

    def add_approval(self, approval_id: int, **fields: Any) -> dict[str, Any]:
        item = {"id": approval_id, "tool": "write_file", "target": "x", "level": "write", "summary": "Write x",
                "reason": "asks", "status": "pending", "conversation": "", "answer": "", **fields}
        self.approvals[approval_id] = item
        return item

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        config = uvicorn.Config(self.app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(500):
            if self._server.started and self._server.servers:
                break
            threading.Event().wait(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ the app
    def app(self) -> Starlette:
        routes = [
            Route("/v1/auth/me", self.me),
            Route("/v1/models", self.models_route),
            Route("/v1/packs", self.packs),
            Route("/v1/projects", self.projects_route),
            Route("/v1/conversations", self.conversations_route, methods=["GET", "POST"]),
            Route("/v1/conversations/{cid}", self.conversation_route, methods=["GET", "PATCH", "DELETE"]),
            Route("/v1/conversations/{cid}/messages", self.messages_route),
            Route("/v1/conversations/{cid}/compact", self.compact_route, methods=["POST"]),
            Route("/v1/conversations/{cid}/title", self.title_route, methods=["POST"]),
            Route("/v1/conversations/{cid}/version", self.version_route, methods=["POST"]),
            Route("/v1/approvals", self.approvals_route),
            Route("/v1/approvals/{aid:int}/decide", self.decide_route, methods=["POST"]),
            Route("/v1/rules", self.rules_route),
            Route("/v1/rules/test", self.rules_test_route, methods=["POST"]),
            Route("/v1/chat/stream", self.chat_stream, methods=["POST"]),
            Route("/v1/turns/{turn}/tool-results", self.tool_results, methods=["POST"]),
        ]
        return Starlette(routes=routes)

    async def _record(self, request: Request) -> JSONResponse | None:
        body: Any = None
        if request.method in ("POST", "PATCH", "PUT"):
            raw = await request.body()
            body = json.loads(raw) if raw else None
        self.requests.append({"method": request.method, "path": request.url.path, "query": dict(request.query_params),
                              "headers": dict(request.headers), "body": body})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self.error(401, "unauthorized", "This token is not valid (or was revoked).")
        return None

    @staticmethod
    def error(status: int, code: str, message: str = "", headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message or code}}, status, headers=headers)

    async def me(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"token": "console", "client": "console", "approvals": "all", "web": False})

    async def models_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"models": self.models, "default": "fake:small"})

    async def packs(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        packs = [{"id": "web", "label": "Web search", "description": "Search the web.", "default": True,
                  "available": True, "reason": "", "where": "both"},
                 {"id": "second_brain", "label": "Second brain", "description": "Vault.", "default": False,
                  "available": False, "reason": "No vault is set up.", "where": "both"}]
        return refused or JSONResponse({"packs": packs})

    async def projects_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"projects": self.projects})

    def _new_conversation(self, body: dict[str, Any]) -> dict[str, Any]:
        cid = f"c{len(self.conversations) + 1:016x}"
        item = {"id": cid, "title": body.get("title", ""), "project": body.get("project"), "model": body.get("model", ""),
                "incognito": body.get("incognito", False), "local_only": body.get("local_only", False),
                "created_at": "2026-10-19T10:00:00Z", "updated_at": "2026-10-19T10:00:00Z",
                "packs": body.get("packs") or ["web"], "messages": []}
        self.conversations[cid] = item
        return item

    async def conversations_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        if request.method == "POST":
            return JSONResponse(self._public(self._new_conversation(self.requests[-1]["body"] or {})), 201)
        return JSONResponse({"conversations": [self._public(c) for c in self.conversations.values()]})

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != "messages"}

    async def conversation_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        item = self.conversations.get(request.path_params["cid"])
        if item is None:
            return self.error(404, "not_found", "No such conversation.")
        if request.method == "DELETE":
            del self.conversations[item["id"]]
            return JSONResponse({"deleted": True})
        if request.method == "PATCH":
            item.update(self.requests[-1]["body"] or {})
        return JSONResponse(self._public(item))

    async def messages_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        item = self.conversations.get(request.path_params["cid"])
        if item is None:
            return self.error(404, "not_found", "No such conversation.")
        return JSONResponse({"conversation": item["id"], "messages": item["messages"]})

    async def compact_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"before": 61.5, "after": 12.0})

    async def title_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"title": "A title"})

    async def version_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"ok": True})

    async def approvals_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        status = request.query_params.get("status") or None
        found = [{**a, "may_answer": True} for a in self.approvals.values() if status is None or a["status"] == status]
        return JSONResponse({"approvals": found})

    async def decide_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        approval = self.approvals.get(request.path_params["aid"])
        if approval is None:
            return self.error(404, "not_found", "No such request.")
        if approval["status"] != "pending":
            return self.error(409, "conflict", "Already answered.")
        answer = self.requests[-1]["body"]["answer"]
        approval.update(status="approved" if answer.startswith("allow") else "denied", answer=answer)
        self.decisions[approval["id"]] = answer
        self._decision_event(approval["id"]).set()
        return JSONResponse(approval)

    async def rules_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"rules": self.rules, "defaults": [{"class": "read", "interactive": "allow"}],
                                        "tainted_rule": True, "tools": ["web_search"]})

    async def rules_test_route(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        return refused or JSONResponse({"effect": "ask", "why": "default for write", "action": {"tool": "x"}})

    # ------------------------------------------------------------------ the turn
    def _results_event(self, turn: str) -> asyncio.Event:
        return self._results_events.setdefault(turn, asyncio.Event())

    def _decision_event(self, approval: int) -> asyncio.Event:
        return self._decision_events.setdefault(approval, asyncio.Event())

    async def tool_results(self, request: Request) -> JSONResponse:
        refused = await self._record(request)
        if refused:
            return refused
        turn = request.path_params["turn"]
        if turn not in self._turn_tokens:
            return self.error(404, "not_found", "No turn of yours is waiting for these results.")
        results = {item["id"]: item["content"] for item in self.requests[-1]["body"]["results"]}
        self.results[turn] = results
        self._results_event(turn).set()
        return JSONResponse({"ok": True})

    async def chat_stream(self, request: Request) -> Any:
        refused = await self._record(request)
        if refused:
            return refused
        steps = self.turns.popleft() if self.turns else [
            {"type": "turn", "id": "t0", "conversation": None, "model": "fake:small", "incognito": False, "local_only": False},
            {"type": "token", "text": "Hello."},
            {"type": "done", "reply": "Hello.", "conversation": None, "tools": [], "usage": {}, "context": {}, "model": "fake:small"},
        ]
        if steps and isinstance(steps[0], HttpError):
            error = steps[0]
            return self.error(error.status, error.code, error.message, error.headers)
        while steps and isinstance(steps[0], BeforeHeaders):
            wait = steps.pop(0).seconds
            end = asyncio.get_running_loop().time() + wait
            while asyncio.get_running_loop().time() < end:
                if await request.is_disconnected():
                    self.cancelled_before_headers += 1
                    return JSONResponse({}, 499)
                await asyncio.sleep(0.02)
        return StreamingResponse(self._play(steps), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def _play(self, steps: list[Any]) -> AsyncIterator[str]:
        finished = False
        turn_id = ""
        try:
            for step in steps:
                if isinstance(step, dict):
                    if step["type"] == "turn":
                        turn_id = step["id"]
                        self._turn_tokens[turn_id] = self.token
                    yield sse(step)
                elif isinstance(step, Keepalive):
                    yield ": keepalive\n\n"
                elif isinstance(step, Sleep):
                    await asyncio.sleep(step.seconds)
                elif isinstance(step, Raw):
                    yield step.text
                elif isinstance(step, WaitResults):
                    await asyncio.wait_for(self._results_event(turn_id).wait(), 30)
                elif isinstance(step, WaitDecision):
                    await asyncio.wait_for(self._decision_event(step.approval).wait(), 30)
                elif isinstance(step, Hang):
                    await asyncio.sleep(3600)
                elif isinstance(step, Drop):
                    raise ConnectionError("dropped on purpose")
            finished = True
        finally:
            if not finished:
                self.disconnects += 1
