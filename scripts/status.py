import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import config, status

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("status.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="status.py",
                                     description="Show apprentice capture, backfill and profile status.")
    parser.add_argument("--full", action="store_true", help="add a details block with the full sections")
    args = parser.parse_args(argv)
    sys.stdout.write(status.report(config.home(), os.environ, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
