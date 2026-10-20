# Custom console rebuild plan

Oct 19, 2026 · @Erwan Mercier

Custom console is rebuilt from zero as a Textual app that speaks the new token-only Clara API. It keeps the shell and the agent console, drops the side features, and fixes every finding of the code review.

## Goal and decisions

Four goals drive the rewrite: make the API calls work with the new Clara, redesign the interface, add the missing shell commands, and add path autocompletion. Every choice below comes from your answers in the question rounds; anything I decided myself is listed in "Decisions I made for you, to confirm".

| Topic | Decision |
| --- | --- |
| Language and stack | Python 3.11+ and Textual for both modes (not prompt\_toolkit) |
| Platforms | Windows first, Linux and macOS supported; Windows-only code sits behind an interface; CI runs on both |
| Old code | Nothing reused. New repository; the old code is a reference only |
| Migration | None. Fresh settings, sessions, rules and ledger |
| Product language | English everywhere: interface, agent prompt, docs, comments |
| Modes | Two, as today: the stand-alone shell (Textual inline mode) and the full-screen `ai agent` console |
| Clara connection | Token only (`Authorization: Bearer clara_<id>_<secret>`) plus an `X-Clara-Client` label; no user name or password |
| Configuration | Token file, `CLARA_URL`, and a `.env` for non-secret settings; every problem reported at once |
| Permissions | A short read-only allowlist; everything else asks. "Always" covers the exact command and all its paths. A new folder asks once to be trusted |
| reMarkable | `rmapi` kept behind a backend interface; `.rmdoc` converted to PDF |
| Pipes and redirection | Built-in commands only; external programs run as today, without piping |
| Autocompletion | Tab menu plus inline ghost text, for virtual paths, commands, flags and history; no fuzzy matching |
| Old sessions | Only the newest 5 per folder are listed; the others stay on Clara and are never deleted |
| `/restore` | Lists this folder's conversations only |
| Server changes | Small additions allowed; they are listed as tasks for Clara |
| This plan | A doc on claude.ai, in phases with gates and no dates, shaped like Clara's own rebuild plan |

## Scope

17 features stay, 8 are removed, and 11 are new or changed. Nothing from the old code is copied: a kept feature is rewritten against the new design.

### Kept

| Feature | What happens to it |
| --- | --- |
| Shell and virtual file system | Rewritten on Textual inline mode; drives, WSL and reMarkable stay |
| reMarkable | `rmapi` stays, behind a backend interface; `.rmdoc` is converted to PDF |
| Agent console | Rewritten as a client of the new Clara API |
| App launcher and finder (`launch`) | Stays in the shell; cross-platform behind an interface |
| Desktop tools | `open_path`, `launch_app`, `clipboard_read`, `clipboard_write` become `local_*` tools |
| Saved sessions and `/restore` | Each folder maps to Clara conversations; `/restore` lists this folder's only |
| `/undo` checkpoints | Stays, with a safer write path (see the review table) |
| Sub-agents (`task`) | Run as a separate incognito Clara conversation with fewer local tools |
| Git tools | `status`, `diff`, `log`, read-only |
| `pdf_to_markdown` | Stays as a local tool |
| Questions and checklist | `ask_user` and `todo_write` stay |
| `/compact`, `/context`, `/usage` | Stay; usage comes from Clara's `usage` events |
| `AGENT.md` and `/init` | Stay |
| `@file` mentions and `!command` | Stay; `/` and `!` escaping fixed |
| Journal and transcript viewer | Stay; secrets redacted before writing |
| Approvals list and rules viewer | `/approvals`, and `/rules` to view and test, on Clara's `/v1/approvals` and `/v1/rules` |
| Clara conversation options | Incognito, local-only, model picker, tool packs, projects, message versions (see New) |

### Removed

| Feature | Why |
| --- | --- |
| Moodle | You chose not to keep it; the review findings SEC-6 and N7 disappear with it |
| Email (SMTP) tool | You chose not to keep it; the SMTP TLS finding SEC-2 disappears with it |
| Local Ollama commands (`ai list`, `ai start`) | Clara reaches its model hosts itself; `/model` covers the choice |
| Weather and location tools | You chose not to keep them |
| Client-side reminders, tasks and to-do commands | You chose not to keep them; Clara has reminders on the server |
| Memory commands (`/facts`, `/remember`, `/forget`) | You chose not to keep them; Clara keeps memory on the server |
| Conversation search (`/conversations`) | You chose not to keep it; `/restore` covers this folder |
| Live notification pop-ups | Not selected; the server still has the event stream if you want them later |

### New or changed

| Feature | Detail |
| --- | --- |
| `mkdir`, `mv`, `touch`, `rmdir` | New shell commands, on the virtual file system |
| `grep`, `head`, `tail`, `wc`, `diff` | New shell commands |
| Pipes and redirection | The pipe, >, >> and < work between built-in commands only |
| `alias`, `history`, `which`, `env`, `open`, `clip` | New shell commands |
| Path autocompletion | Tab menu plus ghost text: virtual paths, commands, flags, history |
| Permissions | Read-only allowlist, "always" with all paths, trust-folder prompt |
| Configuration | Token file, `CLARA_URL`, validated `.env` |
| Attachments | `@file` sends PDF, Word and images as real attachments (text stays inline); Ctrl+V pastes a clipboard image |
| Model picker and tools | `/model`, tool packs, `/tools` toggles for local tools |
| Incognito and local-only | Choosable per conversation |
| Projects and message versions | Clara projects; `fork_of` versions of a message |

## Architecture

The console is one Python package in one process: five layers, dependencies pointing down only. Conversations, model calls and the server rule gate live on Clara; the console runs the local tools and shows the result.

&#91;embedded content: architecture · 5 layers, 2 outside systems\]

A request enters at the top and each layer calls only the one below it. The Clara client and the platform layer are the only code that leaves the program.

### Layers

| Layer | Holds | Rule |
| --- | --- | --- |
| Screens | Shell app (inline), agent app (full-screen), prompt box, completion popup, transcript, approval prompts | Draw and read keys only; never touch disk, network or processes |
| Commands | Shell command registry, parser (pipes, redirection, aliases), agent slash commands | One function per command; it returns data, not printed text, so pipes work |
| Core | Virtual file system, permission engine, local tools, sessions, undo, journal, settings | Plain Python with no Textual import, so it is testable alone |
| Clara client | HTTP and SSE client, token reading, event types | The only code that knows Clara's routes |
| Platform | Desktop adapter (open, launch, clipboard, app finder), process runner, `rmapi` backend, path rules | One interface per OS-specific job, with a Windows, a Linux and a macOS implementation |

The shell starts as a Textual inline app (prompt at the bottom, output in the normal terminal scrollback). `ai agent` closes it, runs the full-screen agent app in the same process, and returns to the shell when the agent ends.

### One message, step by step

1. You type a message. `@file` mentions are resolved and attachments are prepared.
2. The client posts to `/v1/chat/stream` with the message, the time zone, the conversation (or `incognito` / `local_only`), the schemas of the local tools and the console's instructions.
3. Clara streams events. `token` and `thinking` fill the transcript; `tool_start` and `tool` show the server's own tools.
4. An `approval` event shows a prompt. The answer goes to `/v1/approvals/{id}/decide` as `allow_once`, `allow_always`, `refuse_once` or `refuse_always`.
5. A `tool_requests` event lists local tool calls. For each one the permission engine allows, asks or refuses; the tool runs; all results of the round are posted together to `/v1/turns/{id}/tool-results`, and the same stream carries on.
6. `usage`, `compacted` and `done` update the local usage ledger and the saved session; an `error` event ends the turn with a readable message.

### Technology

| Need | Choice |
| --- | --- |
| Interface | Textual (inline for the shell, full-screen for the agent), Rich for rendering |
| HTTP and streaming | `httpx`, async; server-sent events read line by line |
| Packaging | `pyproject.toml`, installed with `uv tool install` |
| Tests | `pytest`, Textual's pilot for screens, a fake Clara server for the client |
| Lint and types | `ruff` and `mypy` (my choice, to confirm) |

## The Clara client

The console becomes a thin client of Clara's token-only API: one token file, one streaming route for turns, and one route to hand local tool results back. This is the part of the old code that no longer works, so it is rebuilt first.

### Authentication and address

- Every request carries `Authorization: Bearer clara_<id>_<secret>` and `X-Clara-Client: console`. The server already accepts `console` as a client label, and a `console` token can answer all approvals by default.
- The token lives in a file only you can read: `%APPDATA%\clara\token` on Windows, `~/.config/clara/token` elsewhere. Windows checks the file's access list instead of mode bits. A file others can read is refused.
- `--save-token` asks for the token with hidden input and writes the file; `--token-file` and `CLARA_TOKEN_FILE` point elsewhere. `CLARA_URL` defaults to `http://127.0.0.1:8765`.
- At start the console calls `GET /v1/auth/me`. A 401 prints one line: the token is wrong or revoked, run `--save-token`.

### Routes used

| Route | Use |
| --- | --- |
| `POST /v1/chat/stream` | One turn as server-sent events. The only chat route: `POST /v1/chat` cannot carry local tools |
| `POST /v1/turns/{id}/tool-results` | Results of local tools; only the token that opened the turn may send them |
| `GET /v1/approvals`, `POST /v1/approvals/{id}/decide` | List pending requests; answer one |
| `/v1/conversations` | Create (with packs and project), list, get, rename, delete, `messages`, `compact`, `title`, `version` |
| `GET /v1/models` | The `/model` picker |
| `GET /v1/rules`, `POST /v1/rules/test` | Read-only rules viewer and tester |
| `GET /v1/auth/me` | Check the token at start |
| `/v1/projects`, tool packs | Pickers; exact shapes are read in Phase 0 |

Not used: `/v1/documents/extract` (the server prepares attachments itself), `/v1/events/stream` (notifications are removed), memory and workspace routes.

### The chat body

`message` (up to 2,000,000 characters), `conversation` (`c` plus 16 hex digits, or absent for a new one), `attachments` (`name` and base64 `data`), `project`, `model`, `fork_of`, `timezone`, `tools` (up to 64 schemas), `instructions` (up to 20,000 characters), `incognito`, `local_only`. A request with no text and no attachment is refused with 422.

### Events and what the console does

| Event | Console action |
| --- | --- |
| `turn` | Keep the turn id, the conversation id and the model; show the model if it changed |
| `thinking` | Show dimmed, collapsible |
| `token` | Append to the answer |
| `retrying` | Show the server's notice and the fallback model |
| `tool_start`, `tool` | Show the server tool and its result in one line |
| `approval` | Open an approval prompt |
| `tool_requests` | Run the local tools (see Local tools and permissions) |
| `usage` | Add prompt, completion and cached tokens to the local ledger |
| `compacted` | Show "compacted: before to after" |
| `discarded` | Show a note that unanswered tool calls of an earlier turn were dropped |
| `warning` | Show the message |
| `done` | Finish the turn; save the conversation id |
| `error` | Show a readable message for its `code`; end the turn |

Lines that do not start with ` data:  ` are ignored. The server sends a `: keepalive` comment every 15 seconds, so the console sets a read timeout of 60 seconds: silence longer than that means a dead connection.

### The local-tools protocol

- Local tool names start with `local_`. The server owns `ws_*`, `remember`, `web_fetch`, `qcm`, `ask_person` and others; a collision is refused.
- A `tool_requests` event carries `calls` with `id`, `name` and `arguments`. The console answers **all calls of the round in one POST**, each result a string tied to its `id`. A refused call still gets a result saying it was refused.
- The server waits at most 900 seconds for the answer (`CLARA_CLIENT_TOOL_TIMEOUT`), then the turn fails with a 504 `timeout`. A permission prompt plus the run must fit in that time, so long commands run in the background.
- The server waits 600 seconds for an approval; after that the turn goes on without the action.

### Errors

HTTP errors come as `{"error": {"code", "message"}}`; a failure during streaming arrives as an `error` event with `status`, `code` and `message`. The console maps each code to one line and one next step.

| Code (status) | Meaning | Console shows |
| --- | --- | --- |
| `unauthorized` (401) | Token wrong or revoked | Run `--save-token` |
| `too_large` (413) | Prompt too large | Suggest `/compact` |
| `unprocessable` (422) | Bad request, empty message, bad attachment | The server's message |
| `conflict` (409) | Nothing to compact or title | The server's message |
| `rate_limited` (429) | Too many requests or wrong tokens | Wait and retry |
| `model_failed` (502) | The model failed | Retry or `/model` |
| `unavailable` (503) | Server stopping, or no model | Say which |
| `timeout` (504) | Model or local-tool wait too long | Retry; use background runs |

### What changed from the old client

| Old console | New console |
| --- | --- |
| `POST /v1/auth/login` with user name, password and surface, and a re-login after a 401 | A token file; no login call, no re-login |
| Identity parameters on many calls | None: the token says who is calling |
| `/v1/settings`, `PUT /v1/models/choice` | The model goes in the chat body; `GET /v1/models` lists choices |
| `/v1/reminders`, `/v1/tasks`, `/v1/notifications/stream` | Removed from the console (server-side features) |
| Approvals decided inside the console only | Server `approval` events answered at `/v1/approvals/{id}/decide` |

## Local tools and permissions

Local tools are `local_*` functions the model asks for through `tool_requests`. Every call passes one permission engine, which asks you unless the call is on a short read-only allowlist inside a trusted folder. The engine is the main fix for the review's security findings.

### The 16 tools

Sixteen schemas are far below the 64 Clara accepts. `/tools` switches single tools off; a tool that is off is not sent.

| Tool | Does | Default |
| --- | --- | --- |
| `local_list` | List a folder, virtual paths included | Allowed |
| `local_read` | Read a text file, with offset and limit | Allowed |
| `local_search` | Find files by name; search text in files | Allowed |
| `local_write` | Create or overwrite a file | Asks |
| `local_edit` | Replace exact text in a file | Asks |
| `local_run` | Run a command, with a timeout or in the background | Asks, unless on the read-only allowlist |
| `local_job` | Status, output or stop of a background command | Allowed |
| `local_git` | `status`, `diff`, `log` | Allowed |
| `local_pdf_to_markdown` | Convert a PDF to Markdown text | Allowed |
| `local_open_path` | Open a file or folder with its default app | Asks |
| `local_launch_app` | Start an installed app found by the finder | Asks |
| `local_clipboard_read` | Read the clipboard | Asks, every time |
| `local_clipboard_write` | Replace the clipboard | Asks, every time |
| `local_ask_user` | Ask you a question with choices | Allowed |
| `local_todo_write` | Write the checklist | Allowed |
| `local_task` | Run a sub-agent | Allowed |

"Allowed" means allowed only when the floor below does not escalate the call.

### How one call is decided

1. **Resolve.** Every path goes through the virtual file system. The engine checks both the path as written and its real path after following links, so a link into a sensitive place does not slip through.
2. **Floor.** These always ask and can never get an "always" rule: sensitive files (`.env`, the token file, `.ssh`, credential and browser-profile folders, the console's own data folder), writes under `.git`, writes outside the trusted folder, and network paths other than `\\wsl$` and `\\wsl.localhost`.
3. **Tainted turn.** After any server tool that read outside content (web, GitHub, Drive, an attachment), every local write and every command asks, whatever the rules say. This mirrors Clara's own rule. A server tool the console does not know counts as outside content.
4. **Allowlist.** Read-only tools inside the trusted folder run without asking. For `local_run`, the allowlist is a short fixed list: `pwd`, `ls`, `dir`, `cat`, `type`, `head`, `tail`, `wc`, `grep`, `which`, and `git status`, `git diff`, `git log`, with no redirection and no chaining.
5. **Stored rules.** An "always" answer stores the tool, the exact command and all of its paths together. It matches only that same call, and it is never applied to a call the floor or a tainted turn escalated.
6. **Ask.** The prompt shows the tool, the exact command or a diff, and every path. Long previews stop after a set number of rows and end with "... N more lines". Answers mirror Clara's: allow once, allow always, refuse once, refuse always.

### Trusted folders

- Starting the agent in a new folder asks once: "Trust this folder?" The answer is remembered by a hash of the real folder path.
- In an untrusted folder the agent gets no write or run tools, and `AGENT.md` is not loaded, because a file there could carry instructions meant for the model.
- Reading sensitive files, and any path outside the trusted folder, still asks.

### Running commands

- Commands run with a scrubbed environment. The console never puts the token or `.env` secrets into its own environment, and it strips variables whose names suggest secrets (`*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASSWORD*`, `CLARA_*`) before starting a child.
- A command has a default timeout. Longer work uses `background: true` and returns a job id that `local_job` follows. This keeps every round well inside Clara's 900-second wait.
- A result is cut to a fixed size, keeping the start and the end, with a note of how much was left out.
- Secret-looking text (Clara tokens, bearer headers, private-key blocks, `NAME=value` for secret names) is redacted before it reaches the journal or a saved session.
- In the stand-alone shell you type the commands yourself, so no permission prompt applies there.

## The shell

The shell keeps the old virtual tree and command names, adds 16 commands (your 15 and unalias), pipes between built-ins, aliases and a completion popup with ghost text. Every command, pipe and completion goes through one path resolver, so a drive, a WSL distribution and the reMarkable behave alike.

### Virtual file system

The path syntax you know stays. On Windows the tree has a virtual root:

| Path | Backend |
| --- | --- |
| `/` | Virtual root: lists drives, WSL distributions and the reMarkable |
| `/C:/Users/...` | A Windows drive |
| `/wsl-Ubuntu/home/...` | A WSL distribution (`\\wsl.localhost\Ubuntu`) |
| `/reMarkable/Notes`, `reMarkable:Notes` | The tablet, through `rmapi` |

On Linux and macOS the real `/` is the root and the reMarkable is reached only as `reMarkable:path` (to confirm). Each backend implements one interface: list, stat, read, write, mkdir, rename, remove, rmdir, touch, copy, and a set of flags saying what it supports. A command asks the flags first, so an unsupported action gives one clear message instead of an exception.

### Commands

| Group | Commands |
| --- | --- |
| Navigate | `cd`, `pwd`, `ls`, `tree`, `stat`, `find` (kept) |
| Read | `cat` (kept); `head`, `tail`, `wc`, `grep`, `diff` (new) |
| Change | `cp`, `rm` (kept); `mkdir`, `mv`, `touch`, `rmdir` (new) |
| Shell | `echo`, `clear`, `exit`, `help`, `reload` (kept); `alias`, `unalias`, `history`, `which`, `env` (new) |
| Desktop | `launch` (kept); `open`, `clip` (new) |
| reMarkable | `rmdoc2pdf` (kept) |
| Agent | `ai agent` (kept); `ai list` and `ai start` are removed |

Behaviour of the new commands:

- `mkdir` (with `-p`), `rmdir`, `touch`, `mv`: work on every backend that supports them. `mv` across backends copies, checks the size, and only then deletes the source.
- `mv` and `cp` refuse to overwrite unless given `-f` (to confirm for `cp`).
- `grep` takes `-i`, `-n`, `-r`, `-v`, `-c`; `head` and `tail` take `-n`; `wc` takes `-l`, `-w`, `-c`; `diff` prints a unified diff of two files.
- `which` says whether a name is a built-in, an alias or an external program, and where it lives. `env` prints the environment with secret-looking values hidden. `history` shows the saved history.
- `open` opens a path with its default app (a reMarkable file is downloaded to a temporary file first). `clip` copies its input, a file or text to the clipboard; `clip -p` prints the clipboard.
- `ai agent` takes `--incognito`, `--local-only` and `--model`; the rest is set with slash commands inside the agent.

### Pipes, redirection and aliases

- A built-in reads lines from its input and writes lines to its output, so `cat notes.txt | grep todo | wc -l` works, and `>`, `>>` and `<` read or write a path on any backend that allows it.
- An external program runs as it does today, attached to the terminal. A pipe or redirection that includes one is refused with a message saying that pipes work between built-ins only.
- The parser handles single and double quotes, `~`, and escaping that does not break Windows paths: a backslash is an ordinary character outside quotes.
- `alias ll='ls -l'` stores an alias in a file in the data folder. Only the first word is expanded, once, so an alias cannot loop. `which` shows what an alias hides.
- History is saved in the data folder, capped, with consecutive duplicates collapsed. Lines that look like they hold a secret are not saved.

### Path autocompletion

The completion engine is plain Python with no Textual import: it takes the typed line and the cursor and returns candidates. The prompt box only draws them.

- **Tab** opens a menu under the prompt; if only one candidate fits, or all share a prefix, Tab completes it. **Ghost text** shows the most recent matching history line in dim type after the cursor; Right arrow or End accepts it. Esc closes the menu.
- **First word:** built-ins, aliases, and app names after `launch`. **Flags:** each command declares its options, and the engine completes those. **Paths:** each command declares which arguments are paths, files or folders only.
- **Virtual paths:** the engine completes `/`, `/C:/`, `/wsl-Ubuntu/`, `/reMarkable/...`, `reMarkable:`, `~` and relative paths. Matching ignores case on Windows. Folders end with `/`. Hidden entries show only when the typed part starts with a dot. Names with spaces are quoted for you.
- **Slow backends:** completion runs in a background worker, a new keystroke cancels the old request, and the menu shows a spinner and "loading" until the listing arrives. Listings are cached for a short time and cleared by any write. A reMarkable listing that takes more than a few seconds gives up with a message.
- **In the agent:** the same engine completes slash commands, `@file` mentions and `!` commands.

### reMarkable

- The backend calls `rmapi` through the process runner, with no input attached, a timeout, and a readable message for each failure: not installed, not signed in (it would otherwise wait for a code), tablet cloud unreachable, file not found.
- Listings are cached with a short lifetime and cleared after a change. A spinner shows while a call is running.
- `rmdoc2pdf` converts a `.rmdoc` file into a PDF; the converter is its own module with tests on sample files.
- Because the backend is an interface, `rmapi` can be replaced later without touching commands or completion.

## The agent console

`ai agent` opens a full-screen Textual app: a transcript on top, a status line, and one prompt box at the bottom. Clara does the thinking; the console runs the local tools, asks for permission and keeps the local records.

### Screen

- **Transcript:** your messages, answers rendered as Markdown, thinking collapsed, one line per tool call (expandable), diffs for edits, and permission prompts inline.
- **Status line:** folder, conversation title, model, badges for incognito and local-only, context fill, tokens used this session, running background jobs.
- **Prompt box:** Enter sends, Shift+Enter adds a line, Up and Down walk the history, Esc stops the running turn, Ctrl+V pastes text or a clipboard image. The completion popup from the shell works here too.
- **Approvals:** Clara's server approvals and the console's local prompts look the same and take the same four answers (o, a, r, n).
- **Speed:** streamed text is drawn in batches rather than per token, and finished messages are not redrawn. This answers the old repaint cost finding (PERF-1).

### Slash commands

| Command | Does |
| --- | --- |
| `/model [name]` | Show or choose the model; the list comes from `GET /v1/models` and the choice goes in the chat body |
| `/tools [list\|on\|off\|reset]` | Switch local tools and Clara tool packs on or off for this conversation |
| `/clear [--incognito] [--local-only]` | Start a new conversation and clear the screen |
| `/project [id\|none]` | Choose the Clara project for new conversations |
| `/retry`, `/version [N]` | Answer again as a new version of the last message (`fork_of`); switch between versions. Shapes confirmed in Phase 0 |
| `/restore [list\|N]` | Bring back a saved session of this folder |
| `/undo` | Undo the file changes of the last turn |
| `/compact [focus]` | Compact the conversation through Clara |
| `/context` | How full the context window is |
| `/usage [window]` | Tokens from the local ledger: this session, 24 hours, 7 days, all |
| `/approvals` | List Clara's pending approvals; answer one by number |
| `/rules [test ...]` | Show the local rules and Clara's rules; test what a call would do |
| `/todo` | Show the agent's checklist |
| `/plan [on\|off]` | Read-only mode: only read tools are offered (to confirm) |
| `/init` | Create `AGENT.md` |
| `/journal`, `/transcript` | Open the journal or the full conversation |
| `/help`, `/exit` | Help and quit |

A message that must start with `/` or `!` starts with `//` or `!!`; the doubled character is removed before sending. This fixes the old escaping bug (BUG-22). `/undo` is the console's own and touches local files only; Clara's workspace undo is never called.

### Sessions and `/restore`

- A session is one Clara conversation. Clara keeps the messages; the console saves a small file per session in `<data>/sessions/<folder hash>/`: conversation id, title, times, model, tool toggles, checklist and totals. It is written after every turn, atomically.
- `/restore` lists the newest 5 sessions of this folder. Older ones stay on Clara and are not listed or deleted. Restoring re-reads the messages from Clara.
- A session file never holds permissions, rules or trust. Restoring cannot raise anything (SEC-7).
- An incognito conversation is not saved locally. Clara deletes it after 24 hours.

### Undo

1. Before each `local_write` or `local_edit`, the console copies the original file to `<data>/undo/` and records its path, hash and modification time, or that it did not exist.
2. `/undo` goes back through the last turn's records in reverse. A file changed by someone else since the console wrote it is skipped with a warning.
3. A restore is written to a temporary file in the same folder and swapped in with `os.replace`, so a crash cannot leave a half-written file. A new file is deleted only after the same check.
4. A record is removed only after its file is restored. A failure keeps it and names the file.
5. `local_run` changes cannot be undone, and its permission prompt says so.
6. Overwriting an existing file requires reading all of it first in this session; a partial read does not unlock it (BUG-5, BUG-6).

### Sub-agents

`local_task` starts a separate incognito Clara conversation with its own instructions and a restricted set of local tools: the read-only ones only, plus Clara's server tools. It cannot start another sub-agent, has a round limit and a time limit, and returns one text result, cut to a fixed size. The transcript shows it as a collapsible block.

### Attachments, `@file` and `!command`

- `@path` completes like a shell path. Small text files go into the message as a fenced block with their path. PDF, Word and image files are sent as `attachments` (base64) and Clara extracts their content. A file over 30 MB is refused before sending.
- Ctrl+V with an image on the clipboard adds a PNG attachment, shown as a chip in the prompt. Where the system has no clipboard tool, the console says so.
- `@` on a sensitive path asks first, like any other read.
- `!command` runs a command now, as you, without a permission prompt. Its output appears in the transcript and goes with your next message as context.

### `AGENT.md`, journal and transcript

- `AGENT.md` in a trusted folder is read at start and sent in `instructions`. The console's base prompt (a plain `prompt.md` file in the package), the tool guidance and `AGENT.md` must fit in the 20,000 characters Clara allows. A check script builds the real prompt with all tools and fails the build above a set size.
- The journal is a JSON Lines file per session: turns, local tool calls with their arguments, permission decisions with the reason, approval answers and errors. Secrets are redacted before writing, and the file is capped and rotated.
- `/transcript` shows the full conversation from Clara's messages, with search.

## Configuration, secrets and data on disk

Settings come from four sources in a fixed order and are validated all at once. The only secret, the Clara token, lives in its own file and never enters the process environment. This follows the way Clara's own command-line client is configured.

### Where a setting comes from

1. Command-line options.
2. Environment variables.
3. A `.env` file in the config folder, for non-secret settings only. It is read with a parser and never loaded into `os.environ` (SEC-1). A key that looks like a secret, such as a token or a password, stops the start with a message pointing to the token file.
4. Built-in defaults.

If anything is wrong, the console prints every problem in one list and exits: unknown names (with a "did you mean" hint), wrong types, out-of-range values, an unreadable token file. Nothing starts half-configured.

### Settings

| Name | Default | Meaning |
| --- | --- | --- |
| `CLARA_URL` | `http://127.0.0.1:8765` | Clara's address. A plain `http://` address that is not loopback prints a warning |
| `CLARA_TOKEN_FILE` | `%APPDATA%\clara\token`, or `~/.config/clara/token` | Where the token is read; shared with Clara's own client |
| `CONSOLE_DATA_DIR` | The platform's data folder | Where sessions, undo records, the journal and rules live |
| `CONSOLE_TOOL_TIMEOUT` | 120 | Seconds before a foreground `local_run` is stopped |
| `CONSOLE_RESULT_LIMIT` | 30000 | Characters kept of one tool result |
| `CONSOLE_RMAPI` | Found on the path | Path to `rmapi` |
| `CONSOLE_LOG_LEVEL` | `info` | Log detail |

The defaults of the two `CONSOLE_` limits are my choice, to confirm.

### Data on disk

The config and data folders come from the platform: `%APPDATA%` on Windows, the XDG folders on Linux, `Application Support` on macOS. They are created readable only by you. At start the console checks them: on Linux and macOS it tightens a folder that is too open and says so; on Windows it checks the access list and warns.

| What | Where | Notes |
| --- | --- | --- |
| Token | `clara/token` | The only secret; mode 600 or an access list for you only |
| Sessions | `sessions/<folder hash>/<id>.json` | Metadata only; files beyond the newest 5 of a folder are removed, the conversations stay on Clara |
| Undo records | `undo/` | Original file copies; the last 20 turns are kept |
| Journal | `journal/<session>.jsonl` | Redacted; capped and rotated |
| Local rules | `rules.json` | "Always" and "never" answers; shown by `/rules` |
| Trusted folders | `trust.json` | Hashes of real folder paths, not the paths |
| Usage ledger | `usage.jsonl` | Token counts from `usage` events |
| Shell history and aliases | `history`, `aliases.json` | Secret-looking lines are not saved |
| Log | `console.log` | Events and errors only: no message text and no tool arguments |

A `--debug` option adds request and response metadata to the log. It never writes bodies or headers.

## How the review findings are fixed

Every finding of the code review (SEC, BUG, PERF, MNT) and of my verification (N1 to N7) is either fixed by design or removed together with its feature. The phase gates use this table as their checklist.

### Security

| Finding | Problem | Fix in the rewrite |
| --- | --- | --- |
| SEC-1 | `.env` secrets inherited by every child process | `.env` is parsed and never loaded into `os.environ`; children get a scrubbed environment |
| SEC-2 | SMTP login over unverified TLS | Gone: there is no email tool |
| SEC-3, N2 | "Always" rules approve far more than expected; the denylist misses `if`, `call`, `git config`, `uv pip install`, `certutil` and more | Allowlist for commands; a rule holds the exact command and all its paths; interpreters, installers and config changes can never be remembered |
| N1 | A remembered copy opened sensitive files without a question | A stored rule is never applied to a call the floor or a tainted turn escalated |
| SEC-4 | Sensitive-path check ignored symlinks | Both the typed path and its real path are checked |
| SEC-5 | The `open_path` list of forbidden executable types was incomplete | An allowlist of passive file types; it asks every time |
| SEC-6, N7 | Moodle tools not confined to the Moodle site; a silent outbound channel | Gone with Moodle |
| SEC-7 | `/restore` silently re-applied the saved permission level | Session files hold no permission data |
| SEC-8 | The trust boundary was whatever folder you launched in | Trust-folder prompt; no write or run tools and no `AGENT.md` in an untrusted folder |
| SEC-9 | Plain-text retention of everything the agent saw | Metadata-only sessions, a redacted journal, incognito |
| SEC-10 | `git` runs programs named in a repository's config; `cd` works anywhere | `local_git` runs only in trusted folders with external diff and file-monitor hooks off; there is no `cd` tool |
| N3 | UNC paths could leak credentials to another host | The path resolver refuses network paths except WSL |
| N4 | A long command hid its tail and the answer keys in the prompt | Rows are capped with "... N more lines"; the answer keys always show |
| Verification | The plain-http warning missed private addresses | Warning for every address that is not loopback |
| Verification | No guidance about prompt injection in the agent prompt | The base prompt says tool results and files are data, not instructions |
| Verification | The README sentence "never leaves this machine" was misleading | The docs say exactly what goes to Clara, and what incognito and local-only change |

### Bugs

| Finding | Problem | Fix in the rewrite |
| --- | --- | --- |
| BUG-1, 2, 3, N5 | Moodle click, deadline and browser-leak bugs | Gone with Moodle |
| BUG-4, 9, 23 | `@name` taken as a target, reminder listener dying, time-zone differences | Gone with the reminder and task commands; the time zone is set in one place, the chat body |
| BUG-5, N6 | `/undo` lost the original after move-then-edit, overwrote later edits, and deleted before restoring | Moves are recorded; a staleness check; temporary file plus `os.replace`; the record is kept until the restore succeeds |
| BUG-6, BUG-21 | A 3-line read unlocked overwriting the whole file; guards were inconsistent | One overwrite guard for write, edit, `mv`, `cp` and redirection: the whole file was read, or the file was created in this session |
| BUG-7 | Pending notes lost when the request failed | Notes stay queued until the first stream event arrives |
| BUG-8 | Ctrl+C before the response headers did not stop the server turn | Esc closes the connection at any point; Phase 1 checks that Clara stops the turn (a server task if not) |
| BUG-10 | The integer 0 was dropped from request bodies | Bodies are built from typed models, and checks use `is None` |
| BUG-11 and the ledger | Sub-agent tokens, and failed or cancelled turns, missing from the statistics | Every `usage` event is recorded, labelled by sub-agent, even when the turn fails |
| BUG-12, 13, 14, 15 | Read details, grep and read disagreeing on line numbers, no `{a,b}` globs, mode checks rejecting full paths | One path resolver, one line-numbering function and one glob matcher, each with tests |
| BUG-16, 17, 18 | Silent partial `cp -r` from the reMarkable, any "429" read as rate limiting, `rmapi` waiting for input | A result per file with a final count; errors classified from `rmapi`'s status line only; input closed and a timeout |
| BUG-19, 20 | Windows: recursive delete of read-only files; `tree` aborting on OS errors | `rm -r` clears the read-only flag and retries; `tree` marks unreadable entries and goes on |
| BUG-22 | Messages starting with `/` or `!` were always commands | `//` and `!!` send a literal first character |
| BUG-24 | Cosmetic wrapping and message order | New renderer, covered by tests |

### Performance and maintenance

| Finding | Problem | Fix in the rewrite |
| --- | --- | --- |
| PERF-1 | Repaint cost grew with the whole conversation | Batched streaming; finished messages are not redrawn |
| PERF-2 | A failing edit could freeze the agent | The "closest passage" search has a time budget |
| PERF-3 | Descriptions built even when no question was asked | Built only when a prompt is shown |
| PERF-4 | Unbounded results and tools that cannot be cancelled | A result limit; one cancel token passed into every backend call |
| PERF-5, PERF-6 | Session persistence; Moodle scraping | Small metadata files written atomically; Moodle is gone |
| MNT-1 | The test suite was red and nothing noticed | CI on Windows and Linux, required before merge |
| MNT-2 | Coverage gaps | A fake Clara server; negative tests for the permission rules |
| MNT-3, MNT-5 | Dead code and fragile structure | New repository; layers with import rules checked by a test |
| MNT-4 | Documentation drift; the token table was measured without `ask`, `task` and the desktop tools | A check script builds all 16 tools and the real prompt; README numbers come from it |
| Verification | No LICENSE file and no CI folder | A LICENSE (all rights reserved, as in Clara) and CI from Phase 0 |

## Repository layout and module map

A new repository, `custom-console`, with the package in `src/custom_console/`. Folders follow the five layers, and a test fails the build if an import goes the wrong way.

```text
custom-console/
  pyproject.toml          package, entry point, tool settings
  LICENSE                 all rights reserved, as in Clara
  README.md               what it is, install, first run
  docs/                   DECISIONS.md, PERMISSIONS.md, CLARA-CLIENT.md
  scripts/check_prompt.py builds the real prompt with all 16 tools, checks the size
  .github/workflows/      CI on Windows and Linux
  src/custom_console/
    __main__.py, cli.py   entry point, options, start-up checks
    settings.py           sources, .env parser, validation, SettingsError
    paths.py              config and data folders per platform
    clara/                client.py, events.py, stream.py, errors.py, types.py
    core/
      vfs/                resolver.py, backend.py, local.py, wsl.py, remarkable.py, rmdoc.py
      permissions/        engine.py, floor.py, allowlist.py, rules.py, trust.py, redact.py
      tools/              registry.py, one file per tool group (files, run, git, pdf, desktop, ask)
      session.py, undo.py, journal.py, ledger.py, subagent.py, attachments.py
    commands/
      parser.py, pipes.py, aliases.py, history.py
      shell/              one file per command group
      slash/              one file per slash command group
      completion/         engine.py, sources.py (paths, commands, flags, history)
    platform/             base.py (interfaces), windows.py, linux.py, macos.py, process.py
    ui/                   shell_app.py, agent_app.py, widgets/, theme.tcss
    prompt/               base.md (the console's instructions to the model)
  tests/                  unit/, integration/, fake_clara/, fixtures/
```

### Import rules

| Layer | May import | Must not import |
| --- | --- | --- |
| `ui` | `commands`, `core`, `clara`, `platform` | Nothing below it imports `ui` |
| `commands` | `core`, `clara`, `platform` | `ui` |
| `core` | `clara`, `platform` interfaces | `ui`, `commands` |
| `clara` | Standard library and `httpx` | Any other package module |
| `platform` | Standard library | Any other package module |

The check uses `import-linter` with these contracts, and runs in CI. Because `core` never imports Textual, most of the program is tested without a terminal.

### Key modules

| Module | Does |
| --- | --- |
| `clara/client.py` | Token reading, requests, the stream; the only place that knows routes |
| `core/permissions/engine.py` | The six-step decision for one call |
| `core/vfs/resolver.py` | Turns any typed path into a backend and a checked real path |
| `core/tools/registry.py` | The 16 tools: schema, permission class and handler in one place |
| `core/undo.py` | Checkpoints and `/undo` |
| `commands/completion/engine.py` | Candidates for a line and a cursor, no Textual |
| `platform/base.py` | Interfaces: desktop (open, launch, clipboard), process runner, path rules |
| `ui/agent_app.py` | The full-screen app: transcript, status line, prompt, prompts |

## Build phases with gates

Six phases, each closed by a gate that must pass before the next one starts. There are no dates: a phase is done when its gate is met. The riskiest unknowns (Textual inline mode, streaming on Windows, `rmapi`) come first, so a bad surprise costs a day, not a month.

&#91;embedded content: build phases · 6 phases, 6 gates\]

The diamonds are the gates, in short. The full criteria are under each phase below.

### Phase 0: spikes and skeleton

- **Build:** the repository, `pyproject.toml`, CI on Windows and Linux, `import-linter`, the LICENSE, and a skeleton of the fake Clara server.
- **Spike A:** Textual inline mode with the prompt at the bottom and a completion popup, then a switch to the full-screen app and back in one process. If it fails, the fallback is a menu drawn as plain lines under the prompt, or a different library for the shell only.
- **Spike B:** `httpx` streaming on Windows against the real Clara, with a `tool_requests` round trip and a cancel in the middle.
- **Spike C:** an `rmapi` wrapper that lists and fetches with input closed and a timeout.
- **Gate 0:** each spike has a written result and, if it failed, a chosen fallback; CI is green on both systems; a script completes one real turn with one local tool.

### Phase 1: Clara client and settings

- **Build:** settings with their four sources and validation, the token file (with the Windows access-list check), the client, typed events, error mapping, the usage ledger, cancel. The fake Clara server learns every event: approvals, tool requests, errors, keepalives.
- **Gate 1:** contract tests pass against the fake server; one real turn passes with an approval and a tool round; Esc cancels at any moment, including before the headers (BUG-8); the token appears in no log or screen.

### Phase 2: core and permissions

- **Build:** the path resolver, the local and WSL backends, the permission engine (floor, allowlist, rules, trust, redaction), the tool registry with the 16 tools (read tools first, then write and run), the scrubbed environment, undo, journal and sessions.
- **Gate 2:** every SEC and N row of the review table has a test that fails on the old behaviour and passes now: symlink, UNC path, copy rule, rule scope, tainted turn, environment scrub. Undo is tested for move-then-edit, a changed file and a failed restore.

### Phase 3: the shell

- **Build:** the parser (quotes, pipes, redirection), the kept and new commands, aliases, history, the completion engine with its popup and ghost text, the `rmapi` backend and `rmdoc2pdf`, the desktop adapter, `launch`, `open` and `clip`.
- **Gate 3:** each command is tested on local files, on WSL on the Windows runner, and on a fake `rmapi`; completion is tested for local, WSL and reMarkable paths; the shell is checked by hand in Windows Terminal and one Linux terminal.

### Phase 4: the agent console

- **Build:** the agent app, status line, transcript, prompts, every slash command, `/restore`, `/undo`, sub-agents, attachments and clipboard paste, `AGENT.md` and `/init`, the journal and transcript viewers, the model, pack, project and version commands.
- **Gate 4:** every row of the Kept table works end to end against the fake Clara, then in a manual pass against the real one; a long answer streams smoothly over a long transcript, against a budget written in Phase 0 (PERF-1).

### Phase 5: hardening and release

- **Build:** README and docs, the prompt check script, packaging for `uv tool install`, the walk through the review table, a fresh-eyes review of the permission engine and the path resolver, and the release tag.
- **Gate 5:** every row of the review table points to a passing test or a "removed" note; the README numbers equal the script's; a fresh install from the tag runs on Windows and Linux.

## Testing and quality

Almost everything is tested without a terminal, a tablet or a real Clara: a fake Clara server, a fake `rmapi` and Textual's test pilot stand in for them. CI runs the same suite on Windows and Linux, and a red build blocks the merge (MNT-1).

### Test layers

| Layer | What it checks | How |
| --- | --- | --- |
| Unit | Path resolver, permission engine, rules, redaction, undo, parser, pipes, completion engine | `pytest`, no Textual; property tests for the parser and the resolver |
| Contract | The Clara client against a fake Clara server over real HTTP: events, keepalives, every error code, approvals, tool requests, the 900-second wait, cancel | `pytest` with async tests |
| Backends | Local files; WSL on the Windows runner only; the reMarkable through a fake `rmapi` that prints success, errors, prompts and hangs | `pytest`, temporary folders |
| Screens | Typing, Tab menu, ghost text, prompts, slash commands, `/restore` | Textual's pilot |
| Negative permissions | A table of calls that must ask or be refused: a symlink to a key, a UNC path, a copy then a read, `python -m pip install`, `if exist x echo ok`, `git config`, a call after web content | One parametrized test; each row of the old reproduction scripts becomes a row |
| Prompt check | The real prompt and all 16 tool schemas fit Clara's limits; prints the size baseline | `scripts/check_prompt.py` |
| Live smoke | One real turn against a running Clara with a token | Marked `live`; run by hand, never in CI |

### CI

- GitHub Actions on Windows and Linux, with the oldest and the newest supported Python (3.11 and 3.13). This assumes the repository is on GitHub (to confirm).
- Steps: `ruff`, `mypy`, `import-linter`, `pytest` without the `live` mark, and the prompt check.
- Dependencies are pinned in a lock file, and update proposals are reviewed by hand, as in Clara.

### Rules

- Every bug fix comes with a test that fails first.
- No test touches the real home folder, the real clipboard or the real network; the platform adapter is replaced by a fake.
- Coverage is reported. The permission modules and the path resolver must have every branch covered (my choice, to confirm).
- Before a release, a manual checklist runs in Windows Terminal, in a Linux terminal, inside WSL, and against a real reMarkable.

## Risks and open points

The biggest risk is the terminal interface (Textual inline mode with a popup, then full-screen and back), so Phase 0 tests it first. The rest are manageable with the answers below.

### Risks

| Risk | Answer | Checked in |
| --- | --- | --- |
| Textual inline mode, the completion popup, or the switch to full-screen does not work well, especially in Windows Terminal | Spike A with a written fallback: a menu drawn as plain lines under the prompt, or another library for the shell only | Phase 0 |
| Completion feels slow on the reMarkable | Background worker, short cache, a newer keystroke cancels the old call, a timeout, a visible spinner | Phase 3 |
| Clara's API changes while it is being rebuilt | Typed events that ignore unknown fields and unknown event types; contract tests against the fake server; a live smoke test | Phase 1 |
| A slow answer to a permission prompt makes the round exceed Clara's 900-second wait | The prompt shows the time left; the round has a budget of 840 seconds; calls still waiting after that are refused with "no answer in time" (the 840 is my choice) | Phase 2 |
| Esc does not stop the turn on the server | Test with the fake server and the real Clara; if Clara keeps running the turn, a server task is added | Phase 1 |
| The list of server tools that read outside content is incomplete, so a tainted turn goes unnoticed | A tool the console does not know counts as outside content; the real list is read from Clara's tool code | Phase 2 |
| Windows-only behaviour (access lists, UNC paths, WSL, clipboard image, console encoding) breaks elsewhere | Platform interfaces with three implementations; CI on both systems. macOS has no CI unless it is added (to confirm) | Phases 2 to 3 |
| `rmapi` is a third-party tool built on an unofficial service and may change | Backend interface; the converter and the backend are separate modules | Phase 3 |
| Approval fatigue: too many prompts, so people stop reading them | A reasonable read-only allowlist; "always" covers exact command plus paths; `/rules` shows each rule so you can remove it | Phase 4 |
| `AGENT.md` and the base prompt together exceed the 20,000 characters Clara allows | `AGENT.md` is capped and a warning is shown; the check script fails the build if the base prompt alone is too big | Phases 4 to 5 |
| The amount of new work (16 commands, completion, a new interface, a new client) | The gates let you stop early: after Gate 3 the shell is usable on its own | All |

### Open points

- The exact shapes of `/v1/projects`, tool packs, `/version` and `fork_of` were not read. Phase 0 reads them from Clara's code.
- Whether Clara stops a turn when the client disconnects is unknown (see the Esc risk above).
- Several Clara files were staged but not read: `DECISIONS.md`, `DATA-MAP.md`, `OPERATIONS.md`, `PROVIDERS.md`, `REVIEW-RESPONSE.md`, `SECURITY.md`, the rule-gate code and the tool packs. Anything they change in the API contract is settled in Phase 0.
- My reproductions of the old bugs ran on Linux. Windows-only items (UNC paths, read-only deletes, `tree` errors, ACLs) are covered by tests that run on the Windows runner, but I have not run them myself.

## Decisions I made for you, to confirm

You did not answer these in the question rounds, so I chose a default for each. Tick what you accept and comment on what you want changed.

### Design

- [ ] Local tool names start with `local_`, as in Clara's own client, so they never collide with server tools.
- [ ] 16 local tools, including `local_job` so long commands run in the background.
- [ ] The tainted-turn rule is mirrored locally, and an unknown server tool counts as outside content.
- [ ] An untrusted folder gets no write or run tools and no `AGENT.md`.
- [ ] The read-only command allowlist is `pwd`, `ls`, `dir`, `cat`, `type`, `head`, `tail`, `wc`, `grep`, `which`, `git status`, `git diff`, `git log`.
- [ ] The old auto-accept levels 0, 1 and 2, `/permissions` and `AGENT_PERMISSION_LEVEL` are gone; the allowlist, the trust prompt and `/rules` replace them.
- [ ] The old `--no-memory` option becomes `--incognito`.
- [ ] A sub-agent is a separate incognito Clara conversation with read-only local tools, and cannot start another sub-agent.
- [ ] `/undo` touches local files only; Clara's workspace undo is never called.
- [ ] Session files hold metadata only, and local files beyond the newest 5 per folder are removed (the conversations stay on Clara).
- [ ] `/plan` stays (read-only mode), and `/retry` and `/version` handle message versions.

### Shell

- [ ] On Linux and macOS the real `/` is the root, and the reMarkable is reached only as `reMarkable:path`.
- [ ] `unalias` is added; `mv` and `cp` refuse to overwrite without `-f`.
- [ ] Pipes carry text lines between built-ins; `>`, `>>` and `<` accept any backend that allows them.

### Numbers and tools

- [ ] Foreground command timeout 120 seconds; tool result limit 30,000 characters; round budget 840 seconds.
- [ ] Keys: Enter sends, Shift+Enter adds a line, Esc stops a turn, Ctrl+V pastes.
- [ ] The `CONSOLE_` prefix for settings, and `platformdirs`-style folders per platform.
- [ ] `ruff`, `mypy`, `import-linter` and GitHub Actions on Python 3.11 and 3.13; macOS added to the CI matrix.
- [ ] Full branch coverage required for the permission modules and the path resolver.
- [ ] Installed with `uv tool install`; licence all rights reserved, as in Clara.

### Server tasks for Clara

The console works with Clara as it is. These are the small additions I would ask for, none of them blocking.

- [ ] Create a `console` token with the admin tool (approvals `all`, the default) and note the steps in Clara's README. No code change: `console` is already an accepted client label.
- [ ] If Phase 1 shows that Clara keeps running a turn after the client disconnects, stop the turn on disconnect.
- [ ] Optional: expose the list of server tools that read outside content, so the console does not have to guess it.
- [ ] Document the `local_` prefix for client tool names in Clara's API notes.

## Appendix: old modules and commands

No old code is copied. This map says where each old responsibility lands, so you can read the old module as a reference while building the new one.

### Old module to new module

| Old module | Did | New home |
| --- | --- | --- |
| `agent/clara.py`, `agent/remote.py` | Clara client, running a turn with local tools | `clara/client.py`, `clara/events.py`, the turn loop in `core/session.py` |
| `agent/turn.py`, `agent/usage.py`, `agent/context.py` | Turn model, statistics, context fill | `core/session.py`, `core/ledger.py` |
| `agent/console.py`, `ui.py`, `render.py`, `overlays.py` | The agent screen and its prompts | `ui/agent_app.py`, `ui/widgets/` |
| `agent/commands.py`, `slash.py` | Slash commands and their registry | `commands/slash/` |
| `agent/permissions.py`, `zone.py` | Permission levels and the free zone | `core/permissions/`: floor, allowlist, rules, trust |
| `agent/checkpoints.py`, `diffs.py` | Undo checkpoints, diffs | `core/undo.py`, a diff widget |
| `agent/journal.py`, `transcript.py` | Journal and transcript | `core/journal.py`, viewers in `ui/` |
| `agent/session.py`, `sessions.py` | Session state and saved sessions | `core/session.py` |
| `agent/subagent.py`, `tools/task.py` | Sub-agents | `core/subagent.py` |
| `agent/mentions.py` | `@file` mentions | `core/attachments.py` |
| `agent/questions.py`, `tools/ask.py`, `tools/todo.py` | Questions and checklist | `core/tools/` |
| `tools/filesystem.py`, `shell.py`, `git.py`, `pdf.py`, `desktop.py` | File, command, git, PDF and desktop tools | `core/tools/`, one file per group |
| `agent/toolset.py`, `schema.py`, `results.py`, `tools/base.py`, `tools/state.py` | Tool plumbing | `core/tools/registry.py` |
| `fs/virtual.py`, `fs/manager.py`, `fs/errors.py` | Virtual paths, file manager, errors | `core/vfs/resolver.py`, `backend.py`, `local.py`, `wsl.py` |
| `fs/remarkable.py`, `fs/rmdoc.py` | `rmapi` backend, `.rmdoc` to PDF | `core/vfs/remarkable.py`, `rmdoc.py` |
| `shell/repl.py`, `printer.py` | Shell loop, coloured output | `ui/shell_app.py` |
| `shell/tokenizer.py`, `completion.py` | Tokenizer, completion from `argparse` | `commands/parser.py`, `commands/completion/` |
| `shell/commands/` | Shell commands | `commands/shell/` |
| `apps/finder.py`, `launcher.py` | Find and start applications | `platform/` desktop adapters |
| `settings.py`, `__main__.py` | Settings and entry point | `settings.py`, `cli.py`, `__main__.py` |
| `agent/cache.py` | Small JSON cache | Not carried over |
| `tools/mail.py`, `tools/moodle.py`, `agent/moodle/` | Email and Moodle | Removed |
| `tools/web.py` | Location and weather tools | Removed |
| `agent/reminders.py` | Reminders | Removed |
| `llm/ollama.py` | Local Ollama models | Removed |

### Slash commands: old to new

| Old command | Status | New |
| --- | --- | --- |
| `/model` | Kept | Uses `GET /v1/models` and the chat body |
| `/usage`, `/context`, `/compact`, `/clear`, `/restore`, `/undo`, `/plan`, `/init`, `/todo`, `/tools` | Kept | Rewritten; `/clear` takes `--incognito` and `--local-only`; `/tools` also covers tool packs |
| `/ls`, `/cd`, `/cat` and the other shell file commands | Kept | Run by the shell's own command registry, so they exist in both modes |
| `/permissions` | Replaced | `/rules` and the trust prompt |
| `/remind`, `/reminders`, `/unremind`, `/tasks`, `/task`, `/notify-after` | Removed | Clara has these on the server |
| (none) | New | `/approvals`, `/rules`, `/project`, `/retry`, `/version`, `/journal`, `/transcript` |
