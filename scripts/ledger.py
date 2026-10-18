import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse
import json

from apprentice import config, ledger, tmpfiles

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _fail(message: str) -> int:
    sys.stderr.write("ledger.py: %s\n" % message)
    return 2


def _record(args, home_dir: str) -> int:
    try:
        return _record_entry(args, home_dir)
    finally:
        for path in (args.text_file, args.user_edit_file):
            tmpfiles.discard(home_dir, path)


def _record_entry(args, home_dir: str) -> int:
    if not os.path.isfile(args.pack):
        return _fail("pack not found: %s" % args.pack)
    merged = [m for m in args.merged_ids.split(",") if m] if args.merged_ids else None
    if merged:
        known = ledger.pack_candidates(ledger.load_pack(args.pack))
        for member in merged:
            if member not in known:
                return _fail("merged id is not a candidate of the pack: %s" % member)
    text = None
    if args.text_file:
        text, error = tmpfiles.read(home_dir, args.text_file, "--text-file")
        if error:
            return _fail(error)
    user_edit = args.user_edit
    if args.user_edit_file:
        user_edit, error = tmpfiles.read(home_dir, args.user_edit_file, "--user-edit-file")
        if error:
            return _fail(error)
    try:
        entry = ledger.record_decision(
            home_dir, args.pack, args.id, args.status, text=text, scope=args.scope, target=args.target,
            user_edit=user_edit, reason=args.reason, n_at_decision=args.n_at_decision,
            merged_ids=merged, profile=config.current_profile(os.environ))
    except ValueError as exc:
        return _fail(str(exc))
    # One fixed line: the entry carries sidecar-derived fields that must never reach the session.
    sys.stdout.write("recorded id=%s status=%s\n" % (entry["id"], entry["status"]))
    return 0


def _show(args, home_dir: str) -> int:
    group = config.group_of_profile(config.current_profile(os.environ), config.load_config(home_dir),
                                    config.read_group_labels(home_dir))
    for entry in ledger.read_proposals(home_dir):
        if entry.get("id") == args.id:
            sys.stdout.write(json.dumps(ledger.for_group(ledger.strip_private(entry), group),
                                        ensure_ascii=False) + "\n")
    return 0


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("ledger.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="ledger.py", description="Record and show review ledger entries.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="append one proposal entry")
    rec.add_argument("--id", required=True)
    rec.add_argument("--status", required=True, choices=ledger.STATUSES)
    rec.add_argument("--pack", required=True, help="path of the pack JSON")
    rec.add_argument("--text-file")
    rec.add_argument("--scope")
    rec.add_argument("--target")
    rec.add_argument("--user-edit", help="kept for compatibility; prefer --user-edit-file")
    rec.add_argument("--user-edit-file", help="file under state/tmp holding the user's edit")
    rec.add_argument("--reason")
    rec.add_argument("--n-at-decision", type=int)
    rec.add_argument("--merged-ids", help="comma-separated candidate ids merged into this one")
    show = sub.add_parser("show", help="print the entries of one id as JSON lines, without sidecar fields and "
                               "with other groups' free text blanked (for manual inspection)")
    show.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    home_dir = config.home()
    if args.command == "record":
        return _record(args, home_dir)
    return _show(args, home_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
