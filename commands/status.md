---
description: Show apprentice capture, backfill and profile status
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)
---

Show the apprentice status report.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`.

2. Run this as one Bash call, with `<root>` replaced:

   ```
   "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/status.py"
   ```

3. Show the report to the user as printed. If a profile row carries `NOT ENABLED`, `STALE` or `INTERPRETER MISSING`, point at the command printed below the table or at the interpreter remedy.

4. If the call exits with 126 or 127, or its output mentions `xcrun`, the Python interpreter is missing. Say so and name the README remedy: run `xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter.
