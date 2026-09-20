"""tools.py — the ten handlers of the ``team`` toolset (spec §7).

Spec §7.1–§7.5. The surface is deliberately small and deliberately
constrained:

* **Visibility** (§7.1) mirrors ``tools/kanban_tools.py:_visible`` — never in
  a ``delegate_task`` child context, never for profiles that are not on any
  team's roster, and ``orchestrator_only`` tools are invisible to dispatched
  workers. The substrate's own delegation predicates are imported lazily and
  every failure **fails closed** (invisible, and mutations refused): a team
  tool that cannot work out where it is running must not guess.
* **Envelope** (§7.3): every handler returns ``{"ok": true, "team": …,
  "data": {…}}`` or ``{"ok": false, "team": …, "error": {code, message, hint,
  remedy}}``. ``hint`` + ``remedy`` are mandatory on every error path — the
  contract lives in ``errors.TeamError`` (constructor-enforced), so handlers
  raise ``TeamError`` and the outer wrapper renders it.
* **Routing** (§7.2): board / tenant / workspace come from the team record,
  never from arguments. A ``team`` argument may only *narrow* to a team the
  caller belongs to.
* ``team_sync`` is dry-run only — it calls ``reconcile.diff`` and
  has no path to ``reconcile.apply``.
* ``team_catalog`` reads ``catalog.py`` and writes nothing; the
  roster change it enables is a *proposal* whose remedy is a CLI command.

Deviation (recorded per plan S14.3): the §7.2 table lists ``team_handoff``
visible to "all members", but implement.md S14.3 — which wins — puts it in
the ``orchestrator_only=True`` set, so dispatched workers see exactly the
four read tools (whoami / list / show / status). ``_ORCHESTRATOR_ONLY`` below
encodes the implement.md split.

Registration convention: :func:`register_tools` mirrors how
``tools/kanban_tools.py`` registers itself (``registry.register(name=…,
toolset=…, schema=…, handler=…, emoji=…, check_fn=…)``); S15's ``register
(ctx)`` calls it with the plugin context's ``ctx.register_tool``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Mapping, Optional

from . import schemas
from .errors import (
    TeamError,
    E_TEAM_INVALID,
    E_TEAM_NOT_FOUND,
    E_TEAM_NOT_MEMBER,
    E_TEAM_NO_BOARD,
)
from .model import _normalize_profile_name, board_label
from .policy import WORKSPACE_KIND

log = logging.getLogger("hermes_plugins.hermes_agent_synergia.tools")

TOOLSET = "team"

# implement.md S14.3 (authoritative over the §7.2 table for team_handoff):
# routing tools + catalog are orchestrator-only, so a dispatched worker sees
# exactly the four read tools and never pays for the 160-entry catalog (§7.5).
_ORCHESTRATOR_ONLY = frozenset({
    "team_create_task",
    "team_assign",
    "team_handoff",
    "team_sync",
    "team_catalog",
})

_EMOJI = {
    "team_whoami": "🪪",
    "team_list": "📃",
    "team_show": "🔎",
    "team_status": "📊",
    "team_catalog": "🗂",
    "team_create_task": "➕",
    "team_assign": "📌",
    "team_handoff": "🤝",
    "team_sync": "🩺",
    "team_attach_path": "📎",
}

# ── gating (§7.1) ────────────────────────────────────────────────────────────

def _delegation_ctx(predicate: str, default: bool) -> bool:
    """``agent.delegation_context.<predicate>()``; ``default`` when it cannot
    be evaluated. Mirrors kanban_tools._delegation_ctx; our default for the
    child-context question is ``True`` — fail CLOSED (a tool that cannot tell
    it is not alone on the machine assumes it is a child and hides)."""
    try:
        from agent import delegation_context
        return bool(getattr(delegation_context, predicate)())
    except Exception:
        return default


def _is_delegated_child_context() -> bool:
    return _delegation_ctx("is_delegated_child_context", True)


def _profile_inventory() -> list:
    try:
        from . import resolve
        return resolve.profile_inventory()
    except Exception:  # noqa: BLE001 — no substrate ⇒ no names
        return []


def _calling_profile(env: Mapping = None) -> str:
    """The profile this session is acting as: HERMES_PROFILE (what the
    substrate stamps for dispatched workers), else the caller's resolved
    profile. Empty when neither is set."""
    if env is None:
        env = os.environ
    name = str(env.get("HERMES_PROFILE") or "").strip()
    return _normalize_profile_name(name) if name else ""


def _teams_of(profile: str) -> list:
    """Slug → team record, for every team whose roster names *profile*.
    Fail open on an unreadable registry (``load_or_empty``, §5.4)."""
    if not profile:
        return []
    from . import storage
    reg = storage.load_or_empty()
    return [(slug, t) for slug, t in reg.teams.items()
            if any(m.profile == profile for m in t.members)]


def _profile_in_any_team() -> bool:
    return bool(_teams_of(_calling_profile()))


def _is_orchestrator_of_its_team() -> bool:
    """True when the calling profile is the ``orchestrator`` of at least one
    team it is on. The §7.1 predicate spells it exactly this way — orchestrator
    is a per-team field of the record, not a role label."""
    me = _calling_profile()
    return any(t.orchestrator == me for _slug, t in _teams_of(me))


def _visible(orchestrator_only: bool) -> Callable[[], bool]:
    """check_fn factory (§7.1). Fails closed everywhere it can."""
    def _check() -> bool:
        if _is_delegated_child_context():          # never for delegate_task children
            return False
        if not _profile_in_any_team():             # non-team profiles see no team tools
            return False
        return (not orchestrator_only) or _is_orchestrator_of_its_team()
    return _check


# ── team resolution for a handler call (§6.3 order, caller-owned env) ────────

def _resolve_team(args: Mapping, env: Mapping = None):
    """(slug, Team) for the team this call acts on, or a ``TeamError``.

    ``args["team"]`` may only narrow to a team the CALLER is on — naming
    another team is a member test the caller fails, not a lookup. With no
    ``team`` arg: the single team the caller is on; two or more →
    ``E_TEAM_INVALID`` naming them (spec §6.3: guessing is worse than asking).
    """
    if env is None:
        env = os.environ
    from . import storage
    reg = storage.load_or_empty()
    me = _calling_profile(env)
    mine = _teams_of(me)
    slug = str((args or {}).get("team") or "").strip()
    if slug:
        t = reg.teams.get(slug)
        if t is None:
            raise TeamError(
                E_TEAM_NOT_FOUND,
                f"no team {slug!r}",
                hint="Known teams you belong to: "
                     + (", ".join(sorted(s for s, _t in mine)) or "(none)"),
                remedy="hermes team list",
            )
        if not any(m.profile == me for m in t.members):
            raise TeamError(
                E_TEAM_NOT_MEMBER,
                f"profile {me!r} is not a member of team {slug}",
                hint="Your teams: "
                     + (", ".join(sorted(s for s, _t in mine)) or "(none)"),
                remedy=f"hermes team add-member {slug} {me}",
            )
        return slug, t
    if len(mine) == 1:
        return mine[0]
    hints = ", ".join(sorted(s for s, _t in mine)) or "(none — you are on no team)"
    raise TeamError(
        E_TEAM_INVALID,
        f"which team? this profile belongs to {len(mine)} of them"
        if mine else "you belong to no team; the team tools need one",
        hint=f"Pass a 'team' argument. Your teams: {hints}.",
        remedy="team_whoami   # or: hermes team list",
    )


# ── envelope helpers (§7.3) ──────────────────────────────────────────────────

def _env_err(exc: TeamError, team: Optional[str]) -> dict:
    return exc.to_envelope(team=team)


def _env_ok(team: Optional[str], data: Mapping) -> dict:
    envelope: dict = {"ok": True}
    if team is not None:
        envelope["team"] = team
    payload = dict(data)
    payload.setdefault("team", team)
    envelope["data"] = payload
    return envelope


def _wrap(fn: Callable[[dict], dict], tool_name: str) -> Callable[..., str]:
    """Handler entry: args dict (+ substrate kwargs) in, JSON envelope string
    out. A ``TeamError`` renders as the §7.3 failure envelope; any *other*
    exception must still leave as an envelope with a hint and a remedy
    (graceful degradation — nothing raises out of a handler)."""
    def handler(args: dict, **kw) -> str:
        team: Optional[str] = None
        try:
            team = str((args or {}).get("team") or "").strip() or None
            return json.dumps(fn(dict(args or {})))
        except TeamError as exc:
            return json.dumps(_env_err(exc, team))
        except Exception as exc:  # noqa: BLE001 — substrate down ⇒ envelope, never traceback
            log.exception("%s failed", tool_name)
            err = TeamError(
                E_TEAM_INVALID,
                f"{tool_name} failed: {exc}",
                hint="Unexpected failure — see the gateway log; the team state "
                     "was not changed.",
                remedy="hermes team show <team>   # verify state, then retry",
            )
            return json.dumps(_env_err(err, team))
    return handler


def _wrap_kw(fn: Callable[[dict, Mapping], dict], tool_name: str) -> Callable[..., str]:
    """:func:`_wrap` for a handler that also needs the dispatch kwargs. Same
    envelope contract, so the ten tools fail the same way."""
    def handler(args: dict, **kw) -> str:
        return _wrap(lambda a: fn(a, kw), tool_name)(args)
    return handler


def _member_profiles(t) -> dict:
    return {m.profile: m for m in t.members}


def _require_member(t, profile: str, *, what: str = "assignee"):
    p = _normalize_profile_name(str(profile or ""))
    if p not in _member_profiles(t):
        raise TeamError(
            E_TEAM_NOT_MEMBER,
            f"{p or '(empty)'} is not a member of team {t.slug}",
            hint=f"Members: {', '.join(m.profile for m in t.members)}.",
            remedy=f"hermes team add-member {t.slug} {p}",
        )
    return p


# ── handlers ─────────────────────────────────────────────────────────────────

def _h_whoami(args: dict) -> dict:
    slug, t = _resolve_team(args)
    me = _calling_profile()
    return _env_ok(slug, {
        "profile": me,
        "role": next((m.role for m in t.members if m.profile == me), "member"),
        "orchestrator": t.orchestrator,
        "is_orchestrator": t.orchestrator == me,
        "board": t.board,
        "workdir": t.workdir,
        "teammates": sorted(m.profile for m in t.members if m.profile != me),
    })


def _h_list(args: dict) -> dict:
    """Teams THIS profile belongs to — never every team on the host (§7.2)."""
    reg = _storage().load_or_empty()
    me = _calling_profile()
    rows = []
    for slug, t in reg.teams.items():
        m = next((m for m in t.members if m.profile == me), None)
        if m is None:
            continue
        rows.append({
            "slug": slug,
            "display_name": t.display_name or slug,
            "role": m.role,
            "orchestrator": t.orchestrator,
            "is_orchestrator": t.orchestrator == me,
            "board": t.board,
            "workdir": t.workdir,
        })
    rows.sort(key=lambda r: r["slug"])
    return _env_ok(None, {"teams": rows,
                          "count": len(rows),
                          "total_teams_on_host": len(reg.teams)})


def _h_show(args: dict) -> dict:
    slug, t = _resolve_team(args)
    from . import resolve
    d = t.to_dict()
    d["_resolved"] = resolve.observe(t).to_dict()
    return _env_ok(slug, {"team": d})


_OPEN_STATUSES = ("todo", "ready", "running", "review", "blocked")
_STATUS_DEFAULT_LIMIT = 25
_STATUS_MAX_LIMIT = 100


def _task_view(task) -> dict:
    return {"id": task.id, "title": task.title, "status": task.status,
            "assignee": task.assignee, "priority": task.priority,
            "created_at": task.created_at,
            # A blocked card reads the same as any other without this —
            # ``needs_input`` is the one kind that waits on a human.
            "block_kind": task.block_kind}


def _list_team_tasks(t) -> list:
    """Tenant-scoped rows off the team's board — read-only, and gated on the
    DB FILE existing (§5.2.1: kanban_db_connect creates/migrates on first
    open; a reader must never mint a board)."""
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(t.board)
    if not db_path.is_file():
        return []
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        return list(kanban_db.list_tasks(conn, tenant=(t.tenant or t.slug)))


def _h_status(args: dict) -> dict:
    slug, t = _resolve_team(args)
    from . import resolve
    status = str(args.get("status") or "").strip() or None
    assignee = str(args.get("assignee") or "").strip() or None
    limit = args.get("limit")
    limit = _STATUS_DEFAULT_LIMIT if limit is None else int(limit)
    if not 1 <= limit <= _STATUS_MAX_LIMIT:
        raise TeamError(
            E_TEAM_INVALID,
            f"limit {limit} out of range",
            hint=f"limit must be 1..{_STATUS_MAX_LIMIT} (default {_STATUS_DEFAULT_LIMIT}).",
            remedy="team_status with no limit, or a limit at most 100.",
        )
    counts = resolve.task_counts(t.board, t.tenant or None)
    if status and status not in counts:
        raise TeamError(
            E_TEAM_INVALID,
            f"unknown status {status!r}",
            hint="Statuses: " + ", ".join(sorted(counts)) + ".",
            remedy=f"team_status team={t.slug}",
        )
    rows = _list_team_tasks(t)
    open_rows = [r for r in rows if (not status and r.status in _OPEN_STATUSES)
                 or (status and r.status == status)]
    if assignee:
        open_rows = [r for r in open_rows if r.assignee == assignee]
    open_rows.sort(key=lambda r: (-int(r.priority or 0), str(r.created_at or "")))
    return _env_ok(slug, {
        "board": t.board,
        "counts": counts,
        "open_cards": [_task_view(r) for r in open_rows[:limit]],
        "total_matching": len(open_rows),
        "limit": limit,
    })


def _h_catalog(args: dict) -> dict:
    """Read-only browse of the profile catalog. Touches nothing:
    ``catalog.entries`` reads pack YAML through the filesystem only."""
    from . import catalog
    rows = catalog.entries(
        pack=(args.get("pack") or None),
        category=(args.get("category") or None),
        q=(args.get("search") or None),
    )
    slim = [{"id": r["id"], "display_name": r["display_name"],
             "category": r["category"], "description": r["description"],
             "suggested_role": r["suggested_role"]} for r in rows]
    return _env_ok(None, {
        "entries": slim,
        "count": len(slim),
        "creates": None,
        "note": "Read-only. To add a member, propose: "
                "hermes team add-member <team> --from <id>",
    })


def _create_card(t, *, title: str, body, assignee: str, parents=(),
                priority: int = 0, skills=None, created_by: str = "team-tool"):
    """The one write path: board/tenant/workspace pre-bound from the record.
    Same substrate call the dashboard's card endpoint makes (public
    ``kanban_db.create_task`` under ``kanban_db_connect``); the board is
    created if missing — this is a mutation path, so minting the DB is in
    scope (unlike the read-only status/catalog surfaces).

    ``creator_task_id`` is the calling worker's own card, exactly as the
    substrate's own writers stamp it (``kanban_create``, ``hermes kanban
    create``). It adds NO dependency edge — it is what puts the new card in
    the thread of the card that asked for it, so an orchestrator never has to
    reach for ``kanban_link`` to make the thread read in order."""
    from hermes_cli import kanban_db, kanban_db_connect
    from . import resolve
    if not t.board:
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"team {t.slug} has no board",
            hint="team_create_task pre-binds the board from the record; the "
                 "record has none.",
            remedy=f"hermes team update {t.slug} --board <slug> --create-board",
        )
    kind = WORKSPACE_KIND.get(t.workspace_policy or "scratch", "scratch")
    workdir = str(os.path.expanduser(t.workdir or ""))
    workspace_path = workdir if kind in ("worktree", "dir") else None
    resolve.prepare_worktree_workdir(t)

    own_card = (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None

    def _write():
        if not resolve.board_exists(t.board):
            resolve.ensure_board(t.board, name=board_label(t),
                                 default_workdir=t.workdir or None)
        with kanban_db_connect.connect_closing(
                db_path=kanban_db.kanban_db_path(t.board)) as conn:
            # A stale or cross-board HERMES_KANBAN_TASK must not graft this
            # card onto a foreign thread: stamp it only when the card is
            # really on this team's board.
            creator = (own_card if own_card
                       and kanban_db.get_task(conn, own_card) is not None
                       else None)
            return kanban_db.create_task(
                conn, created_by=created_by, board=t.board,
                title=str(title).strip(), body=body, assignee=assignee,
                tenant=(t.tenant or t.slug),
                workspace_kind=kind, workspace_path=workspace_path,
                priority=int(priority or 0),
                parents=tuple(parents or ()),
                creator_task_id=creator,
                skills=(list(skills) if skills else None))

    return _write()


def _h_create_task(args: dict) -> dict:
    slug, t = _resolve_team(args)
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TeamError(
            E_TEAM_INVALID,
            "title is required",
            hint='{"title": "...", "body": "...", "assignee": "<member>"} — '
                 "assignee must be a team member; never pass a board.",
            remedy=f"hermes team show {t.slug}   # the roster",
        )
    members = _member_profiles(t)
    assignee = args.get("assignee")
    role = args.get("role")
    if assignee:
        assignee = _require_member(t, assignee)
    elif role:
        match = next((m for m in t.members if m.role == str(role)), None)
        if match is None:
            raise TeamError(
                E_TEAM_NOT_MEMBER,
                f"no team member holds role {str(role)!r}",
                hint="Team roles: "
                     + (", ".join(sorted({m.role for m in t.members})) or "(none)"),
                remedy=f"hermes team add-member {t.slug} <profile> --role {role}",
            )
        assignee = match.profile
    else:
        assignee = t.orchestrator or None
    task_id = _create_card(
        t, title=title, body=args.get("body"), assignee=assignee,
        parents=args.get("parents") or (), priority=args.get("priority") or 0,
        skills=args.get("skills"))
    return _env_ok(slug, {
        "task_id": task_id, "board": t.board,
        "tenant": (t.tenant or t.slug), "assignee": assignee})


def _h_assign(args: dict) -> dict:
    slug, t = _resolve_team(args)
    tid = str(args.get("task_id") or "").strip()
    if not tid:
        raise TeamError(
            E_TEAM_INVALID, "task_id is required",
            hint="team_assign reassigns ONE existing card on your team's board.",
            remedy="team_status   # open cards with ids",
        )
    assignee = _require_member(t, args.get("assignee"))
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(t.board)
    if not db_path.is_file():
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"board {t.board!r} has no database yet",
            hint="There is nothing to reassign on a board with no cards.",
            remedy=f"hermes team sync {t.slug}   # create/repair the board first",
        )
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        task = kanban_db.get_task(conn, tid)
        if task is None:
            raise TeamError(
                E_TEAM_NOT_FOUND,
                f"no card {tid!r} on board {t.board!r}",
                hint="team_assign only touches cards on YOUR team's board.",
                remedy="team_status   # open cards with ids",
            )
        if not kanban_db.assign_task(conn, tid, assignee):
            raise TeamError(
                E_TEAM_INVALID,
                f"could not assign {tid!r} to {assignee!r} (card may be terminal)",
                hint="Only non-terminal cards can be reassigned.",
                remedy="team_status   # confirm the card's status",
            )
    return _env_ok(slug, {"task_id": tid, "assignee": assignee,
                          "board": t.board})


def _h_handoff(args: dict) -> dict:
    """kanban_complete(summary) + successor card with parents=[current] and
    the next assignee — the pairing members get wrong when done by hand (§7.4)."""
    slug, t = _resolve_team(args)
    tid = str(args.get("task_id") or "").strip()
    summary = str(args.get("summary") or "").strip()
    next_assignee = args.get("assignee")
    if not tid:
        raise TeamError(
            E_TEAM_INVALID, "task_id is required (the card you are handing off)",
            hint="Set the card you are done with.",
            remedy="team_status   # open cards with ids",
        )
    if not summary:
        raise TeamError(
            E_TEAM_INVALID, "summary is required — what the next worker must know",
            hint="The summary lands on the completed card AND the successor body.",
            remedy="team_handoff with {'task_id': ..., 'summary': ..., 'assignee': ...}",
        )
    next_assignee = _require_member(t, next_assignee, what="successor assignee")
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(t.board)
    if not db_path.is_file():
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"board {t.board!r} has no database yet",
            hint="There is no card to hand off on a board with no cards.",
            remedy=f"hermes team sync {t.slug}   # create/repair the board first",
        )
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        task = kanban_db.get_task(conn, tid)
        if task is None:
            raise TeamError(
                E_TEAM_NOT_FOUND,
                f"no card {tid!r} on board {t.board!r}",
                hint="Handoff completes a card on YOUR team's board.",
                remedy="team_status   # open cards with ids",
            )
        if not kanban_db.complete_task(conn, tid, summary=summary):
            raise TeamError(
                E_TEAM_INVALID,
                f"could not complete {tid!r} (unknown id or already terminal)",
                hint="A handoff completes the card first; it must still be open.",
                remedy="team_status   # confirm the card's status",
            )
    # The complete landed; the successor write below may still fail — say so
    # in the error rather than pretend nothing happened.
    title = str(args.get("title") or "").strip() or f"Follow up on: {task.title}"
    body = args.get("body") or summary
    try:
        new_id = _create_card(
            t, title=title, body=body, assignee=next_assignee,
            parents=(tid,), created_by=f"team_handoff:{_calling_profile() or 'unknown'}")
    except TeamError as exc:
        raise TeamError(
            exc.code,
            f"{exc.message} — the card WAS completed; create the successor manually",
            hint=exc.hint, remedy=exc.remedy) from exc
    return _env_ok(slug, {
        "completed": tid,
        "created": new_id,
        "parents": [tid],
        "assignee": next_assignee,
        "board": t.board,
    })


def _h_sync(args: dict) -> dict:
    """Diff only. This function has no import of, and no path to,
    ``reconcile.apply`` — say so loudly so the model does not loop trying."""
    slug, t = _resolve_team(args)
    from . import reconcile
    drift = reconcile.diff(t)
    items = [d.to_dict() for d in drift]
    return _env_ok(slug, {
        "dry_run": True,
        "nothing_written": True,
        "drift": items,
        "drift_count": len(items),
        "fix": f"hermes team sync {t.slug}",
        "fix_writes": True,
    })


# ── attach a file that already exists on disk ────────────────────────────────

# Raw bytes crossing a container transport are base64-framed one chunk at a
# time: a whole 25 MB file would be one ~34 MB stdout string, and a partial
# read must never become a truncated attachment.
_ATTACH_PATH_CHUNK_BYTES = 4 * 1024 * 1024


def _attach_reject(message: str, *, hint: str, remedy: str):
    raise TeamError(E_TEAM_INVALID, f"team_attach_path: {message}",
                    hint=hint, remedy=remedy)


def _attach_target_card(args: Mapping, t) -> str:
    """The card to attach to, honouring worker scoping.

    A dispatcher-spawned worker (``HERMES_KANBAN_TASK`` set) may only attach to
    its own card — a prompt-injected ``task_id`` must not reach a sibling run
    (the substrate enforces the same rule for ``kanban_attach``). An
    orchestrator has no env card, so the id is confined to the team's own
    tenant instead.
    """
    env_tid = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    tid = str((args or {}).get("task_id") or "").strip() or env_tid
    if not tid:
        raise TeamError(
            E_TEAM_INVALID, "task_id is required (no HERMES_KANBAN_TASK in the env)",
            hint="Pass the card the file belongs to.",
            remedy="team_status   # open cards with ids",
        )
    if env_tid and tid != env_tid:
        raise TeamError(
            E_TEAM_INVALID,
            f"this worker is scoped to card {env_tid}; refusing to attach to {tid}",
            hint="Workers may only attach to their own card.",
            remedy="kanban_comment   # to hand information to another card",
        )
    if not env_tid and tid not in {r.id for r in _list_team_tasks(t)}:
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"no card {tid!r} under your team's tenant on board {t.board!r}",
            hint="team_attach_path only touches cards on YOUR team's board.",
            remedy="team_status   # open cards with ids",
        )
    return tid


def _resolve_attach_path(path_arg: str, session_task_id: str) -> str:
    """Absolute path for *path_arg* in the backend's own namespace, confined to
    the task workspace. Resolution goes through the substrate's
    ``file_tools_paths`` so the path the model sees in read_file/terminal is the
    path that works here (container namespaces included); an unknown workspace
    root is not treated as permission to walk out of one."""
    from pathlib import Path
    from tools import path_security
    from tools.file_tools_paths import (
        _authoritative_workspace_root, _expand_tilde, _normalize_without_host_deref,
        _resolve_path_for_task, _uses_container_paths)
    remedy = "team_attach_path with a path inside your task workspace"
    if not path_arg:
        _attach_reject("path is required",
                       hint="Pass the same path you would give read_file.",
                       remedy=remedy)
    if path_security.has_traversal_component(_expand_tilde(path_arg)):
        _attach_reject(f"path {path_arg!r} contains a '..' component",
                       hint="Pass a direct path, without '..' segments.",
                       remedy=remedy)
    resolved = _resolve_path_for_task(path_arg, session_task_id)
    root = _authoritative_workspace_root(session_task_id)
    if not root:
        return str(resolved)
    outside = f"path {path_arg!r} resolves outside the task workspace {root!r}"
    if _uses_container_paths(session_task_id):
        # Container paths are meaningful only inside the sandbox: normalize the
        # syntax, never deref a host symlink that happens to share the name.
        if not _normalize_without_host_deref(resolved).is_relative_to(
                _normalize_without_host_deref(_expand_tilde(root))):
            _attach_reject(outside, hint="Copy the file into the workspace first.",
                           remedy=remedy)
    elif path_security.validate_within_dir(Path(str(resolved)), Path(_expand_tilde(root))):
        _attach_reject(outside, hint="Copy the file into the workspace first.",
                       remedy=remedy)
    return str(resolved)


def _check_attach_size(size: int, resolved: str, max_bytes: int) -> None:
    if size > max_bytes:
        _attach_reject(
            f"{resolved} is {size} bytes, over the {max_bytes} byte attachment limit",
            hint=f"The cap is {max_bytes // (1024 * 1024)} MB, shared with the dashboard.",
            remedy="Attach a smaller file, or split it.")


def _read_attach_path_bytes(resolved: str, session_task_id: str, max_bytes: int) -> bytes:
    """The file's bytes, read through the same backend ``read_file`` uses.

    Host backends read directly; a container backend is asked for base64 chunks
    so the bytes survive a transport that decodes stdout with
    ``errors="replace"``. Size is checked before the read, never after, so an
    oversize file costs a stat.
    """
    from tools.file_tools import _file_ops_uses_host_paths, _get_file_ops
    hint = "The file must exist and be readable by the task's backend."
    remedy = "read_file <path>   # confirm the backend can see it"
    file_ops = _get_file_ops(session_task_id)
    if _file_ops_uses_host_paths(file_ops):
        import stat as _stat
        try:
            st = os.stat(resolved)
        except FileNotFoundError:
            _attach_reject(f"no file at {resolved}", hint=hint, remedy=remedy)
        except OSError as exc:
            _attach_reject(f"cannot stat {resolved}: {exc}", hint=hint, remedy=remedy)
        if not _stat.S_ISREG(st.st_mode):
            _attach_reject(
                f"{resolved} is not a regular file (directory, FIFO, socket or device)",
                hint=hint, remedy=remedy)
        _check_attach_size(st.st_size, resolved, max_bytes)
        with open(resolved, "rb") as fh:
            return fh.read()

    size, status = file_ops._probe_regular_file(resolved)
    if status == "missing":
        _attach_reject(f"no file at {resolved}", hint=hint, remedy=remedy)
    if status == "not_regular":
        _attach_reject(
            f"{resolved} is not a regular file (directory, FIFO, socket or device)",
            hint=hint, remedy=remedy)
    if status != "ok":
        _attach_reject(
            f"could not stat {resolved} on the task's terminal backend ({status})",
            hint=hint, remedy=remedy)
    _check_attach_size(size, resolved, max_bytes)
    arg = file_ops._escape_shell_arg(resolved)
    out = bytearray()
    while len(out) < size:
        want = min(_ATTACH_PATH_CHUNK_BYTES, size - len(out))
        result = file_ops._exec(
            f"tail -c +{len(out) + 1} {arg} | head -c {want} | base64", timeout=300)
        if result.exit_code != 0:
            _attach_reject(
                f"could not read {resolved} from the task's terminal backend",
                hint=hint, remedy=remedy)
        chunk = file_ops._decode_base64_sample(result.stdout)
        if not chunk:
            # A short or unframed chunk means the transport mangled the bytes;
            # a partial attachment is worse than none.
            _attach_reject(
                f"could not read {resolved} intact over the terminal transport "
                f"(stopped after {len(out)} of {size} bytes)",
                hint=hint, remedy=remedy)
        out += chunk
    return bytes(out[:size])


def _h_attach_path(args: dict, kw: Mapping) -> dict:
    """Attach a file that already exists on disk — no base64 by hand, no detour
    through a public URL (both of which agents resort to without this)."""
    import mimetypes
    from pathlib import PurePath
    from hermes_cli import kanban_db, kanban_db_connect
    slug, t = _resolve_team(args)
    tid = _attach_target_card(args, t)
    # The kanban card id is NOT the terminal/file-tools key: that one arrives in
    # the dispatch kwargs and is what read_file resolves and reads against.
    session_task_id = str(kw.get("task_id") or "") or "default"
    resolved = _resolve_attach_path(str(args.get("path") or "").strip(), session_task_id)
    data = _read_attach_path_bytes(
        resolved, session_task_id, kanban_db.KANBAN_ATTACHMENT_MAX_BYTES)
    filename = (str(args.get("filename") or "").strip()
                or PurePath(resolved).name or "attachment")
    content_type = args.get("content_type") or mimetypes.guess_type(filename)[0]
    db_path = kanban_db.kanban_db_path(t.board)
    if not db_path.is_file():
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"board {t.board!r} has no database yet",
            hint="There is no card to attach to on a board with no cards.",
            remedy=f"hermes team sync {t.slug}   # create/repair the board first",
        )
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        att_id = kanban_db.store_attachment_bytes(
            conn, tid, filename, data, content_type=content_type,
            uploaded_by="agent", board=t.board)
    return _env_ok(slug, {"task_id": tid, "attachment_id": att_id,
                          "filename": filename, "size": len(data),
                          "content_type": content_type, "board": t.board})


# ── the table ────────────────────────────────────────────────────────────────

_HANDLERS = {
    "team_whoami": _h_whoami,
    "team_list": _h_list,
    "team_show": _h_show,
    "team_status": _h_status,
    "team_catalog": _h_catalog,
    "team_create_task": _h_create_task,
    "team_assign": _h_assign,
    "team_handoff": _h_handoff,
    "team_sync": _h_sync,
    "team_attach_path": _h_attach_path,
}

# Handlers that need the substrate's dispatch kwargs, not just ``args``
# (team_attach_path resolves paths against kw["task_id"], the terminal/
# file-tools session key — which is NOT the kanban card id).
_KW_HANDLERS = frozenset({"team_attach_path"})

_SCHEMA_BY_NAME = {s["name"]: s for s in schemas.ALL}


def _entry(name: str) -> tuple:
    handler = (_wrap_kw(_HANDLERS[name], name) if name in _KW_HANDLERS
               else _wrap(_HANDLERS[name], name))
    return (name, _SCHEMA_BY_NAME[name], handler, _EMOJI[name],
            _visible(name in _ORCHESTRATOR_ONLY))


#: ``(name, schema, handler, emoji, check_fn)`` for the ten tools, in §7.2
#: table order (team_attach_path last). ``handler(args: dict, **kw) ->
#: str`` matches the substrate convention (JSON envelope string out; nothing
#: raises out).
TOOL_TABLE = tuple(_entry(s["name"]) for s in schemas.ALL)


def register_tools(ctx) -> list:
    """Register the ten with a plugin context, mirroring how
    ``tools/kanban_tools.py`` registers against ``registry.register``.
    S15's ``register(ctx)`` calls this; it returns the registrations."""
    reg = []
    for name, schema, handler, emoji, check_fn in TOOL_TABLE:
        r = ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                              handler=handler, emoji=emoji, check_fn=check_fn)
        if r is not None:
            reg.append(r)
    return reg


# Back-compat with the storage module's helper name used above.
def _storage():
    from . import storage
    return storage
