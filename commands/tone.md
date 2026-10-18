---
description: Build the writing profile for this account group and install the tone pointer
argument-hint: [--refresh]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *), AskUserQuestion, Read, Write
---

Build the writing profile for this account group and install the tone pointer.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`. Every script call is one Bash call with the interpreter spelled out in full, with `<root>` replaced by the resolved path; shell variables do not survive between Bash calls.

2. Run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" $ARGUMENTS`. It prints the paths of `tone-<group>.json` and `tone-<group>.md`, then a `group=<group>` line, then `display_name unset` when config.json names no display name. `<home>` is the directory above `profile/` in those paths, and `<group>` is the value after `group=`: every temp file below is named with it. If the call exits with 2, print its stderr line and stop. If it exits with 126 or 127, or stderr mentions `xcrun`, the Python interpreter is missing: say so, name the README remedy (`xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter), and stop.

3. Read the printed JSON file. Open nothing else under `<home>`: never `events/`, `ledger/`, `state/pack-private/`, another group's `tone-*` file, or an existing file under `<home>/state/tmp/`. The scripts delete each temp file once they have read it.

4. Assess the writing from the JSON. Write a JSON file to `<home>/state/tmp/<group>-tone-model.json` with:
   - `proficiency_band`: one of `A1`, `A2`, `B1`, `B2`, `C1`, `C2`. It is an estimate.
   - `proficiency_justification`: one paragraph, at most 2,000 characters, citing the `metrics.proficiency_inputs` values (sentence_len_mean, clause_density, rarity_share, subordination_per_100) and what they suggest.
   - `prose` (optional): guidance that replaces the two "How to use" sections. Keep their rules: drafting as the user is one to three lowercase lines, verb first or "can you", no greetings, no thanks, no emoji, no deliberate typos; talking to the user is recommendation first, plain words, and never commenting on their spelling.

   With `--refresh` and a JSON that already holds `model`, reassess only when `reassess_band` is true; otherwise skip to step 5. Then run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" --set-model <home>/state/tmp/<group>-tone-model.json`. It deletes the file whether or not it succeeds. Rewrite the file before every `--set-model` call.

5. Tell the user that only sessions in this account group load the file: SessionStart names `tone-<group>.md` for the current group and no other.

6. If step 2 printed `display_name unset`, say that the pointer will call them "the user" until a name is set, and that a name typed into the picker's Other field is stored as the display name.

7. Preview the install: run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" --install-pointer --dry-run`. It writes nothing. Its first line is `result=<result> file=<path> id=r_ec4ee9bc`, a `note=` line follows when the global CLAUDE.md does not exist yet (installing creates it), and the diff comes last. On exit 1, print the result line and stop. The global file is tracked when the output has the `tracked=` and `remote_visibility=` lines (with `remote=` between them).

8. Ask one single-question picker with the AskUserQuestion tool (Other allowed):
   - Not tracked: "Install the tone pointer in your global CLAUDE.md?" with the options `Install tone pointer in global CLAUDE.md (Recommended)` and `Not now`.
   - Tracked: first show "this file is tracked in <tracked>; remote <remote> is <remote_visibility>" and the diff from step 7. Then ask "Install the tone pointer into the tracked global CLAUDE.md?" with the options `Install into the tracked file` and `Not now`. Mark `Install into the tracked file (Recommended)` only when `remote_visibility` is `private`; for `public` or `unknown`, mark `Not now (Recommended)` instead.

   Answers:
   - `Install tone pointer in global CLAUDE.md (Recommended)`: install with `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" --install-pointer`.
   - `Install into the tracked file`: install with `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" --install-pointer --confirm-tracked`.
   - `Not now`: print the tone file path and stop.
   - Other with text, when step 2 printed `display_name unset`: Write the text, as typed, to `<home>/state/tmp/<group>-display-name.txt`. Run `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/tone.py" --display-name-file <home>/state/tmp/<group>-display-name.txt`, which stores the name and deletes the file. Never put the typed text on a command line. Then install as for the recommended or tracked option shown. On exit 2, print its stderr line and stop.
   - Otherwise treat the Other text as a reply: answer it, install nothing, and stop.

   After an install, print its result line. On exit 1, print the result line and stop. `r_ec4ee9bc` is the fixed id of the tone pointer, so installing twice gives `result=unchanged` and leaves the file as it was.
