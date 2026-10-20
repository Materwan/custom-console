# Permissions

Everything the agent does on your computer goes through one engine (`core/permissions/engine.py`). The engine *decides*; it never asks or runs anything itself. The turn runner asks you when the decision is "ask", runs the tool when it is "allow", and answers the model with a refusal when it is "refuse".

## The tools and what class they are

| Class | Tools | Without a rule |
| --- | --- | --- |
| read | `local_list` `local_read` `local_search` `local_git` `local_pdf_to_markdown` | allowed inside the trusted folder, asks anywhere else |
| write | `local_write` `local_edit` | always asks |
| run | `local_run` | asks, unless it is on the read-only list below |
| open, launch, clipboard | `local_open_path` `local_launch_app` `local_clipboard_read` `local_clipboard_write` | always asks; the clipboard can never be remembered |
| safe | `local_job` `local_ask_user` `local_todo_write` `local_task` | changes nothing on this computer: allowed |

Write, run and git tools are not even offered to the model in a folder that is not trusted. In `/plan` mode only the read and safe tools are offered, and a call to anything else is refused.

## One decision, six steps

1. **Resolve.** Every path of the call goes through the virtual file system. Both the path *as written* and its *real* path (after links) are kept. A path that cannot be resolved, a network path (`\\server\share`), or one the resolver refuses ends the decision: refused. A stored **refusal** ("never") for exactly this call is checked next of all: saying no is always safe.
2. **Floor.** Some things always ask, whatever any rule says, and an "always" answer is not offered for them:
   - sensitive files: `.env` (not `.env.example`), `.netrc`, `.git-credentials`, key and certificate files, `id_rsa` and friends, `.ssh`, `.gnupg`, `.aws`, `.kube`, the Clara token file, browser profiles and key stores;
   - the console's own data folder and config folder;
   - any write under `.git`, and any write outside the trusted folder;
   - a command that names a network path.
   Each path is checked in both forms, so a link to a key is caught by its real path (SEC-4), and a remembered file that later becomes a link asks again.
3. **Tainted turn.** After a Clara tool read outside content (the web, GitHub, Drive, project files, conversation files), or after you attached a file or ran a `!command`, every change and every command asks, whatever the rules say. The list of Clara tools known to read nothing from outside is short (`SAFE_SERVER_TOOLS` in `core/turn.py`); a tool the console does not know counts as outside content. A sub-agent's taint is passed on to the main turn.
4. **Allowlist.** Reads inside the trusted folder; and for `local_run` a short fixed list of simple commands inside the trusted folder: `pwd ls dir cat type head tail wc grep which`, and `git status|diff|log` without the flags that run programs. "Simple" means no pipe, redirection, chaining, substitution, glob or variable.
5. **Stored answers.** An "always" answer is the tool, the exact command and *all* its paths together. It is never applied to a call that the floor or a tainted turn escalated.
6. **Ask.** The prompt shows the tool, the exact command or a diff (cut after 40 rows with "... N more lines"), every path with its real path when a link redirects it, and the answer keys, always last.

## Answers

| Key | Answer | Stored |
| --- | --- | --- |
| `o` | allow once | nothing |
| `a` | allow always | the exact call, if it can be remembered |
| `r` or Esc | refuse | nothing |
| `n` | refuse, never | the exact call |

Interpreters, installers, shells, network tools, `rm`, `mv`, `cp` and other programs whose effect depends on files that can change (`NEVER_REMEMBERED` in `allowlist.py`) can never be remembered; the prompt says why. `/rules` shows every stored rule and `/rules remove N` deletes a local one; `/rules test local_write path=a.txt` shows what a call would do without running it.

## Trusted folders

Starting `ai agent` in a new folder asks once. The answer is kept as a hash of the folder's real path. In a folder that is not trusted: no write, run or git tools, reads ask, and `AGENT.md` is not read (a file there could carry instructions meant for the model).

## Sessions cannot raise anything

A session file holds no permission, rule or trust. `/restore` therefore cannot bring back a more permissive state (SEC-7).

## `@file`, `!command`, sub-agents

- `@file` is a read like any other: inside the trusted folder it does not ask; a sensitive file, or one outside the folder, asks first. Attached files make the turn tainted.
- `!command` runs as you, with your environment, without a prompt (you typed it). Its output goes with your next message, and that turn is tainted.
- A **sub-agent** (`local_task`) has the read tools only, its own overwrite guard (what it read unlocks nothing for the main agent), a round limit (12), a time limit (300 s) and an 8,000-character result. A prompt it raises reaches you marked `[sub-agent]`.

## Time

A round of local tool calls must be answered to Clara within 900 seconds. The runner gives a round 840; calls still waiting after that are refused with "no answer in time", and a command's timeout never exceeds what is left of the round.

## The overwrite guard

`local_write` and `local_edit` on an existing file require that the whole file was read in this session and has not changed since (BUG-6); a file the console created this session may be replaced. One guard serves the tools, `mv`, `cp` and redirection.
