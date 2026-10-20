"""Slash commands that show things: help, context, usage, approvals, rules, journal and the transcript."""

from __future__ import annotations

import time
from typing import Any

from ...core.permissions.engine import ASK, REFUSE
from ...core.tools.base import ToolSpec
from ..base import Line
from .base import SlashContext, SlashRegistry, SlashSpec, count, split_args

ANSWER_WORDS = {
    "allow": "allow_once", "once": "allow_once", "o": "allow_once", "always": "allow_always", "a": "allow_always",
    "refuse": "refuse_once", "no": "refuse_once", "r": "refuse_once", "never": "refuse_always", "n": "refuse_always",
}
KEYS = (
    ("Enter", "send the message"), ("Shift+Enter or Ctrl+J", "add a line"), ("Esc", "stop the answer or the command running"),
    ("Up / Down", "walk the history of what you sent"), ("Tab", "complete a command, a path or an @file"),
    ("Ctrl+V", "paste text, or an image from the clipboard"), ("Ctrl+D", "leave (when the box is empty)"),
)


def make_help(registry: SlashRegistry) -> SlashSpec:
    async def run(ctx: SlashContext, args: str) -> None:
        ctx.say("Keys", "bold")
        ctx.table(KEYS, ("cyan", ""))
        ctx.say("In a message", "bold")
        ctx.table([("@path", "send a file or a folder with the message (PDF, Word and images are attached)"),
                   ("!command", "run a command now, as you; its output goes with your next message"),
                   ("// and !!", "start a message with a real / or !"), ("/ls, /cat ...", "the shell's own commands work with a slash")],
                  ("cyan", ""))
        groups: dict[str, list[SlashSpec]] = {}
        for spec in registry.specs():
            groups.setdefault(spec.group, []).append(spec)
        for group, specs in groups.items():
            ctx.say(group, "bold")
            ctx.table([(spec.usage, spec.summary) for spec in specs], ("cyan", ""))

    return SlashSpec("help", "Show the keys and the commands", "/help", run, "Session", busy_ok=True, aliases=("?",))


def percent_bar(percent: float, width: int = 24) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "█" * filled + "░" * (width - filled)


async def _context(ctx: SlashContext, args: str) -> None:
    report = ctx.agent.context_report()
    ctx.say(f"Model: {report.model or 'Clara chooses'}")
    if report.window:
        ctx.say(f"Context: {report.percent:.0f}% full  {percent_bar(report.percent)}  "
                f"{count(report.used_tokens)} of {count(report.window)} tokens (after the last answer)")
    else:
        ctx.say("Context: Clara reports it after the first answer.", "dim")
    if warning := ctx.agent.project_warning():
        ctx.say(warning, "yellow")
    ctx.say("Sent with every message (estimates, 4 characters to a token):", "dim")
    ctx.table([("instructions", f"~{count(report.instructions_tokens)} tokens"),
               ("AGENT.md", f"~{count(report.project_tokens)} tokens" if report.project_tokens else "none"),
               ("tool schemas", f"~{count(report.tools_tokens)} tokens ({report.tools_offered} tools)")], ("", "dim"))


async def _usage(ctx: SlashContext, args: str) -> None:
    word = args.strip().lower()
    names = [word] if word else ["session", "24h", "7d", "all"]
    rows = [("", "prompt", "answer", "cached", "model calls", "turns")]
    for name in names:
        totals = ctx.agent.usage(name)
        rows.append((name, count(totals.prompt), count(totals.completion), count(totals.cached), count(totals.calls),
                     f"{totals.turns} ({totals.failed_turns} failed, {totals.cancelled_turns} stopped)"))
    ctx.say("Tokens counted from Clara's usage events:")
    ctx.table(rows, ("yellow", "", "", "dim", "dim", "dim"))
    if len(names) == 1:
        totals = ctx.agent.usage(names[0])
        for agent, used in sorted(totals.by_agent.items()):
            ctx.say(f"  {agent}: {count(used)} tokens", "dim")


async def _approvals(ctx: SlashContext, args: str) -> None:
    words = split_args(args)
    if not words:
        found = await ctx.agent.approvals()
        if not found:
            ctx.say("Clara has no request waiting for an answer.", "dim")
            return
        ctx.say("Waiting for an answer. `/approvals N allow|always|refuse|never` answers one.")
        ctx.table([(f"#{a.id}", a.tool, a.summary or a.target, a.level) for a in found], ("yellow", "cyan", "", "dim"))
        return
    if len(words) != 2 or not words[0].lstrip("#").isdigit() or words[1].lower() not in ANSWER_WORDS:
        ctx.fail("usage: /approvals [N allow|always|refuse|never]")
        return
    approval = await ctx.agent.decide_approval(int(words[0].lstrip("#")), ANSWER_WORDS[words[1].lower()])
    ctx.say(f"Request #{approval.id} ({approval.tool}): {approval.status or words[1]}.", "green")


def _typed(spec: ToolSpec | None, key: str, raw: str) -> Any:
    kind = (spec.parameters.get("properties", {}).get(key, {}).get("type") if spec else None) or "string"
    if kind == "integer" and raw.lstrip("-").isdigit():
        return int(raw)
    if kind == "boolean":
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


async def _rules(ctx: SlashContext, args: str) -> None:
    words = split_args(args)
    agent = ctx.agent
    if words and words[0] == "remove":
        if len(words) != 2 or not words[1].isdigit():
            ctx.fail("usage: /rules remove N")
            return
        ctx.say("Removed." if agent.rules.remove(int(words[1])) else f"No local rule {words[1]}.", "green")
        return
    if words and words[0] == "test":
        await _test_rule(ctx, words[1:])
        return
    if words:
        ctx.fail("usage: /rules [test TOOL key=value ... [--tainted]] [remove N]")
        return
    report = await agent.rule_report()
    ctx.say("Local rules (what you answered \"always\" or \"never\" to here):")
    if report.local:
        ctx.table([(str(r.id), r.effect, r.key.describe(), f"used {r.hits}x") for r in report.local],
                  ("yellow", "green", "", "dim"))
    else:
        ctx.say("  none", "dim")
    ctx.say("Clara's rules (read-only; they are changed on the server):")
    if report.clara is None:
        ctx.say(f"  could not be read: {report.clara_error}", "yellow")
    elif report.clara.rules:
        ctx.table([(f"#{r.id}", r.effect, f"{r.tool} {r.target}".strip(), r.note or r.situation) for r in report.clara.rules],
                  ("yellow", "green", "", "dim"))
    else:
        ctx.say("  none", "dim")
    ctx.say("`/rules test local_write path=a.txt` shows what a call would do. `/rules remove N` deletes a local rule.", "dim")


async def _test_rule(ctx: SlashContext, words: list[str]) -> None:
    tainted = "--tainted" in words
    words = [w for w in words if w != "--tainted"]
    if not words:
        ctx.fail("usage: /rules test TOOL key=value ... [--tainted]")
        return
    tool, pairs = words[0], words[1:]
    bad = [p for p in pairs if "=" not in p]
    if bad:
        ctx.fail(f"{bad[0]!r} is not key=value")
        return
    agent = ctx.agent
    if tool.startswith("local_") or agent.registry.get("local_" + tool) is not None:
        spec = agent.registry.get(tool if tool.startswith("local_") else "local_" + tool)
        arguments = {k: _typed(spec, k, v) for k, v in (p.split("=", 1) for p in pairs)}
        decision = agent.decide_local(tool, arguments, tainted=tainted)
        style = "red" if decision.effect == REFUSE else "yellow" if decision.effect == ASK else "green"
        ctx.say(f"{decision.effect.upper()} (step: {decision.step}): {decision.reason}", style)
        for shown in decision.paths:
            ctx.say(f"  {shown.mode}: {shown.virtual}" + (f"  (really {shown.real})" if shown.redirected else ""), "dim")
        if decision.effect == ASK:
            ctx.say("  \"always\" can be remembered for this call." if decision.can_remember
                    else f"  \"always\" is not offered: {decision.why_not_remembered}.", "dim")
        return
    answer = await agent.test_clara_rule(tool, dict(p.split("=", 1) for p in pairs), tainted=tainted)
    ctx.say(f"Clara would: {answer.get('effect', '?')} - {answer.get('why', '')}")


_KIND_STYLE = {"turn": "cyan", "tool": "", "approval": "yellow", "error": "red"}


def journal_lines(records: list[dict[str, Any]]) -> list[Line]:
    lines: list[Line] = []
    for record in records:
        when = time.strftime("%H:%M:%S", time.localtime(float(record.get("t", 0))))
        kind = str(record.get("kind", "?"))
        if kind == "turn":
            detail = f"{record.get('outcome')}  {record.get('seconds')}s  {record.get('model')}"
        elif kind == "tool":
            detail = (f"{record.get('name')}  {record.get('decision')} ({record.get('step')}"
                      f"{', ' + str(record.get('answer')) if record.get('answer') else ''})  "
                      f"{'ok' if record.get('ok') else 'failed'}  {record.get('seconds')}s\n    {_brief(record.get('arguments'))}")
        elif kind == "approval":
            detail = f"#{record.get('id')} {record.get('tool')} -> {record.get('answer')} (by {record.get('by')})"
        elif kind == "error":
            detail = f"{record.get('code')}: {record.get('message')}"
        else:
            detail = str({k: v for k, v in record.items() if k not in ("t", "kind")})
        lines.append(Line.join((when + "  ", "dim"), (f"{kind:<9}", _KIND_STYLE.get(kind, "")), (detail, "")))
    return lines


def _brief(arguments: Any) -> str:
    text = str(arguments)
    return text if len(text) <= 160 else text[:157] + "..."


async def _journal(ctx: SlashContext, args: str) -> None:
    records = ctx.agent.journal_records()
    if not records:
        ctx.say("The journal of this session is empty so far.", "dim")
        return
    ctx.screen.view(f"Journal - session {ctx.agent.session.id}", journal_lines(records))


async def _transcript(ctx: SlashContext, args: str) -> None:
    lines: list[Line] = []
    for message in await ctx.agent.transcript():
        who = "You" if message.role == "user" else "Clara" if message.role == "assistant" else message.role
        lines.append(Line.of(f"-- {who} --", "bold cyan" if message.role == "user" else "bold green"))
        lines.extend(Line.of(text) for text in (message.content.splitlines() or [""]))
        lines.append(Line.of(""))
    if not lines:
        ctx.say("The conversation is empty so far.", "dim")
        return
    ctx.screen.view(f"Transcript - {ctx.agent.session.saved_title}", lines)


COMMANDS = (
    SlashSpec("context", "How full the context window is", "/context", _context, "Information", busy_ok=True),
    SlashSpec("usage", "Tokens used: this session, 24 hours, 7 days, all", "/usage [session|24h|7d|all]", _usage, "Information",
              busy_ok=True),
    SlashSpec("approvals", "List Clara's pending requests, or answer one", "/approvals [N allow|always|refuse|never]", _approvals,
              "Permissions", busy_ok=True),
    SlashSpec("rules", "Show the rules, test what a call would do", "/rules [test TOOL key=value ...] [remove N]", _rules,
              "Permissions", busy_ok=True),
    SlashSpec("journal", "Open the journal of this session", "/journal", _journal, "Information", busy_ok=True),
    SlashSpec("transcript", "Open the whole conversation, with search", "/transcript", _transcript, "Information"),
)
