import ast
import json
import os
import py_compile
import re
import subprocess
import tempfile
import unittest

from tests.base import REPO_ROOT
from tests.stdlib_modules import STDLIB

_CODE_DIRS = ("apprentice", "hooks", "scripts")
_HOOK_PREFIX = '"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "${CLAUDE_PLUGIN_ROOT}/hooks/capture.py" '
_SESSION_START_CMD = (
    _HOOK_PREFIX + "session-start || { s=$?; printf '%s session-start interpreter-missing: exit %s\\n' "
    '"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$s" >> "${APPRENTICE_HOME:-$HOME/.claude/apprentice}/hook-errors.log"; } '
    "2>/dev/null || true"
)


def _py_files():
    out = []
    for sub in _CODE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, sub)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
    return sorted(out)


def _parse(path):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    return ast.parse(src, filename=path, feature_version=(3, 9))


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


def _has_bitor(node):
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(node))


def _load(*parts):
    with open(os.path.join(REPO_ROOT, *parts), "r", encoding="utf-8") as fh:
        return json.load(fh)


class StaticGateTest(unittest.TestCase):
    def test_stdlib_only(self):
        bad = []
        for path in _py_files():
            for node in ast.walk(_parse(path)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name not in STDLIB and name != "apprentice":
                        bad.append("%s:%d %s" % (_rel(path), node.lineno, name))
        self.assertEqual(bad, [], "non-stdlib imports: " + ", ".join(bad))

    def test_py39_syntax(self):
        bad = []
        for path in _py_files():
            rel = _rel(path)
            for node in ast.walk(_parse(path)):
                line = getattr(node, "lineno", 0)
                annotations = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    every = args.posonlyargs + args.args + args.kwonlyargs
                    every += [a for a in (args.vararg, args.kwarg) if a is not None]
                    annotations = [a.annotation for a in every if a.annotation is not None]
                    if node.returns is not None:
                        annotations.append(node.returns)
                elif isinstance(node, ast.AnnAssign):
                    annotations = [node.annotation]
                for ann in annotations:
                    if _has_bitor(ann):
                        bad.append("%s:%d union operator in annotation" % (rel, line))
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        if func.id == "isinstance" and any(_has_bitor(a) for a in node.args):
                            bad.append("%s:%d union operator in isinstance" % (rel, line))
                        if func.id in ("exit", "quit"):
                            bad.append("%s:%d builtin %s()" % (rel, line, func.id))
                        if func.id == "zip" and any(k.arg == "strict" for k in node.keywords):
                            bad.append("%s:%d zip(strict=)" % (rel, line))
                if isinstance(node, ast.Attribute):
                    if node.attr in ("stdlib_module_names", "bit_count"):
                        bad.append("%s:%d attribute %s" % (rel, line, node.attr))
                    if node.attr == "UTC" and isinstance(node.value, ast.Name) and node.value.id == "datetime":
                        bad.append("%s:%d datetime.UTC" % (rel, line))
        self.assertEqual(bad, [], "3.9 violations: " + ", ".join(bad))

    def test_py_compile(self):
        with tempfile.TemporaryDirectory() as out:
            for i, path in enumerate(_py_files()):
                py_compile.compile(path, cfile=os.path.join(out, "%d.pyc" % i), doraise=True)

    def test_manifests(self):
        plugin = _load(".claude-plugin", "plugin.json")
        market = _load(".claude-plugin", "marketplace.json")
        self.assertEqual(plugin["name"], "apprentice")
        self.assertEqual(plugin["version"], "0.1.0")
        self.assertEqual(market["name"], "claude-apprentice")
        self.assertEqual([p["name"] for p in market["plugins"]], ["apprentice"])

    def test_hooks_json(self):
        hooks = _load("hooks", "hooks.json")["hooks"]
        expected = {
            "PreToolUse": ("AskUserQuestion", False),
            "PostToolUse": ("AskUserQuestion", True),
            "UserPromptSubmit": (None, True),
            "SessionStart": ("startup|resume|clear|compact", False),
        }
        self.assertEqual(sorted(hooks), sorted(expected))
        for event, (matcher, is_async) in expected.items():
            self.assertEqual(len(hooks[event]), 1, event)
            group = hooks[event][0]
            if matcher is None:
                self.assertNotIn("matcher", group, event)
            else:
                self.assertEqual(group["matcher"], matcher, event)
            self.assertEqual(len(group["hooks"]), 1, event)
            entry = group["hooks"][0]
            self.assertEqual(entry["type"], "command", event)
            self.assertEqual(entry["timeout"], 5, event)
            command = entry["command"]
            self.assertTrue(command.startswith(_HOOK_PREFIX), event)
            self.assertNotIn("python3 ", command.replace("${APPRENTICE_PYTHON:-/usr/bin/python3}", ""), event)
            if is_async:
                self.assertIs(entry.get("async"), True, event)
            else:
                self.assertNotIn("async", entry, event)
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["command"], _HOOK_PREFIX + "pre-tool || true")
        self.assertEqual(hooks["PostToolUse"][0]["hooks"][0]["command"], _HOOK_PREFIX + "post-tool || true")
        self.assertEqual(hooks["UserPromptSubmit"][0]["hooks"][0]["command"], _HOOK_PREFIX + "user-prompt || true")
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0]["command"], _SESSION_START_CMD)

    def test_scripts_help(self):
        scripts_dir = os.path.join(REPO_ROOT, "scripts")
        if not os.path.isdir(scripts_dir):
            return
        for name in sorted(os.listdir(scripts_dir)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(scripts_dir, name)
            cmd = '"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "%s" --help' % path
            result = subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, name)

    def test_commands_reference_existing_skills(self):
        commands_dir = os.path.join(REPO_ROOT, "commands")
        if not os.path.isdir(commands_dir):
            return
        for name in sorted(os.listdir(commands_dir)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(commands_dir, name), "r", encoding="utf-8") as fh:
                body = fh.read()
            for skill in set(re.findall(r"apprentice:([A-Za-z0-9_-]+)", body)):
                skill_md = os.path.join(REPO_ROOT, "skills", skill, "SKILL.md")
                self.assertTrue(os.path.isfile(skill_md), "%s references missing skill %s" % (name, skill))
            if name == "suggest.md":
                self.assertIn("apprentice:distill", body)

    def test_stopwords_file(self):
        path = os.path.join(REPO_ROOT, "apprentice", "data", "stopwords.txt")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if text.endswith("\n"):
            text = text[:-1]
        lines = text.split("\n")
        self.assertGreaterEqual(len(lines), 200)
        for line in lines:
            self.assertRegex(line, r"^[a-z']+$")
        self.assertEqual(len(set(lines)), len(lines))
        self.assertEqual(lines, sorted(lines))


if __name__ == "__main__":
    unittest.main()
