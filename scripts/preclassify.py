import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse
import re

from apprentice import config, events, preclassify

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; preclassify only ever sends the current profile's events"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month(value: str) -> str:
    if not _MONTH_RE.match(value):
        raise argparse.ArgumentTypeError("expected YYYY-MM, got %r" % value)
    return value


def _log(line: str) -> None:
    sys.stderr.write(line + "\n")


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("preclassify.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="preclassify.py",
                                     description="Opt-in haiku pre-classification of correction pairs "
                                                 "(current profile only).")
    parser.add_argument("--since", type=_month, metavar="YYYY-MM", help="only events from this month on")
    parser.add_argument("--max-batches", type=int, metavar="N", help="stop after N batches of 40 pairs")
    args = parser.parse_args(argv)
    if args.max_batches is not None and args.max_batches < 1:
        parser.error("--max-batches must be at least 1")
    home_dir = config.home()
    config.ensure_dirs(home_dir)
    found = [event for event in events.iter_events(home_dir)
             if args.since is None or events.month_of(event.get("ts") or "") >= args.since]
    pairs = preclassify.select_uncached(found, home_dir, config.current_profile(os.environ),
                                        config.load_config(home_dir))
    summary = preclassify.run_batches(pairs, home_dir, os.environ, args.max_batches, _log)
    line = "preclassify: %d batches, %d cached, %d skipped" % (summary["batches"], summary["cached"],
                                                              summary["skipped"])
    if summary["stopped_early"]:
        line += ", stopped early"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
