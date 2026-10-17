"""Helper subprocess for tests.test_events: append N events of 1-4 KB to one file."""
import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from apprentice import events  # noqa: E402

_ALPHABET = string.ascii_letters + string.digits + " "


def main() -> None:
    path, count, worker = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    rng = random.Random(worker)
    for n in range(count):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1024, 4096)))
        events.append(path, {"v": 1, "id": "w%d-%d" % (worker, n), "kind": "machine", "worker": worker,
                             "command": text})
    sys.exit(0)


if __name__ == "__main__":
    main()
