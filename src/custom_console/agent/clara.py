"""The Clara server, seen from the console: a small HTTP client.

The server runs the model, keeps the memory and the conversations, and streams its answers as
Server-Sent Events. This module only speaks the protocol; `remote.py` runs a turn with it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

import requests
import urllib3

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 90  # the server sends a keepalive every 15 s, so silence this long means trouble


class ClaraError(Exception):
    """The server could not be reached, or refused or failed the request."""


class NothingToCompact(ClaraError, LookupError):
    pass


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        return str(body.get("detail", body)) if isinstance(body, dict) else str(body)
    except ValueError:
        return response.text.strip()[:300] or response.reason or "error"


class ClaraClient:
    def __init__(
        self,
        url: str,
        token: Optional[str],
        *,
        user_id: str,
        user_name: Optional[str] = None,
        surface: str = "console",
        admin_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.admin_token = admin_token
        self.user_id = user_id
        self.user_name = user_name
        self.surface = surface
        self._http = session or requests.Session()

    # -- plumbing ------------------------------------------------------------------ #

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(self, method: str, path: str, *, token: Optional[str] = None, **options: Any) -> requests.Response:
        token = token if token is not None else self.token
        if not token:
            raise ClaraError("No token for the Clara server: set CLARA_TOKEN in the .env file.")
        try:
            response = self._http.request(
                method,
                self.url + path,
                headers=self._headers(token),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                **options,
            )
        except requests.ConnectionError:
            raise ClaraError(f"Cannot reach the Clara server at {self.url}.") from None
        except requests.Timeout:
            raise ClaraError(f"The Clara server at {self.url} did not answer in time.") from None
        if response.status_code >= 400:
            raise ClaraError(f"Clara server: {_detail(response)} (HTTP {response.status_code})")
        return response

    # -- the conversation ------------------------------------------------------------ #

    def body(self, message: str, conversation: str, **extra: Any) -> Dict[str, Any]:
        """The request of a turn: who speaks, in which conversation, plus tools, instructions..."""
        body: Dict[str, Any] = {
            "surface": self.surface,
            "user_id": self.user_id,
            "message": message,
            "conversation": conversation,
        }
        if self.user_name:
            body["user_name"] = self.user_name
        body.update({key: value for key, value in extra.items() if value not in (None, "", [], False)})
        return body

    def stream_turn(self, body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """The events of one turn. Closing the generator closes the connection, which makes
        the server give the turn up."""
        response = self._request("POST", "/v1/chat/stream", json=body, stream=True)
        try:
            # Line by line, as soon as each arrives: `iter_lines` would wait for a full 512-byte
            # chunk, which delays every event of a server that does not send chunked responses.
            while True:
                raw = response.raw.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data: "):
                    yield json.loads(line[6:])
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as error:
            raise ClaraError(f"The connection to the Clara server broke: {error}") from None
        finally:
            response.close()

    def send_results(self, turn_id: str, results: List[Dict[str, str]]) -> None:
        """Answer a `tool_requests` event: `[{"id": ..., "content": ...}]`."""
        self._request("POST", f"/v1/turns/{turn_id}/tool-results", json={"results": results})

    # -- conversations ------------------------------------------------------------------ #

    def context(self, conversation: str) -> Dict[str, Any]:
        """`{"tokens", "window", "percent", "summary", "messages"}` of a conversation."""
        return self._request("GET", f"/v1/conversations/{conversation}").json()

    def compact(self, conversation: str, focus: str = "") -> Dict[str, Any]:
        try:
            return self._request("POST", f"/v1/conversations/{conversation}/compact", json={"focus": focus}).json()
        except ClaraError as error:
            if "HTTP 409" in str(error):
                raise NothingToCompact("the conversation is empty: nothing to compact") from None
            raise

    def forget(self, conversation: str) -> None:
        self._request("DELETE", f"/v1/conversations/{conversation}")

    # -- the server itself ----------------------------------------------------------------- #

    def health(self) -> Dict[str, Any]:
        """`{"status", "provider", "model"}`; no token needed."""
        try:
            response = self._http.get(self.url + "/health", timeout=(CONNECT_TIMEOUT, 10))
        except requests.RequestException:
            raise ClaraError(f"Cannot reach the Clara server at {self.url}.") from None
        if response.status_code >= 400:
            raise ClaraError(f"Clara server: {_detail(response)} (HTTP {response.status_code})")
        return response.json()

    def admin(self, line: str) -> str:
        """Run a command of the server's console (/provider, /model...); needs the admin token."""
        if not self.admin_token:
            raise ClaraError("No admin token: set CLARA_ADMIN_TOKEN in the .env file to use the server's commands.")
        response = self._request("POST", "/v1/admin/command", token=self.admin_token, json={"line": line})
        return response.json().get("output", "")

    def admin_commands(self) -> List[Dict[str, Any]]:
        """The server's console commands, with the values they complete."""
        if not self.admin_token:
            return []
        return self._request("GET", "/v1/admin/commands", token=self.admin_token).json()
