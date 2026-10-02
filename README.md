# Custom Console

A custom Windows terminal with its own commands, a virtual file system that spans
your disks, WSL and a reMarkable tablet, and a personal AI agent (Ollama + agno)
that can explore and edit your projects.

- **Shell**: `cd ls tree cat stat find cp rm pwd echo clear launch reload help ai exit`,
  tab completion generated from each command's definition, colored output.
- **Virtual file system**: one tree rooted at `/` (see below).
- **AI agent**: a chat in the terminal with file, search, edit, shell, PDF, web, e-mail and
  Moodle tools, a free zone where file work needs no confirmation, undo, context
  management and token accounting.

Windows only (it uses the registry, `\\wsl$` and detached processes).

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"            # core + tests
pip install -e ".[moodle,pdf]"     # optional: Moodle tools, PDF -> Markdown
pip install -e ".[rmdoc]"          # optional: reMarkable .rmdoc -> PDF with the handwriting
playwright install chromium        # only if you use the Moodle tools
copy .env.example .env             # then edit it
```

Start it with `python -m custom_console`, the `custom_console` script, or `run.bat`
(which also works from a plain checkout without installing).

## Configuration

Everything is optional and set through environment variables or a `.env` file at the
project root (see `.env.example`; empty values mean "use the default").

| Variable | Default | Meaning |
|---|---|---|
| `RMAPI_PATH` | – | Path to `rmapi.exe`; enables the reMarkable backend |
| `DATA_DIR` | `<project>/data` | Logs, agent memory, usage ledger, checkpoints, caches |
| `WSL_DISTRO` | `Ubuntu` | Distribution exposed as `/wsl-<name>/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `AGENT_DEFAULT_MODEL` | `gemma4` | Model used by `ai agent` / `ai start` |
| `AGENT_PERMISSION_LEVEL` | `1` | Tools auto-accepted by the agent (see below) |
| `AGENT_INSTRUCTIONS_PATH` | `config/agent_instructions.txt` | The agent's system prompt |
| `AGENT_NUM_CTX` | automatic | Context window requested from local models (default: the model's maximum, at most 32768) |
| `AGENT_COMPACT_PERCENT` | `80` | Compact the conversation automatically when the context is this full (`0` = never) |
| `AGENT_PROJECT_FILE` | `AGENT.md` | Project instructions file looked up in the free zone |
| `MOODLE_ENABLED` / `MOODLE_BASE_URL` / `MOODLE_STATE_PATH` | `true` / EPITA / `config/cookies/moodle_state.json` | Moodle tools |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `SMTP_FROM` | – | The e-mail tool exists only when `SMTP_HOST` is set |

## The shell

Type `help` for the list of commands and `<command> -h` for its options.

### Virtual paths

```
/                        the virtual root: a menu of C:/, D:/, reMarkable/, wsl-Ubuntu/
/C:/Users/me             a drive           (also C:/Users/me when you are on a drive)
/wsl-Ubuntu/home/me      a WSL distribution
/reMarkable/Notes        the tablet         (also reMarkable:Notes, relative to where you are)
~                        your home folder
```

`cp` works across all of them (`cp -r reMarkable:/Notes C:/backup`). Notes on the tablet:
downloads keep the document name (the destination is a folder), uploads are files only,
and every rmapi call is rate-limited and retried.

A document copied from the tablet arrives as an `.rmdoc` file (a zip archive). When it was
imported as a PDF, `rmdoc2pdf` gives the PDF back:

```
rmdoc2pdf Course.rmdoc               # -> Course.pdf, your handwriting and highlights drawn on it
rmdoc2pdf Course.rmdoc out.pdf -f    # choose the output, replace it if it exists
rmdoc2pdf Course.rmdoc --original    # only the original PDF, byte for byte
```

The strokes are drawn as one line per stroke, with the tablet's colours and translucent
highlighters (no pressure or speed effects), placed as the tablet shows a page fitted to
its width; typed text is not rendered. With nothing written on it, the original PDF is
copied untouched. A handwritten notebook (not a PDF) becomes blank pages carrying the
handwriting; an EPUB only gets its handwriting, with a warning. Drawing needs the `rmdoc`
extra (`pymupdf`, `rmscene`); extracting the untouched original does not.

Backslashes work in paths (`cd C:\Users\me`). Names with spaces need quotes. Anything that
is not a built-in command is looked up in your `PATH` and run as usual.

`rm -r` asks for confirmation (skip with `-f`) and refuses the drive root, your home
folder and any folder containing the current directory.

### `launch`

`launch opera` finds an application by name (PATH, registry, install folders, then a
recursive search; `-sl 1..4` limits the stages) and remembers where it found it.

## The AI agent

```
ai list [-P PROVIDER] [-r] [-s] [-c]   models of a provider (default: the local Ollama), sizes, capabilities
ai start [model]                      load a local model in memory
ai agent [-n NAME] [-P PROVIDER] [-m MODEL] [-p 0|1|2] [-d DIR] [--no-memory]
```

### Providers

The agent can run on three **providers**:

| Name | Provider | API key |
|---|---|---|
| `ollama` | Ollama on this computer (`OLLAMA_HOST`) | none |
| `ollama-cloud` | Ollama's API on ollama.com: its large models, without a local Ollama | `OLLAMA_API_KEY` ([get one](https://ollama.com/settings/keys)) |
| `chatgpt` | ChatGPT through the OpenAI API (`OPENAI_BASE_URL`: any OpenAI-compatible server works too) | `OPENAI_API_KEY` ([get one](https://platform.openai.com/api-keys)); billed per token, apart from a ChatGPT subscription |

In the agent, `/provider` lists them (current one, where each key comes from, last model
used) and `/provider chatgpt` switches: if no key is known it is asked in the input line
(typed masked, never kept in the input history), checked by listing the provider's models,
then saved in the **Windows Credential Manager** (`keyring`); a key set in `.env` takes
priority over a saved one. A menu of the provider's models follows (`↑`/`↓`, `PgUp`/`PgDn`,
`Enter`), starting on the model you last used there. The conversation is kept; when it holds
tool calls made through Ollama and you move to ChatGPT, it is first summarised (as `/compact`
does), because OpenAI cannot replay them. `/provider forget chatgpt` deletes a saved key.
Aliases: `local`, `cloud`, `openai`, `gpt`.

The last provider and the last model used with each one are remembered
(`<DATA_DIR>/agent/provider.json`): `ai agent` starts with them, `-P`/`-m` override them, and
`AGENT_PROVIDER` is the default before anything was chosen. `/model` lists and switches the
models of the current provider; a saved session (`/restore`) brings its provider back when
its key is available. Sub-agents and `/compact` use the current provider. The OpenAI model
list comes from the API (chat models only, dated snapshots hidden but accepted by name);
their context windows come from a built-in table (128k when unknown).

### Terminal behaviour

The input is pinned at the bottom of the terminal; it grows with what you type (long
lines wrap, `Shift+Tab` starts a new line, up to 10 rows).

While the agent answers, what is final (a finished paragraph, a finished tool call) is
printed **as it comes** into the normal scrollback, rendered as **Markdown**, so you can
scroll back, select and copy during the answer. Only the part still being written stays
in a live area above the input, as plain text. A paragraph counts as finished once the
next one starts (never in the middle of a code block or a list).

The rule above the input shows the model, what the agent is doing, the time spent and the
tokens generated so far (`⠹ thinking · 12.4s · ↓ 523 tokens`), the progress of its
checklist (`☑ 2/5 · Write the tests`, also recalled while idle when it is unfinished) and
how full the context is. The token count is exact after each model request and estimated
(one token per streamed chunk) in between.

Each tool call takes **one line** (`✔ file_system_edit(path='a.py') · 0.1s · +12 −3`);
what it hides (the diff of a file change, a command's output, what a read returned, a
sub-agent's work) is shown under it once you press `Ctrl+O` (or `/details`), from then on.
`Ctrl+T` (or `/transcript`) opens the whole conversation in a full-screen viewer where any
tool line unfolds: `Tab`/`Shift+Tab` select the next/previous tool line, `Enter` or a
**click** unfolds it, `a` unfolds everything, the arrows, `PgUp`/`PgDn`/`Space` and the
mouse wheel scroll, `q` or `Esc` goes back (your scrollback is left untouched). The checklist's
final state is printed at the end of each turn.

| Key | Action |
|---|---|
| `Enter` | send (while the agent is busy, the text stays in the input line) |
| `Shift+Tab` | new line in the input (in a suggestion list: previous suggestion) |
| `Tab` | complete a `/command`, a path or a model name; cycle through the suggestions |
| `Ctrl+O` | show / hide what tool lines hide, from now on |
| `Ctrl+T` | the transcript viewer (when idle) |
| `Ctrl+C` | stop the answer in progress (the partial answer is kept); clears the input when idle; refuses a pending permission question |
| `Ctrl+D` | leave the agent |

When the agent needs a decision it asks with `ask_user`: the question and its options
(with their descriptions) appear above the input. `↑`/`↓` choose, `Enter` answers; when
several answers are allowed, `Space` ticks them. Typing gives an answer of your own (if the
agent allows it). `Esc` skips the question, `Ctrl+C` skips it and stops the turn.

### Slash commands

Typing `/` lists them above the input, with what they do.

| Command | What it does |
|---|---|
| `/model [NAME]` | list the current provider's models, or switch to one (Tab completes; the conversation is kept) |
| `/provider [NAME \| forget NAME]` | list the providers, or switch to one (`ollama`, `ollama-cloud`, `chatgpt`; see above) |
| `/usage` | tokens used by this session, today, the last 7 days and in total, per model |
| `/context` | how full the context window is: system prompt, tools, project file, summary, messages |
| `/compact [FOCUS]` | replace the conversation by a summary written by the model |
| `/clear` | new conversation (context, checklist), kept as a session of its own, and clear the screen |
| `/restore [list\|N]` | bring back a previous session of this folder (see below) |
| `/undo` | undo the file changes the agent made during its last turn (repeat to go further back) |
| `/init` | ask the agent to write the project instructions file (`AGENT.md`) |
| `/todo` | show the agent's checklist |
| `/permissions [0\|1\|2]` | show or change the auto-accept level, and show the free zone |
| `/tools` | menu to turn the agent's tools on and off (see below) |
| `/details` | show or hide what tool lines hide (same as `Ctrl+O`) |
| `/transcript` | the conversation in a full-screen viewer, tool lines unfoldable (same as `Ctrl+T`) |
| `/cd /pwd /ls /tree /cat /stat /find /cp /rm` | the shell's file commands, working on the agent's current directory (so `/cd` moves the agent too) |
| `/rmdoc FILE [PDF]` (or `/rmdoc2pdf`) | convert a reMarkable `.rmdoc` to PDF (same options as the shell command) |
| `/help` `/bye` | help / leave |

### The free zone and permissions

The **free zone** is the folder the agent is started in (your current directory when you
type `ai agent`, or `-d DIR`) and everything below it. Inside it the agent's file tools
(read, list, search, write, edit, copy, move, remove) never ask. The zone is fixed at
start: a later `cd` by the agent does not move it. Launching from a drive root, your home
folder or one of its parents gives no zone at all (a warning is shown), and the zone folder
itself can never be removed or moved without asking.

Everywhere else, and for the other tools, every tool declares a level; a tool whose level
is ≤ the session's auto-accept level runs without asking, otherwise the question appears
above the input (with the diff, for file changes) and you answer in the input line
(`Enter` or `y` = yes).

| Level | Auto-accepted outside the zone | Examples |
|---|---|---|
| `0` | only trivial tools | `file_system_pwd`, `todo_write` |
| `1` (default) | reads | reading or listing a path outside the zone, `moodle_list_courses`, `get_weather` |
| `2` | everything | writing outside the zone, `run_command`, `moodle_click_element`, `send_email` |

`run_command` is level 2 even inside the zone: it asks unless you chose `-p 2`.

### Tools

- **Navigate and search**: `file_system_pwd list cd tree stat`, `file_system_find` (by name,
  also on the reMarkable), `file_system_glob` (files by pattern, newest first),
  `file_system_grep` (content, regular expressions, context lines).
- **Read**: `file_system_read` (whole file, line range, or outline of a Python file).
- **Change**: `file_system_edit` replaces an exact piece of text (it must be unique, or use
  `replace_all`), `file_system_write` creates or replaces a file, `file_system_copy`,
  `file_system_move`, `file_system_remove`. A file must have been read before it is edited
  or overwritten, and must not have changed since. Line endings (CRLF) and BOM are kept;
  binary and non-UTF-8 files are refused. Each change keeps a diff under its tool line.
- `run_command` – a shell command in the current directory (`cmd.exe`), with a timeout
  (60 s by default, 600 s at most), stopped by `Ctrl+C`.
- `todo_write` – the agent's checklist for multi-step work, followed in the rule above the
  input and printed at the end of the turn.
- `ask_user` – a question to you with options (one or several to pick, each with a
  description) and, if the agent allows it, an answer of your own.
- `task` – hands a self-contained job (exploring a folder, finding where something is done,
  summarising long files) to a **sub-agent**: a fresh agent on the same model that does not
  see the conversation, works with the **read-only** tools (all tools with
  `allow_writes=true`, each still asking its permission) and returns only its report, which
  keeps the main context small. Its tool calls are listed under the `task` line and its
  tokens count in the turn and in `/usage`. Sub-agents run one at a time and cannot start
  sub-agents, ask you questions or touch the checklist. On a local model, the main
  conversation's prompt is processed again after a sub-agent ran (Ollama keeps one prompt
  cache), so delegating pays off for jobs that read a lot.
- `pdf_to_markdown` – PDF -> Markdown next to the PDF (needs the `pdf` extra).
- `rmdoc_to_pdf` – a reMarkable `.rmdoc` (what copying from the tablet gives) -> PDF, with
  the handwriting drawn on it unless `include_handwriting=false` (needs the `rmdoc` extra
  to draw). Free inside the free zone like the other file tools.
- `get_location`, `get_weather`, `send_email` (only with SMTP configured).
- `moodle_*` – courses, course structure, pages, announcements, grades, downloads, clicks
  (needs the `moodle` extra; the first use opens a browser for the SSO login). Disable
  with `MOODLE_ENABLED=false` if you do not need them: every tool costs prompt tokens.

### Choosing the tools: `/tools`

Every tool costs prompt tokens and gives the agent a power: `/tools` opens a menu of them,
grouped (Files, Commands, Checklist, Questions, Sub-agents, Documents, Web, Mail, Moodle).

| Key | |
|---|---|
| `↑` `↓` (or `k` `j`), `PgUp` `PgDn`, `Home` `End` | move |
| `Space` | turn the tool on or off; on a group heading, the whole group |
| `a` | everything on, or everything off |
| `Enter` / `Esc` (or `Ctrl+C`) | apply / cancel |

The change applies from the next message, the agent is told which tools are off so it does
not try them, `/context` counts only the tools that are on, and the choice is saved in
`<DATA_DIR>/agent/tools.json` (kept when you restart, also with `--no-memory`). Without the
menu: `/tools list`, `/tools off run_command files`, `/tools on todo_write`, `/tools reset`
(names can be a tool, a group or a unique prefix; Tab completes them).

### Sessions and `/restore`

Every conversation is a session, saved after each turn per working directory (the folder the
agent was started in, `-d` included) under `<DATA_DIR>/agent/sessions/`. Starting the agent
in a folder always opens a **fresh** conversation; if the folder has earlier sessions the
banner tells you:

```
Previous session: “fix the parser” · 2 h ago · 12 exchange(s) — /restore (+3 older: /restore list)
```

| Command | |
|---|---|
| `/restore` | the most recent session other than the current one |
| `/restore list` | the saved sessions of this folder: number, age, first question, model, exchanges |
| `/restore 3` | that session of the list (Tab completes the numbers) |

Restoring redraws the questions and answers (tool lines and diffs included; the last 40
exchanges at most) and brings back what the session had: the **agent's own memory** of the
conversation and its compaction summary, the **model**, the **tools** that were on (the
`/tools` default for new sessions is left alone), the **auto-accept level** and the
**checklist**. The restored session then continues where it stopped. Not restored: undo
snapshots, and the list of files already read (the agent must read a file again before
editing it). If the saved model is not installed any more, the current one is kept and you
are told. The last 5 sessions per folder are kept (`AGENT_KEEP_SESSIONS`); the others are
deleted, with the agent's memory of them. `ai agent --no-memory` saves nothing, so there is
nothing to restore.

### Context, memory and undo

- **Window**: local models are asked for a context of `AGENT_NUM_CTX` tokens (by default the
  model's maximum, capped at 32768: Ollama's own default is much smaller, and the agent's
  tool definitions alone take about 3000 tokens). Models served by ollama.com use their
  own window. The header shows `ctx 12% of 32k`; `/context` shows the breakdown.
- **Compaction**: at `AGENT_COMPACT_PERCENT` the conversation is summarised by the model
  and continues in a fresh session that starts from that summary (`/compact` does it on
  demand, `/clear` drops everything).
- **Date, time and changed files**: each message reaches the model after a short note
  (`[Automatic note, not written by the user. Current date and time: 2026-10-01 14:32
  (Thursday, UTC+02:00).]`). When files the agent read (whole or in part) were changed since,
  by you or another program, the note lists them and tells it that what it read is outdated,
  so it reads them again before answering about them; reading a file again takes it off the
  list. The note goes before your message rather than into the system prompt so that the
  model's prompt cache is kept from one turn to the next, and it is left out of what you
  see, of the saved sessions and of the compaction summaries.
- **Project instructions**: if `AGENT.md` exists at the root of the free zone it is added
  to the agent's context on every turn (read fresh, first 8000 characters); `/init` has
  the agent write it.
- **Memory**: the conversation and the agent's long-term memories are kept in
  `<DATA_DIR>/agent/memory.db`, one session per conversation (see `/restore`). With `--no-memory` the conversation only lives in memory
  for this run and nothing is stored, except the usage ledger and the `/tools` choice.
- **Undo**: before each file change the previous content is saved (up to 100 MB per file
  or folder; bigger ones are refused) under `<DATA_DIR>/agent/checkpoints/` (kept 7 days).
  `/undo` reverts a whole turn. Changes made by `run_command` are not tracked.

### Usage

Every turn's tokens are appended to `<DATA_DIR>/agent/usage.jsonl`, and `/usage` sums them
for the session, today, the last 7 days and all time. Ollama has no account-wide usage API,
so "all time" means everything run through this console; for cloud models your real quota
is on ollama.com.

Every prompt, answer and tool call is also appended to `<DATA_DIR>/logs/agent.jsonl`
(rotated at 5 MB).

## Layout

```
config/agent_instructions.txt    the agent's system prompt
src/custom_console/
  settings.py                    all configuration, loaded lazily (no import side effects)
  fs/                            virtual file system: manager, reMarkable backend, path parsing,
                                 rmdoc.py (.rmdoc -> PDF)
  shell/                         REPL, completer, tokenizer, printer, commands/
  apps/                          application finder and launcher
  llm/                           Ollama client, providers (Ollama, ollama.com, ChatGPT), API keys
  agent/
    ui.py  render.py  turn.py    terminal UI (what is final printed as Markdown as it comes), turn model
    transcript.py                full-screen transcript viewer (Ctrl+T), foldable tool lines
    questions.py                 ask_user's options and answers
    slash.py  commands.py        slash commands: registry, completion, /model /usage /undo...
    session.py  factory.py       agno wiring: streaming, tool hook, journal, usage
    subagent.py                  sub-agents run by the `task` tool
    context.py  usage.py         context window accounting and compaction, token ledger
    zone.py  checkpoints.py      the free zone, undo snapshots
    toolset.py                   which tools are on (/tools), saved in tools.json
    sessions.py                  saved sessions per folder (/restore)
    permissions.py  results.py   permission gate, ToolResult
    cache.py  journal.py  diffs.py
    tools/                       one module per tool family (filesystem, shell, todo, pdf...)
    moodle/                      Playwright client + dedicated worker thread
tests/                           mirrors the package
```

## Development

```powershell
pip install -e ".[dev]"
pytest
```

The file system layer and the agent UI are tested without a real tablet, Ollama or
terminal (`rmapi` is mocked; the UI is driven through prompt_toolkit's pipe input).
