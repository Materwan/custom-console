# Custom Console

A custom Windows terminal with its own commands, a virtual file system that spans
your disks, WSL and a reMarkable tablet, and a personal AI agent (Ollama + agno).

- **Shell**: `cd ls tree cat stat find cp rm pwd echo clear launch reload help ai exit`,
  tab completion generated from each command's definition, colored output.
- **Virtual file system**: one tree rooted at `/` (see below).
- **AI agent**: a chat in the terminal with file, workspace, PDF, web, e-mail and
  Moodle tools, guarded by a permission system.

Windows only (it uses the registry, `\\wsl$` and detached processes).

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"            # core + tests
pip install -e ".[moodle,pdf]"     # optional: Moodle tools, PDF -> Markdown
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
| `DATA_DIR` | `<project>/data` | Logs, agent memory, workspaces, caches |
| `WSL_DISTRO` | `Ubuntu` | Distribution exposed as `/wsl-<name>/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `AGENT_DEFAULT_MODEL` | `gemma4` | Model used by `ai agent` / `ai start` |
| `AGENT_PERMISSION_LEVEL` | `1` | Tools auto-accepted by the agent (see below) |
| `AGENT_INSTRUCTIONS_PATH` | `config/agent_instructions.txt` | The agent's system prompt |
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

Backslashes work in paths (`cd C:\Users\me`). Names with spaces need quotes. Anything that
is not a built-in command is looked up in your `PATH` and run as usual.

`rm -r` asks for confirmation (skip with `-f`) and refuses the drive root, your home
folder and any folder containing the current directory.

### `launch`

`launch opera` finds an application by name (PATH, registry, install folders, then a
recursive search; `-sl 1..4` limits the stages) and remembers where it found it.

## The AI agent

```
ai list [-r] [-s] [-c]          installed / running models, sizes, capabilities
ai start [model]               load a model in memory
ai agent [-n NAME] [-m MODEL] [-p 0|1|2] [--no-memory]
```

### Terminal behaviour

The input line is pinned at the bottom of the terminal. While the agent answers, its
text streams **as plain text** in a live area above the input. When the answer is
complete, that area disappears and the whole turn is printed again, rendered as
**Markdown**, into the normal scrollback (so you can scroll, select and copy it).
Tool calls and permission decisions appear as dim lines in the same flow.

| Key | Action |
|---|---|
| `Enter` | send (while the agent is busy, the text stays in the input line) |
| `Ctrl+C` | stop the answer in progress (the partial answer is kept); clears the input when idle; refuses a pending permission question |
| `Ctrl+D` / `/bye` | leave the agent |
| `/clear` `/help` | clear the screen / show the commands |

### Permissions

Every tool declares a level; a tool whose level is ≤ the session's auto-accept level runs
without asking, otherwise the question appears above the input and you answer in the
input line (`Enter` or `y` = yes).

| Level | Auto-accepted | Examples |
|---|---|---|
| `0` | only trivial tools (`pwd`) | everything else asks |
| `1` (default) | reads | `file_system_read`, `workspace_file_list`, `moodle_list_courses`, `get_weather` |
| `2` | everything | `file_system_copy`, `workspace_file_write`, `moodle_click_element`, `send_email` |

### Tools

- `file_system_*` – explore the virtual file system (`pwd list read stat find cd tree copy`).
- `workspace_file_*` – read/write/move/delete inside two sandboxed folders, `result` and
  `tmp` (`<DATA_DIR>/agent/`). Paths are resolved and checked, so `..`, absolute paths,
  UNC paths and symlinks cannot escape. `workspace_file_get` copies something from the
  file system into a workspace.
- `pdf_to_markdown` – PDF of a workspace -> Markdown (needs the `pdf` extra).
- `get_location`, `get_weather`, `send_email` (only with SMTP configured).
- `moodle_*` – courses, course structure, pages, announcements, grades, downloads, clicks
  (needs the `moodle` extra; the first use opens a browser for the SSO login). Disable
  with `MOODLE_ENABLED=false` if you do not need them: every tool costs prompt tokens.

Every prompt, answer and tool call is appended to `<DATA_DIR>/logs/agent.jsonl`
(rotated at 5 MB). Conversation history and memories live in
`<DATA_DIR>/agent/memory.db` unless you use `--no-memory`.

## Layout

```
config/agent_instructions.txt    the agent's system prompt
src/custom_console/
  settings.py                    all configuration, loaded lazily (no import side effects)
  fs/                            virtual file system: manager, reMarkable backend, path parsing
  shell/                         REPL, completer, tokenizer, printer, commands/
  apps/                          application finder and launcher
  llm/                           Ollama client
  agent/
    ui.py  render.py  turn.py    terminal UI (live plain text -> Markdown), turn model
    session.py  factory.py       agno wiring: streaming, tool hook, journal
    permissions.py  results.py   permission gate, ToolResult
    workspace.py  cache.py  journal.py
    tools/                       one module per tool family
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
