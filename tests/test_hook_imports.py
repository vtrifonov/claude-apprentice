import json
import os
import unittest

from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.hookrun import hook_command, payload_bytes, run_hook

_PLAIN = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts", "plain.jsonl")
_CLI = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
# startup cost the hook must not pay on the hot events
_HOT_PATH_BANNED = ("typing", "copy", "datetime", "uuid")


def _imported_modules(stderr):
    names = set()
    for line in stderr.decode("utf-8", "replace").splitlines():
        if line.startswith("import time:") and line.count("|") == 2:
            names.add(line.rsplit("|", 1)[1].strip())
    return names


class HookImportBudgetTest(ApprenticeTestCase):
    def imports(self, event, stdin):
        command = hook_command(event).replace(" -I -S ", " -I -S -X importtime ", 1)
        self.assertNotEqual(command, hook_command(event))
        code, out, err = run_hook(event, stdin, _CLI, command=command)
        self.assertEqual(code, 0)
        names = _imported_modules(err)
        # a run that never reached the handlers would pass the checks vacuously
        self.assertIn("apprentice.capture", names)
        return names

    def post_tool_stdin(self):
        data = json.loads(payload_bytes("post_tool_dict", _PLAIN, tool_use_id="tu1"))
        question = data["tool_input"]["questions"][0]
        questions = [dict(question, question="Question %d? XX-123 is a 2-value change" % i) for i in range(3)]
        data["tool_input"]["questions"] = questions
        data["tool_response"] = {"questions": questions,
                                 "answers": {q["question"]: "Quick (Recommended)" for q in questions}}
        return json.dumps(data).encode("utf-8")

    def test_hook_import_budget(self):
        prompt = self.imports("user-prompt", payload_bytes("user_prompt", _PLAIN))
        post = self.imports("post-tool", self.post_tool_stdin())
        start = self.imports("session-start", payload_bytes("session_start", _PLAIN))
        self.assertEqual([m for m in _HOT_PATH_BANNED if m in prompt], [], "user-prompt")
        self.assertEqual([m for m in _HOT_PATH_BANNED if m in post], [], "post-tool")
        self.assertNotIn("apprentice.asks", prompt)
        self.assertNotIn("apprentice.asks", start)
        self.assertNotIn("apprentice.filters", start)


if __name__ == "__main__":
    unittest.main()
