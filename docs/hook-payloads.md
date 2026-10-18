# Hook payload shapes

Observed shapes of the Claude Code events the capture hooks read. Every example is synthetic: made-up
questions, `acme/widgets`, Alice and Bob. Only field names and the harness marker sentences are real.

## AskUserQuestion results

The tool result text starts with one of two prefixes. Both are parsed the same way.

- `Your questions have been answered: "<question>"="<answer>", ...`
- `The user answered: "<question>"="<answer>", ...`

The structured `toolUseResult` beside the text:

```json
{
  "questions": [
    {"question": "Use the cache?", "header": "Cache", "multiSelect": false,
     "options": [{"label": "Yes", "description": "add it"}, {"label": "No", "description": "skip it"}]}
  ],
  "answers": {"Use the cache?": "Yes"},
  "annotations": {"Use the cache?": {"preview": "..."}}
}
```

`answers` is keyed by question text and holds one string per question. `annotations` is keyed the same way
and may carry a `preview`.

### Plain pick

```
The user answered: "Use the cache?"="Yes"
```

### Other (free text)

The answer is whatever Alice typed, not one of the option labels:

```
Your questions have been answered: "Use the cache?"="only for acme/widgets reads"
```

### Two questions, with annotations

```
Your questions have been answered: "Use the cache?"="Yes", "Which store?"="Memory"
```

```json
{
  "answers": {"Use the cache?": "Yes", "Which store?": "Memory"},
  "annotations": {"Which store?": {"preview": "in-process dict"}}
}
```

A multiSelect question joins its picks into one answer string, and an Other entry appears in that string
like any other pick.

### Rejected picker

Bob dismissed the picker. The tool_result has `is_error: true`, text that starts with
`The user doesn't want to proceed with`, and a plain string `toolUseResult`. PostToolUse does not produce an
`ask` for it; the capture writes one `ask_rejected` event and no `ask`.

```json
{
  "type": "tool_result",
  "tool_use_id": "tu1",
  "is_error": true,
  "content": "The user doesn't want to proceed with this tool use. ..."
}
```

## UserPromptSubmit with a pasted block

The `prompt` field carries the harness wrapper verbatim, after any text the user typed. The open and close
tags both repeat the random hex id:

```json
{
  "session_id": "sess-1",
  "cwd": "/home/alice/code/acme/widgets",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "\n\n<pasted_content id=\"3fa9c1d2\">\nAlice pasted this line about acme/widgets\n</pasted_content id=\"3fa9c1d2\">"
}
```

The capture tags the event `pasted` whatever the line count, strips both wrapper tags from the stored text and
from the `len` and `words` counts, and keeps `sha1` on the raw prompt.

An image-only prompt has `text: null` and the `image_only` tag.

## Entrypoint

`CLAUDE_CODE_ENTRYPOINT` is `cli` on every live interactive event. A payload `entrypoint` field wins when
present; otherwise the environment variable, then the transcript tail (the last `POST_TAIL_BYTES`), decide. With none of them the event
carries the `entrypoint_unknown` flag and is captured. Only entrypoints in `capture_entrypoints` (default `cli`,
`claude-desktop`) are captured.

## Fields read per hook event

Only these fields are read. Everything else in the payload is ignored.

| Event | Fields read |
|---|---|
| PreToolUse (`AskUserQuestion`) | `session_id`, `cwd`, `transcript_path`, `entrypoint`, `tool_use_id`, `tool_input` (`questions`) |
| PostToolUse (`AskUserQuestion`) | the same, plus `tool_response` |
| UserPromptSubmit | `session_id`, `cwd`, `transcript_path`, `entrypoint`, `prompt` |
| SessionStart | `session_id`, `cwd`, `transcript_path`, `entrypoint` |

PreToolUse writes a pending marker under `state/pending-ask/`. PostToolUse pairs the answer with it. A rejected
picker leaves a marker and no answer.

## `tool_response` shapes

The examples above cover every shape seen live:

| Shape | Marker |
|---|---|
| Structured, `answers` a map | one string per question text |
| Structured, `answers` a list | items with `question` and `answer` (accepted, not seen live) |
| Text, answered | starts with `Your questions have been answered:` or `The user answered:` |
| Text, rejected | `is_error: true`, starts with `The user doesn't want to proceed with` |

## Prompt serialization (V11)

The hook `prompt` carries the `<pasted_content id=...>` wrapper verbatim, so the capture strips it itself and
keeps `sha1` on the raw prompt. An image-only prompt produces `image_only` and no text. The backfill watermark
does not depend on the two forms matching.

## Entrypoint of a `claude -p` child (V10)

The child that `/apprentice:preclassify` starts writes a transcript with entrypoint `sdk-cli`. Hooks do run in
a `-p` child unless guarded, and an unguarded child fires SessionStart. The plugin guards in three ways:

- the child environment sets `APPRENTICE_INTERNAL=1`, which makes every hook exit at once;
- the `capture_entrypoints` filter drops `sdk-cli`;
- the child runs with `--safe-mode --tools "" --no-session-persistence` and cwd `<home>/state`, which skips
  plugins, hooks and MCP and cut the child's context from 62.7k to 2.5k tokens.

A child that hits a usage limit exits 1 with a JSON envelope whose `is_error` is `true` and whose
`api_error_status` is `429`, while `subtype` still says `success`. A batch counts as failed when the exit
code is not 0, `is_error` is true, or `result` does not parse to a JSON array.

## Inherited entrypoint (V12)

Whether a `claude -p` started from a Bash tool inside an interactive session inherits `CLAUDE_CODE_ENTRYPOINT`
is pending (C5). The guards above do not depend on the answer.

## SessionStart output

SessionStart is the only hook whose stdout reaches the session, and in the CLI it does. It prints at most two
things: the content of a `CLAUDE.local.md` linked into a new worktree, and one tone-file line for the current
account group.
