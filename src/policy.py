"""policy.py — the §6.2 decision table, pure (spec §5.2 layering).

Layer: ``policy.py ─► (nothing)``. This module performs **no I/O** and is the
only decision surface the ``pre_tool_call`` hook consults (§6.4: a team
plugin must never be the reason a host cannot run a tool). To keep it
exhaustively unit-testable and immune to the hook timeout, it imports
nothing but stdlib ``typing`` — no ``hermes_cli``, no ``os``/``os.path``,
no ``pathlib``, no ``subprocess``, no ``socket``, not even ``model`` (team
records are consumed duck-typed: attributes are read, never constructed).

Public API:

* :data:`GUARDED_TOOLS` — the kanban tools the table touches.
* :func:`resolve_team` — §6.3 "which team is this session acting for".
* :func:`decide` — §6.2 ``(team, profile, tool_name, args) -> Directive | None``.

Directive shapes (S14's hook maps them onto the substrate's ``pre_tool_call``
contract): ``{"modify": {top-level-key: value, ...}}`` (optionally with
``"note"``) or ``{"block": message}``. ``args`` merging is **shallow** — a
directive only ever carries whole top-level keys (§6.2 hard rule).

Evaluation: table order, top-down, first match wins — modify rows merge into
one directive (§5.6 stamps ``board`` and ``tenant`` in a single rewrite), and
a matching block row short-circuits: under ``enforce`` the assignee row has
no legal rewrite to fall back to, so it refuses regardless of what an earlier
modify row collected. ``mode: off`` disables every row. ``mode`` values
outside {off, advise, enforce} (impossible past ``model.validate_team``)
behave like ``advise`` — fail open, §6.4.

Two hard rules encoded here (asserted in ``test_policy_matrix.py``, not by
comments): **never guard** ``terminal`` / ``write_file`` / ``patch`` /
``execute_code``, and **never emit dotted keys**.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# The kanban tools §6.2 touches. Nothing else is ever looked at, which is
# what makes the four NEVER_GUARDED tools structurally safe, not merely
# exempted: a tool outside this set never reaches a rule.
GUARDED_TOOLS = frozenset({
    "kanban_create",
    "kanban_link",
    "kanban_unblock",
    "kanban_list",
})

# §6.2 hard rule, named so hooks and tests can reference the same object.
# Path-policing these is ``security.approval``'s product, not ours; guarding
# them would be trivially bypassable and false-positive-prone (§6.2).
NEVER_GUARDED = frozenset({"terminal", "write_file", "patch", "execute_code"})

# team workspace_policy → kanban workspace_kind (the substrate's vocabulary).
WORKSPACE_KIND = {"scratch": "scratch", "worktree": "worktree", "shared": "dir"}

# Any of these in kanban_create args is an explicit workspace choice.
_WORKSPACE_ARGS = ("workspace_kind", "workspace_path", "project", "project_id")

# §6.3 resolution env vars, in order.
TENANT_ENV = "HERMES_TENANT"
BOARD_ENV = "HERMES_KANBAN_BOARD"
TEAM_ENV = "HERMES_TEAM"

_STAMPING_NEVER = "never"


# ── duck-typed record readers (policy.py imports no model, §5.2) ────────────

def _attr(obj: Any, name: str, default: Any = "") -> Any:
    return getattr(obj, name, default)


def _team_slug(team: Any) -> str:
    return str(_attr(team, "slug") or "")


def _team_board(team: Any) -> str:
    return str(_attr(team, "board") or "")


def _effective_tenant(team: Any) -> str:
    """``tenant`` defaults to the team slug; an empty record field means
    the slug, and that is also the value we stamp and match on."""
    return str(_attr(team, "tenant") or "") or _team_slug(team)


def _member_profiles(team: Any) -> frozenset:
    return frozenset(
        str(_attr(m, "profile") or "") for m in (_attr(team, "members") or ())
    )


def _policy_mode(team: Any) -> str:
    return str(_attr(_attr(team, "policy"), "mode") or "advise")


def _stamping_on(team: Any) -> bool:
    """``auto`` and ``always`` both stamp (auto = always on a shared
    board, on by default on a dedicated one); only ``never`` turns it off."""
    mode = str(_attr(team, "tenant_stamping") or "auto")
    return mode != _STAMPING_NEVER


def _iter_teams(reg: Any) -> list:
    """Duck-type the registry: a ``model.Registry`` (has ``.teams`` mapping),
    a plain slug→team mapping, or any iterable of team records."""
    teams = getattr(reg, "teams", reg)
    if hasattr(teams, "values"):
        return list(teams.values())
    return [t for t in (teams or ()) if t is not None]


def _env(env: Any, key: str) -> str:
    """One env value, ``""`` when absent/empty — ``env`` is always a
    Mapping (never ``os.environ`` directly): the caller owns the boundary."""
    try:
        value = env.get(key)
    except AttributeError:
        return ""
    return str(value).strip() if value is not None else ""


# ── §6.3 team resolution ─────────────────────────────────────────────────────

def resolve_team(profile: Any, env: Mapping, reg: Any) -> Optional[str]:
    """Resolve "which team is this session acting for" (§6.3 order, exactly):

    1. ``HERMES_TENANT`` in *env* → the team whose (effective) tenant is that
       value — a dispatched worker: exact, authoritative.
    2. ``HERMES_KANBAN_BOARD`` → the team owning that board.
    3. ``HERMES_TEAM`` → the team with that slug.
    4. The *profile*'s ``primary: true`` team.
    5. Ambiguous or none → ``None`` — **guessing wrong is worse than not
       acting**; that is the point of the list.

    Returns the team **slug** (a ``str``) or ``None``. An env var that names
    nothing known falls through to the next step; an env var that matches
    *several* teams is ambiguous and resolves to ``None``, never a guess.
    *env* is a Mapping (``os.environ`` qualifies), *reg* is the registry.
    """
    teams = _iter_teams(reg)

    tenant = _env(env, TENANT_ENV)
    if tenant:
        hits = [t for t in teams if _effective_tenant(t) == tenant]
        if len(hits) == 1:
            return _team_slug(hits[0])
        if len(hits) > 1:
            return None

    board = _env(env, BOARD_ENV)
    if board:
        hits = [t for t in teams if _team_board(t) == board]
        if len(hits) == 1:
            return _team_slug(hits[0])
        if len(hits) > 1:
            return None

    want = _env(env, TEAM_ENV)
    if want:
        hits = [t for t in teams if _team_slug(t) == want]
        if len(hits) == 1:
            return _team_slug(hits[0])
        if len(hits) > 1:
            return None

    name = str(profile or "").strip()
    if name:
        hits = [
            t for t in teams
            if any(
                getattr(m, "primary", False)
                and str(_attr(m, "profile") or "") == name
                for m in (_attr(t, "members") or ())
            )
        ]
        if len(hits) == 1:
            return _team_slug(hits[0])
        if len(hits) > 1:
            return None

    return None


# ── §5.6-style messages ──────────────────────────────────────────────────────
# A block message names the team, the violation, the legal values AND the
# remedy — a model that gets only "denied" retries the same call (§5.6).

def _members_list(members: frozenset) -> str:
    return ", ".join(sorted(p for p in members if p))


def _outsider_message(team: Any, assignee: Any, members: frozenset) -> str:
    return (
        f"team {_team_slug(team)}: {str(assignee)!r} is not a member. "
        f"Members: {_members_list(members)}. "
        f"Use team_create_task, or add the profile with "
        f"`hermes team add-member {_team_slug(team)} {assignee}`."
    )


def _outsider_note(team: Any, assignee: Any, members: frozenset,
                   orchestrator: str) -> str:
    return (
        f"team {_team_slug(team)}: assignee {str(assignee)!r} is not a member; "
        f"redirected to the orchestrator {orchestrator!r}. "
        f"Members: {_members_list(members)}. To keep the original assignee, "
        f"add the profile with "
        f"`hermes team add-member {_team_slug(team)} {assignee}`."
    )


def _foreign_board_message(team: Any, board: Any, wanted: str) -> str:
    return (
        f"team {_team_slug(team)}: board {str(board)!r} is not this team's "
        f"board (policy: enforce). Legal value: board={wanted!r}. Re-issue "
        f"the call with board={wanted!r} — this team's cards live on that "
        f"board — or route the work with team_create_task."
    )


# ── §6.2 decision table ──────────────────────────────────────────────────────

def decide(team: Any, profile: Any, tool_name: Any,
           args: Any) -> Optional[dict]:
    """The §6.2 table: top-down, first match wins; ``modify`` before
    ``block``, always.

    Returns ``{"modify": {...}}`` (with ``"note"`` where the table adds one),
    ``{"block": message}``, or ``None`` (= allow unchanged). *args* is the
    tool-call argument mapping (any Mapping; ``None`` reads as empty).
    *profile* is the acting profile — accepted for the §6.2 signature and the
    hook's audit line; no row keys off it. A ``None`` *team* (unresolved
    session, §6.3 step 5) means no policy, in every mode.
    """
    if team is None:
        return None
    mode = _policy_mode(team)
    if mode == "off":
        return None
    if tool_name not in GUARDED_TOOLS:
        # Covers team_* (they validate internally), the four NEVER_GUARDED
        # tools, and everything else — one gate, no rule ever runs.
        return None

    if not isinstance(args, Mapping):
        args = {}
    board = _team_board(team)
    patch: dict = {}
    note: Optional[str] = None

    if tool_name == "kanban_create":
        members = _member_profiles(team)
        orchestrator = str(_attr(team, "orchestrator") or "")

        # Row 1: board absent or ≠ team.board → modify{board}.
        if args.get("board") != board:
            patch["board"] = board

        # Row 2: tenant absent and stamping on → modify{tenant}.
        if "tenant" not in args and _stamping_on(team):
            patch["tenant"] = _effective_tenant(team)

        # Rows 3/4: assignee. Row 3 (set, not a member) is the only block row
        # for this tool under enforce: a non-member assignee is an *intent*
        # we must not silently redirect (§6.2). Row 4 (absent) is a plain
        # rewrite in every mode.
        assignee = args.get("assignee")
        if assignee not in (None, ""):
            if str(assignee) not in members:
                if mode == "enforce":
                    return {"block": _outsider_message(team, assignee, members)}
                patch["assignee"] = orchestrator
                note = _outsider_note(team, assignee, members, orchestrator)
        else:
            patch["assignee"] = orchestrator

        # Row 4b: no workspace args on a worktree/shared team → the team's
        # workspace. Omitted, the substrate falls back to scratch, which is
        # deleted on completion, so the work never reaches the team workdir.
        if not any(k in args for k in _WORKSPACE_ARGS):
            kind = WORKSPACE_KIND.get(str(_attr(team, "workspace_policy") or ""))
            if kind in ("worktree", "dir"):
                patch["workspace_kind"] = kind
                workdir = str(_attr(team, "workdir") or "")
                if workdir.startswith("/"):
                    patch["workspace_path"] = workdir

    else:  # kanban_link / kanban_unblock / kanban_list
        # Rows 5/6: a foreign (or absent ⇒ default-board) board.
        if args.get("board") != board:
            if mode == "enforce" and tool_name in ("kanban_link",
                                                   "kanban_unblock"):
                return {"block": _foreign_board_message(
                    team, args.get("board"), board)}
            patch["board"] = board

    if patch:
        directive: dict = {"modify": patch}
        if note:
            directive["note"] = note
        return directive
    return None
