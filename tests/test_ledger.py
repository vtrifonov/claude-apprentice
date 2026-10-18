import json
import os
import shlex
import stat
import subprocess

from apprentice import ledger
from tests.base import REPO_ROOT, ApprenticeTestCase

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "ledger.py")


def _entry(rule_id, status, **extra):
    out = {"id": rule_id, "status": status}
    out.update(extra)
    return out


class LedgerFileTest(ApprenticeTestCase):
    def test_append_and_read_roundtrip_with_v_and_ts(self):
        ledger.append_proposal(self.home, {"id": "r_00000001", "status": "shown", "text": "x"})
        ledger.append_proposal(self.home, {"id": "r_00000002", "status": "shown", "v": 1, "ts": "2026-01-01T00:00:00.000Z"})
        rows = ledger.read_proposals(self.home)
        self.assertEqual([r["id"] for r in rows], ["r_00000001", "r_00000002"])
        self.assertEqual(rows[0]["v"], 1)
        self.assertTrue(rows[0]["ts"].endswith("Z"))
        self.assertEqual(rows[1]["ts"], "2026-01-01T00:00:00.000Z")
        ledger.append_applied(self.home, {"action": "apply", "id": "r_00000001", "file": "f", "result": "ok"})
        applied = ledger.read_applied(self.home)
        self.assertEqual(applied[0]["action"], "apply")
        self.assertEqual(applied[0]["v"], 1)
        path = os.path.join(self.home, "ledger", "proposals.jsonl")
        with open(path, "ab") as fh:
            fh.write(b"not json\n[]\n\n")
        self.assertEqual(len(ledger.read_proposals(self.home)), 2)

    def test_current_states_skips_shown_and_edited(self):
        rows = [
            _entry("a", "shown", ts="1"),
            _entry("a", "edited", ts="2"),
            _entry("a", "accepted", ts="3"),
            _entry("a", "shown", ts="4"),
            _entry("a", "undone", ts="5"),
            _entry("b", "shown", ts="1"),
        ]
        states = ledger.current_states(rows)
        self.assertEqual(sorted(states), ["a"])
        self.assertEqual(states["a"]["decided"]["status"], "undone")
        self.assertEqual(states["a"]["last_shown_ts"], "4")

    def test_shown_only_id_omitted_from_states(self):
        rows = [_entry("a", "shown"), _entry("a", "shown"), _entry("a", "edited")]
        self.assertNotIn("a", ledger.current_states(rows))

    def test_aliases_map(self):
        rows = [_entry("a", "rejected", merged_ids=["b", "c"]), _entry("d", "shown", merged_ids=None)]
        self.assertEqual(ledger.aliases(rows), {"b": "a", "c": "a"})

    def test_old_keys_collects_every_key(self):
        rows = [
            _entry("a", "shown", cluster_key="k1"),
            _entry("a", "rejected", cluster_key="k2"),
            _entry("a", "snoozed", cluster_key="k1"),
            _entry("a", "undone", cluster_key=None),
            _entry("b", "shown", cluster_key=None),
        ]
        self.assertEqual(ledger.old_keys(rows), {"a": {"k1", "k2"}})

    def test_seed_token_cap_by_frequency(self):
        seed = set("t%02d" % i for i in range(70))
        common = set("t%02d" % i for i in range(6, 70))
        got = ledger.seed_token_set([seed, common, common])
        self.assertEqual(len(got), 64)
        self.assertEqual(got, sorted(common))
        self.assertNotIn("t00", got)

    def test_seed_token_set_small_and_empty(self):
        self.assertEqual(ledger.seed_token_set([{"b", "a"}]), ["a", "b"])
        self.assertEqual(ledger.seed_token_set([]), [])

    def test_totals(self):
        rows = [
            _entry("a", "rejected"),
            _entry("b", "accepted"),
            _entry("c", "accepted"),
            _entry("c", "undone"),
            _entry("d", "shown"),
        ]
        self.assertEqual(ledger.totals(rows), {"rejected": 1, "accepted": 1, "undone": 1})

    def test_is_hiding_only_for_rejected_snoozed_accepted(self):
        self.assertEqual(ledger.HIDING, {"rejected", "snoozed", "accepted"})
        for status in ("rejected", "snoozed", "accepted"):
            self.assertTrue(ledger.is_hiding(status), status)
        for status in ("accept_failed", "undone", "shown", "edited", None):
            self.assertFalse(ledger.is_hiding(status), status)

    def test_private_sidecar_roundtrip_outside_review(self):
        pack = os.path.join(self.home, "review", "2026-10-18-1.json")
        path = ledger.private_path(self.home, pack)
        self.assertEqual(path, os.path.join(self.home, "state", "pack-private", "2026-10-18-1.json"))
        self.assertEqual(ledger.read_private(self.home, pack), {})
        mapping = {"A": {"tokens": [["x", "y"]], "cluster_key": None, "kind": "correction",
                         "topic": "t", "evidence_ids": ["e1"], "n": 3}}
        self.assertEqual(ledger.write_private(self.home, pack, mapping), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(ledger.read_private(self.home, pack), mapping)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        self.assertEqual(ledger.read_private(self.home, pack), {})


class ScriptTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.pack = os.path.join(self.home, "review", "2026-10-18-1.json")
        os.makedirs(os.path.dirname(self.pack))

    def run_script(self, *args):
        env = dict(os.environ, APPRENTICE_HOME=self.home, HOME=self.fake_home)
        env.pop("CLAUDE_CONFIG_DIR", None)
        interp = '"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S %s' % shlex.quote(_SCRIPT)
        cmd = interp + " " + " ".join(shlex.quote(a) for a in args)
        return subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def write_pack(self, candidates, group="g1"):
        self.write(self.pack, json.dumps({"inputs": {"group": group}, "candidates": candidates}))

    def entries(self, rule_id):
        # Full stored entries; `show` strips the sidecar-derived fields, so read the ledger file itself.
        return [e for e in ledger.read_proposals(self.home) if e.get("id") == rule_id]

    def shown(self, rule_id):
        result = self.run_script("show", "--id", rule_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.decode().splitlines() if line.strip()]

    def test_record_and_show_stdout_never_echo_sidecar_fields(self):
        marker = "zqmarkerword"
        self.write_pack([{"id": "A", "kind": "correction", "topic": "tabs", "evidence_ids": ["e1"]},
                         {"id": "B", "kind": "correction", "topic": "b", "evidence_ids": ["e2"]}])
        ledger.write_private(self.home, self.pack, {
            "A": {"tokens": [[marker, "beta"]], "cluster_key": marker + "-key", "evidence_ids": ["e1"], "n": 2},
            "B": {"tokens": [[marker + "b"]], "cluster_key": None, "evidence_ids": [marker + "-ev"], "n": 3},
        })
        result = self.run_script("record", "--id", "A", "--status", "shown", "--pack", self.pack,
                                 "--merged-ids", "B")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode(), "recorded id=A status=shown\n")
        self.assertIn(marker, json.dumps(self.entries("A")))  # stored, just never printed
        ledger.append_proposal(self.home, {"id": "A", "status": "edited",
                                           "extra": {"nested": [{"tokens": [marker], "cluster_key": marker,
                                                                 "evidence_ids": [marker], "keep": 1}]}})
        result = self.run_script("show", "--id", "A")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(marker, result.stdout.decode())
        rows = self.shown("A")
        self.assertEqual(len(rows), 2)
        for row in rows:
            for key in ("tokens", "cluster_key", "evidence_ids"):
                self.assertNotIn(key, row)
        self.assertEqual(rows[1]["extra"], {"nested": [{"keep": 1}]})
        self.assertEqual(rows[0]["merged_ids"], ["B"])

    def test_user_edit_file_confined_and_free_text_scrubbed(self):
        self.write_pack([{"id": "A", "kind": "correction", "topic": "a", "evidence_ids": []}])
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"extra_redact_patterns": ["zqsecret\\d+"]}))
        outside = self.write(os.path.join(self.home, "elsewhere.txt"), "hi")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--user-edit-file", outside)
        self.assertEqual(result.returncode, 2)
        inside = self.write(os.path.join(self.home, "state", "tmp", "A.edit.txt"), "it's `zqsecret42` $(x)")
        text = self.write(os.path.join(self.home, "state", "tmp", "A.txt"), "rule zqsecret43")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--user-edit-file", inside, "--text-file", text, "--reason", "why zqsecret44")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries("A")[-1]
        self.assertEqual(entry["user_edit"], "it's `<redacted:custom>` $(x)")
        self.assertEqual(entry["text"], "rule <redacted:custom>")
        self.assertEqual(entry["reason"], "why <redacted:custom>")

    def test_record_deletes_its_temp_files_on_success_and_failure(self):
        self.write_pack([{"id": "A", "kind": "correction", "topic": "a", "evidence_ids": []}])
        edit = self.write(os.path.join(self.home, "state", "tmp", "g1-A.edit.txt"), "say it shorter")
        text = self.write(os.path.join(self.home, "state", "tmp", "g1-A.txt"), "rule")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--user-edit-file", edit, "--text-file", text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(edit))
        self.assertFalse(os.path.exists(text))
        self.assertEqual(self.entries("A")[-1]["user_edit"], "say it shorter")
        edit = self.write(os.path.join(self.home, "state", "tmp", "g1-A.edit.txt"), "again")
        missing = os.path.join(self.home, "review", "nope.json")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", missing,
                                 "--user-edit-file", edit)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(edit))
        outside = self.write(os.path.join(self.home, "elsewhere.txt"), "hi")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--user-edit-file", outside)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(os.path.exists(outside))

    def test_show_blanks_free_text_of_other_groups(self):
        self.write(os.path.join(self.home, "config.json"), json.dumps({"account_groups": {"g1": ["default"]}}))
        ledger.append_proposal(self.home, {"id": "A", "status": "accepted", "group": "g1", "text": "own rule",
                                           "user_edit": "own edit", "reason": "own reason", "scope": "global"})
        ledger.append_proposal(self.home, {"id": "A", "status": "edited", "group": "g2", "text": "zqother rule",
                                           "user_edit": "zqother edit", "reason": "zqother reason",
                                           "scope": "global"})
        ledger.append_proposal(self.home, {"id": "A", "status": "shown", "inputs": {"group": "g2"},
                                           "text": "zqother inputs"})
        ledger.append_proposal(self.home, {"id": "A", "status": "accepted", "group": None, "text": "zqnone"})
        result = self.run_script("show", "--id", "A")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("zq", result.stdout.decode())
        rows = self.shown("A")
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows[0]["text"], rows[0]["user_edit"], rows[0]["reason"]),
                         ("own rule", "own edit", "own reason"))
        for row in rows[1:]:
            self.assertEqual((row["text"], row["user_edit"], row["reason"]), (None, None, None))
        self.assertEqual((rows[1]["status"], rows[1]["scope"]), ("edited", "global"))

    def test_script_record_reads_tokens_from_sidecar(self):
        self.write_pack([{"id": "A", "kind": "correction", "topic": "tabs", "evidence_ids": ["e1", "e2"],
                          "scope": "global", "target": "claude_md", "summary": "Use tabs"}])
        ledger.write_private(self.home, self.pack, {"A": {"tokens": [["alpha", "beta"]], "cluster_key": None,
                                                          "kind": "correction", "topic": "tabs",
                                                          "evidence_ids": ["e1", "e2"], "n": 7}})
        result = self.run_script("record", "--id", "A", "--status", "accepted", "--pack", self.pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries("A")[0]
        self.assertEqual(entry["kind"], "correction")
        self.assertEqual(entry["tokens"], [["alpha", "beta"]])
        self.assertEqual(entry["group"], "g1")
        self.assertEqual(entry["n_at_decision"], 7)
        self.assertTrue(entry["decided_at"])
        self.assertEqual(entry["profile"], "default")
        self.assertEqual(entry["pack"], os.path.join("review", "2026-10-18-1.md"))
        self.assertIsNone(entry["text"])  # no summary fallback: summary describes the rule, it is not its text

    def test_pack_outside_data_root_stored_as_given(self):
        outside = os.path.join(self.fake_home, "elsewhere", "p.json")
        entry = ledger.record_decision(self.home, outside, "A", "rejected")
        self.assertEqual(entry["pack"], os.path.join(self.fake_home, "elsewhere", "p.md"))

    def test_script_record_ask_carries_cluster_key_and_null_tokens(self):
        self.write_pack([{"id": "Q", "kind": "ask", "topic": "mode", "evidence_ids": ["e1"]}])
        ledger.write_private(self.home, self.pack, {"Q": {"tokens": None, "cluster_key": "a\x1fb", "kind": "ask",
                                                          "topic": "mode", "evidence_ids": ["e1"], "n": 4}})
        result = self.run_script("record", "--id", "Q", "--status", "rejected", "--pack", self.pack,
                                 "--reason", "nope")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries("Q")[0]
        self.assertEqual(entry["kind"], "ask")
        self.assertIsNone(entry["tokens"])
        self.assertEqual(entry["cluster_key"], "a\x1fb")
        self.assertEqual(entry["reason"], "nope")
        self.assertEqual(entry["n_at_decision"], 4)

    def test_script_merged_ids_stores_aliases_and_seed_sets(self):
        self.write_pack([
            {"id": "A", "kind": "correction", "topic": "a", "evidence_ids": ["e1"]},
            {"id": "B", "kind": "correction", "topic": "b", "evidence_ids": ["e2", "e1"]},
            {"id": "C", "kind": "correction", "topic": "c", "evidence_ids": ["e3"]},
        ])
        ledger.write_private(self.home, self.pack, {
            "A": {"tokens": [["a1", "a2"]], "cluster_key": None, "n": 2},
            "B": {"tokens": [["b1"]], "cluster_key": None, "n": 3},
            "C": {"tokens": [["c1"]], "cluster_key": None, "n": 4},
        })
        result = self.run_script("record", "--id", "A", "--status", "rejected", "--pack", self.pack,
                                 "--merged-ids", "B,C")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entries("A")[0]
        self.assertEqual(entry["merged_ids"], ["B", "C"])
        self.assertEqual(entry["tokens"], [["a1", "a2"], ["b1"], ["c1"]])
        self.assertEqual(sorted(entry["evidence_ids"]), ["e1", "e2", "e3"])
        self.assertEqual(ledger.aliases(ledger.read_proposals(self.home)), {"B": "A", "C": "A"})
        result = self.run_script("record", "--id", "A", "--status", "rejected", "--pack", self.pack,
                                 "--merged-ids", "X")
        self.assertEqual(result.returncode, 2)

    def test_record_without_pack_minimal_entry(self):
        entry = ledger.record_decision(self.home, None, "tone", "accepted", text="Be brief", scope="global",
                                       target="claude_md", profile="p1")
        self.assertEqual(entry["id"], "tone")
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["text"], "Be brief")
        self.assertEqual(entry["scope"], "global")
        self.assertEqual(entry["target"], "claude_md")
        self.assertEqual(entry["profile"], "p1")
        self.assertIsNone(entry["kind"])
        self.assertIsNone(entry["tokens"])
        self.assertEqual(ledger.read_proposals(self.home)[0]["id"], "tone")
        with_pack = ledger.record_decision(self.home, self.pack, "tone2", "accepted")
        for key in ("evidence_ids", "merged_ids", "cluster_key", "topic", "group", "n_at_decision", "pack"):
            self.assertIn(key, entry)
        self.assertEqual(set(entry), set(with_pack))
        self.assertIsNone(entry["pack"])
        self.assertIsNone(entry["group"])

    def test_script_text_file_outside_tmp_rejected(self):
        self.write_pack([{"id": "A", "kind": "correction", "topic": "a", "evidence_ids": []}])
        outside = self.write(os.path.join(self.home, "elsewhere.txt"), "hi")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--text-file", outside)
        self.assertEqual(result.returncode, 2)
        inside = self.write(os.path.join(self.home, "state", "tmp", "t.txt"), "edited text")
        result = self.run_script("record", "--id", "A", "--status", "edited", "--pack", self.pack,
                                 "--text-file", inside)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries("A")[0]["text"], "edited text")
        self.assertIsNone(self.entries("A")[0]["decided_at"])

    def test_removed_flags(self):
        for flag in ("--include-unknown", "--all-profiles"):
            result = self.run_script("show", "--id", "A", flag)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"removed flag", result.stderr)
