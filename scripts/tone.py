import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse

from apprentice import config, tmpfiles, tone

_REMOVED_FLAGS = ("--include-unknown", "--all-profiles")
_REMOVED_MESSAGE = "removed flag; set account_groups and unknown_group in config.json instead"


def _report_install(outcome: dict, dry_run: bool = False) -> int:
    result = "would-write" if dry_run and outcome["result"] == "written" else outcome["result"]
    sys.stdout.write("result=%s file=%s id=%s\n" % (result, outcome.get("file") or "-", tone.TONE_POINTER_ID))
    if outcome.get("message"):
        sys.stdout.write("note=%s\n" % outcome["message"])
    if outcome.get("tracked"):
        sys.stdout.write("tracked=%s\n" % (outcome.get("root") or "-"))
        sys.stdout.write("remote=%s\n" % (outcome.get("remote") or "-"))
        sys.stdout.write("remote_visibility=%s\n" % (outcome.get("remote_visibility") or "unknown"))
    if outcome.get("diff"):
        sys.stdout.write(outcome["diff"])
    return 1 if outcome["result"].startswith("skipped:") else 0


def _run(args, home_dir: str) -> int:
    if args.print_pointer:
        sys.stdout.write(tone.pointer_rule_text(tone.configured_display_name(config.load_config(home_dir))) + "\n")
        return 0
    if args.display_name is not None:
        tone.set_display_name(home_dir, args.display_name)
        return 0
    if args.display_name_file:
        tone.set_display_name_from_file(home_dir, args.display_name_file)
        return 0
    config.ensure_dirs(home_dir)
    cfg = config.load_config(home_dir)
    if args.install_pointer:
        return _report_install(tone.install_pointer(home_dir, cfg, os.path.expanduser("~"), args.confirm_tracked,
                                                    args.dry_run), args.dry_run)
    if args.model_file:
        profile = tone.set_model(home_dir, os.environ, cfg, args.model_file)
    else:
        build = tone.refresh if args.refresh else tone.build_profile
        profile = build(home_dir, os.environ, cfg, os.path.expanduser("~"))
        tone.write_profile(home_dir, profile)
    for path in tone.profile_paths(home_dir, profile["group"]):
        sys.stdout.write(path + "\n")
    sys.stdout.write("group=%s\n" % tmpfiles.group_tag(profile["group"]))
    if not tone.display_name_set(home_dir):
        sys.stdout.write("display_name unset\n")
    return 0


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
                        help="store proficiency_band, proficiency_justification and optional prose from FILE "
                             "(under state/tmp; deleted once read)")
    action.add_argument("--print-pointer", action="store_true", help="print the global tone pointer rule text")
    action.add_argument("--install-pointer", action="store_true",
                        help="install the tone pointer rule into the global CLAUDE.md (created when missing)")
    action.add_argument("--set-display-name", dest="display_name", metavar="NAME",
                        help="store the name the pointer rule uses in config.json (kept for compatibility; "
                             "prefer --display-name-file)")
    action.add_argument("--display-name-file", metavar="FILE",
                        help="like --set-display-name, reading the name from FILE (under state/tmp; deleted once "
                             "read)")
    parser.add_argument("--confirm-tracked", action="store_true",
                        help="with --install-pointer: allow writing a global CLAUDE.md inside a git work tree")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --install-pointer: print the result and diff, write nothing")
    args = parser.parse_args(argv)
    if (args.confirm_tracked or args.dry_run) and not args.install_pointer:
        parser.error("--confirm-tracked and --dry-run go with --install-pointer")
    home_dir = config.home()
    tmpfiles.sweep(home_dir)
    try:
        return _run(args, home_dir)
    except tone.ToneError as exc:
        sys.stderr.write("tone.py: %s\n" % exc)
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
