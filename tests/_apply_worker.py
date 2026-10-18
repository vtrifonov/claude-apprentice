"""Helper subprocess for tests.test_apply: apply one rule to one target file."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from apprentice import apply  # noqa: E402


def main() -> None:
    path, home, rule_id, text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    out = apply.apply_rule_to_file(path, rule_id, text, 3, "high", "2026-10-18", home)
    sys.exit(0 if out["result"] == "written" else 1)


if __name__ == "__main__":
    main()
