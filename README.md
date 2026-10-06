# Custom Console

A custom Windows terminal with its own commands, a virtual file system that spans
your disks, WSL and a reMarkable tablet, and a personal AI agent that can explore and edit your projects. The agent is a terminal client
of the **Clara server** (the model, the memory and the conversations live there); its tools
run on this computer.

- **Shell**: `cd ls tree cat stat find cp rm pwd echo clear launch reload help ai exit`,
  tab completion generated from each command's definition, colored output.
- **Virtual file system**: one tree rooted at `/` (see below).
- **AI agent**: a chat in the terminal with file, search, edit, git, shell, PDF, desktop,
  e-mail and Moodle tools (and the server's web search), a free zone where file work needs no
  confirmation, permission answers that can last ("always"), undo, plan mode, context
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
| `DATA_DIR` | `<project>/data` | Logs, saved sessions, usage ledger, checkpoints, caches |
| `WSL_DISTRO` | `Ubuntu` | Distribution exposed as `/wsl-<name>/` |
| `CLARA_URL` | `http://127.0.0.1:8765` | The Clara server the agent talks to |
| `CLARA_USER`, `CLARA_PASSWORD` | – | Your user name and password on that server (the administrator makes them with `/user add`): `ai agent` signs in by itself, and the server knows it is you |
| `CLARA_TOKEN` | – | Instead of a user: a chat token (`CLARA_TOKENS` there). Required by `ai agent` when there is no `CLARA_USER` |
| `CLARA_USER_NAME` | – | How Clara should call you |
| `CLARA_TIMEZONE` | this computer's | Your time zone (IANA name, `Europe/Paris`), sent with each message: the server tells the model the date and time in it |
| `OLLAMA_HOST` | `http://localhost:11434` | The local Ollama of `ai list` / `ai start` (not used by the agent) |
| `AGENT_DEFAULT_MODEL` | `gemma4` | Model `ai start` loads when none is given |
| `AGENT_PERMISSION_LEVEL` | `1` | Tools auto-accepted by the agent (see below) |
| `AGENT_INSTRUCTIONS_PATH` | `config/agent_instructions.txt` | The agent's system prompt |
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
ai list [-r] [-s] [-c]                models of the local Ollama, sizes, capabilities
ai start [model]                      load a local model in memory
ai agent [-n NAME] [-p 0|1|2] [-d DIR] [--no-memory]
```

### The Clara server

The agent does not run a model itself. It is a **client of the Clara server**, which runs the
model, keeps the memory (what Clara knows about you, the same on every client) and the
conversations, and compacts them when they grow too long. Set `CLARA_URL` and `CLARA_TOKEN`
in `.env`; `ai agent` refuses to open when the server cannot be reached. The server also has tools
of its own (memory, reminders, notifications, and with an Ollama API key `web_search` and
`web_fetch`): they run there, and each shows one line in the turn (`✔ remember(...) · on the server`).

If a turn stops before its end (you press `Ctrl+C`, the connection breaks, the model fails), the server
keeps what was done so far, with a note saying it was interrupted: Clara knows which files she had
already changed.

The tools stay here. For each message the console sends the server the tools it offers
(their names, descriptions and parameters, generated from the Python functions), the agent's
instructions (`AGENT_INSTRUCTIONS_PATH`, the project file, the tools you turned off) and a
short note (date, files that changed). When the model wants a tool, the server asks the
console to run it, the console runs it on this computer after the usual permission question,
and the answer goes back on the same stream. So files, shell, PDF, Moodle and mail never leave
this machine, and the permissions, the free zone and `/undo` work exactly as before.

Which model runs, and where (Ollama on the server's computer, or ollama.com with an API
key), is the server's business, but you choose among the models an administrator offers:
`/model` lists them with what a token of each costs in credits and sets the one the console
is answered by (it is yours, kept by the server, separately from your other clients). Managing
the server itself (its provider, its own model, which models users may choose) is done on its
web site or console, not here. The header and `/usage` follow whatever model the server used for each turn.

### Terminal behaviour

The screen is full-screen: the conversation scrolls in its own pane (mouse wheel, `PgUp` /
`PgDn`; `Shift`+drag selects text), and the header rule and the input are **fixed at the
bottom of the window**, whatever the scroll position. New output follows the bottom unless
you scrolled up (the rule then says `↑ scrolled`). The input grows with what you type (long
lines wrap, `Shift+Tab` starts a new line, up to 10 rows). When you leave, the conversation
is written back to the normal terminal scrollback.

While the agent answers, what is final (a finished paragraph, a finished tool call) is
added to the pane **as it comes**, rendered as **Markdown**. Only the part still being
written stays in a live area above the input, as plain text. A paragraph counts as finished once the
next one starts (never in the middle of a code block or a list). The model's reasoning, for
the models that show it, streams there too (its last lines), then folds into one line
(`✻ thought for 4.2s`, unfolded by `Ctrl+O`); so does the output of a running command.

A message sent while the agent works is **queued** and sent when it is done (the rule shows
`2 queued`; `Ctrl+C` drops the queue with the answer in progress). In a message:

- `@path` attaches a file (its content goes with the message, and it counts as read) or a folder
  (its list); `Tab` completes the paths. A mention that is not a path (`@someone`) is left alone.
- A line starting with `!` runs a command yourself in the agent's working directory (`!git
  status`): its output is shown, and goes to the agent with your next message.

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
mouse wheel scroll, `q` or `Esc` goes back (the conversation pane is left untouched). The checklist's
final state is printed at the end of each turn.

| Key | Action |
|---|---|
| `Enter` | send (while the agent is busy: queued, sent when it is done) |
| `Shift+Tab` | new line in the input (in a suggestion list: previous suggestion) |
| `Tab` | complete a `/command`, a path or a model name; cycle through the suggestions |
| `Ctrl+O` | show / hide what tool lines hide, from now on |
| `Ctrl+T` | the transcript viewer (when idle) |
| `Ctrl+C` | stop the answer in progress at once (the partial answer is kept, here and on the server); clears the input when idle; refuses a pending permission question |
| `Ctrl+D` | leave the agent |

When the agent needs a decision it asks with `ask_user`: the question and its options
(with their descriptions) appear above the input. `↑`/`↓` choose, `Enter` answers; when
several answers are allowed, `Space` ticks them. Typing gives an answer of your own (if the
agent allows it). `Esc` skips the question, `Ctrl+C` skips it and stops the turn.

### Slash commands

Typing `/` lists them above the input, with what they do.

| Command | What it does |
|---|---|
| `/model [N\|NAME\|default]` | the models an administrator offers you, with their cost in credits per token; choose the one the console is answered by (`default`: the server's own) |
| `/remind [daily\|weekly\|monthly] [@SURFACES] WHEN TEXT` | a reminder for you, shown on your Clara clients at that time (see below) |
| `/reminders` `/unremind ID` | your reminders that have not fired yet; cancel one |
| `/tasks [all\|done]` | your to-do list, kept by Clara: each task with the reminders sent and the next one (see below) |
| `/task ID` `/task add` `done` `reopen` `delete` `set` | one task in full; add, finish, reopen, delete or change one (see below) |
| `/notify-after [SECONDS\|off\|default]` | how long a task (a turn, tools included) takes before you are notified on your Clara clients when it is done; `off`: never; `default`: the server's delay. Kept by the server, so it is the same on every client |
| `/usage` | tokens used by this session, today, the last 7 days and in total, per model |
| `/context` | how full the context window is: system prompt, tools, project file, summary, messages |
| `/compact [FOCUS]` | have the server replace the older messages by a summary written by the model |
| `/clear` | new conversation (context, checklist), kept as a session of its own, and clear the screen |
| `/restore [list\|N]` | bring back a previous session of this folder (see below) |
| `/undo` | undo the file changes the agent made during its last turn (repeat to go further back); the agent is told |
| `/plan [on\|off]` | plan mode: the agent gets only the tools that read, and answers with a plan |
| `/init` | ask the agent to write the project instructions file (`AGENT.md`) |
| `/todo` | show the agent's checklist |
| `/permissions [0\|1\|2\|forget]` | show or change the auto-accept level, the free zone and the "always" answers (`forget` drops them) |
| `/tools` | menu to turn the agent's tools on and off (see below) |
| `/details` | show or hide what tool lines hide (same as `Ctrl+O`) |
| `/transcript` | the conversation in a full-screen viewer, tool lines unfoldable (same as `Ctrl+T`) |
| `/cd /pwd /ls /tree /cat /stat /find /cp /rm` | the shell's file commands, working on the agent's current directory (so `/cd` moves the agent too) |
| `/rmdoc FILE [PDF]` (or `/rmdoc2pdf`) | convert a reMarkable `.rmdoc` to PDF (same options as the shell command) |
| `/help` `/bye` | help / leave |

### Reminders and notifications

`/remind` gives the Clara server a text and a moment; at that moment the server announces it **to you
only**: on all your clients (this console, `clara-chat`, the desktop app... every account linked to you),
or only on the surfaces you name with `@` (`@app`, `@app,discord`). It is shown above the input line
with a bell (`⏰ Dentist`), even while the agent is working. Notifications (`🔔 Answer ready: ...`) arrive
the same way: from Clara (her `notify` tool), from the server (a long answer is done, the conversation
was summarised, the model changed) or from another client.

```
/remind +30m Tea                     in 30 minutes   (+2h, +3d work too)
/remind 09:30 Stand-up               at the next 09:30 (tomorrow if it is past)
/remind tomorrow 14:00 Dentist
/remind 2026-12-24 20:00 Gifts
/remind daily 09:00 Stand-up         also weekly, monthly (same time, same day of the month)
/remind @app +1h Stretch             only on the desktop app
```

What is shown is **the message Clara wrote** for the reminder (its own text if she could not). The
console also says when the server is stopping, gone or back (`● Clara is stopping: she finishes what is
running and takes nothing new.`, `● Clara is not running.`, `● Clara is running again.`); while it
stops, new questions are refused but the turn you are running finishes.

The server keeps the reminders, so they fire while this console is closed. A client that was away
receives what it missed the next time it connects, marked `(missed, it was due …)`; a client that
has never connected starts from now. The console listens in a background thread and reconnects
by itself when the server restarts. A repeating reminder that was missed several times fires once.
You can also just ask Clara ("remind me tomorrow at 9 on my desktop about the meeting"): she picks
where it is shown.

The time you type is your computer's local time; a repeating reminder keeps the UTC offset it was
set with, so it does not follow daylight-saving changes.

### Tasks

Clara keeps a **to-do list** for you on the server (the same on the web site, the desktop app, `clara-chat`
and Discord). Every task has a title, a description, an optional deadline and **reminders**: when you give
none, Clara picks them; and each time a reminder is sent she looks at the task again (its title, description and
the number of reminders already sent) and may move the next ones, so you are not nagged the same way forever.
A reminder arrives like any notification (`🔔 Task: Taxes: ...`). After `CLARA_TASK_MAX_REMINDERS` reminders
(10 by default) a task is left alone until you finish it or ask for more. These are not the agent's own
checklist (`/todo`) nor its `task` sub-agents.

```
/tasks                               what is to do: reminders sent and next reminder of each
/tasks all                           with the finished ones (also: /tasks done)
/task 4                              one task in full: description and every reminder to come
/task add Buy milk                   a task; Clara picks the reminders
/task add due tomorrow 18:00 remind +1h remind tomorrow 09:00 Send the invoice | to ACME
/task add @app Water the plants      reminders only on the desktop app
/task done 4        /task reopen 4   (reopening has Clara pick the reminders again)
/task set 4 title Send the new invoice      also: description TEXT
/task set 4 due tomorrow 18:00              or: due none
/task set 4 remind +2h, tomorrow 09:00      the reminders to come, comma-separated; or: remind none
/task delete 4
```

You can also just ask Clara ("add a task: send the invoice by Friday", "what is on my list?", "when will you
remind me about the taxes?", "I did the taxes").

### The free zone and permissions

The **free zone** is the folder the agent is started in (your current directory when you
type `ai agent`, or `-d DIR`) and everything below it. Inside it the agent's file tools
(read, list, search, write, edit, copy, move, remove) never ask. The zone is fixed at
start: a later `cd` by the agent does not move it. Launching from a drive root, your home
folder or one of its parents gives no zone at all (a warning is shown), and the zone folder
itself can never be removed or moved without asking.

Some paths inside the zone are never free to *change*: `.git`, `.hg`, `.svn`, `.github`,
`.vscode`, `.idea`, `.venv`/`venv`, `.env` files and the project file (`AGENT.md`) at its root.
Writing there could run code later (a git hook, an editor task) or steer the agent; reading
them stays free.

Everywhere else, and for the other tools, every tool declares a level; a tool whose level
is ≤ the session's auto-accept level runs without asking, otherwise the question appears
above the input (with the diff, for file changes; the whole e-mail, for `send_email`).
What you were typing is put aside meanwhile and comes back. An answer must be typed (an
empty `Enter` answers nothing):

| Answer | |
|---|---|
| `y` | yes, this once |
| `a` | yes, and for the rest of the session for calls of the same kind |
| `p` | yes, and from now on in this project (kept in `<DATA_DIR>/agent/permissions/`) |
| `n` | no; `n use pytest -x instead` tells the agent why |

"The same kind" is the tool in that folder (`file_system_write in C:/notes`), the program and
its subcommand for `run_command` (`git status …` commands), the kind of file for `open_path`,
the recipient for `send_email`. A command that chains or redirects (`&&`, `|`, `>`...) is
always asked. `/permissions` lists what you allowed; `/permissions forget` drops it.

| Level | Auto-accepted outside the zone | Examples |
|---|---|---|
| `0` | only trivial tools | `todo_write`, `command_output` |
| `1` (default) | reads | reading or listing a path outside the zone, `git_status`, `moodle_list_courses`, `get_weather` |
| `2` | everything | writing outside the zone, `run_command`, `open_path`, `clipboard_read`, `send_email` |

`run_command` is level 2 even inside the zone: it asks unless you chose `-p 2`.

### Tools

Every tool answers the model in plain text (a failure starts with `Error:`), never in JSON
with escaped strings: the model copies what it read into its edits as it is.

- **Navigate and search**: `file_system_list cd tree stat`, `file_system_find` (by name,
  also on the reMarkable), `file_system_glob` (files by pattern, newest first),
  `file_system_grep` (content, regular expressions, context lines). In a git repository the
  searches only see what git does not ignore (`.gitignore`: builds, caches...). The working
  directory is told with each message.
- **Read**: `file_system_read` (whole file, line range, or outline of a Python file;
  `line_numbers=true`). What it returns is capped to a share of the context window; a cut
  read says which lines it showed and where to go on.
- **Change**: `file_system_edit` replaces an exact piece of text (it must be unique, or use
  `replace_all`; spaces at the ends of lines need not match, and when the text is not found
  the error shows the closest passage), `file_system_multi_edit` makes several replacements
  in one file at once (all or none), `file_system_write` creates or replaces a file,
  `file_system_copy`, `file_system_move`, `file_system_remove`. A file must have been read
  before it is edited or overwritten, and must not have changed since. Line endings (CRLF)
  and BOM are kept; binary and non-UTF-8 files are refused. Each change keeps a diff under
  its tool line.
- **Git** (read only, level 1): `git_status`, `git_diff` (unstaged, staged, or since a
  revision), `git_log`.
- `run_command` – a shell command in the current directory, with `cmd` (default) or
  `shell="powershell"`, a timeout (60 s by default, 600 s at most), stopped by `Ctrl+C`. It
  runs in its own hidden console with UTF-8 output; what it prints shows live under its line.
  `background=true` starts it and returns at once (a dev server, a watcher): `command_output`
  reads what it printed since, `command_stop` stops it; they are stopped when the agent closes.
- **Desktop** (Windows): `open_path` (a file with its application, a folder, a URL),
  `launch_app` (by name, like the shell's `launch`), `clipboard_read`, `clipboard_write`.
- `todo_write` – the agent's checklist for multi-step work, followed in the rule above the
  input and printed at the end of the turn.
- `ask_user` – a question to you with options (one or several to pick, each with a
  description) and, if the agent allows it, an answer of your own.
- `task` – hands a self-contained job (exploring a folder, finding where something is done,
  summarising long files) to a **sub-agent**: a one-shot job the server runs on the same model
  (no conversation, no memory), works with the **read-only** tools (all tools with
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
grouped (Files, Git, Commands, Checklist, Questions, Sub-agents, Documents, Web, Desktop, Mail, Moodle).

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
exchanges at most) and brings back what the session had: the **conversation on the server**
(and its compaction summary), the **tools** that were on (the
`/tools` default for new sessions is left alone), the **auto-accept level** and the
**checklist**. The restored session then continues where it stopped. Not restored: undo
snapshots, and the list of files already read (the agent must read a file again before
editing it). If the server no longer has the conversation, you are told: you can read it, but
Clara will not remember it. The last 5 sessions per folder are kept (`AGENT_KEEP_SESSIONS`);
the others are deleted, from the server too. `ai agent --no-memory` saves nothing, so there is
nothing to restore.

### Context, memory and undo

- **Window**: the server knows the model's context window and reports how full the
  conversation is after each turn. The header shows `ctx 12% of 32k`; `/context` shows the
  breakdown (the system prompt and the tools are estimates, the total is the server's figure).
- **Compaction**: the **server** summarises the older messages when the context is nearly full
  (`CLARA_COMPACT_PERCENT` there, 80 by default) and says so in the turn; `/compact [FOCUS]`
  does it on demand, `/clear` starts a new conversation.
- **Working directory, changed files, attachments**: each message reaches the model after a
  short note (`[Automatic note, not written by the user. Working directory: C:/work.]`). When
  files the agent read (whole or in part) were changed since, by you or another program, the
  note lists them and tells it that what it read is outdated, so it reads them again before
  answering about them; reading a file again takes it off the list. The files you attach with
  `@`, the output of a `!command` and what `/undo` reverted follow it. All this goes before your
  message rather than into the system prompt so that the model's prompt cache is kept from one
  turn to the next (it is sent as the message's `prefix`), and it is left out of what you see,
  of the saved sessions and of the compaction summaries. The date and time come from the server,
  in your time zone (`CLARA_TIMEZONE`).
- **Project instructions**: if `AGENT.md` exists at the root of the free zone it is added
  to the agent's context on every turn (read fresh, first 8000 characters); `/init` has
  the agent write it.
- **Memory**: the conversation lives on the Clara server, one conversation per session (see
  `/restore`), next to what Clara knows about you. With `--no-memory` nothing is saved here
  and the conversation is erased from the server when the console closes; only the usage
  ledger and the `/tools` choice remain.
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
  llm/                           Ollama client (`ai list`, `ai start`)
  agent/
    ui.py  render.py  turn.py    terminal UI (what is final printed as Markdown as it comes), turn model
    overlays.py                  what waits for an answer above the input: questions, menus, choices
    transcript.py                full-screen transcript viewer (Ctrl+T), foldable tool lines
    questions.py                 ask_user's options and answers
    mentions.py                  @path attachments and their completion
    slash.py  commands.py        slash commands: registry, completion, /compact /usage /undo...
    clara.py  remote.py          the Clara server: HTTP client, and a turn with tools run here
    reminders.py                 /remind and /task parsing, the notice, the background listener
    schema.py                    the JSON description of each tool, from its signature
    session.py                   a turn: streaming, tool hook, journal, usage
    subagent.py                  sub-agents run by the `task` tool (ephemeral jobs on the server)
    context.py  usage.py         context accounting, project file, token ledger
    zone.py  checkpoints.py      the free zone, undo snapshots
    toolset.py                   which tools are on (/tools), saved in tools.json
    sessions.py                  saved sessions per folder (/restore)
    permissions.py  results.py   permission gate and "always" rules, ToolResult (plain text for the model)
    cache.py  journal.py  diffs.py
    tools/                       one module per tool family (filesystem, git, shell, desktop, todo, pdf...)
    moodle/                      Playwright client + dedicated worker thread
tests/                           mirrors the package
```

## Development

```powershell
pip install -e ".[dev]"
pytest
```

The file system layer and the agent UI are tested without a real tablet, Clara server or
terminal (`rmapi` is mocked; the UI is driven through prompt_toolkit's pipe input).

## Token budget

Every request carries the tool definitions and the system prompt. Measure them, tool by
tool, with `python scripts/token_baseline.py` (`--log` adds the calls and result sizes of the
journal); `/context` and `/usage` give the live and real numbers.

| Change | Static prefix (estimated tokens) |
|---|---|
| Baseline: 34 tool definitions + system prompt | 5 223 (tools 4 449, prompt 774) |
| Tool descriptions cut to "when to use" + non-obvious constraints, one-line parameter docs | tools 3 628 |
| System prompt rewritten without what the tool descriptions already say | prompt 442 |
| **Now** | **4 070 (-22 %)** |

Not changed here, because the console already has them: tool results as terse plain text with
size limits tied to the context window, `/tools` to switch tool groups off (the Moodle group
alone is ~700 tokens), `/compact`, sub-agents (`task`) for noisy exploration. Per-turn tokens
before/after and the success rate are not measured: use the console for a while with `/usage`.
