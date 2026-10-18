import json
import os
import stat
import subprocess
import tempfile
from unittest import mock

from apprentice import cluster, config, departures, distill, events, ledger, scope
from tests.base import REPO_ROOT, ApprenticeTestCase, isolated_env
from tests.fixtures import worktrees
from tests.fixtures.events import profiles
from tests.fixtures.events.asks import ask_event
from tests.fixtures.events.profiles import MODEL_LABELS, MODE_LABELS, ts
from tests.fixtures.events.prompts import prompt_event

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "distill.py")
ALICE = profiles.ALICE


def corr(text, day, session, profile="default", **extra):
    fields = {"text": text, "ts": ts(day), "session_id": session, "profile": profile, "repo": None, "cwd": None,
              "transcript_path": None, "prev_assistant": "I will change it."}
    fields.update(extra)
    return prompt_event(**fields)


def group(words, days, prefix, profile="default", **extra):
    return [corr("no, %s sorted" % words, day, "%s-%d" % (prefix, index), profile, **extra)
            for index, day in enumerate(days)]


def ask(day, session, used, picked, option_labels=None, header="Models", question="Use fable?",
        profile="default", skill=None, free_text=None, **extra):
    shape = {"header": header, "question": question, "option_labels": list(option_labels or MODEL_LABELS)}
    if free_text is not None:
        shape["free_text"] = free_text
    else:
        shape["answer_labels"] = [picked]
    fields = {"ts": ts(day), "session_id": session, "tool_use_id": "tu-" + session, "profile": profile,
              "skill_hint": skill, "numbers": {"used_pct": float(used)}, "repo": None, "cwd": None,
              "transcript_path": None}
    fields.update(extra)
    fields.update(shape)
    return ask_event(**fields)


def listed(pack):
    return pack["candidates"] + pack["weak"] + pack["covered"] + pack["missing"]


def walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            for found in walk_keys(inner):
                yield found
    elif isinstance(value, list):
        for inner in value:
            for found in walk_keys(inner):
                yield found


class DistillTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        scope._ARTIFACTS.clear()
        self.addCleanup(scope._ARTIFACTS.clear)

    def add(self, records, home=None):
        for record in records:
            events.append_event(home or self.home, record)
        return [record["id"] for record in records]

    def build(self, home=None, **kw):
        home = home or self.home
        args = {"since": None, "repo": None, "scope": None, "include_all": False, "limit": 10}
        args.update(kw)
        return distill.build_pack(home, os.environ, config.load_config(home), args["since"], args["repo"],
                                  args["scope"], args["include_all"], args["limit"], self.fake_home)

    def by_summary(self, pack, needle):
        found = [cand for cand in listed(pack) if needle in (cand["summary"] or "")]
        self.assertEqual(len(found), 1, needle)
        return found[0]


class CleanTest(DistillTestCase):
    def test_clean_watermark_and_entrypoint_and_observer(self):
        cfg = config.load_config(self.home)
        backfill = {"source": "backfill", "fidelity": "transcript"}
        hook = corr("no, alpha sorted", 0, "s1", ts=ts(0, 100))
        before = corr("no, beta sorted", 0, "s1", ts=ts(0, 90), **backfill)
        inside = corr("no, gamma sorted", 0, "s1", ts=ts(0, 97), **backfill)
        after = corr("no, delta sorted", 0, "s1", ts=ts(0, 150), **backfill)
        other_session = corr("no, epsilon sorted", 0, "s2", ts=ts(0, 150), **backfill)
        sdk = corr("no, zeta sorted", 1, "s5", entrypoint="sdk-ts")
        no_entry = corr("no, eta sorted", 1, "s6", entrypoint=None)
        observer = corr("no, theta sorted", 1, "s7", cwd="/tmp/claude-mem-observer/x")
        observer_path = corr("no, iota sorted", 1, "s8", transcript_path="/tmp/claude-mem-observer/t.jsonl")
        machine = events.envelope("machine", "hook", "s9", "default", "cli", None, None, None, None, None,
                                  ts=ts(1))
        duplicate = corr("no, kappa sorted", 1, "s10", duplicate_of="x")
        pasted = corr("no, lambda sorted", 1, "s11", tags=["correction", "pasted"])
        image = corr(None, 1, "s12", tags=["image_only"], register=None)
        live_ask = ask(2, "s3", 10, "Fable (Recommended)", ts=ts(2, 100), tool_use_id="tuX")
        copy_ask = ask(2, "s3", 10, "Fable (Recommended)", ts=ts(2, 40), tool_use_id="tuX", **backfill)
        twin_a = corr("no, mu sorted", 3, "s4", **backfill)
        twin_b = corr("no, mu sorted", 3, "s4", **backfill)
        twin_a["id"], twin_b["id"] = "twin-a", "twin-b"
        ack = corr("ok", 4, "s13", register="ack", tags=[])
        alice = corr("no, nu sorted", 4, "s14", profile=ALICE)
        records = [hook, before, inside, after, other_session, sdk, no_entry, observer, observer_path, machine,
                   duplicate, pasted, image, live_ask, copy_ask, twin_a, twin_b, ack, alice]
        out = distill.clean(records, cfg, "group-1", {"default": "group-1", ALICE: "group-2"})
        kept = {event["id"] for event in out}
        expected = {hook["id"], before["id"], other_session["id"], no_entry["id"], live_ask["id"], twin_a["id"],
                    ack["id"], alice["id"]}
        self.assertEqual(kept, expected)
        by_id = {event["id"]: event for event in out}
        self.assertTrue(by_id[ack["id"]]["count_only"])
        self.assertNotIn("count_only", by_id[hook["id"]])
        self.assertTrue(by_id[hook["id"]]["text_visible"])
        self.assertFalse(by_id[alice["id"]]["text_visible"])
        self.assertNotIn("text_visible", hook, "clean must not mutate its input")


class PackTest(DistillTestCase):
    def test_deterministic_output_except_generated_at(self):
        profiles.build(self.home)
        first, _ = self.build()
        second, _ = self.build()
        self.assertTrue(first["generated_at"])
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_cap_and_remaining_counts(self):
        self.add(group("apples bananas cherries", (0, 8, 16), "a"))
        self.add(group("dates elderberries figs", (1, 9, 17), "b"))
        self.add(group("grapes honeydew kiwis", (2, 10, 18), "c"))
        self.add(group("lemons mangoes nectarines", (3, 11), "d"))
        pack, _ = self.build(limit=2)
        self.assertEqual(len(pack["candidates"]), 2)
        self.assertEqual(len(pack["weak"]), 1)
        remaining = pack["remaining"]
        self.assertEqual(remaining["beyond_cap"], 1)
        self.assertEqual(remaining["weak"], 1)
        self.assertEqual(remaining["covered"], 0)
        self.assertEqual(remaining["hidden_by_ledger"], 0)
        self.assertEqual(remaining["single_signal"], 0)
        self.assertEqual(remaining["text_withheld"], 0)
        everything, _ = self.build(include_all=True)
        self.assertEqual(len(everything["candidates"]), 4)
        self.assertEqual(everything["weak"], [])

    def test_ranking_order(self):
        self.add(group("oranges papayas quinces", (0, 8, 16, 24, 32), "high"))
        self.add([ask(40, "c-0", 10, MODEL_LABELS[0]), ask(48, "c-1", 20, MODEL_LABELS[0]),
                  ask(56, "c-2", 90, MODEL_LABELS[1]), ask(64, "c-3", 95, MODEL_LABELS[1])])
        self.add(group("raspberries strawberries tangerines", (84, 92, 100), "new"))
        self.add(group("ugli vanilla watermelons", (54, 62, 70), "old"))
        pack, _ = self.build()
        order = [cand["summary"] for cand in pack["candidates"]]
        self.assertEqual(len(order), 4)
        self.assertIn("oranges", order[0])
        self.assertTrue(order[1].startswith("models: use fable?"))
        self.assertIn("raspberries", order[2])
        self.assertIn("ugli", order[3])
        self.assertEqual([cand["band"] for cand in pack["candidates"]], ["high", "medium", "medium", "medium"])
        self.assertTrue(pack["candidates"][1]["conditional"])

    def test_withheld_candidates_not_in_any_list(self):
        layout = ["Zorbhidden grid", "Plain"]
        self.add([ask(day, "l-%d" % day, 10, layout[0], option_labels=layout, header="Layout",
                      question="Use the zorbhidden grid?", profile=ALICE) for day in (0, 8, 16)])
        self.add(group("zorbhidden yaks zebras", (1, 9, 17), "hid", profile=ALICE))
        self.add(group("apples bananas cherries", (2, 10, 18), "vis"))
        pack, _ = self.build()
        self.assertEqual(len(listed(pack)), 1)
        self.assertIn("apples", listed(pack)[0]["summary"])
        self.assertEqual(pack["remaining"]["text_withheld"], 2)
        self.assertNotIn("zorbhidden", json.dumps(pack))
        self.assertNotIn("hint", pack["remaining"])

    def test_single_signal_counted(self):
        self.add([corr("no, apples bananas sorted", 0, "same", ts=ts(0, 10)),
                  corr("no, apples bananas sorted", 0, "same", ts=ts(0, 500))])
        pack, _ = self.build(include_all=True)
        self.assertEqual(listed(pack), [])
        self.assertEqual(pack["remaining"]["single_signal"], 1)

    def test_pack_has_no_tokens_key_anywhere(self):
        profiles.build(self.home)
        pack, private = self.build(include_all=True)
        self.assertTrue(listed(pack))
        self.assertNotIn("tokens", set(walk_keys(pack)))
        self.assertTrue(any(entry["tokens"] for entry in private.values()))

    def test_sidecar_written_outside_review_with_tokens(self):
        profiles.build(self.home)
        pack, private = self.build()
        json_path, md_path = distill.write_pack(self.home, pack, private)
        sidecar = os.path.join(self.home, "state", "pack-private", os.path.basename(json_path))
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(sorted(stored), sorted(cand["id"] for cand in listed(pack)))
        kinds = set()
        for cand in listed(pack):
            entry = stored[cand["id"]]
            kinds.add(entry["kind"])
            self.assertEqual(entry["kind"], cand["kind"])
            self.assertEqual(entry["evidence_ids"], cand["evidence_ids"])
            self.assertEqual(entry["n"], cand["band_inputs"]["n"])
            if cand["kind"] == "correction":
                self.assertTrue(entry["tokens"])
                for tokens in entry["tokens"]:
                    self.assertIsInstance(tokens, list)
                    self.assertTrue(tokens)
                    self.assertEqual(tokens, sorted(tokens))
            else:
                self.assertIsNone(entry["tokens"])
                self.assertIsInstance(entry["cluster_key"], str)
        self.assertEqual(kinds, {"ask", "correction"})
        review = os.path.join(self.home, "review")
        self.assertEqual(os.path.dirname(json_path), review)
        self.assertEqual(os.path.dirname(md_path), review)
        for name in os.listdir(review):
            with open(os.path.join(review, name), encoding="utf-8") as fh:
                self.assertNotIn("tokens", fh.read(), name)
        self.assertEqual(stat.S_IMODE(os.stat(json_path).st_mode), 0o600)

    def _expected(self, home, which):
        cfg = config.load_config(home)
        cleaned = distill.clean(list(events.iter_events(home)), cfg, None, {})
        found = [c for c in cluster.build_ask_clusters(cleaned, cfg, lambda event: True) if c["stem"] == "use fable?"]
        analysis = departures.analyze(found[0])
        return analysis, departures.sentence(analysis[which]) if analysis[which] else None

    def test_split_precedence_majority_over_recommended(self):
        quick, full = MODE_LABELS
        both = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        rows = [(full, 10), (full, 20), (full, 30), (full, 40), (quick, 90), (quick, 95)]
        self.add([ask(8 * index, "b-%d" % index, used, picked, option_labels=MODE_LABELS)
                  for index, (picked, used) in enumerate(rows)], home=both)
        analysis, majority = self._expected(both, "majority")
        self.assertIsNotNone(analysis["recommended"])
        recommended = departures.sentence(analysis["recommended"])
        self.assertNotEqual(majority, recommended)
        pack, _ = self.build(home=both)
        self.assertEqual(self.by_summary(pack, "use fable?")["departure"], majority)

        only = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        rec_full = ["Quick", "Full (Recommended)"]
        rows = [(rec_full, "Full (Recommended)", 10), (MODE_LABELS, quick, 20), (rec_full, "Full (Recommended)", 30),
                (MODE_LABELS, quick, 40), (MODE_LABELS, full, 90), (MODE_LABELS, full, 95)]
        self.add([ask(8 * index, "r-%d" % index, used, picked, option_labels=labels)
                  for index, (labels, picked, used) in enumerate(rows)], home=only)
        analysis, recommended = self._expected(only, "recommended")
        self.assertIsNone(analysis["majority"])
        self.assertIsNotNone(recommended)
        pack, _ = self.build(home=only)
        cand = self.by_summary(pack, "use fable?")
        self.assertEqual(cand["departure"], recommended)
        self.assertTrue(cand["conditional"])

    def test_majority_band_raises_conditional(self):
        fable, opus = MODEL_LABELS
        records = [ask(8 * index, "m-%d" % index, 10 * (index + 1), fable, repo="acme/widgets")
                   for index in range(6)]
        records.append(ask(41, "m-6", 90, opus, repo="acme/widgets"))
        records.append(ask(60, "m-7", 95, opus, repo="acme/widgets"))
        self.add(records)
        pack, _ = self.build()
        cand = self.by_summary(pack, "use fable?")
        self.assertTrue(cand["conditional"])
        self.assertTrue(cand["band_inputs"]["has_reversal"])
        self.assertEqual(cand["band_inputs"]["majority_band"], "high")
        self.assertEqual(cand["band"], "high")

    def test_rejected_candidate_hidden_until_threshold_in_pack(self):
        records = group("apples bananas cherries", (0, 8, 12, 20), "rj")
        for index, record in enumerate(records):
            record["id"] = "rj-%d" % index
        self.add(records)
        first, _ = self.build()
        rule_id = self.by_summary(first, "apples")["id"]
        ledger.append_proposal(self.home, {"id": rule_id, "status": "rejected", "n_at_decision": 3,
                                           "decided_at": ts(10), "kind": "correction"})
        hidden, _ = self.build()
        self.assertEqual(listed(hidden), [])
        self.assertEqual(hidden["remaining"]["hidden_by_ledger"], 1)
        third = corr("no, apples bananas cherries sorted", 28, "rj-4")
        third["id"] = "rj-4"
        self.add([third])
        shown, _ = self.build()
        self.assertEqual([cand["id"] for cand in shown["candidates"]], [rule_id])
        self.assertEqual(shown["remaining"]["hidden_by_ledger"], 0)

    def test_accepted_line_present_covered_absent_missing(self):
        self.add(group("apples bananas cherries", (0, 8, 16), "ac"))
        first, _ = self.build()
        cand = self.by_summary(first, "apples")
        self.assertEqual(cand["scope"], "global")
        rule_id = cand["id"]
        ledger.append_proposal(self.home, {"id": rule_id, "status": "accepted", "decided_at": ts(20),
                                           "kind": "correction"})
        target = os.path.join(self.fake_home, ".claude", "CLAUDE.md")
        self.write(target, "# notes\n<!-- apprentice:start -->\n- keep apples sorted <!-- apprentice:%s 2026-01-21 -->"
                           "\n<!-- apprentice:end -->\n" % rule_id)
        covered, _ = self.build()
        self.assertEqual([c["id"] for c in covered["covered"]], [rule_id])
        self.assertEqual(covered["candidates"], [])
        self.assertEqual(covered["remaining"]["covered"], 1)
        self.write(target, "# notes\n<!-- apprentice:start -->\n<!-- apprentice:end -->\n")
        missing, _ = self.build()
        self.assertEqual([c["id"] for c in missing["missing"]], [rule_id])
        self.assertEqual(missing["covered"], [])

    def test_reworded_stem_inherits_in_pack(self):
        cfg = config.load_config(self.home)
        aliases = config.effective_tables(cfg)["header_aliases"]
        old_q = "Use fable model for the release build?"
        new_q = "Use fable model for the nightly release build?"
        fable = MODEL_LABELS[0]
        self.add([ask(day, "o-%d" % day, 10, fable, question=old_q) for day in (0, 8, 16)])
        self.add([ask(day, "n-%d" % day, 10, fable, question=new_q) for day in (24, 32, 40)])
        old_key = cluster.cluster_key(None, "Models", old_q, MODEL_LABELS, aliases)
        new_key = cluster.cluster_key(None, "Models", new_q, MODEL_LABELS, aliases)
        old_id = cluster.ask_rule_id(old_key)
        ledger.append_proposal(self.home, {"id": old_id, "status": "rejected", "n_at_decision": 3,
                                           "decided_at": ts(50), "kind": "ask",
                                           "cluster_key": cluster.key_string(old_key)})
        pack, _ = self.build(include_all=True)
        self.assertEqual(listed(pack), [])
        self.assertEqual(pack["remaining"]["hidden_by_ledger"], 2)
        # an undone decision still matches but hides nothing, so the inheritance shows on the listed candidate
        ledger.append_proposal(self.home, {"id": old_id, "status": "undone", "decided_at": ts(51), "kind": "ask",
                                           "cluster_key": cluster.key_string(old_key)})
        shown, _ = self.build(include_all=True)
        inherited = [cand for cand in listed(shown) if cand["inherited_from"]]
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["id"], old_id)
        self.assertEqual(inherited[0]["inherited_from"], cluster.ask_rule_id(new_key))
        self.assertEqual(inherited[0]["stem"], new_key[2])

    def test_scope_repo_from_artifact_uses_evidence_main(self):
        trees = worktrees.build(os.path.join(self.home, "trees"))
        self.write(os.path.join(trees["main"], "package.json"), '{"name": "widgets-api"}\n')
        bindir = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        fake_git = self.write(os.path.join(bindir, "git"), "#!/bin/sh\nexit 1\n")
        os.chmod(fake_git, os.stat(fake_git).st_mode | stat.S_IXUSR)
        text = "no, the widgets-api package keeps fixtures beside code"
        self.add([corr(text, 0, "w-0", repo="acme/widgets", cwd=trees["wt1"]),
                  corr(text, 8, "w-1", repo="acme/widgets", cwd=trees["wt1"]),
                  corr(text, 16, "w-2", repo="acme/gadgets", cwd="/nonexistent-gadgets/app")])
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
            pack, _ = self.build()
        cand = self.by_summary(pack, "widgets-api")
        self.assertEqual(cand["scope"], "repo")
        self.assertEqual(cand["repos"], ["acme/widgets"])
        self.assertEqual(cand["target_status"], "resolved")
        self.assertEqual(cand["target"], os.path.join(trees["main"], "CLAUDE.local.md"))

    def test_write_pack_increments_n(self):
        self.add(group("apples bananas cherries", (0, 8, 16), "wp"))
        pack, private = self.build()
        first = distill.write_pack(self.home, pack, private)
        second = distill.write_pack(self.home, pack, private)
        day = pack["generated_at"][:10]
        self.assertEqual([os.path.basename(path) for path in first], [day + "-1.json", day + "-1.md"])
        self.assertEqual([os.path.basename(path) for path in second], [day + "-2.json", day + "-2.md"])
        for path in first + second:
            self.assertTrue(os.path.isfile(path))

    def test_other_group_free_text_never_reaches_pack_files(self):
        """Every free-text field of another account group's events is kept out of the written pack."""
        secret = {"prompt": "zorbleakprompt", "anchor": "zorbleakanchor", "free": "zorbleakfree",
                  "header": "zorbleakheader", "stem": "zorbleakstem", "label": "zorbleaklabel",
                  "cwd": "zorbleakcwd", "branch": "zorbleakbranch", "path": "zorbleakpath", "ticket": "424242",
                  "skillfree": "zorbleakskillfree", "sharedfree": "zorbleaksharedfree"}
        place = {"profile": ALICE, "cwd": "/nonexistent/%s/repo" % secret["cwd"], "branch": secret["branch"],
                 "transcript_path": "/nonexistent/%s/t.jsonl" % secret["path"], "ticket": "XX-" + secret["ticket"],
                 "repo": "acme/widgets"}
        other = []
        for index, day in enumerate((0, 8, 16)):
            other.append(prompt_event(text="no, %s yaks zebras" % secret["prompt"], ts=ts(day), session_id="p-%d" % index,
                                      prev_assistant="I will %s now." % secret["anchor"], **place))
        labels = ["%s one" % secret["label"], "Plain"]
        for index, day in enumerate((1, 9, 17)):
            other.append(ask_event(header=secret["header"], question="Pick the %s option?" % secret["stem"],
                                   option_labels=labels, free_text="%s answer" % secret["free"], ts=ts(day),
                                   session_id="a-%d" % index, tool_use_id="ta-%d" % index, skill_hint=None,
                                   numbers={}, **place))
        for index, day in enumerate((2, 10, 18)):
            other.append(ask_event(header="Mode", question="Quick mode?", option_labels=MODE_LABELS,
                                   free_text="%s please" % secret["skillfree"], ts=ts(day), session_id="k-%d" % index,
                                   tool_use_id="tk-%d" % index, skill_hint="oneshot-pipeline", numbers={}, **place))
        shared = [ask(3 + 8 * index, "s-%d" % index, 10, MODEL_LABELS[0]) for index in range(3)]
        shared.append(ask_event(header="Models", question="Use fable?", option_labels=MODEL_LABELS,
                                free_text="%s here" % secret["sharedfree"], ts=ts(30), session_id="s-other",
                                tool_use_id="ts-other", skill_hint=None, numbers={"used_pct": 10.0}, **place))
        self.add(other + shared)
        self.add(group("apples bananas cherries", (4, 12, 20), "own"))
        pack, private = self.build(include_all=True)
        paths = distill.write_pack(self.home, pack, private)
        written = ""
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                written += fh.read()
        for field, marker in sorted(secret.items()):
            self.assertNotIn(marker, written, field)
        inputs = pack["inputs"]
        self.assertEqual(inputs["events_other_groups"], len(other) + 1)
        self.assertEqual(inputs["events_current_group"], 3 + 3)
        self.assertEqual(inputs["events"], len(other) + len(shared) + 3)
        models = self.by_summary(pack, "use fable?")
        self.assertEqual(models["band_inputs"]["n"], 4)
        self.assertEqual(models["text_withheld"], {"events": 1, "free_text": 1})
        picker = self.by_summary(pack, "quick mode?")
        self.assertEqual(picker["quotes"], [])
        self.assertEqual(picker["text_withheld"], {"events": 3, "free_text": 3})
        self.assertEqual(pack["remaining"]["text_withheld"], 2)

    def test_script_prints_paths_and_rejects_removed_flags(self):
        self.add(group("apples bananas cherries", (0, 8, 16), "sc"))
        env = isolated_env()
        env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home})
        cmd = ["/usr/bin/python3", "-I", "-S", _SCRIPT, "--limit", "5", "--all"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(".json") and os.path.isfile(lines[0]))
        self.assertTrue(lines[1].endswith(".md") and os.path.isfile(lines[1]))
        for flag in ("--include-unknown", "--all-profiles"):
            result = subprocess.run(["/usr/bin/python3", "-I", "-S", _SCRIPT, flag], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, universal_newlines=True)
            self.assertEqual(result.returncode, 2, flag)
            self.assertIn("removed flag", result.stderr)


def _cand(**overrides):
    cand = {
        "id": "r_00000001", "kind": "ask", "scope": "global", "target": "/x/.claude/CLAUDE.md",
        "target_status": "resolved", "target_git": None, "target_shared_groups": [], "recommend": "accept",
        "alt_targets": {}, "cluster_key": None, "skill": None, "repos": [], "band": "medium",
        "band_inputs": {"n": 3, "sessions": 3, "repos": 1}, "conditional": False, "departure": None,
        "departure_rows": [], "audit": "new", "summary": "models: use fable? → fable 3/3", "quotes": [],
        "topic": None, "text_withheld": {"events": 0, "free_text": 0}, "evidence_ids": ["e1"],
        "first_seen": ts(0), "last_seen": ts(8), "header": "models", "stem": "use fable?", "labels": [],
        "display_questions": [], "free_texts": [], "rule_sketch": None, "inherited_from": None,
    }
    cand.update(overrides)
    return cand


class MarkdownTest(DistillTestCase):
    def test_markdown_block_lines(self):
        skill = _cand(id="r_00000002", scope="skill", skill="plain-skill", target="/s/plain-skill/SKILL.md",
                      target_git={"tracked": True, "root": "/s", "remote": "acme/skills", "visibility": "private"},
                      quotes=["first quote", "second quote"], text_withheld={"events": 3, "free_text": 1})
        shared = _cand(target_shared_groups=["group-2"], departure='used_pct ≥ 80 → "opus" 3 of 3')
        pack = {"generated_at": ts(30), "inputs": {"profile": "default", "group": "group-1", "events": 9,
                                                   "events_current_group": 6, "events_other_groups": 3},
                "candidates": [shared, skill], "weak": [_cand(id="r_00000003", band="low")], "covered": [],
                "missing": [_cand(id="r_00000004")],
                "remaining": {"beyond_cap": 0, "weak": 1, "covered": 0, "hidden_by_ledger": 0, "single_signal": 2,
                              "text_withheld": 1, "hint": distill.WITHHELD_HINT}}
        lines = distill.render_markdown(pack).splitlines()
        self.assertIn("- Shared with account groups: group-2", lines)
        self.assertIn("- Tracking: tracked in git (acme/skills, private)", lines)
        self.assertIn("- 3 more from other account groups (text withheld)", lines)
        self.assertIn('- Departure: used_pct ≥ 80 → "opus" 3 of 3', lines)
        self.assertIn("> first quote", lines)
        self.assertIn("## Weak", lines)
        self.assertIn("## Missing (run /apprentice:apply --replay)", lines)
        self.assertIn("- single_signal: 2", lines)
        self.assertIn(distill.WITHHELD_HINT, "\n".join(lines))
        self.assertEqual(sum(1 for line in lines if line.startswith("- Tracking:")), 1)
        self.assertEqual(sum(1 for line in lines if line.startswith("- Shared with account groups:")), 1)
