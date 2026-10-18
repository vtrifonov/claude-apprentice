import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import apply, config, ledger

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _fail(message: str) -> int:
    sys.stderr.write("apply.py: %s\n" % message)
    return 2


def _report(entries) -> int:
    code = 0
    for entry in entries:
        line = "result=%s file=%s" % (entry["result"], entry.get("file") or "-")
        if entry.get("id") is not None:
            line += " id=%s" % entry["id"]
        sys.stdout.write(line + "\n")
        if entry.get("message"):
            sys.stdout.write("note=%s\n" % entry["message"])
        if entry.get("diff"):
            sys.stdout.write(entry["diff"])
        if entry["result"].startswith("skipped:"):
            code = 1
    return code


def _read_text(path: str, home_dir: str):
    tmp_root = os.path.realpath(os.path.join(home_dir, "state", "tmp"))
    resolved = os.path.realpath(path)
    if not resolved.startswith(tmp_root + os.sep):
        raise ValueError("--text-file must be under %s" % tmp_root)
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read --text-file: %s" % exc.__class__.__name__)


def _apply(args, home_dir: str, cfg: dict) -> int:
    if not args.id or not args.scope:
        return _fail("--id and --scope are required (or use --undo / --replay)")
    if args.scope == "repo" and not args.pack:
        return _report([{"result": "skipped:no-pack", "file": None}])
    if args.scope == "repo" and not args.target:
        return _report([{"result": "skipped:no-target", "file": None}])
    if not args.text_file:
        return _fail("--text-file is required")
    if args.pack and not os.path.isfile(args.pack):
        return _fail("pack not found: %s" % args.pack)
    merged = [m for m in args.merged_ids.split(",") if m] if args.merged_ids else None
    if merged:
        if not args.pack:
            return _fail("--merged-ids requires --pack")
        known = ledger.pack_candidates(ledger.load_pack(args.pack))
        for member in merged:
            if member not in known:
                return _fail("merged id is not a candidate of the pack: %s" % member)
    try:
        args.text = _read_text(args.text_file, home_dir)
    except ValueError as exc:
        return _fail(str(exc))
    if args.scope == "repo":
        return _report([_apply_repo(args, home_dir, cfg, merged)])
    outcome = apply.apply_candidate(args, home_dir, cfg, os.path.expanduser("~"), merged_ids=merged)
    return _report([outcome])


def _repo_for_target(candidate: dict, target: str):
    """The repo whose `alt_targets.repo` entry names `target`, else the candidate's only repo, else None."""
    alt = candidate.get("alt_targets") if isinstance(candidate.get("alt_targets"), dict) else {}
    entries = alt.get("repo") if isinstance(alt.get("repo"), dict) else {}
    wanted = os.path.realpath(target)
    for repo, entry in entries.items():
        path = entry.get("target") if isinstance(entry, dict) else None
        if isinstance(path, str) and os.path.realpath(path) == wanted:
            return repo
    repos = candidate.get("repos")
    if isinstance(repos, list) and len(repos) == 1 and isinstance(repos[0], str):
        return repos[0]
    return None


def _apply_repo(args, home_dir: str, cfg: dict, merged) -> dict:
    candidate = ledger.pack_candidates(ledger.load_pack(args.pack)).get(args.id, {})
    repo = _repo_for_target(candidate, args.target)
    if repo is None:
        return {"result": "skipped:bad-target", "file": args.target}
    n, band, _repos = apply.pack_facts(args, home_dir, merged)
    return apply.apply_repo(args.target, repo, args.id, args.text, n, band, config.utc_now_iso()[:10], home_dir,
                            cfg, bool(args.dry_run), merged_ids=merged, pack=args.pack)


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("apply.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="apply.py", description="Apply, undo or replay apprentice rules.")
    parser.add_argument("--id")
    parser.add_argument("--text-file")
    parser.add_argument("--scope", choices=("global", "repo", "skill"))
    parser.add_argument("--target")
    parser.add_argument("--pack", help="path of the pack JSON")
    parser.add_argument("--merged-ids", help="comma-separated candidate ids merged into this one")
    parser.add_argument("--confirm-tracked", action="store_true")
    parser.add_argument("--undo", metavar="ID")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    home_dir = config.home()
    cfg = config.load_config(home_dir)
    if args.undo:
        return _report([dict(apply.undo(args.undo, home_dir, cfg, dry_run=args.dry_run), id=args.undo)])
    if args.replay:
        return _report(apply.replay(home_dir, cfg, args.confirm_tracked, args.dry_run))
    return _apply(args, home_dir, cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
