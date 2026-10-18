import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import config, tone

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _run(args, home_dir: str) -> None:
    if args.print_pointer:
        sys.stdout.write(tone.pointer_rule_text(config.load_config(home_dir)["display_name"]) + "\n")
        return
    if args.display_name is not None:
        tone.set_display_name(home_dir, args.display_name)
        return
    config.ensure_dirs(home_dir)
    cfg = config.load_config(home_dir)
    if args.model_file:
        profile = tone.set_model(home_dir, os.environ, cfg, args.model_file)
        paths = tone.profile_paths(home_dir, profile["group"])
    else:
        build = tone.refresh if args.refresh else tone.build_profile
        paths = tone.write_profile(home_dir, build(home_dir, os.environ, cfg, os.path.expanduser("~")))
    for path in paths:
        sys.stdout.write(path + "\n")
    if not tone.display_name_set(home_dir):
        sys.stdout.write("display_name unset\n")


def main(argv) -> int:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_FLAGS:
            sys.stderr.write("tone.py: %s\n" % _REMOVED_MESSAGE)
            return 2
    parser = argparse.ArgumentParser(prog="tone.py",
                                     description="Build the writing profile of the current account group.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--refresh", action="store_true",
                        help="recompute and keep the stored band; flag it for reassessment when the inputs moved")
    action.add_argument("--set-model", dest="model_file", metavar="FILE",
                        help="store proficiency_band, proficiency_justification and optional prose from FILE")
    action.add_argument("--print-pointer", action="store_true", help="print the global tone pointer rule text")
    action.add_argument("--set-display-name", dest="display_name", metavar="NAME",
                        help="store the name the pointer rule uses in config.json")
    args = parser.parse_args(argv)
    try:
        _run(args, config.home())
    except tone.ToneError as exc:
        sys.stderr.write("tone.py: %s\n" % exc)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
