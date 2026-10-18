---
description: Review learned-rule candidates one by one
argument-hint: [--since YYYY-MM] [--repo owner/name] [--scope global|repo|skill] [--all] [--limit N] [--preclassify]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *), AskUserQuestion, Skill, Read, Write
---

Review learned-rule candidates one by one.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`. Every script call is one Bash call with the interpreter spelled out in full, with `<root>` replaced by the resolved path; shell variables do not survive between Bash calls.

2. With `--preclassify` in `$ARGUMENTS`: run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/preclassify.py"` first and remove the flag from the arguments. If it fails, print its one-line message and continue.

3. Run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/distill.py"` with the remaining arguments. It prints the paths of `<pack>.json` and `<pack>.md`, then a `group=<group>` line; pass `<group>` to the skill, which names its temp files with it. If the call exits with 126 or 127, or stderr mentions `xcrun`, the Python interpreter is missing: say so, name the README remedy (`xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter), and stop.

4. Invoke the skill `apprentice:distill` with the Skill tool. If the tool rejects the name, print `apprentice:distill skill not found` and stop. Never review without it.

5. Read the JSON pack. If it has no candidates, print `remaining` and the pack path, and stop.

6. Follow the skill for each candidate in order. End with `accepted A, declined D, later L, edited E · <pack>.md`.
