---
description: Show apprentice capture, backfill and profile status
argument-hint: [--full]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)
---

Show the apprentice status report.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`.

2. Run this as one Bash call, with `<root>` replaced. `$ARGUMENTS` is passed through, so `/apprentice:status --full` adds the details block:

   ```
   "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/status.py" $ARGUMENTS
   ```

3. The script prints Markdown. Output its stdout verbatim as your reply, so it renders as Markdown: no code block, no quoting, no added commentary, no restating of the tables. The only exception is a follow-up question from the user, which you answer from the report.

4. If the call exits with 126 or 127, or its output mentions `xcrun`, the Python interpreter is missing. Say so and name the README remedy: run `xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter.
