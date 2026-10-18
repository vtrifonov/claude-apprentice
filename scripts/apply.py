import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import apply, config, ledger, tmpfiles

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _fail(message: str) -> int:
    sys.stderr.write("apply.py: %s\n" % message)
    return 2


_DRY_RESULTS = {"written": "would-write", "removed": "would-remove"}


def _report(entries, dry_run: bool = False) -> int:
    code = 0
    for entry in entries:
        shown = _DRY_RESULTS.get(entry["result"], entry["result"]) if dry_run else entry["result"]
        line = "result=%s file=%s" % (shown, entry.get("file") or "-")
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


def _apply(args, home_dir: str, cfg: dict) -> int:
    try:
        return _apply_text(args, home_dir, cfg)
    finally:
        # the skill rewrites the file before every call; a dry run keeps it for the real apply that follows
        if args.text_file and not args.dry_run:
            tmpfiles.discard(home_dir, args.text_file)


def _apply_text(args, home_dir: str, cfg: dict) -> int:
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
    args.text, error = tmpfiles.read(home_dir, args.text_file, "--text-file")
    if error:
        return _fail(error)
    if args.scope == "repo":
        return _report([_apply_repo(args, home_dir, cfg, merged)], args.dry_run)
    outcome = apply.apply_candidate(args, home_dir, cfg, os.path.expanduser("~"), merged_ids=merged)
    return _report([outcome], args.dry_run)


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
        out = apply.undo(args.undo, home_dir, cfg, dry_run=args.dry_run)
        return _report([dict(entry, id=args.undo) for entry in out.get("results") or [out]], args.dry_run)
    if args.replay:
        return _report(apply.replay(home_dir, cfg, args.confirm_tracked, args.dry_run), args.dry_run)
    return _apply(args, home_dir, cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
