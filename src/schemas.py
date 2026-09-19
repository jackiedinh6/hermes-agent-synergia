"""schemas.py — the ten tool schemas of the ``team`` toolset (spec §7.2).

Spec §7 (tool surface). Purity rule (plan S21 lint, same as ``policy.py``):
stdlib ``typing`` is the only import — no substrate, no sibling module — so
the S21 import guard and the §7.5 budget check can import this file standalone
(``sys.path.insert(0, 'team_plugin/hermes-agent-synergia'); import schemas``).

``TEAM_CREATE_TASK_SCHEMA`` and ``TEAM_STATUS_SCHEMA`` are copied **verbatim**
from spec §7.4 (implement.md S14.1): the description strings are byte-frozen —
S21 SHA-256-hashes them, and changing one changes the model's behaviour.
``test_tools.py`` re-parses the spec document with ``ast.literal_eval`` and
asserts the two dicts match, so the freeze is enforced by the suite, not by a
comment.

``ALL`` (in §7.2 table order) is the sequence S21 and the budget check import.
§7.5 capped the nine at 1 000 approximate tokens; ``team_attach_path``
raises the cap to 1 200 (``sum(len(json.dumps(s)) for s in ALL) // 4 < 1200``)
— keep the descriptions short. There is deliberately **no** ``team_clone``
schema (§7.2: cloning writes a board, a directory, a registry record, and
other profiles' configs — CLI/dashboard only, where a human approves).
"""

from __future__ import annotations

# ── verbatim from spec §7.4 (byte-frozen; do not edit a single byte) ─────────

TEAM_CREATE_TASK_SCHEMA = {
    "name": "team_create_task",
    "description": (
        "Create a Kanban card for your team. The board, tenant, workspace and default "
        "assignee are supplied by the team record — do not pass a board. The assignee must "
        "be a member of the team; call team_whoami to see the roster. Prefer this over "
        "kanban_create: it cannot put the card on the wrong board."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title":    {"type": "string", "description": "Imperative, ≤ 80 chars."},
            "body":     {"type": "string", "description": "Full spec for the worker on this card."},
            "assignee": {"type": "string", "description": "Team member profile. Omit → the team orchestrator."},
            "role":     {"type": "string", "description": "Route by role (implementer/reviewer/...) instead of naming a profile. Ignored when assignee is set."},
            "priority": {"type": "integer", "description": "Higher runs first. Default 0."},
            "parents":  {"type": "array", "items": {"type": "string"},
                          "description": "Parent card ids; this card stays 'todo' until they are done."},
            "skills":   {"type": "array", "items": {"type": "string"},
                          "description": "Skills to load in the worker. Omit → the member's team defaults."},
            "team":     {"type": "string", "description": "Only when you belong to more than one team."},
        },
        "required": ["title"],
    },
}

TEAM_STATUS_SCHEMA = {
    "name": "team_status",
    "description": (
        "Your team's work: per-status counts plus open cards with id, title, status, "
        "assignee and age. Scoped by the team's tenant label, so other teams sharing the "
        "board are excluded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status":   {"type": "string", "enum": ["todo", "ready", "running", "review", "blocked", "done"]},
            "assignee": {"type": "string", "description": "Filter to one member."},
            "limit":    {"type": "integer", "description": "Default 25, max 100."},
            "team":     {"type": "string"},
        },
        "required": [],
    },
}

# ── the remaining seven (§7.2; short descriptions, §7.5 budget) ──────────────

TEAM_WHOAMI_SCHEMA = {
    "name": "team_whoami",
    "description": (
        "Your team identity: team(s), role, board, workdir, roster. Start here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "team": {"type": "string"},
        },
        "required": [],
    },
}

TEAM_LIST_SCHEMA = {
    "name": "team_list",
    "description": (
        "The teams THIS profile belongs to — not every team on the host."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TEAM_SHOW_SCHEMA = {
    "name": "team_show",
    "description": (
        "One of your teams in full: the record plus a '_resolved' world view."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "team": {"type": "string"},
        },
        "required": [],
    },
}

TEAM_CATALOG_SCHEMA = {
    "name": "team_catalog",
    "description": (
        "Browse the profile catalog (packs + installed) to PROPOSE a roster change. "
        "Read-only: creates nothing. To add a member, propose the command: "
        "hermes team add-member <team> --from <id>."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pack":     {"type": "string"},
            "category": {"type": "string"},
            "search":   {"type": "string"},
            "team":     {"type": "string"},
        },
        "required": [],
    },
}

TEAM_ASSIGN_SCHEMA = {
    "name": "team_assign",
    "description": (
        "Reassign one of your team's cards. Assignee must be a team member."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id":  {"type": "string"},
            "assignee": {"type": "string"},
            "team":     {"type": "string"},
        },
        "required": ["task_id", "assignee"],
    },
}

TEAM_HANDOFF_SCHEMA = {
    "name": "team_handoff",
    "description": (
        "Complete your card with <summary> and create the successor card "
        "(parents=[current], assignee=<next>) in ONE call. Prefer over "
        "kanban_complete + team_create_task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id":    {"type": "string"},
            "summary":    {"type": "string"},
            "assignee":   {"type": "string"},
            "title":      {"type": "string"},
            "body":       {"type": "string"},
            "team":       {"type": "string"},
        },
        "required": ["task_id", "summary", "assignee"],
    },
}

TEAM_SYNC_SCHEMA = {
    "name": "team_sync",
    "description": (
        "Drift report for your team. DRY-RUN ONLY — this tool never writes. "
        "Apply drift with: hermes team sync <team>."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "team": {"type": "string"},
        },
        "required": [],
    },
}

TEAM_ATTACH_PATH_SCHEMA = {
    "name": "team_attach_path",
    "description": (
        "Attach a file already on disk to a card on your team's board. Hermes "
        "reads the bytes itself (25 MB cap) — never hand-encode base64 "
        "(kanban_attach), never publish a file to get a URL "
        "(kanban_attach_url). Pass the path you would give read_file; it must "
        "stay inside your task workspace."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path":         {"type": "string", "description": "File to attach. Absolute, or relative to the task workspace."},
            "task_id":      {"type": "string", "description": "Omit → the card you are working on."},
            "filename":     {"type": "string", "description": "Omit → the path's last component."},
            "content_type": {"type": "string", "description": "MIME override. Omit → guessed from the filename."},
            "team":         {"type": "string", "description": "Only when you belong to more than one team."},
        },
        "required": ["path"],
    },
}

# §7.2 table order, then team_attach_path. Still no team_clone (§7.2).
ALL = [
    TEAM_WHOAMI_SCHEMA,
    TEAM_LIST_SCHEMA,
    TEAM_SHOW_SCHEMA,
    TEAM_STATUS_SCHEMA,
    TEAM_CATALOG_SCHEMA,
    TEAM_CREATE_TASK_SCHEMA,
    TEAM_ASSIGN_SCHEMA,
    TEAM_HANDOFF_SCHEMA,
    TEAM_SYNC_SCHEMA,
    TEAM_ATTACH_PATH_SCHEMA,
]

TOOLS = tuple(s["name"] for s in ALL)
