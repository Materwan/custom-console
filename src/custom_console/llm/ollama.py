"""Thin client for the local Ollama server (model listing and warm-up)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import requests


class OllamaUnavailableError(RuntimeError):
    """The Ollama server cannot be reached or answered with an error."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: Optional[int] = None  # bytes
    capabilities: List[str] = field(default_factory=list)


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
    def __init__(self, host: str = "http://localhost:11434", timeout: float = 5.0):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _get(self, endpoint: str) -> dict:
        try:
            response = requests.get(f"{self.host}{endpoint}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise OllamaUnavailableError(f"cannot reach Ollama at {self.host}: {error}") from error

    def installed(self) -> List[ModelInfo]:
        data = self._get("/api/tags")
        return [
            ModelInfo(
                name=model.get("model") or model.get("name", ""),
                size=model.get("size"),
                capabilities=list(model.get("capabilities") or []),
            )
            for model in data.get("models", [])
        ]

    def running(self) -> List[ModelInfo]:
        names = {model.get("name") for model in self._get("/api/ps").get("models", [])}
        return [model for model in self.installed() if model.name in names]

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
