"""The human-approval inbox: cards a team parked for an operator decision.

One module, three callers — ``cli.py`` (``hermes team approvals|approve|reject``),
``dashboard/plugin_api.py`` (the ``/approvals`` routes) and ``hooks.py`` (the
``kanban_task_blocked`` and ``kanban_task_completed`` observers). They share this core so the predicate, the
comment bodies and the refusals cannot drift between surfaces.

The card is the whole state. Five lanes, disjoint by construction, each with
the one verb that actually moves it:

=============== ========================================== ==================
lane            predicate                                   verb
=============== ========================================== ==================
``needs_input`` ``blocked``/``triage`` AND
                ``block_kind='needs_input'``                 approve / reject
``stuck``       ``blocked`` AND any other kind               release
``review``      ``review``                                   approve / reject
``triage``      ``triage`` AND any other kind                release
``guarded``     ``ready``, newest event
                ``respawn_guarded``                          release
=============== ========================================== ==================

``needs_input`` needs both halves: ``unblock_task`` deliberately keeps
``block_kind`` across an unblock (``kanban_db.py``, the comment inside
:func:`unblock_task`), so the kind alone would also match every card that was
*already* approved. It also covers ``triage``: an agent that parks on a
person a second time after an answer trips ``BLOCK_RECURRENCE_LIMIT``, and
that card is still waiting on the same kind of decision.

A card is *stuck* when it is blocked for any other reason: the dispatcher's
circuit breaker gave up on it (``kanban_db_dispatch._record_failure`` writes
``status='blocked'`` with no ``block_kind`` at all and fires no hook), or an
agent parked it on a ``capability``/``transient`` cause nobody has cleared.
``block_kind`` rides on every row so the two read apart: ``capability`` is
"fix the cause outside Hermes first", a missing kind is "just run it again".

The last three lanes are cards the substrate can park with nothing watching
them. ``review`` starves outright when ``kanban.review_dispatch`` is off — the
dispatcher then skips the whole lane, so an approved card sits there forever.
``triage`` is where ``_route_block`` sends a card at ``BLOCK_RECURRENCE_LIMIT``
for any cause other than a human decision.
``guarded`` is ``check_respawn_guard`` holding a ready row back over quota,
auth or a live PR; ``rate_limit_cooldown`` retries forever by design and never
blocks, so nothing else would ever show it.

Approve, reject and release are comment + one transition, and which transition
depends on the lane. On a blocked card they are ``unblock_task``, as before;
on a triaged one ``specify_triage_task``.
On a ``review`` card approve is :func:`complete_task` — the substrate's own
sanctioned path ("``review`` for human approval") and the only one that works
with ``review_dispatch`` off — and reject is ``reopen_review_task``. That is
the one place this module completes a card for the operator, and it is
deliberate: an operator approving a design HAS made the decision the card was
waiting for, and leaving it to an agent run costs a spawn that cannot happen.
Everywhere else the assignee still resumes, reads the decision and completes
the card itself.

Pause and resume live here too: they are operator verbs over the same board,
but act on a whole thread (every card under the root card) instead of one lane.

Substrate calls are lazy imports inside functions (spec §5.2), and only public
symbols are used (§5.2.1) — ``add_comment`` and ``unblock_task`` append their
own ``commented``/``unblocked`` events, so no private event writer is touched.
"""

from __future__ import annotations

from .errors import (
    TeamError,
    E_TEAM_BUSY,
    E_TEAM_INVALID,
    E_TEAM_NOT_FOUND,
    E_TEAM_NO_BOARD,
)
from . import storage

BLOCKED_STATUS = "blocked"
REVIEW_STATUS = "review"
TRIAGE_STATUS = "triage"
READY_STATUS = "ready"

NEEDS_INPUT = "needs_input"
STUCK = "stuck"
REVIEW = "review"
TRIAGE = "triage"
GUARDED = "guarded"

# Every lane of the inbox, in the order the operator reads them: decisions
# first, then the three lanes nothing in the substrate moves on its own.
QUEUE_KINDS = (NEEDS_INPUT, STUCK, REVIEW, TRIAGE, GUARDED)

# The card statuses each lane can live in — which rows a lane has to read.
LANE_STATUSES = {
    NEEDS_INPUT: (BLOCKED_STATUS, TRIAGE_STATUS),
    STUCK: (BLOCKED_STATUS,),
    REVIEW: (REVIEW_STATUS,),
    TRIAGE: (TRIAGE_STATUS,),
    GUARDED: (READY_STATUS,),
}

# A lane owes a human decision, or it does not. Both halves of this module's
# ordering read off this one table: the inbox sorts rank 0 first, and the
# priority floor is only ever written to a rank-0 card.
DECISION_LANES = (NEEDS_INPUT, REVIEW)
RELEASE_LANES = (STUCK, TRIAGE, GUARDED)
LANE_RANK = {k: (0 if k in DECISION_LANES else 1) for k in QUEUE_KINDS}

# The priority a decision card is raised to. The dispatcher orders every lane
# by ``priority DESC, created_at ASC`` (``kanban_db_dispatch._lane_rows``), so
# a floor above the backlog is what "the board takes this next" means — no
# card is ever paused to make room for it. 9 clears the untouched priority-0
# backlog and the tester's priority-5 defect convention.
DECISION_PRIORITY = 9

# The dispatcher's respawn guard writes this event and leaves the row where it
# is, so the newest event is the only evidence the card is being held back.
#
# Only the READY lane becomes ``guarded``. A guarded ``review`` row is already
# waiting on a person, and that decision is the thing that moves it — so it
# stays in the ``review`` lane and carries ``guard_reason`` as context.
GUARD_EVENT = "respawn_guarded"
_GUARDABLE_STATUSES = (READY_STATUS,)

APPROVE_PREFIX = "APPROVED"
REJECT_PREFIX = "CHANGES REQUESTED"
RELEASE_PREFIX = "RELEASED"
NOTE_PREFIX = "NOTE"

# A card the operator may still steer: not finished, not parked on anyone.
RUNNING_STATUS = "running"
DONE_STATUS = "done"
TODO_STATUS = "todo"
SCHEDULED_STATUS = "scheduled"

PAUSE_REASON = "paused by operator"
_PAUSABLE_STATUSES = (RUNNING_STATUS, READY_STATUS, TODO_STATUS)

_HEADLINE_MAX = 240
# Artifact preview cap, same idiom as the dashboard log tail.
ARTIFACT_TAIL_DEFAULT = 256 * 1024
ARTIFACT_TAIL_MAX = 2_000_000

_TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".text", ".log", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".py", ".js", ".ts", ".sh",
    ".sql", ".html", ".css", ".rst", ".diff", ".patch",
})


# ── team / connection plumbing ───────────────────────────────────────────────

def team_or_raise(slug: str, reg=None):
    """The team record for *slug*, or ``E_TEAM_NOT_FOUND`` with the known slugs."""
    reg = storage.load() if reg is None else reg
    t = reg.teams.get(slug)
    if t is None:
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"no team {slug!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    return t


def tenant_of(t) -> str:
    """The team's tenant label, falling back to the ``tenant = <slug>`` convention."""
    return t.tenant or t.slug


def _conn_ctx(t):
    """Context manager over the team's board DB, or None when it has no DB file
    yet. Delegates to :func:`cli._board_tasks_conn` so a reader never mints a
    board (§5.2.1)."""
    from . import cli
    if not (t.board or "").strip():
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"team {t.slug!r} has no board",
            hint="A team needs a board before it can hold cards.",
            remedy=f"hermes team set-board {t.slug} <board>",
        )
    return cli._board_tasks_conn(t.board)


def is_pending(task) -> bool:
    """The inbox predicate: parked on a human. ``triage`` counts too: a second
    ``needs_input`` block after an answer trips the substrate's
    ``BLOCK_RECURRENCE_LIMIT`` and lands there, still waiting on a person."""
    return (getattr(task, "status", None) in (BLOCKED_STATUS, TRIAGE_STATUS)
            and (getattr(task, "block_kind", None) or "") == NEEDS_INPUT)


def is_stuck(task) -> bool:
    """The second lane: blocked, but not on a human. The dispatcher's give-up
    leaves ``block_kind`` NULL; ``capability``/``transient`` blocks are an agent
    parking work it cannot finish. Disjoint from :func:`is_pending` by
    construction, so a card is never in both lanes."""
    return (getattr(task, "status", None) == BLOCKED_STATUS
            and (getattr(task, "block_kind", None) or "") != NEEDS_INPUT)


def kind_of(task, newest_event=None):
    """The card's lane, or None when it is not parked at all.

    *newest_event* is the kind of the card's newest event of any kind. It is
    what separates ``guarded`` from ``review``: the guard leaves the row in
    place, so a later ``claimed``/``spawned`` event is the proof the hold was
    released. Without it a ready card can never read as guarded, which is the
    safe direction — the lane simply stays hidden.
    """
    if is_pending(task):
        return NEEDS_INPUT
    if is_stuck(task):
        return STUCK
    status = getattr(task, "status", None)
    if newest_event == GUARD_EVENT and status in _GUARDABLE_STATUSES:
        return GUARDED
    if status == REVIEW_STATUS:
        return REVIEW
    if status == TRIAGE_STATUS:
        return TRIAGE
    return None


# ── row rendering ────────────────────────────────────────────────────────────

def _headline(comments) -> str:
    """First non-empty line of the newest comment — what the operator reads first."""
    for c in reversed(list(comments or [])):
        for line in (c.body or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:_HEADLINE_MAX]
    return ""


def sort_key(row) -> tuple:
    """The inbox order: the lanes that owe a human decision first, then the
    newest wait first inside each rank.

    Every surface sorts through this — a card raised a minute ago is the one
    the board is waiting on, and a ``stuck`` row parked since yesterday must
    not bury it. Ordering only: nothing here pauses a card.
    """
    return (LANE_RANK.get(row["reason_kind"], 1),
            -(row["blocked_at"] or row["created_at"] or 0),
            row["id"])


def _attachment_view(att) -> dict:
    return {"id": att.id, "filename": att.filename, "size": int(att.size or 0),
            "content_type": att.content_type, "created_at": att.created_at,
            "previewable": _is_text(att.filename, att.content_type)}


# The events that park a card, one per lane. Newest wins, so a card that was
# blocked long ago and is now waiting in review reads its review request.
_WAIT_EVENTS = ("blocked", "gave_up", "block_loop_detected", GUARD_EVENT,
                "review_requested")


def _park_facts(conn, task_id) -> tuple:
    """``(reason, parked_at, newest_event)``.

    One events read serving both questions the row needs. The first two come
    off the newest event in :data:`_WAIT_EVENTS`: ``blocked`` carries
    ``reason``; the dispatcher's ``gave_up`` carries ``error`` instead, which
    is the only text a stuck card has to explain itself; ``respawn_guarded``
    carries the guard reason. The third is the newest event of ANY kind, which
    is what :func:`kind_of` needs to tell a held card from a moving one.
    """
    from hermes_cli import kanban_db
    events = kanban_db.list_events(conn, task_id)
    newest_event = events[-1].kind if events else None
    for ev in reversed(events):
        if ev.kind in _WAIT_EVENTS:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            return (payload.get("reason") or payload.get("error"),
                    ev.created_at, newest_event)
    return (None, None, newest_event)


def _row(t, conn, task, *, now: int, facts=None) -> dict:
    from hermes_cli import kanban_db
    reason, blocked_at, newest_event = (
        facts if facts is not None else _park_facts(conn, task.id))
    waiting_since = blocked_at or task.created_at
    kind = kind_of(task, newest_event)
    comments = kanban_db.list_comments(conn, task.id)
    attachments = [_attachment_view(a)
                   for a in kanban_db.list_attachments(conn, task.id)]
    row = {
        "id": task.id,
        "team": t.slug,
        "board": t.board,
        "assignee": task.assignee,
        "title": task.title,
        "priority": task.priority,
        "created_at": task.created_at,
        "blocked_at": blocked_at,
        "age_seconds": max(0, int(now) - int(waiting_since or now)),
        "block_reason": reason,
        "block_recurrences": int(getattr(task, "block_recurrences", 0) or 0),
        "headline": _headline(comments),
        "attachments": attachments,
        "reason_kind": kind,
        "status": task.status,
        # ``capability`` means "fix the cause outside Hermes first"; a missing
        # kind means the dispatcher gave up and a retry is the whole remedy.
        # The UI cannot tell them apart without it, so every row carries it.
        "block_kind": task.block_kind,
    }
    if kind in (STUCK, GUARDED):
        # What the dispatcher last saw: the failure text and how many runs it
        # cost. Only these rows have them, so the needs_input payload is
        # unchanged.
        row["failure_error"] = getattr(task, "last_failure_error", None)
        row["consecutive_failures"] = int(
            getattr(task, "consecutive_failures", 0) or 0)
    if kind in DECISION_LANES:
        # The card's revision, so a surface knows when a cached summary of it
        # went stale without reading the summary itself.
        from . import summarize
        row["summary_key"] = summarize.fingerprint(
            task.id, task.status, [c.id for c in comments],
            [a["id"] for a in attachments])
    if newest_event == GUARD_EVENT:
        # ``blocker_auth`` / ``rate_limit_cooldown`` / ``recent_success`` /
        # ``active_pr`` — ``check_respawn_guard``'s own vocabulary, passed
        # through unchanged so the row and ``hermes kanban tail`` agree. Set on
        # a review row too: the guard is context there, not the lane.
        row["guard_reason"] = reason
    return row


# ── reads ────────────────────────────────────────────────────────────────────

def _queue_rows(t, kinds) -> list[dict]:
    """The parked cards of *t* in the requested lanes, newest wait first.

    One ``list_tasks`` query per card status the requested lanes can live in
    — at most four, and at most two when the caller asks for a single lane.
    The lane itself is then decided per row, so the statuses two lanes share
    (``blocked`` for needs_input/stuck, ``triage`` for needs_input/triage) are
    still read once.
    """
    import time
    from hermes_cli import kanban_db
    want = frozenset(k for k in kinds if k in QUEUE_KINDS)
    if not want:
        return []
    ctx = _conn_ctx(t)
    if ctx is None:
        return []
    statuses = sorted({st for k in want for st in LANE_STATUSES[k]})
    now = int(time.time())
    rows: list[dict] = []
    with ctx as conn:
        for status in statuses:
            for task in kanban_db.list_tasks(conn, tenant=tenant_of(t),
                                             status=status):
                facts = _park_facts(conn, task.id)
                if kind_of(task, facts[2]) in want:
                    rows.append(_row(t, conn, task, now=now, facts=facts))
    rows.sort(key=sort_key)
    return rows


def _all_rows(reg, kinds) -> list[dict]:
    """The registry-wide queue — every team, newest wait first.

    One unreadable board never hides the rest of the queue: a team that raises
    is skipped, which is the same fail-soft rule the status read path uses.
    """
    rows: list[dict] = []
    for slug in sorted(reg.teams):
        try:
            rows.extend(_queue_rows(reg.teams[slug], kinds))
        except (TeamError, OSError):
            continue
    rows.sort(key=sort_key)
    return rows


def pending(t) -> list[dict]:
    """Every card of *t* waiting on a human, newest wait first."""
    return _queue_rows(t, (NEEDS_INPUT,))


def stuck(t) -> list[dict]:
    """Every card of *t* blocked for something other than a human decision."""
    return _queue_rows(t, (STUCK,))


def lane(t, kind) -> list[dict]:
    """Every card of *t* in one lane — the generic form of :func:`pending` and
    :func:`stuck`, so a caller never re-derives a lane predicate."""
    return _queue_rows(t, (kind,))


def lane_counts(t) -> dict:
    """``{lane: n}`` over every lane, zeros included, fail-soft.

    The one seam the CLI, ``doctor`` and the dashboard count through, so
    "nothing is happening" reads the same number everywhere. An unreadable
    board counts as empty rather than raising — the same rule
    :func:`count_pending` follows.
    """
    out = dict.fromkeys(QUEUE_KINDS, 0)
    try:
        rows = _queue_rows(t, default_kinds())
    except (TeamError, OSError):
        return out
    for r in rows:
        if r["reason_kind"] in out:
            out[r["reason_kind"]] += 1
    return out


LEGACY_KINDS = (NEEDS_INPUT, STUCK)


def default_kinds() -> tuple:
    """The lanes the inbox shows when the caller asks for all of them.

    ``plugins.entries.hermes-agent-synergia.settings.parked_lanes`` (true by
    default) is the off switch for the three lanes that read cards the
    substrate parked without asking anyone: false restores the two blocked
    lanes and nothing else.
    """
    from . import cli
    value = cli._settings().get("parked_lanes", True)
    if isinstance(value, str):
        value = value.strip().lower() not in ("false", "off", "0", "no", "")
    return QUEUE_KINDS if value else LEGACY_KINDS


def queue(t, *, kinds=None) -> list[dict]:
    """Every lane merged — what the operator's inbox shows."""
    return _queue_rows(t, tuple(default_kinds() if kinds is None else kinds))


def pending_all(reg=None) -> list[dict]:
    """:func:`pending` across the registry."""
    return _all_rows(storage.load() if reg is None else reg, (NEEDS_INPUT,))


def queue_all(reg=None, *, kinds=None) -> list[dict]:
    """:func:`queue` across the registry."""
    return _all_rows(storage.load() if reg is None else reg,
                     tuple(default_kinds() if kinds is None else kinds))


def count_pending(t) -> int:
    try:
        return len(pending(t))
    except (TeamError, OSError):
        return 0


def count_stuck(t) -> int:
    try:
        return len(stuck(t))
    except (TeamError, OSError):
        return 0


def _task_or_raise(conn, t, task_id: str):
    from hermes_cli import kanban_db
    task = kanban_db.get_task(conn, task_id)
    if task is None or (task.tenant or "") != tenant_of(t):
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"no card {task_id!r} on team {t.slug!r}",
            hint=f"The card must exist on board {t.board!r} with "
                 f"tenant={tenant_of(t)!r}.",
            remedy=f"hermes team approvals {t.slug}",
        )
    return task


def detail(t, task_id: str) -> dict:
    """One waiting card in full: row, body, every comment, attachment list."""
    import time
    from hermes_cli import kanban_db
    ctx = _conn_ctx(t)
    if ctx is None:
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"board {t.board!r} has no cards yet",
            hint="The board DB is created on first write.",
            remedy=f"hermes team status {t.slug}",
        )
    now = int(time.time())
    with ctx as conn:
        task = _task_or_raise(conn, t, task_id)
        row = _row(t, conn, task, now=now)
        row["body"] = task.body
        row["awaiting_approval"] = is_pending(task)
        row["stuck"] = is_stuck(task)
        row["comments"] = [
            {"id": c.id, "author": c.author, "body": c.body,
             "created_at": c.created_at}
            for c in kanban_db.list_comments(conn, task.id)
        ]
    return row


SUMMARY_ARTIFACTS_MAX = 3
SUMMARY_ARTIFACT_BYTES = 24 * 1024


def summary_inputs(t, task_id: str) -> dict:
    """Everything a card says about itself, for :mod:`summarize`.

    :func:`detail` plus the two things only the substrate has: the newest run
    summary (how the implementer handed the work off — on a review card it is
    the closest thing to "what was built") and the text of the first few
    attachments, which is where a design document actually lives.
    """
    from hermes_cli import kanban_db
    card = detail(t, task_id)
    ctx = _conn_ctx(t)
    if ctx is not None:
        with ctx as conn:
            card["run_summary"] = kanban_db.latest_summary(conn, task_id)
    previewable = [a for a in card.get("attachments") or [] if a["previewable"]]
    files = []
    for att in previewable[:SUMMARY_ARTIFACTS_MAX]:
        try:
            blob = artifact(t, att["id"], max_bytes=SUMMARY_ARTIFACT_BYTES)
        except (TeamError, OSError):
            continue
        if blob.get("text"):
            files.append({"filename": blob["filename"], "text": blob["text"],
                          "truncated": bool(blob["truncated"])})
    card["artifact_texts"] = files
    return card


def task_attachments(t, task_ids) -> dict:
    """``{task_id: [attachment view, ...]}`` for the cards in *task_ids*.

    The chat thread's bulk form of the list :func:`detail` returns for one
    card, so a deliverable is readable where the work is read instead of only
    where it is approved. Views are :func:`_attachment_view` — the same shape
    and the same ``previewable`` predicate the approval rows carry, so the UI
    has one artifact chip, not two.

    Tolerant like :func:`resolve.task_comments`: a board with no DB yet, or one
    that cannot be read, is ``{}`` and is never minted. Each card's tenant is
    re-checked here even though the caller filtered by tenant — the ids arrive
    from a URL, and the attachment route makes the same check for the same
    reason."""
    from hermes_cli import kanban_db
    ids = [x for x in dict.fromkeys(task_ids or ()) if x]
    if not ids:
        return {}
    ctx = _conn_ctx(t)
    if ctx is None:
        return {}
    tenant = tenant_of(t)
    out: dict = {}
    try:
        with ctx as conn:
            for tid in ids:
                task = kanban_db.get_task(conn, tid)
                if task is None or (task.tenant or "") != tenant:
                    continue
                out[tid] = [_attachment_view(a)
                            for a in kanban_db.list_attachments(conn, tid)]
    except Exception:  # noqa: BLE001 — an unreadable board reads as no files
        return {}
    return out


def _is_text(filename, content_type) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("text/"):
        return True
    if ctype in ("application/json", "application/x-yaml", "application/yaml",
                 "application/xml", "application/javascript"):
        return True
    if ctype and not ctype.startswith("application/octet-stream"):
        return False
    name = (filename or "").lower()
    return any(name.endswith(suffix) for suffix in _TEXT_SUFFIXES)


def artifact(t, attachment_id: int, *, max_bytes: int = ARTIFACT_TAIL_DEFAULT) -> dict:
    """One attachment, tenant-checked and text-only.

    The tenant check is the whole point of the route: the attachment id is
    global to the board, so it is resolved to its task and that task's tenant
    must be the team's, or one team's URL space could read another's blob.
    """
    from pathlib import Path
    from hermes_cli import kanban_db
    max_bytes = max(1, min(int(max_bytes or ARTIFACT_TAIL_DEFAULT), ARTIFACT_TAIL_MAX))
    ctx = _conn_ctx(t)
    if ctx is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"board {t.board!r} has no cards yet",
            hint="The board DB is created on first write.",
            remedy=f"hermes team status {t.slug}")
    with ctx as conn:
        att = kanban_db.get_attachment(conn, int(attachment_id))
        if att is None:
            raise TeamError(
                E_TEAM_NOT_FOUND, f"no attachment {attachment_id!r}",
                hint="Attachment ids come from the card's attachment list.",
                remedy=f"hermes team approvals {t.slug} --show <card>")
        _task_or_raise(conn, t, att.task_id)
    out = _attachment_view(att)
    out["task_id"] = att.task_id
    out["truncated"] = False
    out["text"] = None
    if not out["previewable"]:
        out["note"] = "binary attachment — not previewable; read it from disk"
        out["stored_path"] = att.stored_path
        return out
    path = Path(att.stored_path)
    if not path.is_file():
        out["note"] = "attachment row present but its blob is missing on disk"
        out["stored_path"] = att.stored_path
        return out
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        out["truncated"] = True
    out["text"] = raw.decode("utf-8", errors="replace")
    return out


# ── writes ───────────────────────────────────────────────────────────────────

def operator_author() -> str:
    """``operator:<profile>`` — an agent reading the card can tell a human
    decision from an agent comment."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = (get_active_profile_name() or "").strip()
    except Exception:
        profile = ""
    return f"operator:{profile}" if profile else "operator"


def decision_priority() -> int:
    """``plugins.entries.hermes-agent-synergia.settings.decision_priority``
    (:data:`DECISION_PRIORITY` by default, ``0`` to leave priorities alone)."""
    from . import cli
    try:
        floor = int(cli._settings().get("decision_priority", DECISION_PRIORITY))
    except (TypeError, ValueError):
        return DECISION_PRIORITY
    return max(0, floor)


def _raise_priority(conn, task_id: str, *, floor=None):
    """Raise one card to the decision floor; return its priority, or None when
    the floor is off or the card is gone.

    ``tasks.priority`` is accepted by ``create_task`` and by nothing else —
    there is no public setter to call (§5.2.1), so this is a single raw
    statement, the same documented deviation as ``cli._backfill_tenant``.
    ``max`` is what makes it safe to run twice, and safe to run on a card an
    operator already made hotter by hand: it never lowers a priority.
    """
    floor = decision_priority() if floor is None else int(floor)
    if floor <= 0:
        return None
    conn.execute(
        "update tasks set priority = max(coalesce(priority, 0), ?) where id = ?",
        (floor, task_id))
    row = conn.execute("select priority from tasks where id = ?",
                       (task_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def raise_lane_priority(board: str, *, reg=None, floor=None) -> int:
    """Raise every card on *board* that is parked in a decision lane, and
    return how many rows were written.

    This is the entry side of the floor. ``unblock_task``/``block_task`` fire
    hooks, but ``request_review`` fires none, so a card entering ``review``
    is invisible — no observer can catch it one card at a time. A set-based
    UPDATE over the decision statuses catches every entry instead,
    whatever moved the card there, and ``priority < floor`` makes it a no-op
    on a board that is already ordered.

    Scoped to the tenants of the teams that own *board*: a card on a board
    Hermes dispatches for something other than a team is not this plugin's to
    reorder.
    """
    from hermes_cli import kanban_db, kanban_db_connect
    floor = decision_priority() if floor is None else int(floor)
    if floor <= 0 or not (board or "").strip():
        return 0
    reg = storage.load() if reg is None else reg
    tenants = sorted({tenant_of(t) for t in reg.teams.values()
                      if (t.board or "") == board})
    if not tenants:
        return 0
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return 0
    # ``is_pending`` in SQL: the two blocked statuses are shared with the
    # stuck and triage lanes, so the kind has to come with them or a rank-1
    # card is raised too.
    parked = sorted(LANE_STATUSES[NEEDS_INPUT])
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        cur = conn.execute(
            "update tasks set priority = ?"
            f" where tenant in ({','.join('?' * len(tenants))})"
            " and coalesce(priority, 0) < ?"
            "  and (status = ?"
            f"      or (status in ({','.join('?' * len(parked))})"
            "          and block_kind = ?))",
            (floor, *tenants, floor, REVIEW_STATUS, *parked, NEEDS_INPUT))
        return int(cur.rowcount or 0)


def _stuck_cause(task) -> str:
    """What parked a stuck card: the agent's own block kind, or — when the
    dispatcher's circuit breaker did it with a raw UPDATE — no kind at all."""
    return getattr(task, "block_kind", None) or "the dispatcher gave up"


# The verb that moves each lane, and why. A refusal quotes both, because a
# refusal that only says "no" leaves the operator where the Release button did:
# looking at a card nothing will ever pick up.
LANE_VERB = {
    NEEDS_INPUT: "approve",
    REVIEW: "approve",
    STUCK: "release",
    TRIAGE: "release",
    GUARDED: "release",
}

LANE_HINT = {
    NEEDS_INPUT: "The agent asked a person and is waiting on the answer. "
                 "Approve or request changes so the decision reaches it as a "
                 "comment.",
    REVIEW: "The work is finished and waiting for a human verdict. Approving "
            "completes the card and promotes its children; requesting changes "
            "sends it back to the implementer.",
    STUCK: "Nothing in the substrate moves a blocked card on its own. "
           "Releasing it returns it to the pool with a fresh retry budget.",
    TRIAGE: "Repeated blocks for the same cause routed this card to triage. "
            "Releasing it specifies it back into the queue.",
    GUARDED: "The dispatcher is refusing to respawn this card. Fix the cause "
             "outside Hermes first — releasing it clears the stale failure "
             "the guard reads.",
}


def _lane_phrase(task, lane, reason=None) -> str:
    """How a refusal names the card's current lane."""
    if lane == NEEDS_INPUT:
        return "waiting on a human decision"
    if lane == REVIEW:
        return "waiting in review for a human decision"
    if lane == STUCK:
        return f"stuck ({_stuck_cause(task)})"
    if lane == TRIAGE:
        return "in triage after repeated blocks"
    if lane == GUARDED:
        return f"held back by the dispatcher ({reason or 'respawn guard'})"
    return (f"{task.status!r}"
            + (f"/{task.block_kind!r}" if task.block_kind else ""))


def _wrong_lane(t, task, lane, reason, *, action: str, kind=None) -> TeamError:
    """A refusal naming the verb that DOES move this card.

    *kind* is the lane this verb serves, so a card in no lane at all still
    sends the operator somewhere useful — the queue of the cards the verb
    would work on — instead of the whole inbox.
    """
    verb = LANE_VERB.get(lane)
    if verb:
        return TeamError(
            E_TEAM_INVALID,
            f"card {task.id} is {_lane_phrase(task, lane, reason)} — "
            f"{action} does not move it",
            hint=LANE_HINT[lane],
            remedy=f'hermes team {verb} {task.id} --note "…"',
        )
    return TeamError(
        E_TEAM_INVALID,
        f"card {task.id} is {_lane_phrase(task, lane, reason)} — "
        f"nothing is parked for {action}",
        hint="Only a parked card carries a decision: " + ", ".join(QUEUE_KINDS)
             + ".",
        remedy=f"hermes team approvals {t.slug}"
               + (f" --kind {kind}" if kind else ""),
    )


def _require_lane(allowed, action: str):
    """A ``require`` for :func:`_decide` that admits exactly *allowed*."""
    def require(t, task, lane, reason) -> None:
        if lane not in allowed:
            raise _wrong_lane(t, task, lane, reason, action=action,
                              kind=allowed[0])
    return require


_require_pending = _require_lane(DECISION_LANES, "approve")
_require_stuck = _require_lane(RELEASE_LANES, "release")


def _require_open(t, task, lane, reason) -> None:
    """A card a note can still reach: not finished, not parked on a decision.

    A parked card is refused on purpose. The comment would land, the card
    would stay parked, and the operator would read the thread as answered —
    so each lane is pointed at the verb that actually moves it.
    """
    if lane is not None:
        raise TeamError(
            E_TEAM_INVALID,
            f"card {task.id} is {_lane_phrase(task, lane, reason)}, "
            "not running",
            hint="A note leaves the card where it is. " + LANE_HINT[lane],
            remedy=f'hermes team {LANE_VERB[lane]} {task.id} --note "…"',
        )
    if task.status == DONE_STATUS:
        raise TeamError(
            E_TEAM_INVALID,
            f"card {task.id} is done — nothing is running to steer",
            hint="A finished card never runs again, so a note on it reaches "
                 "nobody. Start a new card for the follow-up work.",
            remedy=f"hermes team status {t.slug}",
        )


_TEXT_FLAG = {"reject": "reason", "note": "text"}
_TEXT_HINT = {
    "note": "Say what the agent should take into account; it is steered into "
            "the run while it works.",
}
_DEFAULT_TEXT_HINT = "Say what you decided — the agent resumes by reading it."


def _require_text(action: str, task_id: str, text) -> str:
    """The decision text, or a refusal. An empty note is the one input that
    would leave the agent resuming with nothing to act on."""
    text = (text or "").strip()
    if not text:
        raise TeamError(
            E_TEAM_INVALID,
            f"{action} needs text",
            hint=_TEXT_HINT.get(action, _DEFAULT_TEXT_HINT),
            remedy=f"hermes team {action} {task_id} "
                   f"--{_TEXT_FLAG.get(action, 'note')} \"…\"",
        )
    return text


# ── lane transitions ─────────────────────────────────────────────────────────
# One callable per (verb, lane) pair, each a single public substrate call.
# They return the transition's name for the envelope, or None when the card
# moved out from under the decision between the read and the write.

def _move_unblock(conn, t, task, text):
    """The blocked lanes: ``unblock_task`` resets ``consecutive_failures`` and
    ``last_failure_error``, so the card gets its full retry budget back
    instead of one run before the circuit breaker trips."""
    from hermes_cli import kanban_db
    return "unblocked" if kanban_db.unblock_task(conn, task.id) else None


def _move_complete(conn, t, task, text):
    """The ``review`` lane's approve: ``review -> done``.

    ``complete_task``'s own docstring names this path ("``review`` for human
    approval"). It is also the only one that works with
    ``kanban.review_dispatch`` off, where nothing ever claims a review row —
    an unblock would put the card straight back into a lane no dispatcher
    reads. Completing promotes the children through the existing parent gate.
    """
    from hermes_cli import kanban_db
    run_id = getattr(task, "current_run_id", None)
    try:
        done = kanban_db.complete_task(
            conn, task.id, result=text,
            expected_run_id=int(run_id) if run_id else None)
    except kanban_db.LiveClaimError as exc:
        raise TeamError(
            E_TEAM_BUSY,
            f"card {task.id} is under a live claim: {exc}",
            hint="A card a worker still holds is completed by that worker, or "
                 "after the claim is reclaimed. Nothing is lost by waiting.",
            remedy=f"hermes team approvals {t.slug} --kind review",
        ) from exc
    if not done:
        raise TeamError(
            E_TEAM_INVALID,
            f"card {task.id} cannot complete yet — a parent card is not done",
            hint="complete_task re-gates on parents inside its own "
                 "transaction, so the card stays in review until every parent "
                 "is done. Your comment is already on the card.",
            remedy=f"hermes team status {t.slug}",
        )
    return "completed"


def _move_reopen(conn, t, task, text):
    """The ``review`` lane's request-changes: back to the implementer named in
    the ``review_requested`` event."""
    from hermes_cli import kanban_db
    return "reopened" if kanban_db.reopen_review_task(conn, task.id) else None


def _move_specify(conn, t, task, text):
    """The ``triage`` rows: ``triage -> todo``, where parent gating still
    applies. ``author=None`` keeps it to one comment — the decision comment is
    already written."""
    from hermes_cli import kanban_db
    return ("specified"
            if kanban_db.specify_triage_task(conn, task.id, author=None)
            else None)


def _move_resume(conn, t, task, text):
    """The ``needs_input`` lane. A triaged card is specified back to ``todo``,
    because ``unblock_task`` only moves ``blocked`` rows."""
    if getattr(task, "status", None) == TRIAGE_STATUS:
        return _move_specify(conn, t, task, text)
    return _move_unblock(conn, t, task, text)


def _move_clear_guard(conn, t, task, text):
    """The ``guarded`` lane: clear the stale failure the respawn guard reads.

    The card is ``ready``, not blocked, so ``unblock_task`` cannot reach it —
    and ``last_failure_error``, which ``blocker_auth`` matches against, is
    exactly what traps it. ``block_task`` followed by ``unblock_task`` is the
    only public pair that clears that column.

    ``block_task`` counts a recurrence, so a card released twice for the same
    cause routes itself to ``triage`` at ``BLOCK_RECURRENCE_LIMIT``. That is
    deliberate and now legible: ``triage`` is a lane the operator can see.
    """
    from hermes_cli import kanban_db
    if not kanban_db.block_task(conn, task.id, kind="transient",
                                reason=f"operator release: {text}"):
        return None
    return "cleared_guard" if kanban_db.unblock_task(conn, task.id) else "blocked"


_APPROVE_MOVES = {NEEDS_INPUT: _move_resume, REVIEW: _move_complete}
_REJECT_MOVES = {NEEDS_INPUT: _move_resume, REVIEW: _move_reopen}
_RELEASE_MOVES = {STUCK: _move_unblock, TRIAGE: _move_specify,
                  GUARDED: _move_clear_guard}


def _board_or_raise(t):
    ctx = _conn_ctx(t)
    if ctx is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"board {t.board!r} has no cards yet",
            hint="The board DB is created on first write.",
            remedy=f"hermes team status {t.slug}")
    return ctx


def _decide(t, task_id: str, *, body: str, text: str, author, action: str,
            require, moves=None) -> dict:
    from hermes_cli import kanban_db
    ctx = _board_or_raise(t)
    author = (author or "").strip() or operator_author()
    with ctx as conn:
        task = _task_or_raise(conn, t, task_id)
        reason, _parked_at, newest_event = _park_facts(conn, task.id)
        lane = kind_of(task, newest_event)
        require(t, task, lane, reason)
        comment_id = kanban_db.add_comment(conn, task_id, author, body)
        move = (moves or {}).get(lane)
        transition = move(conn, t, task, text) if move else None
        # A decided card is the one the board is now waiting on, so it goes
        # above the backlog rather than behind it. A completed card has no
        # work left to order.
        raised = (bool(_raise_priority(conn, task_id))
                  if transition and transition != "completed" else False)
        after = kanban_db.get_task(conn, task_id)
    return {
        "team": t.slug,
        "board": t.board,
        "task_id": task_id,
        "action": action,
        "lane": lane,
        "author": author,
        "comment_id": comment_id,
        "transition": transition,
        # Kept as the envelope's "did the card move" flag across every lane;
        # ``transition`` says which move it was.
        "unblocked": transition is not None,
        "status": getattr(after, "status", task.status),
        "assignee": getattr(after, "assignee", task.assignee),
        "title": task.title,
        "priority": getattr(after, "priority", task.priority),
        "priority_raised": raised,
    }


def approve(t, task_id: str, note: str, *, author=None) -> dict:
    """Record the decision and move the card on.

    ``needs_input`` returns to the pool (unblocked, or specified out of
    ``triage``) — the assignee resumes, acts on the note and completes the
    card itself. ``review`` is completed here: the
    operator has already made the decision the card was waiting for, and with
    ``review_dispatch`` off no agent run can ever make it again.
    """
    note = _require_text("approve", task_id, note)
    return _decide(t, task_id, body=f"{APPROVE_PREFIX}: {note}", text=note,
                   author=author, action="approve",
                   require=_require_pending, moves=_APPROVE_MOVES)


def reject(t, task_id: str, reason: str, *, author=None, park: bool = False) -> dict:
    """Record requested changes. ``needs_input`` returns to the pool,
    ``review`` reopens to the implementer; ``park=True`` records the comment
    and moves nothing."""
    reason = _require_text("reject", task_id, reason)
    return _decide(t, task_id, body=f"{REJECT_PREFIX}: {reason}", text=reason,
                   author=author, action="reject",
                   require=_require_pending,
                   moves=None if park else _REJECT_MOVES)


def release(t, task_id: str, note: str, *, author=None) -> dict:
    """Return a parked card to the pool with the operator's note attached.

    One verb over the three lanes no human decision is owed on: ``stuck``
    unblocks, ``triage`` specifies back to ``todo``, ``guarded`` clears the
    stale failure the dispatcher's respawn guard reads. The operator never
    completes the card here — release says "try again", not "this is done".
    """
    note = _require_text("release", task_id, note)
    return _decide(t, task_id, body=f"{RELEASE_PREFIX}: {note}", text=note,
                   author=author, action="release",
                   require=_require_stuck, moves=_RELEASE_MOVES)


def note(t, task_id: str, text: str, *, author=None) -> dict:
    """Steer a card that is already moving: comment, and nothing else.

    No unblock, no status change — the delivery path is the substrate's own
    live bridge. A worker polls its card for comments it has not seen
    (``tools/kanban_tools.inject_new_comments_from_env``, every ≥6s) and
    steers them into the running agent out-of-band, so an operator can talk
    to a running task without the block → comment → unblock detour. On a card
    that is not running yet the comment simply waits in the card's history,
    which its next run reads as context.

    ``delivery`` says which of the two happened, because the operator needs to
    know whether the agent hears this now or at the next run.
    """
    text = _require_text("note", task_id, text)
    res = _decide(t, task_id, body=f"{NOTE_PREFIX}: {text}", text=text,
                  author=author, action="note", require=_require_open)
    res["delivery"] = ("mid-run" if res["status"] == RUNNING_STATUS
                       else "next_run")
    return res


# ── pause / resume a thread ──────────────────────────────────────────────────

def _thread_ids(t, task_id: str) -> tuple:
    """``(root_id, [card ids])`` of the thread *task_id* belongs to."""
    from . import resolve
    for root_id, group in resolve.task_forest(t.board, tenant_of(t)).items():
        ids = [task.id for task in group["tasks"]]
        if task_id in ids:
            return root_id, ids
    return task_id, [task_id]


def _is_reviewer_run(conn, task_id: str) -> bool:
    from hermes_cli import kanban_db
    for event in reversed(kanban_db.list_events(conn, task_id)):
        if event.kind == "claimed":
            payload = event.payload if isinstance(event.payload, dict) else {}
            return payload.get("source_status") == REVIEW_STATUS
    return False


def pause(t, task_id: str, *, signal_fn=None) -> dict:
    """Park every running, ready and todo card of *task_id*'s thread in
    ``scheduled`` and kill the running workers; :func:`resume` re-queues them.

    ``schedule_task``, not ``block_task``: every block counts toward
    ``BLOCK_RECURRENCE_LIMIT``, so the second pause of one card would route it
    to triage. ``scheduled`` sits in no approvals lane, fires no blocked hook
    and nothing in the dispatcher promotes it. The worker is killed, not
    frozen — a frozen worker outlives its claim TTL and is reclaimed anyway —
    so a resumed card is a fresh run that keeps the workdir and the card's
    history, not the agent's context.

    A running reviewer run is skipped: ``unblock_task`` resumes a scheduled
    card to ``ready``, which would turn it into an implementation run.
    ``review`` cards cannot be scheduled at all, so with
    ``kanban.review_dispatch`` on one can still be claimed while paused.
    """
    from hermes_cli import kanban_db
    from . import resolve
    paused, skipped, kills = [], [], []
    with _board_or_raise(t) as conn:
        _task_or_raise(conn, t, task_id)
        root_id, ids = _thread_ids(t, task_id)
        for tid in ids:
            task = kanban_db.get_task(conn, tid)
            if task is None or task.status not in _PAUSABLE_STATUSES:
                continue
            if task.status == RUNNING_STATUS and _is_reviewer_run(conn, tid):
                skipped.append({"id": tid, "reason": "reviewer run"})
                continue
            pid, lock = task.worker_pid, task.claim_lock
            if kanban_db.schedule_task(conn, tid, reason=PAUSE_REASON):
                paused.append(tid)
                if pid:
                    kills.append((tid, pid, lock))
    if not paused:
        raise TeamError(
            E_TEAM_INVALID,
            f"nothing to pause in thread {root_id}",
            hint="Pause parks the running, ready and todo cards of a thread; "
                 "a reviewer run finishes on its own.",
            remedy=f"hermes team status {t.slug}",
        )
    return {
        "team": t.slug,
        "board": t.board,
        "task_id": task_id,
        "root_id": root_id,
        "action": "pause",
        "paused": paused,
        "skipped": skipped,
        "not_killed": resolve.terminate_workers(kills, signal_fn=signal_fn),
    }


def resume(t, task_id: str) -> dict:
    """Re-queue every ``scheduled`` card of *task_id*'s thread.
    ``unblock_task`` re-gates each on its parents, so a child whose parent is
    not done lands in ``todo``, not ``ready``."""
    from hermes_cli import kanban_db
    resumed, statuses = [], {}
    with _board_or_raise(t) as conn:
        _task_or_raise(conn, t, task_id)
        root_id, ids = _thread_ids(t, task_id)
        for tid in ids:
            task = kanban_db.get_task(conn, tid)
            if task is None or task.status != SCHEDULED_STATUS:
                continue
            if kanban_db.unblock_task(conn, tid):
                resumed.append(tid)
                statuses[tid] = kanban_db.get_task(conn, tid).status
    if not resumed:
        raise TeamError(
            E_TEAM_INVALID,
            f"nothing is paused in thread {root_id}",
            hint="Resume re-queues the cards a pause parked.",
            remedy=f"hermes team status {t.slug}",
        )
    return {
        "team": t.slug,
        "board": t.board,
        "task_id": task_id,
        "root_id": root_id,
        "action": "resume",
        "resumed": resumed,
        "statuses": statuses,
    }


# ── messenger push (the kanban_task_blocked / dispatch_tick observers) ───────

def configured_home_channels() -> list[dict]:
    """Every gateway platform with a home channel, from the live GatewayConfig.

    Copied from the kanban dashboard plugin rather than imported (§5.2.1: never
    import a private symbol from another plugin). Empty when no messaging
    platform is configured, which makes the observer a no-op.
    """
    try:
        from gateway.config import load_gateway_config
        gw_cfg = load_gateway_config()
    except Exception:
        return []
    homes = [
        {"platform": platform.value,
         "chat_id": pcfg.home_channel.chat_id,
         "thread_id": pcfg.home_channel.thread_id or "",
         "name": pcfg.home_channel.name or "Home"}
        for platform, pcfg in gw_cfg.platforms.items()
        if pcfg and pcfg.home_channel
    ]
    homes.sort(key=lambda h: h["platform"])
    return homes


def team_for_tenant(tenant, reg=None):
    """The team owning *tenant*, or None. Matches an explicit ``tenant`` label
    first, then the ``tenant = <slug>`` convention."""
    tenant = (tenant or "").strip()
    if not tenant:
        return None
    reg = storage.load() if reg is None else reg
    for slug in sorted(reg.teams):
        t = reg.teams[slug]
        if (t.tenant or "") == tenant:
            return t
    t = reg.teams.get(tenant)
    return t if t is not None and not (t.tenant or "") else None


def notify_home_channels(board: str, task_id: str) -> list[str]:
    """Subscribe *task_id* to every configured home channel; returns the
    platforms subscribed.

    Delivery is then the existing gateway notifier — ``blocked`` and
    ``unblocked`` are both already terminal kinds there — so the operator is
    pinged when the card starts waiting AND when it is released, with no
    delivery code here. With no home channel configured this is a no-op.
    """
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_notify
    homes = configured_home_channels()
    if not homes:
        return []
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return []
    author = operator_author()
    done: list[str] = []
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        for home in homes:
            kanban_db_notify.add_notify_sub(
                conn, task_id=task_id, platform=home["platform"],
                chat_id=home["chat_id"], thread_id=home["thread_id"] or None,
                notifier_profile=author)
            done.append(home["platform"])
    return done


def notify_enabled() -> bool:
    """``plugins.entries.hermes-agent-synergia.settings.approvals_notify`` (``home``
    default, ``off`` to stay silent)."""
    from . import cli
    return str(cli._settings().get("approvals_notify", "home")).strip().lower() != "off"


def on_blocked(task_id: str, board: str, reg=None) -> dict:
    """``kanban_task_blocked`` body: ping the operator when a card parks on a
    human. The hook payload carries no ``block_kind``, so the row is read here;
    the hook fires after the DB commit, so the read sees the blocked row."""
    from hermes_cli import kanban_db, kanban_db_connect
    result = {"task_id": task_id, "board": board, "needs_input": False,
              "team": None, "platforms": []}
    if not task_id or not board:
        return result
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return result
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        task = kanban_db.get_task(conn, task_id)
    if task is None or not is_pending(task):
        return result
    result["needs_input"] = True
    t = team_for_tenant(task.tenant, reg=reg)
    if t is None:
        return result
    result["team"] = t.slug
    # Ordering first, notification second: the floor must not depend on
    # whether a home channel is configured.
    result["priority_raised"] = raise_lane_priority(board, reg=reg)
    if not notify_enabled():
        result["notify"] = "off"
        return result
    result["platforms"] = notify_home_channels(board, task_id)
    return result


def on_auto_blocked(task_ids, board: str, reg=None) -> dict:
    """``on_kanban_dispatch_tick`` body: ping the operator for the cards the
    dispatcher's circuit breaker just blocked.

    The give-up path (``kanban_db_dispatch._record_failure``) writes
    ``status='blocked'`` with a raw UPDATE and fires no ``kanban_task_blocked``
    hook, so without this the whole stuck lane is silent. ``DispatchResult
    .auto_blocked`` carries the ids; each row is re-read so a card that has
    already moved on is skipped, and the push itself is the same subscription
    :func:`on_blocked` uses — the gateway notifier does the delivery.
    """
    from hermes_cli import kanban_db, kanban_db_connect
    ids = [str(i) for i in (task_ids or []) if i]
    result = {"board": board, "stuck": [], "notified": [], "platforms": []}
    if not ids or not board:
        return result
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return result
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        tasks = [kanban_db.get_task(conn, tid) for tid in ids]
    blocked = [task for task in tasks if task is not None and is_stuck(task)]
    result["stuck"] = [task.id for task in blocked]
    if not blocked:
        return result
    if not notify_enabled():
        result["notify"] = "off"
        return result
    reg = storage.load() if reg is None else reg
    platforms: set = set()
    for task in blocked:
        if team_for_tenant(task.tenant, reg=reg) is None:
            continue
        done = notify_home_channels(board, task.id)
        if done:
            result["notified"].append(task.id)
            platforms.update(done)
    result["platforms"] = sorted(platforms)
    return result



# ── auto-merge a finished thread ─────────────────────────────────────────────

AUTO_MERGE_AUTHOR = "team:auto-merge"


def auto_merge_enabled() -> bool:
    """``plugins.entries.hermes-agent-synergia.settings.auto_merge`` (true by
    default): false leaves every result branch for ``hermes team merge``."""
    from . import cli
    value = cli._settings().get("auto_merge", True)
    if isinstance(value, str):
        value = value.strip().lower() not in ("false", "off", "0", "no", "")
    return bool(value)


def merge_comment(res: dict) -> str:
    """The root-card line for a :func:`resolve.merge_result` that merged at
    least one head."""
    n = res["ahead"]
    body = (f"Merged `{res['branch']}` into `{res['base']}` "
            f"(`{res['to'][:12]}`, {n} commit{'' if n == 1 else 's'}). "
            f"Files: `{res['workdir']}`.")
    landed = [h for h in (res.get("heads") or []) if h["merged"]]
    if len(landed) > 1:
        body += " Heads: " + ", ".join(
            f"`{h['branch']}` ({h['card']})" for h in landed) + "."
    if res["others"]:
        body += " Not included: " + ", ".join(
            f"`{o['branch']}` ({o['card']})" for o in res["others"]) + "."
    if res["blocker"]:
        body += (f" Stopped: {res['blocker']['message']}. Finish by hand with "
                 f"`{res['blocker']['remedy']}`, then "
                 f"`hermes team merge {res['team']} {res['root']}` shows what "
                 "is left.")
    return body


def on_completed(task_id: str, board: str, reg=None) -> dict:
    """``kanban_task_completed`` body: once every card of a worktree team's
    thread is done, land every head of the thread on the workdir's branch
    (:func:`resolve.merge_result`) and say so on the root card.

    A conflict is left to a human: the heads before it stay merged and the
    comment names the branch and what to run. A refusal that a
    parallel completion already made moot — its merge landed the result —
    stays silent."""
    from hermes_cli import kanban_db, kanban_db_connect
    from . import resolve
    result = {"task_id": task_id, "board": board, "team": None, "root": None,
              "merged": False, "skipped": None}
    if not task_id or not board or not auto_merge_enabled():
        return result
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return result
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        task = kanban_db.get_task(conn, task_id)
    if task is None:
        return result
    t = team_for_tenant(task.tenant, reg=reg)
    if t is None or t.board != board or t.workspace_policy != "worktree":
        return result
    result["team"] = t.slug
    try:
        root_id, tasks = resolve.thread_of(t, task_id)
    except TeamError:
        return result
    result["root"] = root_id
    if any(x.status != "done" for x in tasks):
        return result
    try:
        res = resolve.merge_result(t, root_id, apply=True)
    except TeamError as exc:
        try:
            if not resolve.merge_result(t, root_id)["ahead"]:
                return result
        except TeamError:
            pass
        result["skipped"] = exc.message
        body = (f"Auto-merge skipped: {exc.message}. Fix: `{exc.remedy}` — "
                f"then `hermes team merge {t.slug} {root_id}` shows what is left.")
    else:
        if not res["merged"]:
            return result
        result.update(merged=True, branch=res["branch"], sha=res["to"])
        body = merge_comment(res)
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        kanban_db.add_comment(conn, root_id, AUTO_MERGE_AUTHOR, body)
    return result


# ── the review-round loop guard ──────────────────────────────────────────────

REVIEW_ROUND_EVENT = "review_requested"
REVIEW_ROUND_LIMIT_DEFAULT = 3


def review_round_limit() -> int:
    """``plugins.entries.hermes-agent-synergia.settings.review_round_limit``
    (3 by default, ``0`` disables the guard)."""
    from . import cli
    try:
        return max(0, int(cli._settings().get("review_round_limit",
                                              REVIEW_ROUND_LIMIT_DEFAULT)))
    except (TypeError, ValueError):
        return REVIEW_ROUND_LIMIT_DEFAULT


def review_rounds(conn, task_id: str) -> int:
    """How many times this card has been handed to a reviewer."""
    from hermes_cli import kanban_db
    return sum(1 for ev in kanban_db.list_events(conn, task_id)
               if ev.kind == REVIEW_ROUND_EVENT)


def check_review_loop(task_ids, board: str, *, limit=None) -> dict:
    """Park the cards that have gone round the review loop too many times.

    The substrate has no review-round cap: ``request_review`` and
    ``reopen_review_task`` can alternate forever, and the only thing that
    changes between rounds is the event log. A card self-reviewed by its own
    profile will do exactly that.

    Called from the dispatch-tick observer with the ids the tick just spawned,
    which is one check per spawn — a card is in ``DispatchResult.spawned``
    only on the tick it started, so each round costs one events read and
    nothing at all on an idle tick.

    Past the cap the card is blocked as ``needs_input``, which lands it in the
    lane the operator already reads and fires ``kanban_task_blocked``, so the
    existing :func:`on_blocked` push does the notifying. Blocking a running
    card closes its run; the substrate's own ``reap_terminal_workers`` ends the
    worker that outlives it. Like :func:`on_completed`, this writes without an
    operator verb, which is why it has an off switch.
    """
    from hermes_cli import kanban_db, kanban_db_connect
    ids = [str(i) for i in (task_ids or []) if i]
    limit = review_round_limit() if limit is None else max(0, int(limit))
    result = {"board": board, "limit": limit, "parked": [], "rounds": {}}
    if not ids or not board or limit <= 0:
        return result
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return result
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        for tid in ids:
            task = kanban_db.get_task(conn, tid)
            if task is None or task.status not in (RUNNING_STATUS,
                                                   READY_STATUS):
                continue
            rounds = review_rounds(conn, tid)
            if rounds < limit:
                continue
            result["rounds"][tid] = rounds
            reason = (f"review loop: {rounds} review rounds without a human "
                      f"decision (limit {limit})")
            if kanban_db.block_task(conn, tid, kind=NEEDS_INPUT,
                                    reason=reason):
                result["parked"].append(tid)
    return result


def suppression_line(result) -> str:
    """One line naming why a dispatch tick held ready work back, or ``""``.

    Straight through to ``kanban_db_dispatch.describe_suppression`` so the
    plugin's log and ``hermes kanban tail`` use the same words for the same
    guard, and a wording change upstream never has to be mirrored here.
    """
    try:
        from hermes_cli import kanban_db_dispatch
        return kanban_db_dispatch.describe_suppression([result])
    except Exception:  # noqa: BLE001 — a log line is never worth an exception
        return ""
