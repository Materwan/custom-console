"""The Clara server, seen from the console: a small HTTP client.

The server runs the model, keeps the memory and the conversations, and streams its answers as
Server-Sent Events. This module only speaks the protocol; `remote.py` runs a turn with it.
"""

from __future__ import annotations

import json
import socket
import threading
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


def _socket_of(response: requests.Response) -> Optional[socket.socket]:
    """The socket under a streamed response (urllib3 keeps it in private attributes)."""
    raw = response.raw
    for find in (lambda: raw._connection.sock, lambda: raw._fp.fp.raw._sock):
        try:
            sock = find()
        except AttributeError:
            continue
        if isinstance(sock, socket.socket):
            return sock
    return None


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
        password: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timezone: Optional[str] = None,
    ) -> None:
        """With a `password`, the client signs in as `user_id` when it needs a token (and again if the server
        signs it out); the token it gets is kept in memory only. Without one, `token` is a shared client token."""
        self.url = url.rstrip("/")
        self.token = token
        self.password = password
        self.admin_token = admin_token
        self.user_id = user_id
        self.user_name = user_name
        self.surface = surface
        self.timezone = timezone  # IANA name: the server tells the model the date and time in it
        self._http = session or requests.Session()
        self._streaming: Optional[requests.Response] = None  # the turn being streamed (see abort_stream)
        self._streaming_lock = threading.Lock()

    def conversation_id(self, name: str) -> str:
        """The server's id for one of this user's conversations: `console:<user>:<name>`. A user who signed in may
        only use conversations under their own account (HTTP 403 otherwise), and a client token kept to the
        console surface (CLARA_CLIENT_SURFACES) only those starting with `console:`."""
        return f"{self.surface}:{self.user_id}:{name}"

    def owns(self, conversation: str) -> bool:
        return conversation.startswith(f"{self.surface}:{self.user_id}:")

    # -- plumbing ------------------------------------------------------------------ #

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _login(self) -> str:
        """Sign in with the password: a token bound to this user and surface."""
        try:
            response = self._http.post(
                self.url + "/v1/auth/login",
                json={"username": self.user_id, "password": self.password, "surface": self.surface,
                      "device": socket.gethostname()},
                timeout=(CONNECT_TIMEOUT, 30),
            )
        except requests.ConnectionError:
            raise ClaraError(f"Cannot reach the Clara server at {self.url}.") from None
        except requests.Timeout:
            raise ClaraError(f"The Clara server at {self.url} did not answer in time.") from None
        if response.status_code >= 400:
            raise ClaraError(f"Cannot sign in as {self.user_id}: {_detail(response)}")
        self.token = response.json()["token"]
        return self.token

    def _request(
        self, method: str, path: str, *, token: Optional[str] = None, _retry: bool = True, **options: Any
    ) -> requests.Response:
        own = token is None
        if own and not self.token and self.password:
            self._login()
        token = token if token is not None else self.token
        if not token:
            raise ClaraError("No way to sign in to the Clara server: set CLARA_USER and CLARA_PASSWORD (or CLARA_TOKEN) in the .env file.")
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
        if response.status_code == 401 and own and self.password and _retry:  # signed out: sign in again, once
            response.close()
            self.token = None
            return self._request(method, path, _retry=False, **options)
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
        if self.timezone:
            body["timezone"] = self.timezone
        body.update({key: value for key, value in extra.items() if value not in (None, "", [], False)})
        return body

    def stream_turn(self, body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """The events of one turn. Closing the generator closes the connection, which makes
        the server give the turn up."""
        response = self._request("POST", "/v1/chat/stream", json=body, stream=True)
        with self._streaming_lock:
            self._streaming = response
        try:
            yield from self._events(response)
        finally:
            with self._streaming_lock:
                if self._streaming is response:
                    self._streaming = None

    def abort_stream(self) -> None:
        """Give up the turn being streamed, from any thread: the connection is shut down (the server then
        gives the turn up) and closed in the background."""
        with self._streaming_lock:
            response = self._streaming
        if response is None:
            return
        sock = _socket_of(response)
        if sock is not None:
            try:  # tells the server at once, whatever the reading thread is doing
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Closing waits for a read in progress (it holds the buffer's lock): never block the caller on it
        threading.Thread(target=self._close_quietly, args=(response,), name="stream-close", daemon=True).start()

    @staticmethod
    def _close_quietly(response: requests.Response) -> None:
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _events(response: requests.Response) -> Iterator[Dict[str, Any]]:
        """The Server-Sent Events of a streaming response; closes it when done or abandoned."""
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

    # -- reminders ------------------------------------------------------------------------------ #

    def _identity(self) -> Dict[str, str]:
        return {"surface": self.surface, "user_id": self.user_id}

    def add_reminder(self, at: str, text: str, repeat: str = "", targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Set a reminder for this user, shown on the surfaces in `targets` (none: on all of theirs). `at` is
        ISO 8601 with its offset. Returns `{"id", "text", "due_at", "repeat", "targets"}`."""
        body: Dict[str, Any] = {**self._identity(), "text": text, "at": at, "repeat": repeat}
        if self.user_name:
            body["user_name"] = self.user_name
        if targets:
            body["targets"] = list(targets)
        return self._request("POST", "/v1/reminders", json=body).json()

    def notify(self, text: str, title: str = "", targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Send this user a notification now (e.g. when a long job is done), on the surfaces in `targets`
        (none: on all of theirs)."""
        body: Dict[str, Any] = {**self._identity(), "text": text, "title": title, "targets": list(targets or [])}
        return self._request("POST", "/v1/notifications", json=body).json()

    def settings(self) -> Dict[str, Any]:
        """This user's settings on the server: `notify_after` (seconds a task takes before it notifies them when
        done; 0: never; None: not set), `notify_after_default` and `notify_after_effective`."""
        return self._request("GET", "/v1/settings", params=self._identity()).json()

    def set_notify_after(self, seconds: Optional[int]) -> Dict[str, Any]:
        """Set how long a task takes before this user is notified when it is done (0: never; None: the server's
        default). Returns the settings."""
        body: Dict[str, Any] = {**self._identity(), "notify_after": seconds}
        if self.user_name:
            body["user_name"] = self.user_name
        return self._request("PATCH", "/v1/settings", json=body).json()

    def models(self) -> Dict[str, Any]:
        """The models this user may choose (`models`: `ref`, `name`, `provider_label`, `weight`, the credits a token
        costs), the server's own (`default`), what they chose (`choices`, by surface) and the model in use
        (`current`)."""
        return self._request("GET", "/v1/models", params=self._identity()).json()

    def choose_model(self, ref: Optional[str]) -> Dict[str, Any]:
        """Choose the model Clara answers this user with in the console (None: the server's own)."""
        return self._request("PUT", "/v1/models/choice", json={**self._identity(), "model": ref}).json()

    def reminders(self) -> List[Dict[str, Any]]:
        """This user's reminders that have not fired yet."""
        return self._request("GET", "/v1/reminders", params=self._identity()).json()["reminders"]

    def cancel_reminder(self, reminder_id: int) -> None:
        self._request("DELETE", f"/v1/reminders/{reminder_id}", params=self._identity())

    def reminder_events(self) -> Iterator[Dict[str, Any]]:
        """What the server announces to this user, for as long as the connection holds: ``reminder`` and
        ``notification`` events (what came while this client was away first) and ``server`` events
        (``state``: running, stopping or stopped). Closing the generator closes the connection."""
        response = self._request("GET", "/v1/notifications/stream", params=self._identity(), stream=True)
        for event in self._events(response):
            if event.get("type") in ("reminder", "notification", "server"):
                yield event

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
        """Run a command of the server's console (/provider, /model...); needs the admin token, or to be signed
        in as an administrator."""
        if not self.admin_token and not self.password:
            raise ClaraError("No admin token: set CLARA_ADMIN_TOKEN in the .env file to use the server's commands.")
        response = self._request("POST", "/v1/admin/command", token=self.admin_token, json={"line": line})
        return response.json().get("output", "")

    def admin_commands(self) -> List[Dict[str, Any]]:
        """The server's console commands, with the values they complete."""
        if not self.admin_token and not self.password:
            return []
        try:
            return self._request("GET", "/v1/admin/commands", token=self.admin_token).json()
        except ClaraError:
            return []  # signed in as a user who is not an administrator
