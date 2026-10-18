import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import config, rescrub


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="rescrub.py",
        description="One-off: re-run the scrubber over stored events and replace prompt text that ends in a "
                    "secret fragment at its cut. Prints counts only. Run it while no session is active.")
    parser.add_argument("--apply", action="store_true", help="rewrite the event files (default: dry run)")
    args = parser.parse_args(argv)
    home_dir = config.home()
    counts = rescrub.run(home_dir, config.load_config(home_dir), apply=args.apply)
    print("rescrub %s: files %d, events %d, events changed %d, cut fragments redacted %d, files rewritten %d"
          % ("applied" if args.apply else "dry run, nothing written", counts["files"], counts["events"],
             counts["events_changed"], counts["cut_redacted"], counts["files_rewritten"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
