---
description: Backfill apprentice events from past transcripts
argument-hint: [--since YYYY-MM] [--project=<slug>] [--dry-run]
allowed-tools: Bash(nohup *), Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)
---

Start the apprentice backfill in the background. It reads past Claude Code transcripts and adds the events the live hooks did not capture.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`.

2. Check the interpreter in the foreground first, with `<root>` replaced:

   ```
   "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/backfill.py" --help > /dev/null
   ```

   If this call exits with 126 or 127, or its output mentions `xcrun`, the Python interpreter is missing. Say so, name the README remedy (run `xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter), and stop here.

3. Run this as one Bash call, with `<root>` replaced. Pass a project slug as `--project=<slug>`, since slugs start with `-`:

   ```
   nohup "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/backfill.py" $ARGUMENTS > "${APPRENTICE_HOME:-$HOME/.claude/apprentice}/state/backfill.log" 2>&1 &
   ```

4. Tell the user the backfill is running in the background, and that `/apprentice:status` shows its progress and the summary when it finishes. Only one backfill runs at a time; a second launch ends at once with `backfill already running`.
