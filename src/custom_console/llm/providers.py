"""The model providers the agent can use.

* ``ollama``       – Ollama on this computer (``OLLAMA_HOST``).
* ``ollama-cloud`` – Ollama's API on ollama.com, with an API key (``OLLAMA_API_KEY``).
* ``chatgpt``      – the OpenAI API, with an API key (``OPENAI_API_KEY``). A ChatGPT
  subscription cannot be used from code: the API is billed apart, per token.

A provider lists its models through a *catalog* (``installed()`` and ``info(name)``,
like :class:`OllamaClient`), builds the agno model the agent talks to, and answers
plain chat requests (used to summarise the conversation).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

import requests

from .errors import ProviderUnavailableError
from .keys import KeyStore
from .ollama import ModelInfo, OllamaClient, format_size, search_model

if TYPE_CHECKING:
    from ..settings import Settings

Messages = List[Dict[str, str]]  # [{"role": "system" | "user", "content": "..."}]


class Provider:
    name = ""
    label = ""
    key_variable: Optional[str] = None  # environment variable (and saved key) holding the API key
    key_url = ""  # where to get a key
    family = "ollama"  # providers of a family read each other's conversations
    local = False  # models run on this computer (they are loaded, and their window is ours to set)

    @property
    def needs_key(self) -> bool:
        return self.key_variable is not None

    def catalog(self, settings: "Settings", key: Optional[str]) -> Any:
        raise NotImplementedError

    def model(self, settings: "Settings", name: str, key: Optional[str], num_ctx: Optional[int] = None) -> Any:
        """The agno model of the agent."""
        raise NotImplementedError

    def chat(self, settings: "Settings", name: str, key: Optional[str], messages: Messages, num_ctx: Optional[int] = None) -> str:
        """One answer, without tools or streaming."""
        raise NotImplementedError

    def default_model(self, settings: "Settings") -> str:
        raise NotImplementedError

    def where(self, info: ModelInfo) -> str:
        """Where a model runs, for the /model table."""
        return "cloud" if info.remote else "local"


class OllamaLocal(Provider):
    name = "ollama"
    label = "Ollama (this computer)"
    local = True

    def catalog(self, settings, key):
        return OllamaClient(settings.ollama_host)

    def model(self, settings, name, key, num_ctx=None):
        from agno.models.ollama import Ollama

        # api_key=None: agno would otherwise send OLLAMA_API_KEY and go to ollama.com.
        return Ollama(id=name, host=settings.ollama_host, api_key=None, options={"num_ctx": num_ctx} if num_ctx else None)

    def chat(self, settings, name, key, messages, num_ctx=None):
        import ollama

        response = ollama.Client(host=settings.ollama_host).chat(
            model=name, messages=messages, options={"num_ctx": num_ctx} if num_ctx else None
        )
        return response["message"]["content"] or ""

    def default_model(self, settings):
        return settings.default_model


class OllamaCloud(Provider):
    name = "ollama-cloud"
    label = "Ollama API (ollama.com)"
    key_variable = "OLLAMA_API_KEY"
    key_url = "https://ollama.com/settings/keys"

    def catalog(self, settings, key):
        return OllamaClient(settings.ollama_cloud_host, timeout=15.0, api_key=key, remote=True)

    def model(self, settings, name, key, num_ctx=None):
        from agno.models.ollama import Ollama

        return Ollama(id=name, host=settings.ollama_cloud_host, api_key=key)

    def chat(self, settings, name, key, messages, num_ctx=None):
        import ollama

        client = ollama.Client(host=settings.ollama_cloud_host, headers={"Authorization": f"Bearer {key}"})
        return client.chat(model=name, messages=messages)["message"]["content"] or ""

    def default_model(self, settings):
        return settings.ollama_cloud_default_model

    def where(self, info):
        return "ollama.com"


class ChatGPT(Provider):
    name = "chatgpt"
    label = "ChatGPT (OpenAI API)"
    key_variable = "OPENAI_API_KEY"
    key_url = "https://platform.openai.com/api-keys"
    family = "openai"

    def catalog(self, settings, key):
        return OpenAICatalog(settings.openai_base_url, key)

    def model(self, settings, name, key, num_ctx=None):
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=name, api_key=key, base_url=settings.openai_base_url)

    def chat(self, settings, name, key, messages, num_ctx=None):
        import openai

        client = openai.OpenAI(api_key=key, base_url=settings.openai_base_url)
        response = client.chat.completions.create(model=name, messages=messages)  # type: ignore[arg-type]
        return response.choices[0].message.content or ""

    def default_model(self, settings):
        return settings.openai_default_model

    def where(self, info):
        return "OpenAI"


PROVIDERS: Dict[str, Provider] = {provider.name: provider for provider in (OllamaLocal(), OllamaCloud(), ChatGPT())}
ALIASES = {
    "local": "ollama",
    "ollama-local": "ollama",
    "cloud": "ollama-cloud",
    "ollama-api": "ollama-cloud",
    "openai": "chatgpt",
    "gpt": "chatgpt",
}


def get_provider(name: str) -> Provider:
    key = (name or "").strip().lower()
    provider = PROVIDERS.get(ALIASES.get(key, key))
    if provider is None:
        raise LookupError(f"unknown provider {name!r}: choose {', '.join(PROVIDERS)}")
    return provider


# --------------------------------------------------------------------------- #
# The OpenAI models
# --------------------------------------------------------------------------- #

CHAT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4")
# Models that are not chat models, or only work through another API than chat completions.
NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation",
            "codex", "computer-use", "deep-research", "-pro")
SNAPSHOT = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")  # gpt-4o-2024-08-06, gpt-3.5-turbo-0125

# Context windows by model prefix, most specific first (the API does not tell them).
CONTEXT_WINDOWS = (
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_385),
    ("o1-mini", 128_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
)
DEFAULT_OPENAI_WINDOW = 128_000


def openai_context_window(name: str) -> int:
    for prefix, window in CONTEXT_WINDOWS:
        if name.startswith(prefix):
            return window
    return DEFAULT_OPENAI_WINDOW


def is_chat_model(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(CHAT_PREFIXES) and not any(word in lowered for word in NOT_CHAT)


class OpenAICatalog:
    """The chat models the account can use, from the API (``GET /models``)."""

    def __init__(self, base_url: str, api_key: Optional[str], timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _models(self) -> List[dict]:
        try:
            response = requests.get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout
            )
            if response.status_code in (401, 403):
                raise ProviderUnavailableError(f"OpenAI refused the API key (HTTP {response.status_code})")
            response.raise_for_status()
            data = response.json().get("data") or []
        except (requests.RequestException, ValueError, AttributeError) as error:
            raise ProviderUnavailableError(f"cannot reach {self.base_url}: {error}") from error
        return sorted((m for m in data if isinstance(m, dict) and m.get("id")), key=lambda m: -int(m.get("created") or 0))

    @staticmethod
    def _info(name: str) -> ModelInfo:
        return ModelInfo(name=name, context_length=openai_context_window(name), remote=True)

    def installed(self) -> List[ModelInfo]:
        """Chat models, newest first, without the dated snapshots (they are still accepted by name).
        An OpenAI-compatible server whose models do not look like OpenAI's gets them all listed."""
        names = [m["id"] for m in self._models()]
        chat = [name for name in names if is_chat_model(name) and not SNAPSHOT.search(name)]
        return [self._info(name) for name in (chat or names)]

    def info(self, name: str) -> Optional[ModelInfo]:
        names = [m["id"] for m in self._models()]
        return self._info(name) if name in names else None


# --------------------------------------------------------------------------- #
# The choice that is remembered
# --------------------------------------------------------------------------- #


class ProviderMemory:
    """The provider used last and the last model used with each one (``provider.json``)."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.provider: Optional[str] = None
        self.models: Dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path else {}
            self.provider = str(data["provider"]) if data.get("provider") in PROVIDERS else None
            self.models = {str(k): str(v) for k, v in (data.get("models") or {}).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # missing or damaged: nothing remembered

    def last_model(self, provider: str) -> Optional[str]:
        return self.models.get(provider)

    def remember(self, provider: str, model: str) -> None:
        self.provider = provider
        self.models[provider] = model
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"provider": provider, "models": self.models}, indent=2), encoding="utf-8")
        except OSError:
            pass  # only remembered for this run


def initial_provider(settings: "Settings", memory: ProviderMemory) -> Provider:
    """The provider remembered from last time, else ``AGENT_PROVIDER``."""
    try:
        return get_provider(memory.provider or settings.agent_provider)
    except LookupError:
        return PROVIDERS["ollama"]


def preferred_model(provider: Provider, settings: "Settings", memory: ProviderMemory) -> str:
    return memory.last_model(provider.name) or provider.default_model(settings)


# --------------------------------------------------------------------------- #
# Connecting: key, then the list of models
# --------------------------------------------------------------------------- #

CatalogFactory = Callable[[Provider, Optional[str]], Any]


@dataclass
class Connection:
    provider: Provider
    key: Optional[str]
    catalog: Any
    models: List[ModelInfo]
    notes: List[str] = field(default_factory=list)


def key_prompt(provider: Provider) -> str:
    return (
        f"{provider.label} needs an API key ({provider.key_url}). Paste it: it will be saved in the "
        "Windows Credential Manager (Esc cancels)"
    )


def connect(
    provider: Provider,
    keys: KeyStore,
    ask_key: Callable[[str], Optional[str]],
    make_catalog: CatalogFactory,
) -> Optional[Connection]:
    """Get the provider's key (asked with `ask_key` when there is none), check it by
    listing the models, and save a typed key once it worked. None when the user
    gave no key; ProviderUnavailableError when the provider cannot be used."""
    key: Optional[str] = None
    typed = False
    if provider.needs_key:
        key = keys.get(provider.key_variable)  # type: ignore[arg-type]
        if not key:
            key = (ask_key(key_prompt(provider)) or "").strip()
            if not key:
                return None
            typed = True

    catalog = make_catalog(provider, key)
    try:
        models = catalog.installed()
    except ProviderUnavailableError as error:
        raise ProviderUnavailableError(f"{error}{' (the key was not saved)' if typed else ''}") from error
    if not models:
        raise ProviderUnavailableError(f"{provider.label} offers no model")

    connection = Connection(provider, key, catalog, models)
    if typed:
        try:
            keys.save(provider.key_variable, key)  # type: ignore[arg-type]
            connection.notes.append(f"Key saved in the Windows Credential Manager ({provider.key_variable}).")
        except Exception as error:
            connection.notes.append(f"The key works but could not be saved ({error}): it lasts for this run only.")
    return connection


def find_model(models: Sequence[ModelInfo], name: str) -> Optional[ModelInfo]:
    """`name` among `models` (a bare Ollama name matches any tag of it)."""
    match = search_model(name, list(models))
    return next((model for model in models if model.name == match), None)


def describe_model(info: ModelInfo) -> str:
    """``2.0 GB · 32k context``: what the model menu shows under a model."""
    parts = []
    if info.size and not info.remote:
        parts.append(format_size(info.size))
    if info.context_length:
        window = info.context_length
        parts.append(f"{window // 1000}k context" if window >= 1000 else f"{window} context")
    return " · ".join(parts)
