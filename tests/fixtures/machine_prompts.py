"""Synthetic prompts the user never typed, one per built-in machine-prompt pattern."""

BUILT_IN = {
    "teammate": 'Another Claude session sent a message: <teammate-message teammate_id="bob" summary="status">'
                "Build is green for acme/widgets</teammate-message>",
    "agent": 'Another Claude session sent a message: <agent-message from="bob">Done with XX-123</agent-message>',
    "cross_session": 'Another Claude session sent a message: <cross-session-message from="bob">Tests pass'
                     "</cross-session-message>",
    "skill_body": "Base directory for this skill: /home/alice/.claude/skills/widgets\n\n# Widgets\nUse the cache.",
    "reinvocation": "(Re-invocation of /widgets — run it again for acme/widgets)",
    "usage_limit": "[Usage limit reached; resets at 10:00]",
    "usage_cleared": "[Earlier usage-limit notes no longer apply. Carry on.]",
    "continue": "Continue from where you left off.",
    "task_notification": "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n"
                         "</task-notification>",
    "autonomous_loop": "<<autonomous-loop>>",
    "tag_anywhere": 'FYI from Bob\n<teammate-message teammate_id="bob">Build is green</teammate-message>',
    "padded": "  \n Another Claude session sent a message: hello",
}

QUOTING_HUMAN = "no, check what another Claude session sent a message about before you merge"
HEARTBEAT = "Heartbeat for acme/widgets: check the build"
