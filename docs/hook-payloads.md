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

`CLAUDE_CODE_ENTRYPOINT` is `cli` on every live interactive event.
