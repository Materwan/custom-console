"""Slash commands about the conversation itself: clear, restore, undo, retry, versions, compact, init, plan, todo, exit."""

from __future__ import annotations

from ...core.prompt import INIT_REQUEST
from .base import SlashContext, SlashSpec, count, split_args, stamp


async def _exit(ctx: SlashContext, args: str) -> None:
    ctx.screen.exit()


async def _clear(ctx: SlashContext, args: str) -> None:
    words = split_args(args)
    unknown = [w for w in words if w not in ("--incognito", "--local-only")]
    if unknown:
        ctx.fail(f"unknown option {unknown[0]!r}. Usage: /clear [--incognito] [--local-only]")
        return
    session = ctx.agent.new_session(incognito="--incognito" in words, local_only="--local-only" in words)
    ctx.screen.clear_transcript()
    ctx.screen.status_changed()
    kind = [name for name, on in (("incognito", session.incognito), ("local only", session.local_only)) if on]
    ctx.say("New conversation" + (f" ({', '.join(kind)})" if kind else "") + ".", "dim")


async def _restore(ctx: SlashContext, args: str) -> None:
    word = args.strip()
    saved = ctx.agent.restorable()
    if word in ("", "list"):
        if not saved:
            ctx.say("No saved session for this folder yet.", "dim")
            return
        ctx.say("Sessions of this folder, newest first. `/restore N` brings one back.")
        ctx.table([(str(i), s.saved_title, stamp(s.updated), s.model or "default model",
                    f"{count(s.prompt_tokens + s.completion_tokens)} tokens") for i, s in enumerate(saved, 1)],
                  ("yellow", "", "dim", "dim", "dim"))
        return
    if not word.isdigit():
        ctx.fail("usage: /restore [list|N]")
        return
    restored = await ctx.agent.restore(int(word))
    ctx.screen.clear_transcript()
    ctx.screen.replay(restored.messages)
    ctx.screen.status_changed()
    ctx.say(f"Restored: {restored.session.saved_title}. Permissions, rules and trust are not part of a session.", "dim")


async def _undo(ctx: SlashContext, args: str) -> None:
    report = await ctx.agent.undo_last()
    if report.nothing:
        ctx.say("Nothing to undo: the last turn changed no file.", "dim")
        return
    for path in report.restored:
        ctx.say(f"  restored {path}", "green")
    for path in report.deleted:
        ctx.say(f"  deleted {path} (it did not exist before)", "green")
    for path in report.unchanged:
        ctx.say(f"  {path} is already as it was", "dim")
    for path, why in report.skipped:
        ctx.say(f"  skipped {path}: {why}", "yellow")
    for path, why in report.failed:
        ctx.say(f"  could not restore {path}: {why}", "red")
    if not report.clean:
        ctx.say("What was skipped or failed is still recorded: /undo again once it is sorted out.", "dim")
    ctx.say("Commands run with local_run cannot be undone.", "dim")


async def _retry(ctx: SlashContext, args: str) -> None:
    if ctx.agent.session.conversation is None:
        ctx.fail("there is no message to answer again yet.")
        return
    ctx.say("Answering again as a new version. This answer has no local tools (Clara does not allow them for a new version).", "dim")
    ctx.screen.start_turn("", retry=True)


async def _version(ctx: SlashContext, args: str) -> None:
    word = args.strip()
    if not word:
        info = await ctx.agent.versions()
        ctx.say(f"The last question has {len(info.ids)} version{'s' if len(info.ids) != 1 else ''}; version {info.active} is shown.")
        if len(info.ids) > 1:
            ctx.say("`/version N` switches to another one.", "dim")
        return
    if not word.isdigit():
        ctx.fail("usage: /version [N]")
        return
    info = await ctx.agent.switch_version(int(word))
    ctx.say(f"Version {info.active} of {len(info.ids)}:", "dim")
    if info.answer:
        ctx.screen.show_answer(info.answer)


async def _compact(ctx: SlashContext, args: str) -> None:
    before, after = await ctx.agent.compact(args.strip())
    ctx.say(f"Compacted: {before:.0f}% to {after:.0f}% of the context.", "green")


async def _init(ctx: SlashContext, args: str) -> None:
    if not ctx.agent.trusted:
        ctx.fail("this folder is not trusted, so the agent may not write AGENT.md here. Restart `ai agent` and trust it.")
        return
    ctx.screen.start_turn(INIT_REQUEST, label="/init")


async def _plan(ctx: SlashContext, args: str) -> None:
    word = args.strip().lower()
    if word not in ("", "on", "off"):
        ctx.fail("usage: /plan [on|off]")
        return
    on = (not ctx.agent.session.plan_mode) if not word else word == "on"
    ctx.agent.set_plan_mode(on)
    ctx.screen.status_changed()
    ctx.say("Plan mode is on: only read tools are offered. `/plan off` leaves it." if on else "Plan mode is off.", "yellow" if on else "dim")


async def _todo(ctx: SlashContext, args: str) -> None:
    for line in ctx.agent.ctx.todos.render().splitlines():
        ctx.say("  " + line)


COMMANDS = (
    SlashSpec("exit", "Leave the agent console", "/exit", _exit, "Session", busy_ok=True, aliases=("quit",)),
    SlashSpec("clear", "Start a new conversation and clear the screen", "/clear [--incognito] [--local-only]", _clear, "Session"),
    SlashSpec("restore", "Bring back a saved session of this folder", "/restore [list|N]", _restore, "Session"),
    SlashSpec("undo", "Undo the file changes of the last turn", "/undo", _undo, "Session"),
    SlashSpec("retry", "Answer the last message again as a new version", "/retry", _retry, "Conversation"),
    SlashSpec("version", "Show or switch the versions of the last message", "/version [N]", _version, "Conversation"),
    SlashSpec("compact", "Compact the conversation through Clara", "/compact [focus]", _compact, "Conversation"),
    SlashSpec("init", "Create AGENT.md for this folder", "/init", _init, "Conversation"),
    SlashSpec("plan", "Read-only mode: only read tools are offered", "/plan [on|off]", _plan, "Conversation"),
    SlashSpec("todo", "Show the agent's checklist", "/todo", _todo, "Conversation", busy_ok=True),
)
