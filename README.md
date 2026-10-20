# Custom console

A terminal shell and a full-screen agent console for [Clara](https://github.com/Materwan/clara). One program, two modes:

- **the shell**: a prompt with a virtual file system (drives, WSL, the reMarkable tablet), pipes, aliases, a history, and path completion with a menu and ghost text;
- **`ai agent`**: a full-screen console where Clara, the assistant, reads and changes files and runs commands **on your computer**, after asking you.

Clara does the thinking and keeps the conversations. The console runs the local tools, asks for permission, and keeps the local records. It talks to Clara with a token only: no user name, no password.

Windows is the first target; Linux and macOS are supported. Python 3.11 or later.

## Install

```text
uv tool install .                      # from a checkout of this repository
uv tool install ".[pdf,rmdoc]"         # with the PDF reader and the reMarkable converter
```

`uv tool install` puts `custom_console` (and `custom-console`) on your path. `python -m custom_console` works too.

The extras are optional: `pdf` adds `local_pdf_to_markdown`, `rmdoc` adds the `rmdoc2pdf` command.

## First run

1. Create a token for the console on the Clara server (a token with the `console` client label).
2. `custom_console --save-token` asks for it with hidden input and saves it where only you can read it:
   `~/.config/clara/token` (Linux, macOS) or `%APPDATA%\clara\token` (Windows). The file is shared with Clara's own command-line client.
3. `custom_console` starts the shell. It checks the token (`GET /v1/auth/me`) and prints one line saying whether Clara is usable. The shell works without Clara; `ai agent` needs it.

Every start-up problem (bad setting, unreadable token file, ...) is printed in one list, not one at a time.

## The shell

```text
~/projects/notes rwx
> ls -l | grep todo
> cat notes.txt | head -5 > /tmp/first.txt
> cd /C:/Users/me        # drives are /C:/, WSL is /wsl-Ubuntu/, the tablet is reMarkable:
> ai agent               # opens the agent in this folder
```

| Group | Commands |
| --- | --- |
| Navigate | `cd` `ls` `pwd` `tree` `find` `stat` |
| Read | `cat` `head` `tail` `wc` `grep` `diff` |
| Change | `cp` `mv` `rm` `mkdir` `rmdir` `touch` |
| Shell | `alias` `unalias` `echo` `env` `history` `which` `help` `clear` `reload` `exit` |
| Desktop | `open` `launch` `clip` |
| reMarkable | `rmdoc2pdf` |
| Agent | `ai agent` |

- **Pipes and redirection** (`|`, `>`, `>>`, `<`) work between the built-in commands. External programs run attached to the terminal and cannot be piped.
- **Tab** completes commands, flags, paths (virtual ones too) and history; the rest of the newest matching history line is shown as grey ghost text, and the Right arrow accepts it.
- `rm`, `mv` and `cp` never overwrite or delete without a check; **your home folder, its parents and the drive roots can never be removed** (see [docs/DECISIONS.md](docs/DECISIONS.md)).
- Ctrl+C stops the running command and never leaves the console.

## The agent

`ai agent [--incognito] [--local-only] [--model NAME]` closes the prompt, opens the full-screen console in the same process, and returns to the shell when you leave with `/exit`.

| Key | Does |
| --- | --- |
| Enter | send |
| Shift+Enter, Ctrl+J, or `\` before Enter | add a line |
| Esc | stop the answer or the command running |
| Up / Down | walk the history of what you sent |
| Tab | complete a `/command`, a path, an `@file` |
| Ctrl+V | paste text, or an image from the clipboard (it becomes an attachment) |
| Ctrl+X | remove the last attachment |
| Ctrl+D | leave (empty box) |

In a message: `@path` sends a file or a folder (small text files go in the message as a fenced block; PDF, Word, spreadsheet, slide and image files are attached and Clara reads them; a file over 30 MB is refused); `!command` runs a command now, as you, and its output goes with your next message; `//` and `!!` start a message with a real `/` or `!`.

Commands: `/model` `/tools` `/clear` `/project` `/retry` `/version` `/restore` `/undo` `/compact` `/context` `/usage` `/approvals` `/rules` `/todo` `/plan` `/init` `/journal` `/transcript` `/help` `/exit`. The shell's own commands also work with a slash (`/ls src`, `/cat notes.txt | grep todo`).

- **Sessions.** A session is one Clara conversation. The console saves a small metadata file per session and keeps the newest 5 of each folder; `/restore` lists and brings them back. Clara keeps the messages.
- **Undo.** Before each `local_write` and `local_edit` the original is copied away; `/undo` goes back through the last turn. A file someone else changed since is left alone with a warning. What `local_run` does cannot be undone.
- **Sub-agents.** `local_task` hands a reading job to a separate incognito conversation that can only read.
- **`AGENT.md`** in a trusted folder is sent as project instructions; `/init` asks the agent to write one.
- **Journal.** `/journal` shows what happened in this session: turns, tool calls with the permission decision and the reason, approval answers, errors. Secrets are redacted before anything is written.

## Permissions

Nothing happens on your computer without a decision, and the decision is one engine's, in this order: **resolve** the paths (typed and real), the **floor** (sensitive files, `.git`, anything outside the trusted folder, network paths: always asks, never an "always"), a **tainted turn** (after Clara read outside content, every change and command asks), the short **read-only allowlist** inside the trusted folder, **stored answers**, and otherwise it **asks**.

Answer a prompt with `o` (once), `a` (always, for exactly that call), `r` (refuse) or `n` (never). A new folder asks once to be trusted; an untrusted folder gets read tools only and its `AGENT.md` is not read. The full description is in [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## What goes to Clara, and what stays here

Sent to Clara on every turn: your message (with the `@file` text and attachments you chose, and the output of a `!command` you ran), the time zone, the schemas of the local tools, and the console's instructions (including `AGENT.md` of a trusted folder). Sent back during a turn: the results of the local tools **the agent asked for and you allowed**.

Stays on this computer: the token file, the permission rules, the trusted folders, the journal, the undo copies, the usage ledger, the command history, and the session metadata. A session file holds no message text and no permission.

**Incognito**: Clara keeps no memory of the conversation and deletes it after 24 hours; the console saves no session for it. **Local only**: Clara uses only models that run on its own network. Neither changes what the console sends to Clara; both change what Clara keeps or where it sends it on.

## Settings

Four sources, in this order: the command line, the environment, a `.env` file in the console's config folder, the defaults. The `.env` file is read but **never loaded into the environment**, and a secret in it stops the start. Every problem is listed at once.

| Setting | Option | Default | Meaning |
| --- | --- | --- | --- |
| `CLARA_URL` | `--url` | `http://127.0.0.1:8765` | Clara's address (plain http to another host warns) |
| `CLARA_TOKEN_FILE` | `--token-file` | see First run | where the token is read |
| `CONSOLE_DATA_DIR` | `--data-dir` | `~/.local/share/custom-console` (Windows: `%LOCALAPPDATA%\custom-console\data`) | sessions, undo, journal, rules, history |
| `CONSOLE_TOOL_TIMEOUT` | `--tool-timeout` | 120 | seconds before a foreground command is stopped |
| `CONSOLE_RESULT_LIMIT` | `--result-limit` | 30000 | characters kept of one tool result |
| `CONSOLE_RMAPI` | `--rmapi` | search the path | path to `rmapi` |
| `CONSOLE_LOG_LEVEL` | `--log-level` | `info` | log detail |

`--debug` logs the method, path and status of each request, never a body or a header. The log redacts secrets.

## Numbers

These come from `scripts/check_prompt.py`, which builds the real prompt with all the tools and fails the build when it would not fit what Clara accepts. A test keeps this line equal to the script's output.

<!-- numbers -->
16 local tools · tool schemas 7,996 characters · base prompt 2,767 characters (3,866 with the session facts) · longest possible prompt 11,987 of 20,000 characters
<!-- /numbers -->

## Documents

- [docs/DECISIONS.md](docs/DECISIONS.md): what was decided, why, and what differs from the plan
- [docs/PERMISSIONS.md](docs/PERMISSIONS.md): the permission engine step by step
- [docs/CLARA-CLIENT.md](docs/CLARA-CLIENT.md): the routes, events and errors, and the tasks for the Clara server
- [docs/REVIEW-MAP.md](docs/REVIEW-MAP.md): every finding of the code review, and the test that proves it fixed
- [docs/SPIKES.md](docs/SPIKES.md): the Phase 0 experiments and their results

## Development

```text
uv sync --all-extras --frozen
uv run ruff check . && uv run mypy && uv run lint-imports
uv run python scripts/check_prompt.py
uv run pytest                          # the live tests need a Clara and are skipped: pytest -m live
```

- Five layers with import rules that `lint-imports` checks: `ui` -> `commands` -> `core` -> `clara`, `platform`. Only `ui` imports Textual or prompt_toolkit.
- **The tests never touch your real files.** A safety net (`tests/support/guard.py`, installed first by `tests/conftest.py`) refuses any delete or rename outside a temp folder, points `HOME` at an empty folder for every test, and a tripwire checks the real home after the run. Destructive tests build their world with `sandbox_resolver` or `Sh` and never name a real path. Read [docs/DECISIONS.md](docs/DECISIONS.md) before writing a test that removes anything.
- CI (`.github/workflows/ci.yml`) runs on Windows, Linux and macOS with Python 3.11 and 3.13.
- A fake Clara server (`tests/fake_clara/server.py`) speaks the real protocol over HTTP, so the client, the turn runner and the screens are tested against it.

## Licence

All rights reserved (see `LICENSE`).
