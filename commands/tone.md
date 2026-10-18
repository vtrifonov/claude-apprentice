---
description: Build the writing profile for this account group and install the tone pointer
argument-hint: [--refresh]
allowed-tools: Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *), AskUserQuestion
---

Build the writing profile for this account group and install the tone pointer.

1. Resolve the plugin root. Use `${CLAUDE_PLUGIN_ROOT}` when it is set. Otherwise use the content of `~/.claude/apprentice/state/plugin-root`. Call the result `<root>`. Set `PY='"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S'`; every script call below is `$PY "<root>/scripts/<name>.py" …` as one Bash call.

2. Run `scripts/tone.py $ARGUMENTS`. It prints the paths of `tone-<group>.json` and `tone-<group>.md`, then `display_name unset` when config.json names no display name. `<home>` is the directory above `profile/` in those paths. If the call exits with 2, print its stderr line and stop. If it exits with 126 or 127, or stderr mentions `xcrun`, the Python interpreter is missing: say so, name the README remedy (`xcode-select --install`, or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter), and stop.

3. Read the printed JSON file. Open nothing else under `<home>`: never `events/`, `ledger/`, `state/pack-private/` or another group's `tone-*` file.

4. Assess the writing from the JSON and write a temp JSON file under `<home>/state/tmp/` (for example `<home>/state/tmp/tone-model.json`) with:
   - `proficiency_band`: one of `A1`, `A2`, `B1`, `B2`, `C1`, `C2`. It is an estimate.
   - `proficiency_justification`: one paragraph, at most 2,000 characters, citing the `metrics.proficiency_inputs` values (sentence_len_mean, clause_density, rarity_share, subordination_per_100) and what they suggest.
   - `prose` (optional): guidance that replaces the two "How to use" sections. Keep their rules: drafting as the user is one to three lowercase lines, verb first or "can you", no greetings, no thanks, no emoji, no deliberate typos; talking to the user is recommendation first, plain words, and never commenting on their spelling.

   With `--refresh` and a JSON that already holds `model`, reassess only when `reassess_band` is true; otherwise skip to step 6. Then run `scripts/tone.py --set-model <tmp>`.

5. Tell the user that only sessions in this account group load the file: SessionStart names `tone-<group>.md` for the current group and no other.

6. If step 2 printed `display_name unset`, say that the pointer will call them "the user" until a name is set, and that a name typed into the picker's Other field is stored as the display name.

7. Ask one single-question picker with the AskUserQuestion tool: "Install the tone pointer in your global CLAUDE.md?" with the options `Install tone pointer in global CLAUDE.md (Recommended)` and `Not now` (Other allowed).
   - Other with text: run `scripts/tone.py --set-display-name "<text>"`, then install as below.
   - `Install tone pointer in global CLAUDE.md (Recommended)`: install.
   - `Not now`: print the tone file path and stop.

   To install: run `scripts/tone.py --print-pointer`, write its output to `<home>/state/tmp/tone-pointer.txt`, then run `scripts/apply.py --id r_ec4ee9bc --text-file <home>/state/tmp/tone-pointer.txt --scope global` and print its result line. `r_ec4ee9bc` is the fixed id of the tone pointer, so installing twice leaves the file unchanged.
