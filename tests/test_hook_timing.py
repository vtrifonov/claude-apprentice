import json
import os
import statistics
import time
import unittest

from tests.base import ApprenticeTestCase
from tests.fixtures.gen import generate_transcript
from tests.hookrun import baseline_command, payload_bytes, run_hook

# Relative gate: the hook may cost at most 40 ms over a bare interpreter start with the same flags, and never
# more than 150 ms. The spec's 80 ms absolute target is checked by the controller in C2 on a quiet machine.
_OVER_BASELINE = 0.040
_CEILING = 0.150
_WARMUPS = 3
_RUNS = 20
_CLI = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
_LAST_MEDIANS = {}


def _timed(event, stdin, command=None):
    start = time.perf_counter()
    code, out, _ = run_hook(event, stdin, _CLI, command=command)
    return time.perf_counter() - start, code, out


class HookTimingTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.fake_home, "big.jsonl")
        generate_transcript(self.transcript, lines=2000)
        # a cwd under the temp dir: lstat on /home hits the macOS automounter and costs ~13 ms per call
        self.cwd = os.path.join(self.fake_home, "acme", "widgets")

    def medians(self, event, stdin):
        # HOME is the per-test temp dir, so the hooks.json pycache prefix lands there and starts cold.
        # Baseline and hook runs alternate so machine load drifts onto both sides equally.
        bare = baseline_command(event)
        hook_times, base_times = [], []
        for i in range(_WARMUPS + _RUNS):
            base, base_code, base_out = _timed(event, b"", bare)
            elapsed, code, out = _timed(event, stdin)
            self.assertEqual((base_code, base_out), (0, b""))
            self.assertEqual((code, out), (0, b""))
            if i >= _WARMUPS:
                base_times.append(base)
                hook_times.append(elapsed)
        return statistics.median(base_times), statistics.median(hook_times)

    def assert_within_gate(self, event, stdin):
        base, hook = self.medians(event, stdin)
        _LAST_MEDIANS[event] = {"baseline": round(base * 1000, 1), "hook": round(hook * 1000, 1)}
        msg = "hook median %.1f ms, baseline median %.1f ms" % (hook * 1000, base * 1000)
        self.assertLessEqual(hook, base + _OVER_BASELINE, msg)
        self.assertLessEqual(hook, _CEILING, msg)

    def test_user_prompt_within_gate_on_2000_line_transcript(self):
        self.assert_within_gate("user-prompt", payload_bytes("user_prompt", self.transcript, cwd=self.cwd))

    def test_post_tool_three_questions_within_gate(self):
        data = json.loads(payload_bytes("post_tool_dict", self.transcript, tool_use_id="tu1", cwd=self.cwd))
        question = data["tool_input"]["questions"][0]
        questions = []
        answers = {}
        for i in range(3):
            copy = dict(question, question="Question %d? XX-123 is a 2-value change" % i)
            questions.append(copy)
            answers[copy["question"]] = "Quick (Recommended)"
        data["tool_input"]["questions"] = questions
        data["tool_response"] = {"questions": questions, "answers": answers}
        self.assert_within_gate("post-tool", json.dumps(data).encode("utf-8"))

    def tearDown(self):
        if _LAST_MEDIANS:
            print("hook timing medians (ms): %s" % _LAST_MEDIANS)
        super().tearDown()


if __name__ == "__main__":
    unittest.main()
