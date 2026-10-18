from apprentice import audit, cluster
from tests.base import ApprenticeTestCase

SKILL = "deploy"
HEADER = "rollout"
LABELS = "no|yes"


def _ts(day, hour=10):
    return "2026-05-%02dT%02d:00:00.000Z" % (day, hour)


def _key(stem):
    return (SKILL, HEADER, stem, LABELS)


def _ask(stem, first_seen, last_seen=None):
    key = _key(stem)
    return {"kind": "ask", "key": key, "id": cluster.ask_rule_id(key), "stem": stem,
            "first_seen": first_seen, "last_seen": last_seen or first_seen}


def _pair(event_id, tokens):
    return {"event": {"id": event_id}, "tokens": set(tokens), "visible": True}


def _group(group_id, pairs, topic=None, first_seen=None):
    return {"kind": "correction", "id": group_id, "pairs": pairs, "topic": topic,
            "first_seen": first_seen or _ts(20)}


def _entry(rule_id, status, decided_at=None, cluster_key=None, tokens=None, topic=None, evidence_ids=None,
           merged_ids=None, n_at_decision=None, kind=None):
    return {"id": rule_id, "status": status, "decided_at": decided_at, "ts": decided_at,
            "cluster_key": cluster_key, "tokens": tokens, "topic": topic, "evidence_ids": evidence_ids,
            "merged_ids": merged_ids, "n_at_decision": n_at_decision, "kind": kind}


OLD_STEM = "rebase widgets canary pipeline"
NEW_STEM = "rebase widgets canary staging"  # Jaccard 3/5 = 0.6 with OLD_STEM
S1 = ["alpha", "bravo", "charlie", "delta"]
S2 = ["foxtrot", "golf"]


class MatchByIdTest(ApprenticeTestCase):
    def test_match_by_id(self):
        cand = _ask(OLD_STEM, _ts(2))
        entry = _entry(cand["id"], "rejected", _ts(1))
        index = audit.LedgerIndex([entry])
        self.assertEqual(audit.match_candidate(cand, index, {}), (cand["id"], entry))

    def test_match_by_alias_merged_ids(self):
        cand = _ask(OLD_STEM, _ts(2))
        entry = _entry("r_merged1", "accepted", _ts(1), merged_ids=[cand["id"]])
        index = audit.LedgerIndex([entry])
        self.assertEqual(audit.match_candidate(cand, index, {}), ("r_merged1", entry))


class RewordedStemTest(ApprenticeTestCase):
    def test_reworded_stem_inherits(self):
        old_key = cluster.key_string(_key(OLD_STEM))
        entry = _entry("r_old00001", "rejected", _ts(3), cluster_key=old_key, n_at_decision=40)
        index = audit.LedgerIndex([entry])
        cand = _ask(NEW_STEM, _ts(6))
        # the old key's events run past decided_at (day 3) but all end before the new stem's first event
        last_by_key = {old_key: _ts(5)}
        self.assertEqual(audit.match_candidate(cand, index, last_by_key), ("r_old00001", entry))
        original = cand["id"]
        self.assertEqual(audit.ledger_disposition(cand, entry, 39, _ts(20), None), "hidden-rejected")
        self.assertEqual(cand["id"], "r_old00001")
        self.assertEqual(cand["inherited_from"], original)
        self.assertEqual(audit.ledger_disposition(dict(cand), entry, 40, _ts(20), None), "proposable")

    def test_declined_twice_third_stem_inherits_latest_decision(self):
        first_key = cluster.key_string(_key("lint widgets ledger report"))
        second_key = cluster.key_string(_key(OLD_STEM))
        first = _entry("r_twice001", "rejected", _ts(1), cluster_key=first_key, n_at_decision=5)
        second = _entry("r_twice001", "rejected", _ts(4), cluster_key=second_key, n_at_decision=8)
        index = audit.LedgerIndex([first, second])
        cand = _ask(NEW_STEM, _ts(9))
        self.assertEqual(audit.match_candidate(cand, index, {first_key: _ts(1), second_key: _ts(4)}),
                         ("r_twice001", second))

    def test_cooccurring_clusters_never_merge(self):
        a = _ask(OLD_STEM, _ts(1), _ts(5))
        b = _ask(NEW_STEM, _ts(2), _ts(6))
        last_by_key = {cluster.key_string(a["key"]): a["last_seen"], cluster.key_string(b["key"]): b["last_seen"]}
        empty = audit.LedgerIndex([])
        self.assertIsNone(audit.match_candidate(a, empty, last_by_key))
        self.assertIsNone(audit.match_candidate(b, empty, last_by_key))
        accepted = _entry(a["id"], "accepted", _ts(7), cluster_key=cluster.key_string(a["key"]))
        index = audit.LedgerIndex([accepted])
        self.assertIsNone(audit.match_candidate(b, index, last_by_key))
        own = b["id"]
        self.assertEqual(audit.ledger_disposition(b, None, 0, _ts(8), None), "proposable")
        self.assertEqual(b["id"], own)

    def test_stopped_but_overlapping_still_separate(self):
        old_key = cluster.key_string(_key(OLD_STEM))
        entry = _entry("r_stop0001", "rejected", _ts(10), cluster_key=old_key)
        index = audit.LedgerIndex([entry])
        # the old cluster stopped on day 4, but the new one started on day 3 while it was still running
        cand = _ask(NEW_STEM, _ts(3), _ts(12))
        self.assertIsNone(audit.match_candidate(cand, index, {old_key: _ts(4)}))

    def test_old_key_absent_from_run_passes_time_check(self):
        old_key = cluster.key_string(_key(OLD_STEM))
        entry = _entry("r_gone0001", "rejected", _ts(1), cluster_key=old_key)
        cand = _ask(NEW_STEM, _ts(3))
        self.assertEqual(audit.match_candidate(cand, audit.LedgerIndex([entry]), {}), ("r_gone0001", entry))

    def test_different_labels_never_inherit(self):
        other = cluster.key_string((SKILL, HEADER, OLD_STEM, "maybe|no|yes"))
        entry = _entry("r_label001", "rejected", _ts(1), cluster_key=other)
        cand = _ask(NEW_STEM, _ts(3))
        self.assertIsNone(audit.match_candidate(cand, audit.LedgerIndex([entry]), {}))

    def test_low_jaccard_keeps_own_id(self):
        old_key = cluster.key_string(_key("rebase widgets lint ledger report"))  # 2/7 with NEW_STEM
        entry = _entry("r_low00001", "rejected", _ts(1), cluster_key=old_key)
        cand = _ask(NEW_STEM, _ts(3))
        self.assertIsNone(audit.match_candidate(cand, audit.LedgerIndex([entry]), {}))

    def test_tie_keeps_own_id(self):
        key_a = cluster.key_string(_key(OLD_STEM))
        key_b = cluster.key_string(_key("rebase widgets canary mirror"))  # also 3/5 with NEW_STEM
        index = audit.LedgerIndex([_entry("r_tiea0001", "rejected", _ts(1), cluster_key=key_a),
                                   _entry("r_tieb0001", "rejected", _ts(1), cluster_key=key_b)])
        cand = _ask(NEW_STEM, _ts(3))
        self.assertIsNone(audit.match_candidate(cand, index, {}))


class CorrectionMatchTest(ApprenticeTestCase):
    def test_correction_token_match_per_seed_not_union(self):
        entry = _entry("r_corr0001", "rejected", _ts(1), tokens=[S1, S2], kind="correction")
        pair_tokens = ["alpha", "bravo", "charlie", "echo"]
        self.assertEqual(cluster.jaccard(set(pair_tokens), set(S1)), 0.6)
        self.assertLess(cluster.jaccard(set(pair_tokens), set(S1) | set(S2)), 0.5)
        group = _group("r_newgrp01", [_pair("e1", pair_tokens)])
        self.assertEqual(audit.match_candidate(group, audit.LedgerIndex([entry]), {}), ("r_corr0001", entry))

    def test_correction_topic_match(self):
        entry = _entry("r_topic001", "snoozed", _ts(1), tokens=[S2], topic="commit-style", kind="correction")
        group = _group("r_newgrp02", [_pair("e1", S1)], topic="commit-style")
        self.assertEqual(audit.match_candidate(group, audit.LedgerIndex([entry]), {}), ("r_topic001", entry))

    def test_correction_undone_entry_not_matched_by_tokens(self):
        entry = _entry("r_undo0001", "undone", _ts(1), tokens=[S1], kind="correction")
        group = _group("r_newgrp03", [_pair("e1", S1)])
        self.assertIsNone(audit.match_candidate(group, audit.LedgerIndex([entry]), {}))

    def test_window_slide_declined_stays_hidden(self):
        declined = ["widgets", "ledger", "squash", "commits", "rebase", "branch", "history", "lint", "merge"]
        entry = _entry("r_slide001", "rejected", _ts(1), tokens=[declined], n_at_decision=4, kind="correction")
        # two reworded prompts, no shared quote; the first overlaps the declined seed at 6/11 = 0.55
        first = ["widgets", "ledger", "squash", "commits", "rebase", "branch", "tidy", "tree"]
        second = ["tidy", "tree", "linear", "graph"]
        self.assertAlmostEqual(cluster.jaccard(set(first), set(declined)), 6 / 11)
        group = _group("r_newgrp04", [_pair("e20", first), _pair("e21", second)])
        index = audit.LedgerIndex([entry])
        matched = audit.match_candidate(group, index, {})
        self.assertEqual(matched, ("r_slide001", entry))
        self.assertEqual(audit.ledger_disposition(group, matched[1], 2, _ts(20), None), "hidden-rejected")
        self.assertEqual(group["id"], "r_slide001")
        self.assertEqual(group["inherited_from"], "r_newgrp04")

    def test_window_slide_accepted_listed_covered(self):
        entry = _entry("r_slide002", "accepted", _ts(1), tokens=[S1], kind="correction")
        group = _group("r_newgrp05", [_pair("e30", ["alpha", "bravo", "charlie", "echo"])])
        matched = audit.match_candidate(group, audit.LedgerIndex([entry]), {})
        self.assertEqual(matched, ("r_slide002", entry))
        self.assertEqual(audit.ledger_disposition(group, matched[1], 0, _ts(20), True), "covered")

    def test_evidence_id_inheritance(self):
        entry = _entry("r_evid0001", "rejected", _ts(1), tokens=[S2], evidence_ids=["e5", "e6"], kind="correction")
        index = audit.LedgerIndex([entry])
        group = _group("r_newgrp06", [_pair("e6", ["unrelated", "words"]), _pair("e9", ["other", "stuff"])])
        self.assertEqual(audit.inherit_by_evidence(group, index), "r_evid0001")
        self.assertEqual(audit.match_candidate(group, index, {}), ("r_evid0001", entry))
        stranger = _group("r_newgrp07", [_pair("e8", ["unrelated", "words"])])
        self.assertIsNone(audit.inherit_by_evidence(stranger, index))


class DispositionTest(ApprenticeTestCase):
    def test_snooze_expiry_14_days(self):
        entry = _entry("r_snooze01", "snoozed", _ts(1))
        self.assertEqual(audit.ledger_disposition({"id": "r_snooze01"}, entry, 0, "2026-05-14T23:59:59.000Z", None),
                         "hidden-snoozed")
        self.assertEqual(audit.ledger_disposition({"id": "r_snooze01"}, entry, 0, _ts(15), None), "proposable")

    def test_rejected_threshold_max3(self):
        entry = _entry("r_reject01", "rejected", _ts(1), n_at_decision=1)
        self.assertEqual(audit.ledger_disposition({"id": "r_reject01"}, entry, 2, _ts(9), None), "hidden-rejected")
        self.assertEqual(audit.ledger_disposition({"id": "r_reject01"}, entry, 3, _ts(9), None), "proposable")
        unknown = _entry("r_reject02", "rejected", _ts(1))
        self.assertEqual(audit.ledger_disposition({"id": "r_reject02"}, unknown, 2, _ts(9), None),
                         "hidden-rejected")

    def test_accepted_missing_when_line_absent(self):
        entry = _entry("r_accept01", "accepted", _ts(1))
        self.assertEqual(audit.ledger_disposition({"id": "r_accept01"}, entry, 0, _ts(9), False), "missing")
        self.assertEqual(audit.ledger_disposition({"id": "r_accept01"}, entry, 0, _ts(9), True), "covered")

    def test_undone_and_accept_failed_proposable(self):
        for status in ("undone", "accept_failed"):
            entry = _entry("r_eligib01", status, _ts(1))
            self.assertEqual(audit.ledger_disposition({"id": "r_eligib01"}, entry, 0, _ts(9), None), "proposable")

    def test_no_decision_proposable(self):
        self.assertEqual(audit.ledger_disposition({"id": "r_fresh001"}, None, 0, _ts(9), None), "proposable")
