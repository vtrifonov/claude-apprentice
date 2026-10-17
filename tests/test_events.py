import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unittest

from apprentice import SCHEMA_VERSION, events
from tests.base import REPO_ROOT, ApprenticeTestCase

_CAP = 32768

_ENVELOPE_KEYS = {"v", "id", "ts", "kind", "source", "fidelity", "session_id", "profile", "entrypoint",
                  "cwd", "repo", "branch", "ticket", "transcript_path"}
_OPTIONAL_KEYS = {"flags", "partial", "truncated"}
_ASK_KEYS = {"tool_use_id", "skill_hint", "questions", "numbers", "numbers_other", "reset_at", "risk_words",
             "risk_words_negated", "hour_local", "weekday", "raw_response"}
_ASK_REJECTED_KEYS = {"tool_use_id", "questions", "numbers", "numbers_other", "reset_at", "risk_words",
                      "risk_words_negated", "hour_local", "weekday", "skill_hint"}
_PROMPT_KEYS = {"text", "sha1", "len", "words", "tags", "register", "strong_marker", "after_interrupt",
                "after_rejected_ask", "prev_assistant", "duplicate_of"}
_MACHINE_KEYS = {"command"}
_QUESTION_KEYS = {"header", "question", "multi_select", "options", "answer"}
_OPTION_KEYS = {"label", "description", "recommended"}
_ANSWER_KEYS = {"labels", "free_text", "is_other", "followed_recommended"}


def _env(kind, source="hook", **kw):
    return events.envelope(kind, source, "sess-1", "default", "cli", "/work/widgets", "acme/widgets",
                           "main", "XX-123", "/t/sess-1.jsonl", **kw)


def _question(text="Pick one?", desc="why", answer=True):
    return {
        "header": "Choice",
        "question": text,
        "multi_select": False,
        "options": [{"label": "A", "description": desc, "recommended": True},
                    {"label": "B", "description": desc, "recommended": False}],
        "answer": {"labels": ["A"], "free_text": None, "is_other": False, "followed_recommended": True}
        if answer else None,
    }


def _signals():
    return {"numbers": {}, "numbers_other": [], "reset_at": None, "risk_words": [], "risk_words_negated": [],
            "hour_local": 10, "weekday": 2}


def _ask(raw="", questions=None):
    rec = _env("ask")
    rec.update({"tool_use_id": "toolu_1", "skill_hint": None,
                "questions": questions if questions is not None else [_question()], "raw_response": raw})
    rec.update(_signals())
    return rec


class EventsTest(ApprenticeTestCase):
    def test_new_id_is_uuid4_shape(self):
        pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        ids = [events.new_id() for _ in range(200)]
        for value in ids:
            self.assertRegex(value, pattern)
        self.assertEqual(len(set(ids)), 200)

    def test_envelope_fields_and_fidelity(self):
        live = _env("ask", ts="2026-10-17T10:00:00.000Z")
        self.assertEqual(set(live), _ENVELOPE_KEYS)
        self.assertEqual(live["v"], SCHEMA_VERSION)
        self.assertEqual(live["fidelity"], "live")
        self.assertEqual(live["ts"], "2026-10-17T10:00:00.000Z")
        self.assertEqual(live["kind"], "ask")
        self.assertEqual(live["repo"], "acme/widgets")
        back = _env("prompt", source="backfill", flags=["no_answer"])
        self.assertEqual(back["fidelity"], "transcript")
        self.assertEqual(back["flags"], ["no_answer"])
        self.assertIsNotNone(events.config.parse_iso(back["ts"]))
        self.assertNotIn("flags", _env("prompt", flags=[]))

    def test_serialize_cap_order(self):
        rec = _ask(raw="r" * 40000)
        out = events.serialize(rec)
        self.assertTrue(out.endswith(b"\n"))
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        self.assertNotIn("raw_response", parsed)
        self.assertNotIn("truncated", parsed)
        self.assertEqual(parsed["questions"][0]["options"][0]["description"], "why")

        rec = _ask(questions=[_question(desc="d" * 10000), _question(desc="d" * 10000)])
        out = events.serialize(rec)
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        for q in parsed["questions"]:
            for opt in q["options"]:
                self.assertNotIn("description", opt)
                self.assertIn("label", opt)
        self.assertNotIn("truncated", parsed)

        rec = _ask(questions=[_question(text="q" * 40000)])
        out = events.serialize(rec)
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        self.assertEqual(len(parsed["questions"][0]["question"]), 2000)
        self.assertIs(parsed["truncated"], True)

        small = _ask(raw="ok")
        self.assertEqual(events.serialize(small),
                         (json.dumps(small, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))

    def test_serialize_step4_cuts_long_strings(self):
        q = _question()
        q["options"][0]["label"] = "L" * 40000
        out = events.serialize(_ask(questions=[q]))
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        self.assertEqual(len(parsed["questions"][0]["options"][0]["label"]), 2000)
        self.assertIs(parsed["truncated"], True)

    def test_serialize_step5_keeps_first_question(self):
        questions = [_question(text="%02d" % i + "q" * 1997) for i in range(20)]
        out = events.serialize(_ask(questions=questions))
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        self.assertEqual(len(parsed["questions"]), 1)
        self.assertTrue(parsed["questions"][0]["question"].startswith("00"))
        self.assertIs(parsed["truncated"], True)

    def test_serialize_always_within_cap(self):
        q = _question()
        q["options"] = [{"label": "L" * 1999, "description": "d", "recommended": False}
                        for _ in range(100)]
        rec = _ask(questions=[q])
        rec["numbers_other"] = list(range(20000))
        out = events.serialize(rec)
        self.assertLessEqual(len(out), _CAP)
        parsed = json.loads(out.decode("utf-8"))
        self.assertIs(parsed["truncated"], True)
        self.assertEqual(parsed["id"], rec["id"])
        self.assertEqual(parsed["kind"], "ask")

    def test_serialize_lone_surrogate_valid_utf8(self):
        rec = _env("prompt")
        rec["text"] = "hi \ud83d"
        out = events.serialize(rec)
        parsed = json.loads(out.decode("utf-8"))
        self.assertEqual(parsed["text"], "hi ?")
        path = os.path.join(self.home, "s.jsonl")
        events.append(path, rec)
        self.assertEqual(len(list(events.tail_events(path))), 1)

    def test_append_creates_0600(self):
        path = os.path.join(self.home, "events", "2026-10.jsonl")
        os.makedirs(os.path.dirname(path))
        events.append(path, _ask())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        written = events.append_event(self.home, _env("machine", ts="2026-09-01T00:00:00Z"))
        self.assertEqual(written, os.path.join(self.home, "events", "2026-09.jsonl"))
        self.assertEqual(stat.S_IMODE(os.stat(written).st_mode), 0o600)

    def test_append_event_creates_dir_0700(self):
        written = events.append_event(self.home, _env("machine", ts="2026-09-01T00:00:00Z"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(written)).st_mode), 0o700)
        self.assertEqual(events.month_files(self.home), [written])

    def test_append_proceeds_when_lock_held(self):
        path = os.path.join(self.home, "locked.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        released = threading.Event()

        def hold():
            time.sleep(0.2)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            released.set()

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            start = time.monotonic()
            events.append(path, _env("machine"))
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.15)
            self.assertGreaterEqual(elapsed, 0.045)
            self.assertFalse(released.is_set())
            with open(path, "r", encoding="utf-8") as fh:
                self.assertEqual(len(fh.read().splitlines()), 1)
        finally:
            holder.join()

    def test_month_helpers(self):
        self.assertEqual(events.month_of("2026-10-31T23:59:59.000Z"), "2026-10")
        self.assertEqual(events.month_of("2026-11-01T01:00:00+02:00"), "2026-10")
        self.assertEqual(events.events_path(self.home, "2026-01-02T00:00:00Z"),
                         os.path.join(self.home, "events", "2026-01.jsonl"))
        for month in ("2026-10", "2025-12", "2026-02"):
            self.write(os.path.join(self.home, "events", month + ".jsonl"), "")
        self.write(os.path.join(self.home, "events", "notes.txt"), "")
        self.assertEqual([os.path.basename(p) for p in events.month_files(self.home)],
                         ["2025-12.jsonl", "2026-02.jsonl", "2026-10.jsonl"])

    def test_iter_skips_torn_non_object_and_missing_keys(self):
        good = json.dumps({"v": 1, "id": "a", "kind": "machine", "command": "x"})
        lines = [good, '{"v":1}', "[]", '"x"', '{"v":1,"id":"b","kind":"pro']
        self.write(os.path.join(self.home, "events", "2026-10.jsonl"), "\n".join(lines) + "\n")
        counters = {}
        got = list(events.iter_events(self.home, counters=counters))
        self.assertEqual([e["id"] for e in got], ["a"])
        self.assertEqual(counters["bad"], 4)

    def test_iter_skips_future_version(self):
        lines = [json.dumps({"v": 2, "id": "f", "kind": "ask"}), json.dumps({"v": 1, "id": "a", "kind": "ask"})]
        self.write(os.path.join(self.home, "events", "2026-10.jsonl"), "\n".join(lines) + "\n")
        counters = {}
        got = list(events.iter_events(self.home, counters=counters))
        self.assertEqual([e["id"] for e in got], ["a"])
        self.assertEqual(counters["future"], 1)

    def test_iter_order_and_month_filter(self):
        self.write(os.path.join(self.home, "events", "2026-10.jsonl"),
                   json.dumps({"v": 1, "id": "oct", "kind": "ask"}) + "\n")
        self.write(os.path.join(self.home, "events", "2026-09.jsonl"),
                   json.dumps({"v": 1, "id": "sep", "kind": "ask"}) + "\n")
        self.assertEqual([e["id"] for e in events.iter_events(self.home)], ["sep", "oct"])
        self.assertEqual([e["id"] for e in events.iter_events(self.home, months=["2026-10"])], ["oct"])

    def test_unknown_field_kept(self):
        self.write(os.path.join(self.home, "events", "2026-10.jsonl"),
                   json.dumps({"v": 1, "id": "a", "kind": "ask", "zz_new": {"x": 1}}) + "\n")
        got = list(events.iter_events(self.home))
        self.assertEqual(got[0]["zz_new"], {"x": 1})

    def test_tail_events_drops_partial_line(self):
        path = os.path.join(self.home, "events", "2026-10.jsonl")
        lines = [json.dumps({"v": 1, "id": "e%03d" % i, "kind": "machine", "pad": "p" * 50}) for i in range(100)]
        self.write(path, "\n".join(lines) + "\n")
        got = events.tail_events(path, nbytes=1000)
        ids = [e["id"] for e in got]
        self.assertEqual(ids[-1], "e099")
        self.assertEqual(ids, ["e%03d" % i for i in range(100 - len(ids), 100)])
        self.assertLess(len(ids), 100)
        self.assertEqual(len(events.tail_events(path, nbytes=10 ** 6)), 100)
        exact = len(lines[-1]) + 1
        self.assertEqual([e["id"] for e in events.tail_events(path, nbytes=exact)], ["e099"])

    def test_schema_doc_field_list(self):
        ask = _ask(raw="{}")
        rejected = _env("ask_rejected")
        rejected.update({"tool_use_id": "toolu_2", "questions": [_question(answer=False)], "skill_hint": None})
        rejected.update(_signals())
        prompt = _env("prompt")
        prompt.update({"text": "hello", "sha1": "0" * 40, "len": 5, "words": 1, "tags": [], "register": "plain",
                       "strong_marker": False, "after_interrupt": False, "after_rejected_ask": False,
                       "prev_assistant": None, "duplicate_of": None})
        machine = _env("machine")
        machine.update({"command": "/apprentice:status"})
        self.assertEqual(set(ask), _ENVELOPE_KEYS | _ASK_KEYS)
        self.assertEqual(set(rejected), _ENVELOPE_KEYS | _ASK_REJECTED_KEYS)
        self.assertEqual(set(prompt), _ENVELOPE_KEYS | _PROMPT_KEYS)
        self.assertEqual(set(machine), _ENVELOPE_KEYS | _MACHINE_KEYS)
        question = ask["questions"][0]
        self.assertEqual(set(question), _QUESTION_KEYS)
        self.assertEqual(set(question["options"][0]), _OPTION_KEYS)
        self.assertEqual(set(question["answer"]), _ANSWER_KEYS)
        with open(os.path.join(REPO_ROOT, "docs", "record-schema.md"), "r", encoding="utf-8") as fh:
            doc = fh.read()
        every = (_ENVELOPE_KEYS | _OPTIONAL_KEYS | _ASK_KEYS | _ASK_REJECTED_KEYS | _PROMPT_KEYS | _MACHINE_KEYS
                 | _QUESTION_KEYS | _OPTION_KEYS | _ANSWER_KEYS)
        missing = sorted(k for k in every if "`%s`" % k not in doc)
        self.assertEqual(missing, [])
        self.assertIn("v1 — 2026-10-17", doc)
        rows = {}
        for line in doc.splitlines():
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 5 and cells[0].startswith("`"):
                rows.setdefault(cells[0].strip("`"), cells)
        self.assertTrue(rows["numbers"][1].startswith("object"))
        self.assertEqual(rows["numbers_other"][1], "list of number")
        self.assertEqual(rows["text"][2], "yes")
        self.assertIn("image_only", rows["text"][4])

    def test_concurrent_appends_no_loss(self):
        path = os.path.join(self.home, "events", "2026-10.jsonl")
        os.makedirs(os.path.dirname(path))
        workers, per = 8, 500
        procs = [subprocess.Popen([sys.executable, "tests/_append_worker.py", path, str(per), str(i)],
                                  cwd=REPO_ROOT) for i in range(workers)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        with open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
        self.assertEqual(lines[-1], b"")
        lines = lines[:-1]
        self.assertEqual(len(lines), workers * per)
        seen = set()
        for line in lines:
            obj = json.loads(line.decode("utf-8"))
            seen.add(obj["id"])
        for i in range(workers):
            expected = {"w%d-%d" % (i, n) for n in range(per)}
            self.assertEqual(expected - seen, set(), "worker %d lost events" % i)


if __name__ == "__main__":
    unittest.main()
