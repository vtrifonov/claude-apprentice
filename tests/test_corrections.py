import hashlib

from apprentice import cluster
from tests.base import ApprenticeTestCase
from tests.fixtures.events import asks as ask_fixtures
from tests.fixtures.events.prompts import prompt_event


def _ts(second, minute=0):
    return "2030-01-05T10:%02d:%02d.000Z" % (minute, second)


def _prompt(event_id, text, second=0, minute=0, **overrides):
    return prompt_event(id=event_id, text=text, ts=_ts(second, minute), **overrides)


def _pairs(*events):
    return cluster.select_pairs(list(events))


class SelectTest(ApprenticeTestCase):
    def test_select_requires_prev_assistant_and_signal(self):
        kept = _prompt("e1", "no use zorp", 1)
        no_anchor = _prompt("e2", "no use zorp", 2, prev_assistant=None)
        no_signal = _prompt("e3", "looks fine", 3, tags=["ack"])
        interrupt = _prompt("e4", "stop that", 4, tags=[], after_interrupt=True)
        rejected = _prompt("e5", "not that", 5, tags=[], after_rejected_ask=True)
        standing = _prompt("e6", "always run lint", 6, tags=["standing"])
        ask = ask_fixtures.ask_event(id="e7")
        found = cluster.select_pairs([kept, no_anchor, no_signal, interrupt, rejected, standing, ask])
        self.assertEqual(sorted(pair["event"]["id"] for pair in found), ["e1", "e4", "e5", "e6"])
        first = [pair for pair in found if pair["event"]["id"] == "e1"][0]
        self.assertEqual(first["tokens"], cluster.content_tokens("no use zorp"))
        self.assertEqual(first["session_id"], "sess-1")
        self.assertEqual(first["repo"], "acme/widgets")
        self.assertEqual(first["profile"], "default")
        self.assertEqual(first["ts"], kept["ts"])

    def test_select_limit_most_recent(self):
        events = [_prompt("e%d" % index, "fix zorp", index) for index in range(10)]
        found = cluster.select_pairs(events, limit=4)
        self.assertEqual(sorted(pair["event"]["id"] for pair in found), ["e6", "e7", "e8", "e9"])

    def test_content_tokens_and_jaccard(self):
        self.assertEqual(cluster.content_tokens("The Zorp, and a b2c-thing x"), {"zorp", "b2c-thing"})
        self.assertEqual(cluster.content_tokens(None), set())
        self.assertEqual(cluster.jaccard(set(), set()), 0.0)
        self.assertEqual(cluster.jaccard({"a", "b"}, {"b", "c"}), 1 / 3)


class GroupTest(ApprenticeTestCase):
    def test_jaccard_grouping_greedy_against_seed(self):
        # A newest, then B, then C. J(A,B)=0.6, J(B,C)=0.6, J(A,C)=1/3: C must not chain via B.
        a = _prompt("ea", "alpha bravo cobalt delta", 30)
        b = _prompt("eb", "alpha bravo cobalt ember", 20)
        c = _prompt("ec", "bravo cobalt ember fjord", 10)
        groups = cluster.group_pairs(_pairs(a, b, c), {})
        sizes = sorted(len(group["pairs"]) for group in groups)
        self.assertEqual(sizes, [1, 2])
        big = [group for group in groups if len(group["pairs"]) == 2][0]
        self.assertEqual(sorted(pair["event"]["id"] for pair in big["pairs"]), ["ea", "eb"])
        self.assertEqual(big["tokens"], cluster.content_tokens("alpha bravo cobalt delta"))
        small = [group for group in groups if len(group["pairs"]) == 1][0]
        self.assertEqual(small["pairs"][0]["event"]["id"], "ec")

    def test_preclass_topic_grouping_and_drop(self):
        t1 = _prompt("e1", "alpha bravo", 1)
        t2 = _prompt("e2", "totally different words", 2)
        dropped = _prompt("e3", "alpha bravo", 3)
        weak_drop = _prompt("e4", "alpha bravo", 4)
        preclass = {
            "e1": {"is_correction": True, "confidence": 0.9, "topic": "naming"},
            "e2": {"is_correction": True, "confidence": 0.9, "topic": "naming"},
            "e3": {"is_correction": False, "confidence": 0.8},
            "e4": {"is_correction": False, "confidence": 0.79},
        }
        groups = cluster.group_pairs(_pairs(t1, t2, dropped, weak_drop), preclass)
        by_topic = {group["topic"]: group for group in groups}
        self.assertEqual(sorted(pair["event"]["id"] for pair in by_topic["naming"]["pairs"]), ["e1", "e2"])
        ids = sorted(pair["event"]["id"] for group in groups for pair in group["pairs"])
        self.assertEqual(ids, ["e1", "e2", "e4"])
        self.assertEqual(len(groups), 2)
        self.assertIsNone([group for group in groups if group["topic"] is None][0]["topic"])

    def test_group_id_from_earliest_event(self):
        newer = _prompt("e-newer", "alpha bravo cobalt", 30)
        older = _prompt("e-older", "alpha bravo cobalt", 5)
        groups = cluster.group_pairs(_pairs(newer, older), {})
        self.assertEqual(len(groups), 1)
        expected = "r_" + hashlib.sha1(b"corr:e-older").hexdigest()[:8]
        self.assertEqual(groups[0]["id"], expected)
        self.assertEqual(groups[0]["first_seen"], older["ts"])
        self.assertEqual(groups[0]["last_seen"], newer["ts"])
        self.assertEqual(groups[0]["seed"], "e-newer")

    def test_visible_and_withheld_counts(self):
        one = _prompt("e1", "alpha bravo cobalt", 1, strong_marker=True)
        two = _prompt("e2", "alpha bravo cobalt", 2, session_id="sess-2", repo="acme/gadgets")
        three = _prompt("e3", "alpha bravo cobalt", 3, strong_marker=True)
        pairs = cluster.select_pairs([one, two, three], visible=lambda event: event["id"] != "e2")
        groups = cluster.group_pairs(pairs, {})
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["visible_pairs"], 2)
        self.assertEqual(group["withheld_pairs"], 1)
        self.assertEqual(group["strong_marker_pairs"], 2)
        self.assertEqual(group["sessions"], ["sess-1", "sess-2"])
        self.assertEqual(group["repos"], ["acme/gadgets", "acme/widgets"])


class LinkTest(ApprenticeTestCase):
    def _cluster(self, ask):
        found = cluster.build_ask_clusters([ask], dict(cluster.config.DEFAULTS), lambda event: True)
        return found[0]

    def _link(self, prompt, ask, ask_cluster):
        groups = cluster.group_pairs(_pairs(prompt), {})
        return cluster.link_groups_to_clusters(groups, [ask_cluster], [prompt, ask]), groups[0]["id"]

    def test_link_by_two_shared_tokens(self):
        ask = ask_fixtures.ask_event(id="a1", header="Deploy window", question="Ship the zorpling build?",
                                     ts=_ts(0, 59), session_id="other")
        prompt = _prompt("p1", "please ship zorpling builds later", 1, 0)
        ask_cluster = self._cluster(ask)
        linked, group_id = self._link(prompt, ask, ask_cluster)
        self.assertEqual(linked[group_id], [ask_cluster["id"]])

    def test_one_shared_token_does_not_link(self):
        ask = ask_fixtures.ask_event(id="a1", header="Deploy window", question="Ship the zorpling build?",
                                     ts=_ts(0, 59), session_id="other")
        prompt = _prompt("p1", "please ship things", 1, 0)
        linked, group_id = self._link(prompt, ask, self._cluster(ask))
        self.assertEqual(linked[group_id], [])

    def test_link_by_after_rejected_ask(self):
        ask = ask_fixtures.ask_event(id="a1", header="Deploy window", question="Ship the zorpling build?",
                                     ts=_ts(0, 59), session_id="other")
        prompt = _prompt("p1", "unrelated words entirely", 1, 0, tags=[], after_rejected_ask=True)
        ask_cluster = self._cluster(ask)
        linked, group_id = self._link(prompt, ask, ask_cluster)
        self.assertEqual(linked[group_id], [ask_cluster["id"]])

    def test_link_by_120s_same_session(self):
        ask = ask_fixtures.ask_event(id="a1", header="Deploy window", question="Ship the zorpling build?",
                                     ts=_ts(0, 0), session_id="sess-1")
        ask_cluster = self._cluster(ask)
        near = _prompt("p1", "unrelated words entirely", 0, 2)
        linked, group_id = self._link(near, ask, ask_cluster)
        self.assertEqual(linked[group_id], [ask_cluster["id"]])
        far = _prompt("p2", "unrelated words entirely", 1, 2)
        linked, group_id = self._link(far, ask, ask_cluster)
        self.assertEqual(linked[group_id], [])
        other_session = _prompt("p3", "unrelated words entirely", 30, 0, session_id="sess-9")
        linked, group_id = self._link(other_session, ask, ask_cluster)
        self.assertEqual(linked[group_id], [])
        before = _prompt("p4", "unrelated words entirely", 0, 0, session_id="sess-1")
        before["ts"] = "2029-12-31T23:59:00.000Z"
        linked, group_id = self._link(before, ask, ask_cluster)
        self.assertEqual(linked[group_id], [])
