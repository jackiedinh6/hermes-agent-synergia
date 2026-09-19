"""The human-approval inbox: cards a team parked for an operator decision.

One module, three callers — ``cli.py`` (``hermes team approvals|approve|reject``),
``dashboard/plugin_api.py`` (the ``/approvals`` routes) and ``hooks.py`` (the
``kanban_task_blocked`` observer). They share this core so the predicate, the
comment bodies and the refusals cannot drift between surfaces.

The card is the whole state. A card waits on a human exactly when

    ``status == 'blocked'`` AND ``block_kind == 'needs_input'``

inside the team's tenant. Both halves are required: ``unblock_task``
deliberately keeps ``block_kind`` across an unblock (``kanban_db.py``, the
comment inside :func:`unblock_task`), so the kind alone would also match every
card that was *already* approved. The ``review`` lane cannot serve as the gate
— with ``kanban.review_dispatch`` on, the dispatcher hands ``review`` rows to an
agent reviewer; ``blocked/needs_input`` is the one lane it leaves alone.

A card is *stuck* when it is blocked for any other reason: the dispatcher's
circuit breaker gave up on it (``kanban_db_dispatch._record_failure`` writes
``status='blocked'`` with no ``block_kind`` at all and fires no hook), or an
agent parked it on a ``capability``/``transient`` cause nobody has cleared.
Nothing in the substrate moves such a card back on its own, so it gets its own
lane and its own verb — ``release`` — instead of widening the needs_input predicate.

Approve, reject and release are all comment + ``unblock_task``. The assignee
resumes, reads the decision and completes the card itself, which promotes the
children through the existing parent gate. This module never completes a card
for the operator.

Substrate calls are lazy imports inside functions (spec §5.2), and only public
symbols are used (§5.2.1) — ``add_comment`` and ``unblock_task`` append their
own ``commented``/``unblocked`` events, so no private event writer is touched.
"""

from __future__ import annotations

from .errors import (
    TeamError,
    E_TEAM_INVALID,
    E_TEAM_NOT_FOUND,
    E_TEAM_NO_BOARD,
)
from . import storage

BLOCKED_STATUS = "blocked"
NEEDS_INPUT = "needs_input"
STUCK = "stuck"

# Both lanes of the inbox, in the order the operator reads them.
QUEUE_KINDS = (NEEDS_INPUT, STUCK)

APPROVE_PREFIX = "APPROVED"
REJECT_PREFIX = "CHANGES REQUESTED"
RELEASE_PREFIX = "RELEASED"

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
    """The inbox predicate: blocked AND parked on a human."""
    return (getattr(task, "status", None) == BLOCKED_STATUS
            and (getattr(task, "block_kind", None) or "") == NEEDS_INPUT)


def is_stuck(task) -> bool:
    """The second lane: blocked, but not on a human. The dispatcher's give-up
    leaves ``block_kind`` NULL; ``capability``/``transient`` blocks are an agent
    parking work it cannot finish. Disjoint from :func:`is_pending` by
    construction, so a card is never in both lanes."""
    return (getattr(task, "status", None) == BLOCKED_STATUS
            and (getattr(task, "block_kind", None) or "") != NEEDS_INPUT)


def kind_of(task):
    """``needs_input`` / ``stuck`` / None for a card that is not blocked."""
    if is_pending(task):
        return NEEDS_INPUT
    return STUCK if is_stuck(task) else None


# ── row rendering ────────────────────────────────────────────────────────────

def _headline(comments) -> str:
    """First non-empty line of the newest comment — what the operator reads first."""
    for c in reversed(list(comments or [])):
        for line in (c.body or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:_HEADLINE_MAX]
    return ""


def _attachment_view(att) -> dict:
    return {"id": att.id, "filename": att.filename, "size": int(att.size or 0),
            "content_type": att.content_type, "created_at": att.created_at,
            "previewable": _is_text(att.filename, att.content_type)}


_WAIT_EVENTS = ("blocked", "gave_up", "block_loop_detected")


def _wait_event(conn, task_id) -> tuple:
    """``(reason, blocked_at)`` off the newest event that parked the card, else
    ``(None, None)``. ``blocked`` carries ``reason``; the dispatcher's
    ``gave_up`` carries ``error`` instead, which is the only text a stuck card
    has to explain itself."""
    from hermes_cli import kanban_db
    for ev in reversed(kanban_db.list_events(conn, task_id)):
        if ev.kind in _WAIT_EVENTS:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            return (payload.get("reason") or payload.get("error"), ev.created_at)
    return (None, None)


def _row(t, conn, task, *, now: int) -> dict:
    from hermes_cli import kanban_db
    reason, blocked_at = _wait_event(conn, task.id)
    waiting_since = blocked_at or task.created_at
    kind = kind_of(task)
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
        "headline": _headline(kanban_db.list_comments(conn, task.id)),
        "attachments": [_attachment_view(a)
                        for a in kanban_db.list_attachments(conn, task.id)],
        "reason_kind": kind,
    }
    if kind == STUCK:
        # What the dispatcher last saw: the failure text and how many runs it
        # cost. Only a stuck row has them, so the needs_input payload is
        # unchanged.
        row["failure_error"] = getattr(task, "last_failure_error", None)
        row["consecutive_failures"] = int(
            getattr(task, "consecutive_failures", 0) or 0)
    return row


# ── reads ────────────────────────────────────────────────────────────────────

def _queue_rows(t, kinds) -> list[dict]:
    """The blocked cards of *t* in the requested lanes, newest wait first. One
    DB pass whichever lanes are asked for — both predicates read the row that
    the one ``status=blocked`` query already returned."""
    import time
    from hermes_cli import kanban_db
    ctx = _conn_ctx(t)
    if ctx is None:
        return []
    want_input, want_stuck = NEEDS_INPUT in kinds, STUCK in kinds
    now = int(time.time())
    with ctx as conn:
        tasks = [task for task
                 in kanban_db.list_tasks(conn, tenant=tenant_of(t),
                                         status=BLOCKED_STATUS)
                 if (want_input and is_pending(task))
                 or (want_stuck and is_stuck(task))]
        rows = [_row(t, conn, task, now=now) for task in tasks]
    rows.sort(key=lambda r: (-(r["blocked_at"] or r["created_at"] or 0), r["id"]))
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
    rows.sort(key=lambda r: (-(r["blocked_at"] or r["created_at"] or 0), r["id"]))
    return rows


def pending(t) -> list[dict]:
    """Every card of *t* waiting on a human, newest wait first."""
    return _queue_rows(t, (NEEDS_INPUT,))


def stuck(t) -> list[dict]:
    """Every card of *t* blocked for something other than a human decision."""
    return _queue_rows(t, (STUCK,))


def queue(t, *, kinds=QUEUE_KINDS) -> list[dict]:
    """Both lanes merged — what the operator's inbox shows."""
    return _queue_rows(t, tuple(kinds))


def pending_all(reg=None) -> list[dict]:
    """:func:`pending` across the registry."""
    return _all_rows(storage.load() if reg is None else reg, (NEEDS_INPUT,))


def queue_all(reg=None, *, kinds=QUEUE_KINDS) -> list[dict]:
    """:func:`queue` across the registry."""
    return _all_rows(storage.load() if reg is None else reg, tuple(kinds))


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
        row["status"] = task.status
        row["block_kind"] = task.block_kind
        row["awaiting_approval"] = is_pending(task)
        row["stuck"] = is_stuck(task)
        row["comments"] = [
            {"id": c.id, "author": c.author, "body": c.body,
             "created_at": c.created_at}
            for c in kanban_db.list_comments(conn, task.id)
        ]
    return row


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


def _stuck_cause(task) -> str:
    """What parked a stuck card: the agent's own block kind, or — when the
    dispatcher's circuit breaker did it with a raw UPDATE — no kind at all."""
    return getattr(task, "block_kind", None) or "the dispatcher gave up"


def _require_pending(t, task) -> None:
    if is_pending(task):
        return
    if is_stuck(task):
        raise TeamError(
            E_TEAM_INVALID,
            f"card {task.id} is stuck ({_stuck_cause(task)}), "
            "not waiting on a human",
            hint="A stuck card has no decision to approve — releasing it "
                 "returns it to the pool for another run.",
            remedy=f'hermes team release {task.id} --note "…"',
        )
    raise TeamError(
        E_TEAM_INVALID,
        f"card {task.id} is {task.status!r}"
        + (f"/{task.block_kind!r}" if task.block_kind else "")
        + ", not waiting on a human",
        hint=f"Only cards with status={BLOCKED_STATUS!r} and "
             f"block_kind={NEEDS_INPUT!r} can be approved or rejected.",
        remedy=f"hermes team approvals {t.slug}",
    )


def _require_stuck(t, task) -> None:
    if is_stuck(task):
        return
    if is_pending(task):
        raise TeamError(
            E_TEAM_INVALID,
            f"card {task.id} is waiting on a human decision, not stuck",
            hint="A card parked on a person is approved or rejected, so the "
                 "decision reaches the agent as a comment.",
            remedy=f'hermes team approve {task.id} --note "…"',
        )
    raise TeamError(
        E_TEAM_INVALID,
        f"card {task.id} is {task.status!r}, not blocked",
        hint=f"Only a card with status={BLOCKED_STATUS!r} can be released.",
        remedy=f"hermes team approvals {t.slug} --kind stuck",
    )


def _require_text(action: str, task_id: str, text) -> str:
    """The decision text, or a refusal. An empty note is the one input that
    would leave the agent resuming with nothing to act on."""
    text = (text or "").strip()
    if not text:
        raise TeamError(
            E_TEAM_INVALID,
            f"{action} needs a decision text",
            hint="Say what you decided — the agent resumes by reading it.",
            remedy=f"hermes team {action} {task_id} "
                   f"--{'reason' if action == 'reject' else 'note'} \"…\"",
        )
    return text


def _decide(t, task_id: str, *, body: str, author, unblock: bool,
            action: str, require=_require_pending) -> dict:
    from hermes_cli import kanban_db
    ctx = _conn_ctx(t)
    if ctx is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"board {t.board!r} has no cards yet",
            hint="The board DB is created on first write.",
            remedy=f"hermes team status {t.slug}")
    author = (author or "").strip() or operator_author()
    with ctx as conn:
        task = _task_or_raise(conn, t, task_id)
        require(t, task)
        comment_id = kanban_db.add_comment(conn, task_id, author, body)
        unblocked = kanban_db.unblock_task(conn, task_id) if unblock else False
        after = kanban_db.get_task(conn, task_id)
    return {
        "team": t.slug,
        "board": t.board,
        "task_id": task_id,
        "action": action,
        "author": author,
        "comment_id": comment_id,
        "unblocked": bool(unblocked),
        "status": getattr(after, "status", task.status),
        "assignee": getattr(after, "assignee", task.assignee),
        "title": task.title,
    }


def approve(t, task_id: str, note: str, *, author=None) -> dict:
    """Record the decision and return the card to the pool. Never completes it —
    the assignee resumes, acts on the note and completes the card itself, which
    is what promotes the children."""
    note = _require_text("approve", task_id, note)
    return _decide(t, task_id, body=f"{APPROVE_PREFIX}: {note}",
                   author=author, unblock=True, action="approve")


def reject(t, task_id: str, reason: str, *, author=None, park: bool = False) -> dict:
    """Record requested changes. Unblocks by default (the agent must rework);
    ``park=True`` records the comment and leaves the card blocked."""
    reason = _require_text("reject", task_id, reason)
    return _decide(t, task_id, body=f"{REJECT_PREFIX}: {reason}",
                   author=author, unblock=not park, action="reject")


def release(t, task_id: str, note: str, *, author=None) -> dict:
    """Return a stuck card to the pool with the operator's note attached.

    Same shape as :func:`approve` and, deliberately, no more power: comment +
    ``unblock_task``. The unblock is what makes it useful — it resets
    ``consecutive_failures`` and ``last_failure_error`` (``kanban_db.py``), so
    the card gets the full retry budget again instead of one run before the
    circuit breaker trips. The operator still never completes it.
    """
    note = _require_text("release", task_id, note)
    return _decide(t, task_id, body=f"{RELEASE_PREFIX}: {note}",
                   author=author, unblock=True, action="release",
                   require=_require_stuck)


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
