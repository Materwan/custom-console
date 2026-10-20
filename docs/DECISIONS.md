# Decisions

What was decided while building, why, and where the build differs from `Custom console rebuild plan.md`. The plan's own "decisions to confirm" were accepted as written; this file lists the rest.

## Accepted from the plan

Local tool names start with `local_`; 16 local tools including `local_job`; the tainted-turn rule is mirrored locally and an unknown Clara tool counts as outside content; an untrusted folder gets no write or run tools and no `AGENT.md`; the read-only command allowlist (`pwd ls dir cat type head tail wc grep which`, `git status|diff|log`); the old permission levels are gone; `--no-memory` became `--incognito`; sub-agents are separate incognito conversations with read tools; `/undo` touches local files only; session files hold metadata only and the newest 5 per folder are kept; `/plan`, `/retry` and `/version` exist; on Linux and macOS `/` is the root and the tablet is `reMarkable:path`; `mv` and `cp` refuse to overwrite without `-f`; timeouts 120 s / result limit 30,000 characters / round budget 840 s; the `CONSOLE_` prefix; `ruff`, `mypy`, `import-linter` and CI on Python 3.11 and 3.13 including macOS; `uv tool install`; licence all rights reserved.

## Where the build differs from the plan

| Topic | Plan | Build | Why |
| --- | --- | --- | --- |
| The shell's prompt | Textual inline mode | **prompt_toolkit** for the shell; Textual only for the full-screen agent | Spike A: Textual 8.2.8 has no inline mode on Windows (`inline and not WINDOWS` in `App._build_driver`), and Windows is the first target. See `docs/SPIKES.md` |
| `/retry`, `/version` and local tools | Shapes to confirm | These answers have **no local tools** | Clara refuses `fork_of` together with `tools` |
| Stop a turn on disconnect | A possible server task | Not needed | Checked live: Clara stops the turn and stores it as interrupted |
| Answers rendered as Markdown | The Markdown widget | **One Rich-Markdown `Static` per answer**, plain text while streaming | The Markdown widget is dozens of widgets per message and Textual lays out every widget again whenever one changes size |
| Long transcripts | Not redrawn once finished | Also **folded**: past 80 blocks the oldest finished ones become one widget | Same reason; see the performance budget below |
| `/rules` | A read-only viewer | Also `/rules remove N` for the local rules | Without it an "always" or "never" answer could never be taken back |
| `!command` | Output goes with the next message | Also **taints that turn** | The output may be anything, a `curl` for example; a tainted turn asks for every change |
| `/clear` | New conversation | Keeps the model, project, tool packs, tool switches and plan mode | They are choices about how you work, not about the conversation |
| Tool packs | Set per conversation | A conversation is created first when packs were changed before the first message | The chat body cannot carry packs |
| Commands in the agent | Not planned | The shell's commands work with a slash (`/ls`); `cd`, `ai`, `exit`, `reload`, `clear` do not; a command that would ask a question is answered "no" | Convenient, and it keeps the agent's folder fixed |
| Multi-line history | Not planned | A message with several lines is stored as one line (`\n` escaped) | The history file is one entry per line |

## Rules about words and keys

- A message that starts with `/` or `!` must start with `//` or `!!` (one character is dropped). A line that starts with `/` and is not a known command is an error that says so, never silently a message.
- `@word` is a mention only when it looks like a path (it has a separator, a dot or `~`); `@Erwan` is just a word. A path-like mention that does not exist stops the message ("Not sent. @x: no such file") rather than sending something different from what you meant.
- Enter sends. Shift+Enter works where the terminal reports it (many do not; to check in Windows Terminal); **Ctrl+J** and a trailing `\` work everywhere.
- A new folder asks once to be trusted. Saying no leaves the agent read-only for that run; it asks again next time.

## Safety of the tests (read this before writing a test that deletes)

On 19 October 2026 a test written during this build ran `rm -rf ~` through the console's own shell. The parser expanded `~` with the platform's real home folder while the file layer's protection compared against the test's fake home, so the protection did not fire and the real home folder was deleted. Most of it was recovered from a snapshot; the lesson is built in:

- **Product fixes.** The parser expands `~` with the *resolver's* home (one source of truth). The virtual file system refuses to remove or move the home folder, **all of its parents**, the drive roots, and **anything that resolves to home** (a link to home may be removed as a link; the folder it points at may not).
- **A safety net for every test** (`tests/support/guard.py`, installed first by `tests/conftest.py`): `os.remove`, `unlink`, `rmdir`, `removedirs`, `rename`, `replace` and `shutil.rmtree` refuse any path outside the temp folders, the repository and pytest's base temp, and refuse a real home folder or any of its parents even if they are inside an allowed root. A violation is a `BaseException`, so code under test cannot swallow it. `HOME`, `USERPROFILE` and `Path.home()` point at an empty folder for every test, and a session-wide tripwire checks the real home after the run.
- **Test harnesses.** Shell and file-system tests are built with `Sh` / `sandbox_resolver` (a local backend that refuses to change anything outside its root), which refuses to exist without the net. A destructive test never *names* a real path (`/`, `/home`, `~user`); dangerous places are judged with the pure `Vfs.protected_reason`, and only paths inside `tmp_path` are executed.
- When running the suite after changing a destructive path, run it with `HOME` pointing at a throwaway folder and compare `ls -A ~` before and after.

## Performance budget (PERF-1)

The old console repainted the whole conversation for every token. The budget for the new one is measured by `tests/perf/test_streaming_budget.py`: 9,000 streamed words (about 63,000 characters) arriving over a transcript of 120 earlier messages must not make the event loop late by more than **60 ms at the 95th percentile** or **500 ms at worst**, and a 100,000-character answer must be drawn as Markdown, once, in under **4 seconds**. Measured on the development machine (Linux): median 0.3 ms, p95 25 ms, worst 130 ms, the 9,000 words in under a second; a 106,000-character answer in 1.2 s. With a Markdown widget per message the same scenario took 42 seconds; with one widget per message but no folding its p95 lag was 105 to 114 ms.

Under coverage tracing (CI runs `pytest --cov`) every line is several times slower, so the budgets are multiplied by 8 and the workload cut to a third (3,000 words over 50 messages) when `coverage` is loaded, or by `CONSOLE_PERF_SCALE` when it is set; the figures above are from a run without coverage.

What made the difference: one widget per finished message instead of a Markdown widget tree; complete paragraphs of the answer being written frozen so only the last one is redrawn; drawing in batches at 12 frames a second; and folding old finished blocks into one widget, so that the number of widgets (and so the cost of a layout) stays bounded. A second finding came out of the measurement: a check that the PDF package is installed imported it, which froze the screen for 1.7 seconds at the first message; it now looks the package up without importing it.

## Limits chosen

| Limit | Value | Where |
| --- | --- | --- |
| `AGENT.md` used | 8,000 characters, cut with a note | `core/prompt.py` |
| Base prompt and session facts | 9,000 characters | `scripts/check_prompt.py` |
| Tool schemas | 12,000 characters | `scripts/check_prompt.py` |
| Inline text of one `@file` | 100,000 bytes (the model can read the rest) | `core/attachments.py` |
| Mentions in one message | 12 | `core/attachments.py` |
| Sub-agent | 12 rounds, 300 s, 8,000 characters back | `core/subagent.py` |
| `!command` | 600 s, 20,000 characters of output | `core/agent.py` |
| Sessions kept per folder | 5 | `core/session.py` |
| Undo turns kept | 20 | `core/undo.py` |

## Still to be checked by hand

The test suite cannot do these here; they are listed in the final report and in `docs/SPIKES.md`:

- the shell and the agent in **Windows Terminal** (prompt, Tab menu, resize, switch to full screen and back);
- the CI run on **Windows and macOS** (the workflow exists; it needs a push);
- a **real reMarkable** with `rmapi`;
- an **approval round against the real Clara** with a safe approval-triggering action;
- the Windows-only branches: access-list checks, UNC and WSL paths, the clipboard image tool, console encoding.

**A known gap in the tests.** The tests of the file system, the shell, the turn runner and the screens build their world with POSIX paths, so on the Windows runner they are *skipped*. What runs there is the code that is pure: the path syntax of drives, UNC and WSL forms (`test_vfs_resolver.py` parses Windows paths on any system), access-list parsing, the WSL list parser, settings, the permission engine's text rules, the prompt check, the documents' tests, and `test_wsl_windows.py` (which needs a WSL distribution). The Windows implementations of the platform interface (`platform/windows.py`: access lists, clipboard, `launch`, restart) are covered mostly by hand. A Windows world builder (a sandboxed local backend over `C:\` paths) would let the same tests run there; it is the first thing to add once a Windows machine is at hand.
