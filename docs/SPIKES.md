# Phase 0 spikes: results

Each spike has a result, and a chosen fallback when it failed. Written on Linux (Python 3.11, Textual 8.2.8,
prompt_toolkit 3.0.53); what could not be run here is listed under "Still to check by hand".

## Spike A: Textual inline mode, completion popup, switch to full screen and back

**Result: failed for the shell on Windows. Fallback chosen: prompt_toolkit for the shell prompt only.**

- `textual/app.py::_build_driver` picks the inline driver only `elif inline and not WINDOWS`. On Windows
  `App.run(inline=True)` silently uses the normal full-screen `WindowsDriver`. Textual 8.2.8 is the latest release.
  The stand-alone shell ("prompt at the bottom, output in the normal scrollback") cannot be built on Textual inline mode
  on the platform the project targets first.
- The plan's two fallbacks were "a menu drawn as plain lines under the prompt" or "a different library for the shell
  only". Decision (yours): **prompt_toolkit for the shell prompt**, Textual stays for the full-screen `ai agent`.
- `scripts/spikes/spike_a.py` + `spike_a_driver.py` run, in a pseudo-terminal: a prompt_toolkit prompt with a Tab menu
  and history ghost text accepted with the Right arrow, then a full-screen Textual app, then the prompt again, in one
  process. All steps work on Linux (menu shown, ghost text accepted, Textual screen drawn and exited with a value, the
  next prompt reads normally).
- Consequence for the layers: prompt_toolkit and Textual are both confined to `ui/`; `import-linter` forbids them below.

## Spike B: httpx streaming against Clara, a tool round trip, a cancel in the middle

**Result: passed, against the fake Clara and against the real one (Clara 127.0.0.1:8765, model `cloud:gemma4:31b`).**

- `scripts/spikes/spike_b.py --fake` completes one turn with one local tool (`tool_requests` -> one post of the results
  -> the same stream carries on). Without `--fake` it does the same against `CLARA_URL` with the token file, and
  `--cancel` stops a second turn after its first token.
- Contract tests prove: cancelling mid-stream closes the connection; **cancelling before the response headers also closes
  it** (BUG-8); keepalives hold a slow turn open; silence for longer than the read timeout is reported as a dead
  connection; a dropped connection is reported.
- **Live results** (`uv run pytest -m live tests/live`, 4 passed, 1 skipped):
  - The token file works as a `console` client (approvals `all`); `GET /v1/auth/me`, `/v1/models`, `/v1/packs`, `/v1/projects`,
    `/v1/rules`, `/v1/approvals` and `POST /v1/rules/test` answer in the shapes the typed client reads.
  - A local tool round trip works: `tool_requests` -> one post of the results -> the same stream finishes (1.8 s).
  - **Clara stops a turn when the client disconnects, and stores it as interrupted**: after a cancel in the middle of a
    stored turn, the conversation ends with `[This answer was interrupted: the connection to the client was lost.]`, holds
    only the partial text, and does not grow afterwards. So the plan's server task "stop the turn on disconnect" is **not
    needed**.
  - Cancelling *before the headers* cannot be provoked live (Clara answers the headers at once); the client behaviour is
    covered by the fake server's `BeforeHeaders` test.
  - The approval round was **not exercised live, on purpose**. A first live test asked the model to create a file; that
    ended up writing `console-live-test.txt` into your Clara workspace (`ws_write` is allowed without asking), and the
    second run's overwrite then asked for an approval, which the test refused. I deleted that file afterwards (the
    create and the delete are in the workspace's git log) and removed the test. The approval answer path is covered by
    the contract tests against the fake server, and the read-only approval and rule routes are checked live.
- Read from Clara's source (`~/clara-rebuild`), so the open points of the plan are closed:
  - Packs: `GET /v1/packs?where=chat` lists `{id, label, description, default, available, reason, where, tools}`; a
    conversation takes `packs` (ids) on create (`POST /v1/conversations`) and on `PATCH`. `web` is on by default.
  - Projects: `GET /v1/projects` -> `{projects: [{id, name, description, files, ...}]}`; chat takes `project` (id) for a new
    conversation; `PATCH /v1/conversations/{id}` with `project` (0 = none).
  - Versions: `fork_of` is the **id of a user message** (from `GET .../messages`); with a `message` it is the edited question,
    without one it asks the same question again. It needs `conversation`, and **refuses `tools`**: see below. `api/chat.py` also rejects an empty `message` even with
    `fork_of`, so `/retry` resends the question's text (read from `GET .../messages`) together with its id.
    `POST .../version {message_id}` switches the active version.
  - Tool schemas are OpenAI-style (`{"type": "function", "function": {name, description, parameters}}`). A name used twice
    or reserved by the server is a 422.
  - `done` carries `tainted` (the server's own tainted-turn flag), `context {tokens, window, percent}` and `model`.
  - Events not in the plan: `qcm` (a quiz form) and `file` (a note written); both are handled as notices.
- **Found: `fork_of` and local tools cannot be combined.** `ChatBody` validation says "A question is asked again in its own
  conversation, as a plain answer", and rejects `fork_of` with `tools`. So `/retry` and `/version` (message versions) answer
  **without local tools**. See `docs/DECISIONS.md` (to write in Phase 5) and the server task list.

## Spike C: `rmapi` wrapper with input closed and a timeout

**Result: passed against a fake `rmapi` (the real one is not installed here).**

- `platform/process.py::run_capture` starts every child with input closed and kills its whole process tree at the timeout.
  `scripts/spikes/spike_c.py` shows: a listing works; a not-signed-in `rmapi` (which would wait for a code) returns in
  0.05 s with its prompt text in the output; a hanging one is killed at the timeout (1.01 s for a 1 s limit).
- `tests/fixtures/fake_rmapi.py` prints success, errors, the sign-in prompt, HTTP 429 and hangs; the Phase 3 backend and its
  tests use it.

## Phase 4 live pass (the agent screen against the real Clara)

`uv run pytest -m live tests/live/test_live_agent.py` drives the full-screen agent with Textual's pilot against the running
Clara and the `console` token, in an incognito conversation it deletes afterwards (2026-10-20, both passed in about 10 s):

- a real question, a real `local_list` round (allowed by the read allowlist, so no prompt), the answer drawn as Markdown,
  `/usage`, `/model` and `/rules` printed from the real server's answers, the journal holding the allowlist decision;
- the console's **own** permission prompt for `local_write` with a real model: the prompt appears and nothing has happened
  yet, `o` writes the file, `/undo` removes it.

Clara's own server approvals (`approval` events) are still not exercised live, for the reason given under Spike B.

## Gate 0

| Criterion | State |
| --- | --- |
| Each spike has a written result, and a fallback if it failed | Done (this file) |
| CI is green on both systems | The workflow exists (`.github/workflows/ci.yml`, Windows, Linux and macOS, Python 3.11 and 3.13). The same steps pass locally on Linux (ruff, mypy, import-linter, pytest). **Not run on Windows or macOS: needs a push.** |
| A script completes one real turn with one local tool | Done: `scripts/spikes/spike_b.py` passes against the fake and the real Clara |

## Still to check by hand

- Spike A on Windows Terminal: prompt -> `ai agent` (Textual) -> prompt, resize, Tab menu, ghost text.
- Spike C with a real `rmapi` and a real tablet.
- Spike B on Windows against the real Clara.
- A Clara **server** approval round against the real Clara (needs a safe approval-triggering action; see above).
- The whole agent screen in Windows Terminal: Tab popup, Esc, Ctrl+V with an image, resize, and Shift+Enter (which a terminal may not report; Ctrl+J and a trailing backslash always work).
