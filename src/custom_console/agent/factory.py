"""Builds the agno agent (imports agno lazily: it is a heavy dependency)."""

from __future__ import annotations

from typing import Any, Callable, List

from ..settings import Settings


def build_agent(
    settings: Settings,
    model: str,
    name: str,
    tools: List[Callable[..., Any]],
    tool_hook: Callable[..., Any],
    memory: bool = True,
) -> Any:
    """An agno Agent on an Ollama model.

    With `memory`, conversation history and long-term memories are kept in the
    SQLite file ``settings.agent_db_path`` (created on first use).
    """
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.ollama import Ollama

    db = None
    if memory:
        settings.agent_db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(settings.agent_db_path))

    return Agent(
        model=Ollama(id=model, host=settings.ollama_host),
        name=name,
        db=db,
        add_history_to_context=memory,
        num_history_runs=2,
        update_memory_on_run=memory,
        tools=tools,
        instructions=settings.load_instructions(),
        use_instruction_tags=True,
        markdown=True,
        tool_hooks=[tool_hook],
    )
