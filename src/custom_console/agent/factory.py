"""Builds the agno agent (imports agno lazily: it is a heavy dependency)."""

from __future__ import annotations

import json
from typing import Any, Callable, List, Optional

from ..settings import Settings
from .context import estimate_tokens

HISTORY_RUNS = 20  # past turns kept in the context; compaction normally acts before this
TOOL_CALLS_FROM_HISTORY = 8  # older tool results are dropped from the history


def build_model(
    settings: Settings,
    name: str,
    num_ctx: Optional[int] = None,
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> Any:
    """The agno model `name` of `provider` (see `llm.providers`). `num_ctx` is the
    context window to request (local Ollama models only: Ollama otherwise picks its
    own, often much smaller, default)."""
    from ..llm.providers import get_provider

    return get_provider(provider).model(settings, name, api_key, num_ctx)


def build_agent(
    settings: Settings,
    model: str,
    name: str,
    tools: List[Callable[..., Any]],
    tool_hook: Callable[..., Any],
    memory: bool = True,
    num_ctx: Optional[int] = None,
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> Any:
    """An agno Agent on the model `model` of `provider`.

    With `memory`, conversation history and long-term memories are kept in the
    SQLite file ``settings.agent_db_path`` (created on first use). Without it the
    conversation still has a history, but only in memory and only for this run.
    """
    from agno.agent import Agent
    from agno.db.in_memory import InMemoryDb
    from agno.db.sqlite import SqliteDb

    if memory:
        settings.agent_db_path.parent.mkdir(parents=True, exist_ok=True)
        db: Any = SqliteDb(db_file=str(settings.agent_db_path))
    else:
        db = InMemoryDb()

    return Agent(
        model=build_model(settings, model, num_ctx, provider, api_key),
        name=name,
        db=db,
        add_history_to_context=True,
        num_history_runs=HISTORY_RUNS,
        max_tool_calls_from_history=TOOL_CALLS_FROM_HISTORY,
        update_memory_on_run=memory,
        tools=tools,
        instructions=settings.load_instructions(),
        use_instruction_tags=True,
        markdown=True,
        tool_hooks=[tool_hook],
    )


def build_subagent(
    settings: Settings,
    model: str,
    tools: List[Callable[..., Any]],
    tool_hook: Callable[..., Any],
    instructions: str,
    num_ctx: Optional[int] = None,
    tool_call_limit: int = 40,
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> Any:
    """A throwaway agno Agent for the `task` tool: no history, no memory, its own tools."""
    from agno.agent import Agent

    return Agent(
        model=build_model(settings, model, num_ctx, provider, api_key),
        name="sub-agent",
        tools=tools,
        instructions=instructions,
        tool_hooks=[tool_hook],
        tool_call_limit=tool_call_limit,
        markdown=False,
    )


def tools_token_estimate(tools: List[Callable[..., Any]]) -> int:
    """Rough size of the tool definitions sent with every request."""
    from agno.tools.function import Function

    total = 0
    for tool in tools:
        try:
            total += estimate_tokens(json.dumps(Function.from_callable(tool).to_dict(), default=str))
        except Exception:
            total += estimate_tokens(tool.__doc__ or "") + 40
    return total
