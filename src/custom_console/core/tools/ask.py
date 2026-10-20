"""`local_ask_user`, `local_todo_write` and `local_task`: the tools that talk to the person, keep the checklist and start
a sub-agent. None of them changes anything on this computer."""

from __future__ import annotations

from typing import Any

from ..permissions.engine import SAFE, CallInfo
from .base import STR, Question, Todo, ToolContext, ToolError, ToolSpec, cut_middle, obj

MAX_OPTIONS = 8
MAX_TODOS = 50
STATUSES = ("pending", "in_progress", "completed")


def _inspect_ask(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_ask_user", SAFE, summary="Ask the person a question")


async def _run_ask(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.ask is None:
        raise ToolError("There is no one to ask right now. Decide on your own, or say what you need.")
    options = args.get("options") or []
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise ToolError("options must be a list of strings.")
    question = Question(str(args["question"]).strip(), tuple(options[:MAX_OPTIONS]))
    if not question.text:
        raise ToolError("The question is empty.")
    answer = await ctx.ask(question)
    return f"The person answered: {answer}" if answer else "The person gave no answer."


ASK_TOOL = ToolSpec(
    "local_ask_user",
    "Ask the person a question and wait for the answer. Give `options` (up to 8) when the answer is a choice; they can "
    "always type something else.",
    obj({"question": STR, "options": {"type": "array", "items": STR, "description": "Choices to offer"}}, ["question"]),
    SAFE, _inspect_ask, _run_ask)


def _inspect_todo(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_todo_write", SAFE, summary="Update the checklist")


def _run_todo(ctx: ToolContext, args: dict[str, Any]) -> str:
    raw = args.get("todos")
    if not isinstance(raw, list) or len(raw) > MAX_TODOS:
        raise ToolError(f"todos must be a list of at most {MAX_TODOS} items.")
    items: list[Todo] = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("content", "")).strip():
            raise ToolError("Each todo needs a non-empty content.")
        status = str(entry.get("status", "pending"))
        if status not in STATUSES:
            raise ToolError(f"status must be one of {', '.join(STATUSES)}.")
        items.append(Todo(str(entry["content"]).strip()[:300], status))
    ctx.todos.replace(items)
    return "Checklist updated.\n" + ctx.todos.render()


TODO_TOOL = ToolSpec(
    "local_todo_write",
    "Write the whole checklist for the current task (it replaces the previous one). Each item has `content` and a "
    "`status`: pending, in_progress or completed. Keep one item in_progress at a time.",
    obj({"todos": {"type": "array", "items": obj({"content": STR, "status": {**STR, "enum": list(STATUSES)}}, ["content"])}},
        ["todos"]),
    SAFE, _inspect_todo, _run_todo)


def _inspect_task(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_task", SAFE, summary=f"Sub-agent: {str(args.get('description') or args.get('prompt', ''))[:80]}")


async def _run_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.subagent is None:
        raise ToolError("A sub-agent cannot start another sub-agent.")
    prompt = str(args["prompt"]).strip()
    if not prompt:
        raise ToolError("The prompt is empty.")
    result = await ctx.subagent(prompt, ctx.cancel)
    return cut_middle(result, ctx.result_limit)


TASK_TOOL = ToolSpec(
    "local_task",
    "Hand a self-contained job (searching, reading, summarising files) to a sub-agent that can only read. It works in its "
    "own conversation and returns one text result. Describe the job completely: it sees nothing of this conversation.",
    obj({"prompt": {**STR, "description": "Everything the sub-agent needs to know and the answer you want"},
         "description": {**STR, "description": "A few words naming the job"}}, ["prompt"]),
    SAFE, _inspect_task, _run_task)

ASK_TOOLS = (ASK_TOOL, TODO_TOOL, TASK_TOOL)
