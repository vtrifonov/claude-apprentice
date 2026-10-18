"""Ledger end to end through the real scripts: distill, decide, let the 150-pair window slide, distill again."""
import hashlib
import json
import os
import shlex
import subprocess

from apprentice import events, ledger
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures.events.profiles import ts
from tests.fixtures.events.prompts import prompt_event

WINDOW = 150
G_BASE = "gherkin marinade walnut zebra quarterly"
A_BASE = "pickle cobbler lantern ribbon"
B_BASE = "tundra falcon harbor quilt"
LISTS = ("candidates", "weak", "covered", "missing")


def own_id(event_ids_by_ts):
    """The id a fresh correction group gets: its earliest event."""
    return "r_" + hashlib.sha1(("corr:" + event_ids_by_ts[0]).encode()).hexdigest()[:8]


class LedgerE2ETest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.claude_md = self.write(os.path.join(self.fake_home, ".claude", "CLAUDE.md"), "# Mine\n")
        self.nonce = 0

    # seeding

    def prompt(self, text, day, seconds, session):
        record = prompt_event(text=text, ts=ts(day, seconds), session_id=session, profile="default", repo=None,
                              cwd=None, transcript_path=None, prev_assistant="I will change it.")
        events.append_event(self.home, record)
        return record["id"]

    def group(self, base, count, day, session_prefix):
        """`count` correction prompts sharing `base`, split over two sessions on two days (day, day + 1)."""
        out = []
        for index in range(count):
            half = index * 2 // count
            out.append(self.prompt("no, %s vx%dk" % (base, index), day + half, index * 10,
                                   "%s-%d" % (session_prefix, index)))
        return out

    def unrelated(self, count, day):
        for _ in range(count):
            self.nonce += 1
            self.prompt("no, zq%dx wv%dy" % (self.nonce, self.nonce), day, self.nonce, "nonce-%d" % self.nonce)

    def reword(self, base, day):
        """Two prompts reworded from `base` in two sessions on two different local days."""
        return [self.prompt("no, %s rw%dk" % (base, index), day + index, 0, "rw-%s-%d" % (day, index))
                for index in range(2)]

    # scripts

    def run_script(self, name, *args):
        env = dict(os.environ, APPRENTICE_HOME=self.home, HOME=self.fake_home)
        env.pop("CLAUDE_CONFIG_DIR", None)
        script = os.path.join(REPO_ROOT, "scripts", name + ".py")
        cmd = '"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S %s' % shlex.quote(script)
        cmd += "".join(" " + shlex.quote(arg) for arg in args)
        result = subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode()

    def distill(self):
        paths = self.run_script("distill", "--all").split()
        with open(paths[0], encoding="utf-8") as fh:
            raw = fh.read()
        with open(paths[1], encoding="utf-8") as fh:
            markdown = fh.read()
        self.assertNotIn('"tokens"', raw)
        self.assertNotIn('"tokens"', markdown)
        return json.loads(raw), markdown, paths[0]

    def record(self, *args):
        return self.run_script("ledger", "record", *args)

    def apply(self, *args):
        return self.run_script("apply", *args)

    # assertions

    def entries(self, pack):
        return [cand for name in LISTS for cand in pack[name]]

    def find(self, pack, event_ids):
        found = [cand for cand in self.entries(pack) if set(cand["evidence_ids"]) == set(event_ids)]
        self.assertEqual(len(found), 1)
        return found[0]

    def names(self, pack, rule_id):
        return [name for name in LISTS if any(cand["id"] == rule_id for cand in pack[name])]

    def assert_slid(self, pack, *old_ids):
        old = set()
        for ids in old_ids:
            old.update(ids)
        seen = set()
        for cand in self.entries(pack):
            seen.update(cand["evidence_ids"])
        self.assertEqual(seen & old, set())

    def rule_file(self, rule_id, text):
        return self.write(os.path.join(self.home, "state", "tmp", rule_id + ".txt"), text)

    def last_entry(self, rule_id):
        return [e for e in ledger.read_proposals(self.home) if e["id"] == rule_id][-1]

    def two_groups(self):
        a_ids = self.group(A_BASE, 4, 0, "a")
        b_ids = self.group(B_BASE, 4, 2, "b")
        self.unrelated(WINDOW - 8, 10)
        pack, _, path = self.distill()
        return a_ids, b_ids, self.find(pack, a_ids)["id"], self.find(pack, b_ids)["id"], path

    # tests

    def test_declined_group_stays_hidden_after_window_slide(self):
        g_ids = self.group(G_BASE, 6, 0, "g")
        self.unrelated(WINDOW - 6, 10)
        first, _, path = self.distill()
        group = self.find(first, g_ids)
        self.assertEqual(sorted(group["evidence_ids"]), sorted(g_ids))
        self.assertEqual(first["remaining"]["hidden_by_ledger"], 0)
        self.record("--id", group["id"], "--status", "rejected", "--n-at-decision", "6", "--pack", path)
        new_ids = self.reword(G_BASE, 20)
        self.unrelated(10, 30)
        second, _, _ = self.distill()
        self.assert_slid(second, g_ids)
        new_id = own_id(new_ids)
        for rule_id in (group["id"], new_id):
            self.assertEqual(self.names(second, rule_id), [])
        self.assertGreaterEqual(second["remaining"]["hidden_by_ledger"], 1)

    def backdate(self, rule_id, decided_at):
        """Re-record the last decision as made at `decided_at`, before the fixture events that follow it."""
        entry = dict(self.last_entry(rule_id), decided_at=decided_at)
        ledger.append_proposal(self.home, entry)

    def test_declined_group_resurfaces_after_enough_new_evidence(self):
        g_ids = self.group(G_BASE, 3, 0, "g")
        first, _, path = self.distill()
        group = self.find(first, g_ids)
        self.record("--id", group["id"], "--status", "rejected", "--n-at-decision", "3", "--pack", path)
        self.backdate(group["id"], ts(15))
        hidden, _, _ = self.distill()
        self.assertEqual(self.names(hidden, group["id"]), [])
        self.assertEqual(hidden["remaining"]["hidden_by_ledger"], 1)
        new_ids = self.reword(G_BASE, 20) + self.reword(G_BASE, 30)
        second, _, _ = self.distill()
        listed = [cand for cand in second["candidates"] + second["weak"] if cand["id"] == group["id"]]
        self.assertEqual(len(listed), 1)
        self.assertEqual(second["remaining"]["hidden_by_ledger"], 0)
        self.assertTrue(set(new_ids) <= set(listed[0]["evidence_ids"]))

    def test_merged_then_declined_hidden_under_alias(self):
        a_ids, b_ids, a, b, path = self.two_groups()
        self.record("--id", a, "--status", "rejected", "--merged-ids", b, "--n-at-decision", "4", "--pack", path)
        entry = self.last_entry(a)
        self.assertEqual(len(entry["tokens"]), 2)
        self.assertEqual(entry["merged_ids"], [b])
        new_ids = self.reword(B_BASE, 20)
        self.unrelated(10, 30)
        second, _, _ = self.distill()
        self.assert_slid(second, a_ids, b_ids)
        self.assertGreaterEqual(second["remaining"]["hidden_by_ledger"], 1)
        for rule_id in (a, b, own_id(new_ids)):
            self.assertEqual(self.names(second, rule_id), [])

    def test_accepted_with_line_present_listed_covered(self):
        g_ids = self.group(G_BASE, 6, 0, "g")
        self.unrelated(WINDOW - 6, 10)
        first, _, path = self.distill()
        rule_id = self.find(first, g_ids)["id"]
        text = self.rule_file(rule_id, "Keep gherkin marinade walnut zebra quarterly sorted.")
        self.apply("--id", rule_id, "--text-file", text, "--scope", "global", "--target", self.claude_md,
                   "--pack", path)
        entry = self.last_entry(rule_id)
        self.assertEqual(entry["status"], "accepted")
        self.assertIsNotNone(entry["tokens"])
        self.assertIsNone(entry["cluster_key"])
        second, _, _ = self.distill()
        self.assertEqual(self.names(second, rule_id), ["covered"])
        self.write(self.claude_md, "# Mine\n")
        third, _, _ = self.distill()
        self.assertEqual(self.names(third, rule_id), ["missing"])

    def test_merged_then_accepted_covered_under_alias(self):
        a_ids, b_ids, a, b, path = self.two_groups()
        text = self.rule_file(a, "Keep pickle cobbler lantern ribbon sorted.")
        self.apply("--id", a, "--text-file", text, "--scope", "global", "--target", self.claude_md, "--pack", path,
                   "--merged-ids", b)
        entry = self.last_entry(a)
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["merged_ids"], [b])
        self.assertEqual(len(entry["tokens"]), 2)
        self.assertIsNone(entry["cluster_key"])
        new_ids = self.reword(B_BASE, 20)
        self.unrelated(10, 30)
        second, _, _ = self.distill()
        self.assert_slid(second, a_ids, b_ids)
        new_id = own_id(new_ids)
        covered = [cand for cand in second["covered"] if cand["id"] == a]
        self.assertEqual(len(covered), 1)
        self.assertEqual(covered[0]["inherited_from"], new_id)
        for rule_id in (a, b, new_id):
            for name in ("candidates", "weak", "missing"):
                self.assertNotIn(name, self.names(second, rule_id))
        self.write(self.claude_md, "# Mine\n")
        third, _, _ = self.distill()
        self.assertEqual(self.names(third, a), ["missing"])

    def test_shown_only_candidate_still_proposable(self):
        g_ids = self.group(G_BASE, 6, 0, "g")
        self.unrelated(WINDOW - 6, 10)
        first, _, path = self.distill()
        rule_id = self.find(first, g_ids)["id"]
        self.record("--id", rule_id, "--status", "shown", "--pack", path)
        second, _, _ = self.distill()
        self.assertEqual(self.find(second, g_ids)["id"], rule_id)
        self.assertEqual(self.names(second, rule_id), ["candidates"])
