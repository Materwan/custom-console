"""Thin client for an Ollama server (model listing and warm-up): the local one,
or ollama.com's API with an API key."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

import requests

from .errors import ProviderUnavailableError


class OllamaUnavailableError(ProviderUnavailableError):
    """The Ollama server cannot be reached or answered with an error."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: Optional[int] = None  # bytes
    capabilities: List[str] = field(default_factory=list)
    context_length: Optional[int] = None  # largest context window the model supports
    remote: bool = False  # served by ollama.com: nothing to load locally


def format_size(size: Optional[int]) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover


def search_model(name: str, models: List[ModelInfo]) -> Optional[str]:
    """Resolve `name` among `models`.

    ``llama3:8b`` must match exactly; a bare ``llama3`` matches the first model
    with that base name, whatever its tag.
    """
    base, _, tag = name.partition(":")
    for model in models:
        if tag:
            if model.name == name:
                return model.name
        elif model.name.split(":")[0] == base:
            return model.name
    return None


class OllamaClient:
    """`api_key` is for ollama.com's API; there every model is `remote` (served there)."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        timeout: float = 5.0,
        *,
        api_key: Optional[str] = None,
        remote: bool = False,
    ):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.remote = remote

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        try:
            response = requests.request(
                method, f"{self.host}{endpoint}", headers=self._headers(), timeout=self.timeout, **kwargs
            )
            if response.status_code in (401, 403):
                raise OllamaUnavailableError(f"{self.host} refused the API key (HTTP {response.status_code})")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise OllamaUnavailableError(f"cannot reach Ollama at {self.host}: {error}") from error

    def _get(self, endpoint: str) -> dict:
        return self._request("GET", endpoint)

    def installed(self) -> List[ModelInfo]:
        data = self._get("/api/tags")
        return [
            ModelInfo(
                name=model.get("model") or model.get("name", ""),
                size=model.get("size") or None,
                capabilities=list(model.get("capabilities") or []),
                context_length=(model.get("details") or {}).get("context_length"),
                remote=self.remote or bool(model.get("remote_host")),
            )
            for model in data.get("models", [])
        ]

    def context_length(self, name: str) -> Optional[int]:
        """The model's largest context window, from ``/api/show`` (None if unknown)."""
        try:
            details = self._request("POST", "/api/show", json={"model": name}).get("model_info") or {}
        except OllamaUnavailableError:
            return None
        for key, value in details.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
        return None

    def running(self) -> List[ModelInfo]:
        names = {model.get("name") for model in self._get("/api/ps").get("models", [])}
        return [model for model in self.installed() if model.name in names]

    def info(self, name: str) -> Optional[ModelInfo]:
        """Installed model matching `name` (see :func:`search_model`), or None."""
        models = self.installed()
        match = search_model(name, models)
        model = next((model for model in models if model.name == match), None)
        if model is not None and model.remote and not model.context_length and self.remote:
            model = replace(model, context_length=self.context_length(model.name))
        return model

    def find(self, name: str) -> Optional[str]:
        """Installed model matching `name`, or None."""
        return search_model(name, self.installed())

    def is_running(self, name: str) -> bool:
        return search_model(name, self.running()) is not None

    def start(self, name: str) -> None:
        """Load `name` in memory and keep it there."""
        import ollama

        try:
            ollama.Client(host=self.host).generate(model=name, prompt="", keep_alive=-1)
        except Exception as error:  # ollama.ResponseError, httpx errors, ...
            raise OllamaUnavailableError(f"cannot start {name}: {error}") from error
