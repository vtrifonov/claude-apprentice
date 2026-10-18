---
description: Apply, undo or replay apprentice rules
argument-hint: --undo <id> | --replay | --id … --text-file … --scope … --target … [--pack …] [--merged-ids …] [--confirm-tracked] [--dry-run]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)
---

Apply, undo or replay apprentice rules.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`.

2. Run this as one Bash call, with `<root>` replaced. `$ARGUMENTS` is passed through unchanged:

   ```
   "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/apply.py" $ARGUMENTS
   ```

3. The script prints one `result=<r> file=<f>` line per rule (plus a diff for `--dry-run`). Report it as is. Exit 0 means `written`, `unchanged` or `removed`; exit 1 means a `skipped:*` result, which you explain in one sentence; exit 2 means a usage error, whose stderr you quote.

4. If the call exits with 126 or 127, or its output mentions `xcrun`, the Python interpreter is missing. Say so and name the README remedy: run `xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter.
