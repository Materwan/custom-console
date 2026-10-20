# The Clara client

`clara/client.py` is the only code that knows Clara's routes. It speaks Clara's token-only API.

## Authentication

Every request carries `Authorization: Bearer clara_<id>_<secret>` and `X-Clara-Client: console`. The token comes from a file only you can read (`~/.config/clara/token`, `%APPDATA%\clara\token`); on Windows the access list is checked instead of mode bits, and a file others can read is refused. The token is never put in the environment, a log, a repr or a screen (a test checks each). At start `GET /v1/auth/me` tells whether the token is good.

## Routes used

| Route | Use |
| --- | --- |
| `POST /v1/chat/stream` | One turn, as server-sent events. The only chat route (`/v1/chat` cannot carry local tools) |
| `POST /v1/turns/{id}/tool-results` | The results of a round of local tools, all in one post |
| `GET /v1/approvals`, `POST /v1/approvals/{id}/decide` | List pending requests; answer one (`allow_once`, `allow_always`, `refuse_once`, `refuse_always`) |
| `/v1/conversations`, `.../messages`, `.../compact`, `.../title`, `.../version` | Create (with packs, project, model), read, change, compact, title, switch version |
| `GET /v1/models`, `/v1/packs`, `/v1/projects` | The pickers |
| `GET /v1/rules`, `POST /v1/rules/test` | The rules viewer and tester |
| `GET /v1/auth/me` | Check the token |

Not used: `/v1/documents/extract` (the server prepares attachments itself), `/v1/events/stream`, the memory and workspace routes.

## The chat body

`message` (up to 2,000,000 characters), `conversation`, `attachments` (`name`, base64 `data`; at most 30 MB each), `project`, `model`, `fork_of`, `timezone`, `tools` (up to 64 schemas; 16 are sent), `instructions` (up to 20,000 characters), `incognito`, `local_only`. A body is built from typed fields: a field is left out only when it is `None`, never because it is `0` or `False` (BUG-10). A body Clara would refuse (empty message, bad conversation id, too many tools, instructions too long) is refused before sending.

**Tool packs** cannot be put in the chat body. When you change them before the first message, the console creates the conversation first (`POST /v1/conversations` with `packs`) and chats in it.

**Versions.** `fork_of` is the id of a user message, needs the message text, needs the conversation, and **Clara refuses it together with `tools`**: `/retry` and `/version` answers have no local tools.

## Events

| Event | What the console does |
| --- | --- |
| `turn` | keeps the turn id, the conversation id and the model |
| `thinking`, `token` | thinking is collapsed; text is drawn in batches |
| `retrying`, `warning`, `discarded`, `compacted` | a one-line note |
| `tool_start`, `tool` | one line per Clara tool, opened to see its result |
| `approval` | an inline prompt; the answer goes to `/v1/approvals/{id}/decide`; closes if answered elsewhere |
| `tool_requests` | runs the local tools through the permission engine, posts all results together |
| `usage` | written to the ledger at once, so a failed or stopped turn still counts |
| `done` | context fill, message id, the conversation id is saved |
| `error` | one readable line; ends the turn |
| `qcm`, `file` | noted (the console cannot show a quiz) |

Unknown events and unknown fields are ignored, and a known event that does not parse becomes an unknown one; a newer Clara does not break the console. Lines that do not start with `data:` are ignored. The server sends `: keepalive` every 15 seconds; **60 seconds of silence is a dead connection**.

## Cancel

Esc cancels the task that reads the stream, which closes the connection at any point, including before the response headers arrive (BUG-8). **Clara stops the turn when the client disconnects and stores it as interrupted** (checked live), so no server change is needed. Tools running in a worker thread are stopped through the cancel token.

## Errors

| Code | Shown |
| --- | --- |
| `unauthorized` (401) | The token is wrong or revoked. Run `custom_console --save-token`. |
| `too_large` (413) | The prompt is too large. Try /compact. |
| `unprocessable` (422), `conflict` (409), `not_found`, `forbidden`, `bad_request` | the server's own message |
| `rate_limited` (429) | Wait a moment and retry. |
| `model_failed` (502) | The model failed. Retry, or choose another model with /model. |
| `unavailable` (503), `stopping`, `no_model` | the server's message / try again in a minute |
| `timeout` (504) | The model, or a local tool, took too long. Run long commands in the background. |
| connection, silence | Is Clara running, and is CLARA_URL right? / the turn was dropped; retry |

## Limits that matter

- A round of local tool results must reach Clara within **900 seconds** (`CLARA_CLIENT_TOOL_TIMEOUT`); the console uses 840.
- Clara waits **600 seconds** for an approval; then the turn goes on without the action.
- A local tool's name starts with `local_`; the server owns `ws_*`, `remember`, `web_fetch`, `qcm`, `ask_person` and others, and a collision is a 422.

## Tasks for the Clara server

None of them blocks the console.

1. **Create a `console` token** with the admin tool (approvals `all`, the default) and note the steps in Clara's README. No code change: `console` is already an accepted client label.
2. ~~Stop the turn on disconnect.~~ Not needed: Clara already does (see Cancel).
3. **Optional:** accept `fork_of` together with `tools`, so that `/retry` and `/version` answers can use local tools.
4. **Optional:** expose the list of Clara tools that read outside content, so the console does not have to keep its own (`SAFE_SERVER_TOOLS`).
5. **Document the `local_` prefix** for client tool names in Clara's API notes.
