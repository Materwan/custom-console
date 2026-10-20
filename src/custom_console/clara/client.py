"""The Clara client: the only place that knows Clara's routes.

Every request carries `Authorization: Bearer <token>` and `X-Clara-Client`. The token says who is calling, so no
request carries an identity. Cancelling the task that reads a turn closes the connection, at any point, even before
the response headers arrive.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any

import httpx

from .errors import ClaraError, ConnectionFailed, StreamSilent
from .events import Event
from .stream import read_events
from .types import (
    ANSWERS,
    Approval,
    ChatBody,
    Conversation,
    Me,
    Message,
    Models,
    Pack,
    Project,
    Rules,
)

DEFAULT_URL = "http://127.0.0.1:8765"
READ_TIMEOUT = 60.0  # the server sends a keepalive every 15 s: silence for 60 s means a dead connection
CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 413: "too_large",
         422: "unprocessable", 429: "rate_limited", 500: "internal", 502: "model_failed", 503: "unavailable",
         504: "timeout"}


def error_from_response(response: httpx.Response) -> ClaraError:
    """`{"error": {"code", "message"}}`, or whatever the server (or a proxy) sent instead."""
    code = CODES.get(response.status_code, "error")
    message = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or "")
        elif isinstance(body, dict) and isinstance(body.get("detail"), str):
            message = body["detail"]
    except ValueError:
        message = response.text[:300]
    retry = response.headers.get("retry-after")
    try:
        retry_after = float(retry) if retry else None
    except ValueError:
        retry_after = None
    return ClaraError(code, message or f"HTTP {response.status_code}", response.status_code, retry_after)


class ClaraClient:
    def __init__(self, url: str = DEFAULT_URL, token: str = "", *, client_label: str = "console",
                 read_timeout: float = READ_TIMEOUT, transport: httpx.AsyncBaseTransport | None = None,
                 trace: Callable[[str], None] | None = None):
        self.url = url.rstrip("/")
        self.read_timeout = read_timeout
        hooks: dict[str, list[Callable[..., Any]]] = {}
        if trace is not None:  # `--debug`: the method, the path and the status; never a body or a header
            async def on_response(response: httpx.Response) -> None:
                trace(f"{response.request.method} {response.request.url.path} -> {response.status_code}")
            hooks["response"] = [on_response]
        self._http = httpx.AsyncClient(
            base_url=self.url, transport=transport,
            headers={"Authorization": f"Bearer {token}", "X-Clara-Client": client_label},
            timeout=httpx.Timeout(30.0, connect=10.0), event_hooks=hooks,
        )

    def __repr__(self) -> str:  # the token is never shown
        return f"ClaraClient({self.url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ClaraClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ plain requests
    async def _call(self, method: str, path: str, *, body: Any = None, params: Mapping[str, Any] | None = None,
                    timeout: float | None = None) -> Any:
        try:
            response = await self._http.request(
                method, path, json=body, params={k: v for k, v in (params or {}).items() if v is not None},
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException:
            raise ConnectionFailed(f"Clara at {self.url} did not answer in time.") from None
        except httpx.TransportError as error:
            raise ConnectionFailed(f"Cannot reach Clara at {self.url} ({type(error).__name__}).") from None
        if response.is_error:
            raise error_from_response(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ClaraError("bad_response", f"Clara sent something that is not JSON ({response.status_code}).") from None

    # ------------------------------------------------------------------ account, models, packs, projects
    async def me(self) -> Me:
        return Me.from_json(await self._call("GET", "/v1/auth/me"))

    async def models(self) -> Models:
        return Models.from_json(await self._call("GET", "/v1/models"))

    async def packs(self, where: str = "chat") -> tuple[Pack, ...]:
        data = await self._call("GET", "/v1/packs", params={"where": where})
        return tuple(Pack.from_json(p) for p in data.get("packs", []) if isinstance(p, dict))

    async def projects(self) -> tuple[Project, ...]:
        data = await self._call("GET", "/v1/projects")
        return tuple(Project.from_json(p) for p in data.get("projects", []) if isinstance(p, dict))

    # ------------------------------------------------------------------ conversations
    async def create_conversation(self, *, title: str = "", project: int | None = None, model: str = "",
                                  incognito: bool = False, local_only: bool = False,
                                  packs: list[str] | None = None) -> Conversation:
        body: dict[str, Any] = {"title": title, "project": project, "model": model, "incognito": incognito,
                                "local_only": local_only, "packs": packs}
        data = await self._call("POST", "/v1/conversations", body={k: v for k, v in body.items() if v is not None})
        return Conversation.from_json(data)

    async def conversations(self, *, project: str = "any", limit: int = 50, offset: int = 0) -> tuple[Conversation, ...]:
        data = await self._call("GET", "/v1/conversations", params={"project": project, "limit": limit, "offset": offset})
        return tuple(Conversation.from_json(c) for c in data.get("conversations", []) if isinstance(c, dict))

    async def conversation(self, conversation_id: str) -> dict[str, Any]:
        result = await self._call("GET", f"/v1/conversations/{conversation_id}")
        return result if isinstance(result, dict) else {}

    async def change_conversation(self, conversation_id: str, **fields: Any) -> Conversation:
        data = await self._call("PATCH", f"/v1/conversations/{conversation_id}", body=fields)
        return Conversation.from_json(data)

    async def delete_conversation(self, conversation_id: str) -> None:
        await self._call("DELETE", f"/v1/conversations/{conversation_id}")

    async def messages(self, conversation_id: str, *, calls: bool = False) -> tuple[Message, ...]:
        data = await self._call("GET", f"/v1/conversations/{conversation_id}/messages", params={"calls": calls or None})
        return tuple(Message.from_json(m) for m in data.get("messages", []) if isinstance(m, dict))

    async def compact(self, conversation_id: str, focus: str = "") -> tuple[float, float]:
        data = await self._call("POST", f"/v1/conversations/{conversation_id}/compact", body={"focus": focus},
                                timeout=300.0)
        return float(data.get("before", 0)), float(data.get("after", 0))

    async def title(self, conversation_id: str) -> str:
        data = await self._call("POST", f"/v1/conversations/{conversation_id}/title", timeout=120.0)
        return str(data.get("title", ""))

    async def switch_version(self, conversation_id: str, message_id: int) -> None:
        await self._call("POST", f"/v1/conversations/{conversation_id}/version", body={"message_id": message_id})

    # ------------------------------------------------------------------ approvals and rules
    async def approvals(self, status: str = "pending", limit: int = 100) -> tuple[Approval, ...]:
        data = await self._call("GET", "/v1/approvals", params={"status": status, "limit": limit})
        return tuple(Approval.from_json(a) for a in data.get("approvals", []) if isinstance(a, dict))

    async def decide(self, approval_id: int, answer: str) -> Approval:
        if answer not in ANSWERS:
            raise ValueError(f"answer must be one of {', '.join(ANSWERS)}")
        return Approval.from_json(await self._call("POST", f"/v1/approvals/{approval_id}/decide", body={"answer": answer}))

    async def rules(self) -> Rules:
        return Rules.from_json(await self._call("GET", "/v1/rules"))

    async def test_rule(self, tool: str, arguments: Mapping[str, Any], *, tainted: bool = False,
                        local_only: bool = False) -> dict[str, Any]:
        body = {"tool": tool, "arguments": dict(arguments), "tainted": tainted, "local_only": local_only}
        result = await self._call("POST", "/v1/rules/test", body=body)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ the turn
    async def chat_stream(self, body: ChatBody) -> AsyncGenerator[Event, None]:
        """One turn as typed events. Closing the generator (or cancelling its task) closes the connection."""
        problems = body.problems()
        if problems:
            raise ClaraError("unprocessable", " ".join(problems), 422)
        timeout = httpx.Timeout(10.0, read=self.read_timeout, write=60.0, pool=10.0)
        request = self._http.build_request("POST", "/v1/chat/stream", json=body.to_json(), timeout=timeout)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.ReadTimeout:
            raise StreamSilent(self.read_timeout) from None
        except httpx.TimeoutException:
            raise ConnectionFailed(f"Clara at {self.url} did not answer in time.") from None
        except httpx.TransportError as error:
            raise ConnectionFailed(f"Cannot reach Clara at {self.url} ({type(error).__name__}).") from None
        try:
            if response.is_error:
                await response.aread()
                raise error_from_response(response)
            async for event in read_events(response.aiter_lines()):
                yield event
        except httpx.ReadTimeout:
            raise StreamSilent(self.read_timeout) from None
        except httpx.TransportError as error:
            raise ConnectionFailed(f"The connection to Clara broke ({type(error).__name__}).") from None
        finally:
            await response.aclose()

    async def send_tool_results(self, turn_id: str, results: Mapping[str, str]) -> None:
        """All the results of a round in one post, each tied to its call id."""
        body = {"results": [{"id": call_id, "content": content} for call_id, content in results.items()]}
        await self._call("POST", f"/v1/turns/{turn_id}/tool-results", body=body)
