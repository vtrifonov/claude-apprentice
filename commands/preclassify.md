---
description: Opt-in haiku pre-classification of correction pairs (current profile only)
argument-hint: [--since YYYY-MM] [--max-batches N]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)
---

Pre-classify correction pairs with haiku so `/apprentice:suggest` groups them better. This sends your prompts and the assistant text before them to the API, under the account of the current profile. Only events from the current profile are sent; there is no flag to widen that.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`.

2. Run this as one Bash call, with `<root>` replaced:

   ```
   "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/preclassify.py" $ARGUMENTS
   ```

   If the call exits with 126 or 127, or its output mentions `xcrun`, the Python interpreter is missing. Say so, name the README remedy (run `xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter), and stop.

3. Print the summary line it prints. A batch that fails is skipped and logged, and three failures in a row stop the run. Verdicts are cached under `state/preclass/`, so a later run skips pairs already classified.
