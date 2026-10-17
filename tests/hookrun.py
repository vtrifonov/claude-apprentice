import json
import os
import subprocess

from tests.base import REPO_ROOT

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
_EVENT_KEYS = {
    "pre-tool": "PreToolUse",
    "post-tool": "PostToolUse",
    "user-prompt": "UserPromptSubmit",
    "session-start": "SessionStart",
}


def hook_command(event):
    """Return the command string registered in hooks/hooks.json for a capture event name."""
    key = _EVENT_KEYS[event]
    with open(os.path.join(REPO_ROOT, "hooks", "hooks.json"), "r", encoding="utf-8") as fh:
        hooks = json.load(fh)["hooks"]
    return hooks[key][0]["hooks"][0]["command"]


def payload_bytes(name, transcript, **overrides):
    """Load a hook-payload fixture, point it at `transcript` and return it as JSON bytes."""
    with open(os.path.join(_FIXTURES, "hook-payloads", name + ".json"), "r", encoding="utf-8") as fh:
        text = fh.read()
    data = json.loads(text.replace("__TRANSCRIPT__", transcript))
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def baseline_command(event):
    """Return the hooks.json command for `event` with the script swapped for `-c pass`: same shell,
    interpreter and flags, no apprentice code."""
    command = hook_command(event)
    script = '"${CLAUDE_PLUGIN_ROOT}/hooks/capture.py" %s' % event
    if script not in command:
        raise ValueError("hook command for %s does not run %s" % (event, script))
    return command.replace(script, "-c pass")


def run_hook(event, stdin, env_extra, root=None, home=None, fake_home=None, command=None):
    """Run the hooks.json command for `event` through sh; return (code, stdout, stderr).

    root defaults to the repo, home and fake_home to the current APPRENTICE_HOME and HOME;
    command overrides the hooks.json command string.
    """
    env = {"PATH": "/usr/bin:/bin"}
    env["CLAUDE_PLUGIN_ROOT"] = root or REPO_ROOT
    env["APPRENTICE_HOME"] = home if home is not None else os.environ["APPRENTICE_HOME"]
    env["HOME"] = fake_home if fake_home is not None else os.environ["HOME"]
    env.update(env_extra)
    result = subprocess.run(["sh", "-c", command or hook_command(event)], input=stdin, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode, result.stdout, result.stderr
