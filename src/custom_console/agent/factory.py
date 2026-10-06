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

    With `memory`, the conversation history is kept in the SQLite file
    ``settings.agent_db_path`` (created on first use), and so are the long-term
    memories when ``AGENT_LONG_TERM_MEMORY`` is on.
    """
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.ollama import Ollama

    db = None
    if memory:
        settings.agent_db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(settings.agent_db_path))

    # Keep everything before the history byte-stable (tools, instructions: no date, no
    # per-turn text) so that Ollama can reuse its prompt cache from one request to the next.
    return Agent(
        model=Ollama(
            id=model, host=settings.ollama_host, options={"num_predict": settings.agent_max_output_tokens}
        ),
        name=name,
        db=db,
        add_history_to_context=memory,
        num_history_runs=2,
        max_tool_calls_from_history=settings.agent_history_tool_calls,
        update_memory_on_run=memory and settings.agent_long_term_memory,
        tools=tools,
        instructions=settings.load_instructions(),
        use_instruction_tags=True,
        markdown=True,
        tool_hooks=[tool_hook],
    )
