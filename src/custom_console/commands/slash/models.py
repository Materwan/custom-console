"""Slash commands that choose things: the model, the tools (local and Clara's packs) and the project."""

from __future__ import annotations

from ...core.tools.registry import PREFIX
from .base import SlashContext, SlashSpec, split_args


async def _model(ctx: SlashContext, args: str) -> None:
    word = args.strip()
    if word:
        chosen = await ctx.agent.set_model(word)
        ctx.screen.status_changed()
        ctx.say(f"Model: {chosen}" if chosen else "Model: Clara's default.", "green")
        return
    models = await ctx.agent.models()
    if not models.models:
        ctx.say("Clara lists no model.", "dim")
        return
    cells = [(m.ref, "local" if m.local else "cloud", f"{m.window // 1000}k" if m.window else "", "vision" if m.vision else "")
             for m in models.models]
    widths = [max(len(cell[i]) for cell in cells) for i in range(4)]
    rows = [(m.ref, "  ".join(c.ljust(w) for c, w in zip(cell, widths, strict=True)).rstrip()) for m, cell in zip(models.models, cells, strict=True)]
    choice = await ctx.screen.pick("Model for the next messages", [("default", "Clara's default"), *rows], ctx.agent.session.model or "default")
    if choice is not None:
        await _model(ctx, choice)


async def _tools(ctx: SlashContext, args: str) -> None:
    words = split_args(args)
    action = words[0].lower() if words else "list"
    names = words[1:]
    agent = ctx.agent
    if action == "list":
        ctx.say("Local tools in this conversation:")
        ctx.table([("on" if s.offered else "off" if s.spec.name in agent.session.disabled_tools else "--",
                    s.spec.name.removeprefix(PREFIX), s.reason) for s in agent.tool_states()], ("green", "", "dim"))
        packs = await agent.packs()
        ctx.say("Clara's tool packs:")
        ctx.table([("on" if p.on else "off" if p.pack.available else "--", p.pack.id,
                    p.pack.reason if not p.pack.available else p.pack.label) for p in packs], ("green", "", "dim"))
        ctx.say("`/tools off NAME...`, `/tools on NAME...`, `/tools reset`. `--` means it cannot be used right now.", "dim")
        return
    if action == "reset":
        agent.reset_tools()
        await agent.reset_packs()
        ctx.screen.status_changed()
        ctx.say("All tools are back to their defaults.", "green")
        return
    if action not in ("on", "off") or not names:
        ctx.fail("usage: /tools [list|on NAME...|off NAME...|reset]")
        return
    on = action == "on"
    pack_ids = None
    for name in names:
        full = name if name.startswith(PREFIX) else PREFIX + name
        if agent.registry.get(full) is not None:
            agent.set_tool(full, on)
            continue
        if pack_ids is None:
            pack_ids = {p.pack.id for p in await agent.packs()}
        if name in pack_ids:
            await agent.set_pack(name, on)
        else:
            agent.resolve_tool_name(name)  # raises with the list of tools
    ctx.screen.status_changed()
    ctx.say(f"Switched {action}: {', '.join(names)}.", "green")


async def _project(ctx: SlashContext, args: str) -> None:
    word = args.strip()
    agent = ctx.agent
    if word:
        project = await agent.set_project(word)
        ctx.screen.status_changed()
        where = " (applied to this conversation)" if agent.session.conversation else " (for new conversations)"
        ctx.say(f"Project: {project.name}{where}." if project else "No project.", "green")
        return
    projects = await agent.projects()
    if not projects:
        ctx.say("Clara has no project.", "dim")
        return
    ctx.say("Projects. `/project ID` chooses, `/project none` clears.")
    ctx.table([("*" if p.id == agent.session.project else " ", str(p.id), p.name, f"{p.files} files") for p in projects],
              ("yellow", "yellow", "", "dim"))


COMMANDS = (
    SlashSpec("model", "Show or choose the model", "/model [NAME|default]", _model, "Choices"),
    SlashSpec("tools", "Switch local tools and Clara tool packs", "/tools [list|on NAME...|off NAME...|reset]", _tools, "Choices"),
    SlashSpec("project", "Choose the Clara project for this conversation", "/project [ID|none]", _project, "Choices"),
)
