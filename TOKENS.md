# Token budget of the agent

Goal: fewer tokens per successful task, same success rate. Method: one change at a time,
measured against the baseline; a change that could lower success is rejected.

## Measuring

```
python scripts/token_baseline.py          # static prefix sent on EVERY request (tools + system prompt)
python scripts/token_baseline.py --log    # + per-tool call counts / result sizes and tokens per turn,
                                          #   from <DATA_DIR>/logs/agent.jsonl
/tokens                                   # in the agent console: last answer and conversation
```

The journal has one `turn` entry per answer: `input_tokens`, `output_tokens`, `total_tokens`,
`tool_calls`, `tool_result_chars`, `duration_seconds`, `interrupted`, `error`. Static sizes are
estimated at 4 chars/token; per-turn numbers are the real counts reported by Ollama.

**Phase 0 status:** the static baseline is recorded below. Per-turn baselines (cache hit rate,
turns per task, tokens per successful task) need real sessions: use the agent for a few days
and run `--log`. Until then the dynamic changes below are justified by construction, not by
measurement, and are marked *unmeasured*.

## Baseline (before any change)

Static prefix: **13 901 chars ≈ 3 475 tokens** per request (26 tools = 2 695, system prompt = 780),
resent at every model round trip, i.e. on every tool call.
Heaviest definitions: `pdf_to_markdown` 193, `moodle_get_page_content` 186, `get_weather` 178,
`file_system_read` 172, `workspace_file_write` 153.

## Changelog

| # | Phase | Change | Static tokens | Notes |
|---|---|---|---|---|
| 1 | 1 | Tool descriptions cut to "when to use" + non-obvious constraints; `Args:` blocks removed | tools 2 695 → 1 830 (-32 %) | schemas keep types and defaults |
| 2 | 1 | System prompt rewritten: no list of tools (they describe themselves), no JSON-envelope explanation, terse-answer rule | prompt 780 → 260; total 3 475 → **2 090** (-40 %) | |
| 3 | 1 | Tool results are terse: strings as is, data as compact JSON, no `{"success","data"}` envelope, errors as one `ERROR <Type>: <message>` line | *unmeasured* (~12 tokens saved per call) | journal keeps the full form |
| 4 | 1 | Caps with a pointer: list/find/tree 150 entries ("+N more; narrow…"), `file_system_read` 20 000 → 8 000 chars (continue with `mode="range"`), workspace read 50 000 → 12 000, Moodle page 8 000 → 6 000, announcements 20 → 10 | *unmeasured* | bounds the worst case of a single result |
| 5 | 1 | Prompt-cache friendliness: tools and instructions are byte-stable (no date, nothing per turn) so Ollama can reuse its prefix cache | *unmeasured* | |
| 6 | 3 | `max_tool_calls_from_history=3`: old tool calls and their results leave the context (`AGENT_HISTORY_TOOL_CALLS`) | *unmeasured* | |
| 7 | 4 | Identical read calls within 30 s are answered from memory (cleared by any write and by `cd`; failures never cached) | *unmeasured* | also skips the repeated permission question |
| 8 | 5 | Answers capped at 4 096 tokens (`AGENT_MAX_OUTPUT_TOKENS`) | *unmeasured* | |
| 9 | 5 | Long-term memory extraction off by default (`AGENT_LONG_TERM_MEMORY`): it is an extra model call, with its own prompt, after every turn | *unmeasured*, ~1 call per turn | **behaviour change**: the agent no longer learns facts across conversations unless you turn it on |

Success rate before/after is not measured yet (no task suite): the changes are chosen so that
nothing the model needs is removed (a cut result says how to get the rest; a cached result is
identical to a fresh one within 30 s unless something was written). Item 9 is the one to
reconsider if you rely on long-term memories.

## Not done (and why)

- **Deferred tool loading, two-step retrieval, spill to files:** agno has no native deferred
  loading; the Moodle family (the largest, 8 tools) can be switched off with `MOODLE_ENABLED=false`.
  Revisit once `--log` shows which tools are never used.
- **Tool consolidation:** after trimming, the tools cost ~70 tokens each; merging would save
  less than the 5 % threshold for a riskier schema.
- **Compaction / sub-agents / model routing / batch API:** history is already limited to 2 runs;
  these pay off on long sessions, which the journal will show if they exist.
- **Parallel tool calls:** decided by the model and Ollama, not configurable here.

Stop rule: remaining static savings per change are now under 5 %; the next step is real
per-turn data from `scripts/token_baseline.py --log`.
