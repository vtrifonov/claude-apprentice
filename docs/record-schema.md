# Event record schema

Every captured event is one JSON object per line in `events/YYYY-MM.jsonl` under the data root
(`~/.claude/apprentice/`, override `APPRENTICE_HOME`). The month is the UTC month of `ts`. A record is the
envelope merged with the payload of its `kind`; payload fields sit at the top level next to the envelope.

Lines are serialized compactly as UTF-8 (`ensure_ascii=False`) and capped at 32,768 bytes. Over the cap the
writer applies, in order and stopping once under: drop `raw_response`; drop every `options[*].description`
under `questions`; cut each `questions[*].question` to 2,000 characters and set `truncated: true`; cut every
remaining string anywhere in the record to 2,000 characters; keep only the first entry of `questions`. If the
line is still over, nested strings and collections are shrunk step by step (the identity fields `v`, `id`,
`ts`, `kind`, `source`, `fidelity` and `session_id` are kept whole) until it fits. Every step after the
second sets `truncated: true`.

Readers skip lines that are not JSON objects or lack `v`, `id` or `kind` (counted as bad), skip records with
`v` above the reader's schema version (counted as future), upgrade older versions step by step, and keep
unknown fields.

Source column: `hook` = written by the live hooks, `backfill` = reconstructed from transcripts, `both` = either.

## Envelope

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `v` | int | no | both | Schema version, currently 1. |
| `id` | string | no | both | Version-4 UUID. |
| `ts` | string | no | both | UTC ISO-8601 timestamp with milliseconds, `Z` suffix. In backfill, the transcript line timestamp. |
| `kind` | string | no | both | `ask`, `ask_rejected`, `prompt` or `machine`. |
| `source` | string | no | both | `hook` or `backfill`. |
| `fidelity` | string | no | both | `live` for `hook`, `transcript` for `backfill`. |
| `session_id` | string | no | both | Claude Code session id. |
| `profile` | string | no | both | Basename of `CLAUDE_CONFIG_DIR`, or `default` when unset or `~/.claude`. Backfill assigns it from `profile_rules` and writes `unknown` when no rule matches. |
| `entrypoint` | string | yes | both | Claude Code entrypoint. Null comes with the `entrypoint_unknown` flag. |
| `cwd` | string | yes | both | Working directory of the session. |
| `repo` | string | yes | both | `owner/name` from the git remote. |
| `branch` | string | yes | both | Current git branch. |
| `ticket` | string | yes | both | Ticket key, when one is detected. |
| `transcript_path` | string | yes | both | Path of the session transcript. |
| `flags` | list of string | optional | both | Present only when non-empty. |
| `partial` | bool | optional | both | Set when a question had no matching answer pair (that question gets `answer: null`). |
| `truncated` | bool | optional | both | Set when the 32 KB cap cut text or dropped entries (see above). |

## Payload: `ask`

An AskUserQuestion call and its answer.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `tool_use_id` | string | no | both | Tool call id. |
| `skill_hint` | string | yes | both | Skill the question is attributed to. |
| `questions` | list of question | no | both | See the question table. |
| `numbers` | object (string → number or string) | no | both | Named values from the question text, `{name: float or string}`, e.g. `{"used_pct": 74.0, "pace": "tight"}`. |
| `numbers_other` | list of number | no | both | Numbers that no extractor named. |
| `reset_at` | string | yes | both | Reset time parsed from the question text. |
| `risk_words` | list of string | no | both | Risk words found in the question text and in **every** option's description, after negation handling. |
| `risk_words_negated` | list of string | no | both | Risk words dropped because a negation preceded them. |
| `hour_local` | int | no | both | Hour 0-23 of local time at capture, or of the transcript line timestamp in backfill. |
| `weekday` | int | no | both | Local weekday, 0 = Monday. Same time source as `hour_local`. |
| `raw_response` | string | optional | both | Raw tool response, scrubbed and capped at 4 KB; first field dropped by the cap. |

### Question

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `header` | string | yes | both | Short header shown above the question. |
| `question` | string | no | both | Question text, scrubbed. |
| `multi_select` | bool | no | both | Whether several options may be picked. |
| `options` | list of option | no | both | See the option table. |
| `answer` | answer | yes | both | Null when no answer was recorded. |

### Option

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `label` | string | no | both | Option label. |
| `description` | string | optional | both | Dropped by the cap. |
| `recommended` | bool | no | both | True when the label contains "(Recommended)". |

### Answer

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `labels` | list of string | no | both | Picked option labels, exactly as `options[].label` stores them, including "(Recommended)". Matching the answer to an option is case-insensitive and whitespace-collapsed and ignores "(Recommended)". |
| `free_text` | string | yes | both | Text typed under "Other". |
| `is_other` | bool | no | both | The answer used "Other". |
| `followed_recommended` | bool | yes | both | True when a recommended option exists and every chosen label is recommended; false when a recommended option exists and was not chosen; null when the question had no recommended option or the answer is free text only. |

## Payload: `ask_rejected`

An AskUserQuestion call the user dismissed. Questions carry `answer: null`.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `tool_use_id` | string | no | both | Tool call id. |
| `questions` | list of question | no | both | Same shape as in `ask`. |
| `numbers` | object (string → number or string) | no | both | As in `ask`. |
| `numbers_other` | list of number | no | both | As in `ask`. |
| `reset_at` | string | yes | both | As in `ask`. |
| `risk_words` | list of string | no | both | As in `ask`. |
| `risk_words_negated` | list of string | no | both | As in `ask`. |
| `hour_local` | int | no | both | As in `ask`. |
| `weekday` | int | no | both | As in `ask` (0 = Monday). |
| `skill_hint` | string | yes | both | As in `ask`. |

## Payload: `prompt`

A user prompt.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `text` | string | yes | both | Scrubbed prompt text, ≤ 4,000 chars; 300 when `pasted`. Null for `image_only`. |
| `sha1` | string | no | both | SHA-1 hex of the full raw text, before scrubbing and truncation. |
| `len` | int | no | both | Length of the full raw text, not the stored text. |
| `words` | int | no | both | Word count of the full raw text, not the stored text. |
| `tags` | list of string | no | both | Classifier tags. |
| `register` | string | yes | both | One of `ack`, `status_ask`, `pushback`, `report`, `question`, `direction`. Null only for `image_only`; every other prompt gets one. |
| `strong_marker` | bool | no | both | Contains a strong correction marker. |
| `after_interrupt` | bool | no | both | Follows an interrupted turn. |
| `after_rejected_ask` | bool | no | both | Follows a rejected AskUserQuestion. |
| `prev_assistant` | string | yes | both | Scrubbed anchor of the previous assistant message, at most 400 chars. |
| `duplicate_of` | string | yes | both | `id` of an earlier `prompt` with the same normalized sha1 (lowercased, whitespace collapsed) from a **different session** within 120 s. When either stored `text` was cut (`pasted`, or `len` over 4,000), the raw `sha1` must match as well. |

## Payload: `machine`

A prompt whose first token is a slash command; only the token is stored.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `command` | string | no | both | The recognised command. |

## Change log

- v1 — 2026-10-17: initial schema.
