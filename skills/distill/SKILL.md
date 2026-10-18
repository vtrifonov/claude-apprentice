---
name: distill
description: Judgment step for /apprentice:suggest — word, show and review one rule candidate at a time
---

# apprentice:distill

Run by `/apprentice:suggest` after `scripts/distill.py` has built a pack. `<root>` is the plugin root, `<home>` is `${APPRENTICE_HOME:-~/.claude/apprentice}`, `<json>` is the pack JSON path. Every script call is one Bash call of the form `"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/<name>.py" …`.

## Inputs

The pack JSON path given by the command, and nothing else. Read `review/<pack>.json` and `review/<pack>.md` only. Never open `events/`, `ledger/`, `state/pack-private/` (the private sidecar) or other packs to recover text the pack withheld.

## Per candidate: write the rule

- Bold imperative lead, a one-clause why, at most two sentences, in the house style of the user's global CLAUDE.md.
- Conditional when `departure` is a split sentence. Example: *Default quick mode without asking when no trigger category matches; ask only when auth, persistence or deploy is in play.*
- `kind: ask`: always word it as a change to the asking skill named in `skill`. When `skill` is null, word it as an instruction about when and how to ask, and the block says "asking skill unknown". Never word it as an answer to memorize.
- For a target with `target_git.tracked` true: name no repo, ticket key, email, customer or service from the evidence.
- Employer, customer and service names are only caught when the user lists them as regexes in `extra_redact_patterns` in `<home>/config.json`; on a `sensitive-text` refusal, suggest adding them there.
- Correction groups that mean the same thing may be merged. Keep the higher-ranked id and pass the others as `--merged-ids <id2>,<id3>` on every `ledger.py record` call for that id (shown and the decision alike) and on the Accept `apply.py` call, so the ledger stores their aliases and seed sets whichever way the candidate is decided. Merged ids are not shown on their own.
- Never cite evidence absent from the pack.
- Use `target`, or after a rescope the matching `alt_targets` entry, as is. Never invent a path.

## Record exposure

Before showing a candidate:

```
scripts/ledger.py record --id <id> --status shown --pack <json> [--merged-ids <id2>,<id3>]
```

## Show one block

Write the rule text to `<home>/state/tmp/<id>.txt` before the dry-run or any tracked block, and rewrite it after every edit. The dry-run for a tracked target is:

```
scripts/apply.py --id <id> --text-file <home>/state/tmp/<id>.txt --scope <scope> --target <target> --pack <json> --confirm-tracked --dry-run
```

`--confirm-tracked` only lets the dry run reach the diff; a dry run writes nothing and records nothing.

Lines, in order:

1. The rule text.
2. `scope: <scope> · target: <target>`, plus:
   - `no-checkout`: "no local checkout found for <repo>".
   - `git-tracked-local`: "CLAUDE.local.md is committed in <repo>; repo rules cannot go there".
   - `unknown-tracking`: "git could not say whether CLAUDE.local.md is committed in <repo>; repo rules cannot go there".
   - global: "global CLAUDE.md is shared by symlink with account groups <target_shared_groups>" (when the list is non-empty).
   - Any target, the default or a rescope entry, with `target_git.tracked` true: "this file is tracked in <root>; remote <remote> is <visibility>", then the exact diff from the dry-run above, run with that entry's scope and target. For `public_mirror`: "this skill is mirrored publicly by hand (public_skills)".
3. `confidence: <band> · n=<n> · sessions=<s> · repos=<r> · <first_seen>..<last_seen>`.
4. The `departure` sentence, or "no separating feature" followed by `departure_rows`.
5. Up to two `quotes`, then "N more from other account groups (text withheld)" when `text_withheld.events > 0`.
6. `audit: <audit>`.

## Ask

Ask exactly one `AskUserQuestion` with exactly one question. Options, in this order:

- Default: `Accept (Recommended)`, `Decline`, `Later`, `Edit or rescope`.
- `recommend` is `rescope-global` and the target is a tracked skill: `Rescope to global CLAUDE.md (Recommended)`, `Accept into the tracked file`, `Decline`, `Later`.
- The target is the tracked global file and `recommend` is `rescope-global` (public or unknown remote): `Accept into the tracked file`, `Decline`, `Later (Recommended)`, `Edit or rescope`.
- The target is the tracked global file and `recommend` is not `rescope-global` (private remote): `Accept into the tracked file (Recommended)`, `Decline`, `Later`, `Edit or rescope`.
- `recommend` is `rescope-global` because `target_status` is `git-tracked-local` or `unknown-tracking`: `Rescope to global CLAUDE.md (Recommended)`, `Decline`, `Later`, `Edit or rescope`.

The Other field is always available. Free text wins over the pick and is read as an edit (`accept but repo`, `reword: …`, `global`, `skill <name>`). `stop` or `enough` alone, any case, ends the run before the next candidate. A rejected picker (`is_error`, content starting `The user doesn't want to proceed`) means stop the review and print the summary line; record nothing for that candidate. No other picker shape anywhere.

## Outcomes

**Accept.** Write the final text to `<home>/state/tmp/<id>.txt`, then:

```
scripts/apply.py --id <id> --text-file <home>/state/tmp/<id>.txt --scope <scope> --target <target> --pack <json> [--merged-ids <id2>,<id3>] [--confirm-tracked]
```

- Pass `--merged-ids` whenever the candidate was merged, with the same list given to its `ledger.py record` calls, so the `accepted` entry keeps the aliases and seed sets.
- Pass `--confirm-tracked` only after `Accept into the tracked file`, or after `Accept` on a block that showed the tracking line and diff.
- On exit 1, show the one-line reason and run `scripts/ledger.py record --id <id> --status accept_failed --reason <reason> --pack <json>`. Pass the reason without the `skipped:` prefix (`result=skipped:hardlinked` gives `--reason hardlinked`).
- On exit 2, show stderr, record nothing, and continue with the next candidate.
- On `sensitive-text`, offer one reworded version through the same block and picker.
- A candidate with `target_status` `no-checkout`, `git-tracked-local` or `unknown-tracking` is never passed to apply. Record `accept_failed` with reason `no-checkout`, `git-tracked-local` or `git-index-unknown` respectively.

**Decline.** `scripts/ledger.py record --id <id> --status rejected --n-at-decision <band_inputs.n> --pack <json>`.

**Later.** `scripts/ledger.py record --id <id> --status snoozed --pack <json>`.

**Edit or free text.** Write the user's words, as typed, to `<home>/state/tmp/<id>.edit.txt`, then run `scripts/ledger.py record --id <id> --status edited --user-edit-file <home>/state/tmp/<id>.edit.txt --pack <json>`. Never put the user's text on the command line. Rewrite `<home>/state/tmp/<id>.txt` with the edited rule, then re-show once with the edit applied and the same picker. Further edits repeat.

**Rescope.** `--scope` and `--target` both come from the chosen entry: its scope (`global`, `repo` or `skill`) and its `target`, on the dry-run and on apply alike. An entry with `target_git.tracked` true re-shows the block with the tracking line and the dry-run diff for that entry's scope and target, and Accept on it passes `--confirm-tracked`.
- `global`: use `alt_targets.global`.
- `repo`: use the single `alt_targets.repo` entry, or the one named by `owner/name` or bare `name`. Several and none named: re-show once listing the names. Still none: `accept_failed --reason no-target`.
- An entry with `no-checkout`, `git-tracked-local` or `unknown-tracking`: `accept_failed` with that reason (as above); apply is not called.
- `skill`: use `alt_targets.skill` when it matches the named skill (or none is named). A `rescoped-readonly` entry falls back to global with the skill in the lead. A different skill, or null: `accept_failed --reason no-target`.

## End

Print the one-line tally and the pack path.
