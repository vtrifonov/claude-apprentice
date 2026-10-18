import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import config, distill

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _since(value: str) -> str:
    try:
        distill.since_epoch(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM, YYYY-MM-DD or an ISO timestamp, got %r" % value)
    return value


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a whole number, got %r" % value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected at least 1, got %r" % value)
    return number


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("distill.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="distill.py",
                                     description="Build a review pack of rule candidates from captured events.")
    parser.add_argument("--since", type=_since, metavar="DATE", help="only events at or after this date (UTC)")
    parser.add_argument("--repo", metavar="OWNER/NAME", help="only candidates with evidence in this repo")
    parser.add_argument("--scope", choices=distill.SCOPES, help="only candidates of this scope")
    parser.add_argument("--all", dest="include_all", action="store_true",
                        help="list low-band candidates with the rest instead of under Weak")
    parser.add_argument("--limit", type=_limit, default=distill.DEFAULT_LIMIT, metavar="N",
                        help="candidates to list (default %d)" % distill.DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    home_dir = config.home()
    config.ensure_dirs(home_dir)
    pack, private = distill.build_pack(home_dir, os.environ, config.load_config(home_dir), args.since, args.repo,
                                       args.scope, args.include_all, args.limit, os.path.expanduser("~"))
    for path in distill.write_pack(home_dir, pack, private):
        sys.stdout.write(path + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
