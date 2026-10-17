import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse
import fcntl
import re

from apprentice import backfill, config

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month(value: str) -> str:
    if not _MONTH_RE.match(value):
        raise argparse.ArgumentTypeError("expected YYYY-MM, got %r" % value)
    return value


def _same_file(path: str, stream) -> bool:
    try:
        a = os.fstat(stream.fileno())
        b = os.stat(path)
    except (OSError, ValueError, AttributeError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


class _Log(object):
    """Appends lines to the log file, or to stdout when stdout already is that file (the command's redirect)."""

    def __init__(self, path: str):
        self.to_stdout = _same_file(path, sys.stdout)
        self.fh = None
        if self.to_stdout:
            # the redirect opened stdout without O_APPEND; append so a truncating second launch leaves no hole
            flags = fcntl.fcntl(sys.stdout.fileno(), fcntl.F_GETFL)
            fcntl.fcntl(sys.stdout.fileno(), fcntl.F_SETFL, flags | os.O_APPEND)
        else:
            folder = os.path.dirname(os.path.abspath(path))
            os.makedirs(folder, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            self.fh = os.fdopen(fd, "a", encoding="utf-8")

    def __call__(self, line: str) -> None:
        target = sys.stdout if self.fh is None else self.fh
        target.write(line + "\n")
        target.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("backfill.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="backfill.py",
                                     description="Backfill apprentice events from past Claude Code transcripts.")
    parser.add_argument("--since", type=_month, metavar="YYYY-MM",
                        help="skip transcripts last modified, and lines written, before this month")
    parser.add_argument("--project", metavar="SLUG", help="only this project directory under projects/")
    parser.add_argument("--dry-run", action="store_true", help="count what would be written; write only the log")
    parser.add_argument("--log", metavar="PATH", help="progress log (default: <home>/state/backfill.log)")
    args = parser.parse_args(argv)
    home_dir = config.home()
    config.ensure_dirs(home_dir)
    if args.log is None:
        args.log = os.path.join(home_dir, "state", "backfill.log")
    log = _Log(args.log)
    try:
        summary = backfill.run(args, os.environ, log)
    finally:
        log.close()
    return int(summary.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
