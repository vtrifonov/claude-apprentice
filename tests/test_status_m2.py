import json
import os
import time

from apprentice import ledger
from tests.test_status import StatusTestBase

_DAY = 86400.0


class StatusM2Test(StatusTestBase):
    def setUp(self):
        super().setUp()
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"acme": ["default"], "work": ["work"]}}))

    def pack(self, name, bands, generated="2026-10-01T10:00:00.000Z", group="acme"):
        cands = [{"id": "r_%08d" % i, "band": band, "summary": "SECRET-SUMMARY-%d" % i}
                 for i, band in enumerate(bands)]
        path = os.path.join(self.home, "review", name + ".json")
        self.write(path, json.dumps({"v": 1, "generated_at": generated, "inputs": {"group": group},
                                     "candidates": cands}))
        return path

    def test_pending_ignores_members_merged_into_a_hiding_primary(self):
        self.pack("2026-10-01-1", ["high", "medium", "low"])
        ledger.append_proposal(self.home, {"id": "r_00000000", "status": "accepted",
                                           "merged_ids": ["r_00000001"]})
        self.assertIn("| pending | low 1 |", self.report())
        ledger.append_proposal(self.home, {"id": "r_00000000", "status": "accept_failed"})
        self.assertIn("| pending | high 1, medium 1, low 1 |", self.report())

    def test_pending_uses_newest_pack_of_the_current_group(self):
        self.pack("2026-10-01-1", ["high"])
        self.pack("2026-10-02-1", ["low", "low"], "2026-10-02T10:00:00.000Z", group="work")
        out = self.report()
        self.assertIn("| review pack | 2026-10-01 (2026-10-01-1.json) |", out)
        self.assertIn("| pending | high 1 |", out)
        work = self.report(env={"CLAUDE_CONFIG_DIR": "/nowhere/work"})
        self.assertIn("| review pack | 2026-10-02 (2026-10-02-1.json) |", work)
        self.assertIn("| pending | low 2 |", work)

    def test_no_pack_for_this_group(self):
        self.pack("2026-10-02-1", ["low"], group="work")
        out = self.report()
        self.assertIn("| review pack | no pack yet for this group |", out)
        self.assertNotIn("| pending |", out)

    def test_pending_by_band_from_newest_pack(self):
        self.pack("2026-09-01-1", ["low", "low", "low"], "2026-09-01T10:00:00.000Z")
        self.pack("2026-10-01-2", ["high", "high", "medium", "low"])
        out = self.report()
        self.assertIn("| review pack | 2026-10-01 (2026-10-01-2.json) |", out)
        self.assertIn("| pending | high 2, medium 1, low 1 |", out)
        self.assertNotIn("SECRET-SUMMARY", out)

    def test_pending_skips_hidden_by_ledger(self):
        self.pack("2026-10-01-1", ["high", "high"])
        ledger.append_proposal(self.home, {"id": "r_00000000", "status": "rejected"})
        self.assertIn("| pending | high 1 |", self.report())

    def test_no_pack_yet(self):
        self.assertIn("| review pack | no pack yet |", self.report())

    def test_ledger_totals(self):
        for rule_id, status in (("r_a", "accepted"), ("r_b", "accepted"), ("r_c", "rejected"),
                                ("r_d", "accept_failed"), ("r_e", "undone")):
            ledger.append_proposal(self.home, {"id": rule_id, "status": status})
        out = self.report()
        self.assertIn("| ledger | accepted 2, rejected 1, accept_failed 1, undone 1 |", out)

    def test_tone_file_ages_per_group(self):
        now = time.time()
        for slug, age in (("acme", 3), ("globex", 10)):
            path = self.write(os.path.join(self.home, "profile", "tone-%s.md" % slug), "x\n")
            os.utime(path, (now - age * _DAY, now - age * _DAY))
        out = self.report(now=now)
        self.assertIn("| tone-acme.md | 3 days old |", out)
        self.assertIn("| tone-globex.md | 10 days old |", out)

    def test_preclass_cache_count(self):
        for name in ("e1", "e2", "e3"):
            self.write(os.path.join(self.home, "state", "preclass", name + ".json"), "{}")
        self.assertIn("| preclassify cache | 3 entries |", self.report())

    def test_preclass_cache_absent(self):
        self.assertIn("| preclassify cache | none |", self.report())
