# Event record schema

Every captured event is one JSON object per line in `events/YYYY-MM.jsonl` under the data root
(`~/.claude/apprentice/`, override `APPRENTICE_HOME`). The month is the UTC month of `ts`. A record is the
envelope merged with the payload of its `kind`; payload fields sit at the top level next to the envelope.

Lines are serialized compactly as UTF-8 (`ensure_ascii=False`) and capped at 32,768 bytes. Over the cap the
writer applies, in order and stopping once under: drop `raw_response`; drop every `options[*].description`
under `questions`; cut each `questions[*].question` to 2,000 characters and set `truncated: true`.

Readers skip lines that are not JSON objects or lack `v`, `id` or `kind` (counted as bad), skip records with
`v` above the reader's schema version (counted as future), upgrade older versions step by step, and keep
unknown fields.

Source column: `hook` = written by the live hooks, `backfill` = reconstructed from transcripts, `both` = either.

## Envelope

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `v` | int | no | both | Schema version, currently 1. |
| `id` | string | no | both | Version-4 UUID. |
| `ts` | string | no | both | UTC ISO-8601 timestamp, `Z` suffix. |
| `kind` | string | no | both | `ask`, `ask_rejected`, `prompt` or `machine`. |
| `source` | string | no | both | `hook` or `backfill`. |
| `fidelity` | string | no | both | `live` for `hook`, `transcript` for `backfill`. |
| `session_id` | string | no | both | Claude Code session id. |
| `profile` | string | no | both | Profile name the session ran under. |
| `entrypoint` | string | yes | both | Claude Code entrypoint, when known. |
| `cwd` | string | yes | both | Working directory of the session. |
| `repo` | string | yes | both | `owner/name` from the git remote. |
| `branch` | string | yes | both | Current git branch. |
| `ticket` | string | yes | both | Ticket key, when one is detected. |
| `transcript_path` | string | yes | both | Path of the session transcript. |
| `flags` | list of string | optional | both | Present only when non-empty. |
| `partial` | bool | optional | both | Set when the record was built from incomplete input. |
| `truncated` | bool | optional | both | Set when the 32 KB cap cut question text. |

## Payload: `ask`

An AskUserQuestion call and its answer.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `tool_use_id` | string | no | both | Tool call id. |
| `skill_hint` | string | yes | both | Skill the question is attributed to. |
| `questions` | list of question | no | both | See the question table. |
| `numbers` | list | no | both | Numeric values extracted from the questions. |
| `numbers_other` | list | no | both | Numbers that matched no extractor. |
| `reset_at` | string | yes | both | Reset time parsed from the question text. |
| `risk_words` | list of string | no | both | Risk words present. |
| `risk_words_negated` | list of string | no | both | Risk words preceded by a negation. |
| `hour_local` | int | no | both | Local hour, 0-23. |
| `weekday` | int | no | both | Local weekday. |
| `raw_response` | string | optional | both | Scrubbed raw tool response; first field dropped by the cap. |

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
| `recommended` | bool | no | both | Option was marked recommended. |

### Answer

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `labels` | list of string | no | both | Picked option labels. |
| `free_text` | string | yes | both | Text typed under "Other". |
| `is_other` | bool | no | both | The answer used "Other". |
| `followed_recommended` | bool | yes | both | Picked the recommended option; null when none was recommended. |

## Payload: `ask_rejected`

An AskUserQuestion call the user dismissed. Questions carry `answer: null`.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `tool_use_id` | string | no | both | Tool call id. |
| `questions` | list of question | no | both | Same shape as in `ask`. |
| `numbers` | list | no | both | As in `ask`. |
| `numbers_other` | list | no | both | As in `ask`. |
| `reset_at` | string | yes | both | As in `ask`. |
| `risk_words` | list of string | no | both | As in `ask`. |
| `risk_words_negated` | list of string | no | both | As in `ask`. |
| `hour_local` | int | no | both | As in `ask`. |
| `weekday` | int | no | both | As in `ask`. |
| `skill_hint` | string | yes | both | As in `ask`. |

## Payload: `prompt`

A user prompt.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `text` | string | no | both | Scrubbed prompt text, at most 4 KB. |
| `sha1` | string | no | both | SHA-1 hex of the prompt text. |
| `len` | int | no | both | Prompt length. |
| `words` | int | no | both | Word count. |
| `tags` | list of string | no | both | Classifier tags. |
| `register` | string | yes | both | Tone register. |
| `strong_marker` | bool | no | both | Contains a strong correction marker. |
| `after_interrupt` | bool | no | both | Follows an interrupted turn. |
| `after_rejected_ask` | bool | no | both | Follows a rejected AskUserQuestion. |
| `prev_assistant` | string | yes | both | Scrubbed anchor of the previous assistant message, at most 400 chars. |
| `duplicate_of` | string | yes | both | `id` of an earlier identical prompt. |

## Payload: `machine`

A prompt recognised as machine-generated, such as a slash command expansion.

| Field | Type | Nullable | Source | Notes |
|---|---|---|---|---|
| `command` | string | no | both | The recognised command. |

## Change log

- v1 — 2026-10-17: initial schema.
