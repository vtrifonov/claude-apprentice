import json
import random

_WORDS = (
    "alpha beta gamma delta widget cache render queue token parser buffer stream "
    "module signal handler config thread socket record column bundle router mapper"
).split()


def _text(rng, count):
    return " ".join(rng.choice(_WORDS) for _ in range(count))


def generate_transcript(path, lines=2000, seed=1, words_per_line=60):
    """Write alternating user/assistant lines of seeded random words; return the last assistant text."""
    rng = random.Random(seed)
    last_assistant = None
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(lines):
            role = "user" if i % 2 == 0 else "assistant"
            text = _text(rng, words_per_line)
            if role == "user":
                content = text
            else:
                content = [{"type": "text", "text": text}]
                last_assistant = text
            obj = {
                "type": role,
                "timestamp": "2030-01-05T10:%02d:%02dZ" % ((i // 60) % 60, i % 60),
                "sessionId": "sess-gen-%d" % seed,
                "cwd": "/home/alice/code/acme/widgets",
                "message": {"role": role, "content": content},
            }
            fh.write(json.dumps(obj) + "\n")
    return last_assistant
