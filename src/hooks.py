"""hooks.py — the ``pre_tool_call`` ladder and the registration surface
behind it (spec §5.3, §5.4, §6.3, §6.4).

Layer: ``hooks.py ─► policy, prompt, (lazy) storage/model``. This is the I/O
edge of the policy path: ``policy.py`` and ``prompt.py`` are pure, and every
filesystem touch the §6.2 table needs happens here, inside the §5.4 budget:

* ``pre_tool_call`` fires **once per tool call** and the host bounds it by
  ``plugins.hook_callback_timeout`` and **fails closed** on timeout
  (``plugins_dispatch.py`` ``_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS``) — a slow hook
  would block every tool on the machine. So the ladder is, in order:

  1. ``tool_name not in policy.GUARDED_TOOLS`` → ``None``. A frozenset
     lookup; 99% of all tool calls end here without touching the disk.
  2. registry cache freshness — **one** ``Path.stat`` comparing the
     ``(st_mtime_ns, st_size)`` tuple, the exact memo key ``hermes_cli.config``
     uses for ``read_raw_config()`` (§5.4). Same tuple → cached parse.
  3. profile → teams index (a prebuilt dict off the registry parse).
  4. ``policy.resolve_team`` + ``policy.decide`` — pure calls, no I/O.

* **No network, no subprocess, no DB open, no lock acquisition** on this
  path. The only filesystem touch on a warm call is the one ``stat``. The
  registry *path* is memoised too, because resolving it (an env var, then a
  read of the root ``config.yaml``) would otherwise cost more than the whole
  budget; ``teardown()`` drops it so a reload re-resolves.

* Fail **open** (§6.4): an unreadable/unparseable registry returns the last
  good parse (or no teams on the first-ever failure) and logs **once** — a
  YAML typo must never block every tool call on the host. Any internal
  exception is caught, logged, and turned into ``None``: *a team plugin must
  never be the reason a host cannot run a tool.*

Directive translation (§6.2 → host contract): the host accepts
``{"action": "modify", "args": {...}}`` / ``{"action": "block", "message":
...}`` / ``None`` (``hermes_cli/plugins.py:_get_pre_tool_call_directive_details``
— modify shallow-merges, block needs a message). ``policy.decide`` returns
``{"modify": {...}[, "note"]} | {"block": msg} | None``; an advise-mode
``note`` has no slot in the modify contract, so it is logged, not smuggled.

Profile identity: the ``pre_tool_call`` payload carries no profile field
(tool_name/args/task_id/session ids only), so the session is identified
exactly the way ``policy.resolve_team`` describes it — ``HERMES_PROFILE``
(what the substrate stamps for dispatched workers) plus the §6.3 env trio,
snapshotted from ``os.environ`` into a plain mapping here because policy
takes the boundary as a Mapping, never ``os.environ`` directly (§5.2).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from . import policy, prompt

log = logging.getLogger("hermes_plugins.hermes_agent_synergia.hooks")

# §5.3: the toolset id every team tool registers under.
TOOLSET = "team"

# ── module caches (§5.4) ─────────────────────────────────────────────────────
_UNSET = object()
_PATH: Any = _UNSET        # memoised registry file (resolving it reads config.yaml)
_MEMO: Any = _UNSET        # (key, registry, index); key = (st_mtime_ns, st_size) or None
_LAST_GOOD: Any = None     # last successful parse (Registry), kept hooks-side
_LOGGED_FAILURE = False    # log-once flag so a broken file cannot spam the log

# Task ids already pushed by the dispatch-tick observer. The tick repeats every
# few seconds and a crash sweep can re-report the same id, so without this the
# operator gets the same ping on every tick. Bounded: trimmed to the newest
# _NOTIFIED_MAX, which is far more than one sweep can produce.
_NOTIFIED: list = []
_NOTIFIED_MAX = 256

# The last suppression line logged, so a respawn guard that holds for hours
# costs one log line instead of one per tick.
_LAST_SUPPRESSION: str = ""


def teardown() -> None:
    """``ctx.on_unload`` seam (§5.3): drop the registry cache, the profile
    index and the memoised path. Closes nothing else — the hot path opens
    nothing."""
    global _PATH, _MEMO, _LAST_GOOD, _LOGGED_FAILURE, _LAST_SUPPRESSION
    _PATH = _UNSET
    _MEMO = _UNSET
    _LAST_GOOD = None
    _LOGGED_FAILURE = False
    _LAST_SUPPRESSION = ""
    _NOTIFIED.clear()


# ── registry cache (§5.4) ────────────────────────────────────────────────────

def _registry_file():
    """The registry path, resolved once per load cycle (see module docstring
    for why the *path* is cached, not just the parse)."""
    global _PATH
    if _PATH is _UNSET:
        from . import storage
        _PATH = storage.registry_path()
    return _PATH


def _parse():
    """Re-read + parse the registry **once** per freshness miss.

    ``storage.load`` is the correctness path (raises on a bad file); the
    failure it raises is converted here into the §5.4 fail-open value — last
    good parse, or no teams on the first-ever failure — logged exactly once
    per failure spell (§6.4)."""
    global _LAST_GOOD, _LOGGED_FAILURE
    from . import storage
    try:
        reg = storage.load()
    except Exception as exc:                       # noqa: BLE001 — fail OPEN, §5.4/§6.4
        if _LAST_GOOD is None:
            reg = storage.Registry()               # first-ever failure: no teams
        else:
            reg = _LAST_GOOD                       # later failures: keep serving
        if not _LOGGED_FAILURE:
            log.warning("hermes-agent-synergia: registry unreadable (%s); running with %s teams",
                        exc, len(getattr(reg, "teams", {}) or {}))
            _LOGGED_FAILURE = True
        return reg
    _LAST_GOOD = reg
    _LOGGED_FAILURE = False                        # next spell gets its own line
    return reg


def _build_index(reg: Any) -> dict:
    """profile → [team records], prebuilt once per parse (ladder step 3)."""
    index: dict = {}
    for team in (getattr(reg, "teams", None) or {}).values():
        for m in (getattr(team, "members", None) or ()):
            prof = str(getattr(m, "profile", "") or "")
            if prof:
                index.setdefault(prof, []).append(team)
    return index


def _refresh() -> Optional[tuple]:
    """Ladder step 2: exactly one ``stat`` per call; re-parse only when the
    ``(mtime_ns, size)`` tuple moved. Returns ``(registry, index)``; the
    whole function body may raise — ``pre_tool_call`` owns the catch."""
    global _MEMO
    path = _registry_file()
    try:
        st = path.stat()
        key: Any = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None                                 # absent/unstatable ⇒ "no registry" is a valid memo value
    memo = _MEMO
    if memo is not _UNSET and memo[0] == key:
        return memo[1], memo[2]
    reg = _parse()
    _MEMO = (key, reg, _build_index(reg))
    return reg, _MEMO[2]


# ── session identity ─────────────────────────────────────────────────────────

def _calling_profile(env: Mapping) -> str:
    """The acting profile, from ``HERMES_PROFILE`` — the same source
    ``tools._calling_profile`` reads (the substrate stamps it for dispatched
    workers). Normalised like ``model._normalize_profile_name``; "" when the
    env carries nothing usable (which then makes ``resolve_team`` fall to its
    §6.3 env steps or to ``None`` — never a guess)."""
    name = str(env.get("HERMES_PROFILE") or "").strip()
    if not name:
        return ""
    try:
        from .model import _normalize_profile_name
        return _normalize_profile_name(name)
    except Exception:                              # noqa: BLE001 — env is untrusted input
        return name.lower()


def _env_snapshot() -> dict:
    """Just the §6.3 trio (+ the profile stamp), snapshotted once per call.
    ``policy`` reads env through a Mapping — ``os.environ`` never crosses the
    boundary (§5.2); building the Mapping is the hook's job."""
    return {k: os.environ.get(k, "") for k in
            ("HERMES_PROFILE", policy.TENANT_ENV, policy.BOARD_ENV, policy.TEAM_ENV)}


# ── registration gate (§5.3) ─────────────────────────────────────────────────

def policy_needed(profile: Any) -> bool:
    """True iff *profile* is a member of at least one team whose
    ``policy.mode`` is not ``off`` (§5.3: the hook is registered only then,
    so non-team profiles pay nothing on the tool hot path). Cheap (cache +
    index, same one-stat ladder) and **must not raise** — register() calls
    it while the host is still loading us."""
    try:
        pair = _refresh()
        if pair is None:
            return False
        _reg, index = pair
        name = str(profile or "").strip()
        if not name:
            return False
        return any(policy._policy_mode(t) != "off" for t in index.get(name, ()))
    except Exception:                              # noqa: BLE001 — no hook beats a broken install
        return False


# ── the hot path (§5.4, §6.2, §6.4) ──────────────────────────────────────────

def pre_tool_call(tool_name: str, args: Any, task_id: str = "",
                  **kw: Any) -> Optional[dict]:
    """The §5.4 ladder; §6.2 translation onto the host's ``pre_tool_call``
    contract. Everything inside one try/except: this hook runs before every
    guarded tool call on this profile and **failing open is the contract**
    (§6.4) — an internal error costs the policy, never the tool."""
    try:
        # (1) Ladder step 1 — frozenset lookup first: the hot path for every
        # non-kanban tool call (and for all four NEVER_GUARDED tools).
        if tool_name not in policy.GUARDED_TOOLS:
            return None
        # (2) One stat; (mtime_ns, size) unchanged ⇒ cached parse.
        pair = _refresh()
        if pair is None:
            return None
        reg, index = pair
        # (3) Identity + profile→teams index. Membership short-circuits on a
        # dict lookup; a §6.3 env var overrides it (a dispatched worker is
        # identified by its tenant, not its roster).
        env = _env_snapshot()
        profile = _calling_profile(env)
        if profile not in index and not any(
                env[k] for k in (policy.TENANT_ENV, policy.BOARD_ENV, policy.TEAM_ENV)):
            return None
        # (4) Pure resolution + pure decision.
        slug = policy.resolve_team(profile, env, reg)
        if slug is None:
            return None
        team = (getattr(reg, "teams", None) or {}).get(slug)
        directive = policy.decide(team, profile, tool_name, args)
        if directive is None:
            return None
        # §6.2 directives → host contract (plugins.py
        # _get_pre_tool_call_directive_details: modify takes "args", block
        # requires a non-empty "message").
        if "block" in directive:
            return {"action": "block", "message": str(directive["block"])}
        note = directive.get("note")
        if note:
            # advise mode: the rewrite applied, the reason logged. The host's
            # modify directive has no note channel; the message is the audit
            # trail, not a smuggled tool result.
            log.info("hermes-agent-synergia: %s", note)
        return {"action": "modify", "args": dict(directive.get("modify") or {})}
    except Exception as exc:                       # noqa: BLE001 — §6.4: never the host's problem
        log.warning("hermes-agent-synergia: pre_tool_call failed open on %s: %s", tool_name, exc)
        return None


# ── the prompt section (§5.3) ────────────────────────────────────────────────

def render_contract(session_info: Any) -> str:
    """``register_system_prompt_section(content=...)`` (§5.3): the team
    contract for the session's profile, rendered once per session from the
    live registry. "" when the session belongs to no team (the host drops
    empty sections). Callable-safe: never raises, tolerates a missing
    profile key or a non-mapping payload."""
    try:
        info = session_info if isinstance(session_info, Mapping) else {}
        profile = str(info.get("profile_name") or "").strip()
        if not profile:
            return ""
        pair = _refresh()
        if pair is None:
            return ""
        reg, _index = pair
        team = None
        slug = policy.resolve_team(profile, _env_snapshot(), reg)
        if slug is not None:
            team = (getattr(reg, "teams", None) or {}).get(slug)
        if team is None:
            # No §6.3 resolution — a single-team member is unambiguous, and
            # exactly what the contract block is for.
            teams = pair[1].get(profile) or ()
            team = teams[0] if len(teams) == 1 else None
        if team is None:
            return ""
        return prompt.render(team, profile, prompt.MAX_CHARS)
    except Exception:                              # noqa: BLE001 — a missing contract beats a lost session
        return ""


def render_attachments(session_info: Any) -> str:
    """``register_system_prompt_section(content=...)`` for the attachments
    block. Same gate as :func:`render_contract` — "" when the session belongs
    to no team, so a non-team profile pays nothing for a tool it cannot see.
    Callable-safe: never raises."""
    try:
        info = session_info if isinstance(session_info, Mapping) else {}
        profile = str(info.get("profile_name") or "").strip()
        if not profile:
            return ""
        pair = _refresh()
        if pair is None or not (pair[1].get(profile) or ()):
            return ""
        return prompt.render_attachments()
    except Exception:                              # noqa: BLE001 — guidance is optional
        return ""


# ── kanban observer (§5.3) ───────────────────────────────────────────────────

def on_task_claimed(**kw: Any) -> None:
    """Observability only (§5.3): stamp the tenant onto the claimed card is
    NOT done here — the payload arrives after the board's DB commit, a stamp
    would open a second write path and a second policy surface, and §5.4
    forbids the DB on hook paths. Log the tuple and stop; never raises."""
    try:
        log.info("hermes-agent-synergia: card claimed task_id=%s board=%s assignee=%s",
                 kw.get("task_id"), kw.get("board"), kw.get("assignee"))
    except Exception:                              # noqa: BLE001 — observer
        return None


def on_task_blocked(**kw: Any) -> None:
    """A card that parks on a human is pushed to the operator's home
    channels, so the wait is visible without opening a dashboard.

    The hook fires AFTER the board's DB commit (hermes_cli/plugins.py), so
    reading the row back is allowed here — and necessary: the payload carries
    ``reason`` but not ``block_kind``, and ``needs_input`` is the only kind
    that waits on a person. Subscribing the card is all this does; delivery is
    the gateway notifier, which already treats ``blocked`` and ``unblocked``
    as terminal — so the operator is pinged when the wait starts AND when it
    ends, with no delivery code here.

    An observer, so it never raises and never blocks: with no home channel
    configured (this host today) it is a no-op.
    """
    try:
        from . import approvals
        result = approvals.on_blocked(str(kw.get("task_id") or ""),
                                      str(kw.get("board") or ""))
        if result.get("platforms"):
            log.info("hermes-agent-synergia: approval requested task_id=%s team=%s "
                     "pushed to %s", result["task_id"], result["team"],
                     ", ".join(result["platforms"]))
    except Exception:                              # noqa: BLE001 — observer
        return None


def on_task_completed(**kw: Any) -> None:
    """When the last card of a worktree team's thread finishes, fast-forward
    the workdir to the thread's result branch — the substrate never merges a
    ``wt/<id>`` branch, so otherwise the work lives only under ``.worktrees/``
    and the next thread branches off a HEAD that never saw it.

    Fires in the worker AFTER the board's DB commit, so the finished row reads
    back as ``done``. The whole decision is :func:`approvals.on_completed`;
    this observer never raises and never blocks a completion."""
    try:
        from . import approvals
        result = approvals.on_completed(str(kw.get("task_id") or ""),
                                        str(kw.get("board") or ""))
        if result.get("merged"):
            log.info("hermes-agent-synergia: merged %s (%s) into the workdir "
                     "of team=%s root=%s", result["branch"], result["sha"][:12],
                     result["team"], result["root"])
        elif result.get("skipped"):
            log.info("hermes-agent-synergia: auto-merge skipped team=%s root=%s: %s",
                     result["team"], result["root"], result["skipped"])
    except Exception:                              # noqa: BLE001 — observer
        return None


def _fresh(task_ids) -> list:
    """*task_ids* minus the ones already pushed, remembering the rest."""
    fresh = [tid for tid in task_ids if tid not in _NOTIFIED]
    _NOTIFIED.extend(fresh)
    if len(_NOTIFIED) > _NOTIFIED_MAX:
        del _NOTIFIED[:-_NOTIFIED_MAX]
    return fresh


def on_dispatch_tick(**kw: Any) -> None:
    """Everything the dispatcher does that no other hook reports.

    Three jobs, each keyed off its own ``DispatchResult`` list:

    * ``auto_blocked`` — the stuck lane's push. The circuit breaker writes
      ``status='blocked'`` with a raw UPDATE and fires no
      ``kanban_task_blocked``, so it is invisible to :func:`on_task_blocked`.
    * ``spawned`` — the review-round loop guard (:func:`_check_review_loop`).
    * ``respawn_guarded`` — why ready work is being held back
      (:func:`_log_suppression`).

    This fires once per dispatcher tick — every few seconds, forever — and
    almost always with all three empty, so the cost of the idle case is three
    ``getattr``s and three falsy checks: nothing is imported, no DB is opened,
    the registry is not read. Only a non-empty list reaches ``approvals``
    (which owns every substrate call; ``hooks.py`` is not one of the six files
    allowed to import ``hermes_cli``). An observer, so it never raises — and
    each job is isolated, so one failing never costs the other two.
    """
    tick = kw.get("result")
    board = str(kw.get("board") or "")
    try:
        given_up = getattr(tick, "auto_blocked", None)
        fresh = _fresh([str(tid) for tid in given_up if tid]) if given_up else []
        if fresh:
            from . import approvals
            result = approvals.on_auto_blocked(fresh, board)
            if result.get("notified"):
                log.info("hermes-agent-synergia: dispatcher gave up on %s, pushed to %s",
                         ", ".join(result["notified"]),
                         ", ".join(result["platforms"]) or "(no home channel)")
    except Exception:                              # noqa: BLE001 — observer
        pass
    _check_review_loop(tick, board)
    _log_suppression(tick)
    return None


def _check_review_loop(tick, board: str) -> None:
    """Park a card that has gone round the review loop too many times.

    Driven off ``DispatchResult.spawned`` because a card appears there exactly
    once per run: one events read per spawn, and on the overwhelmingly common
    idle tick a single ``getattr`` and a falsy check. ``spawned`` carries
    ``(task_id, assignee, workspace_path)`` triples.

    The DB work — and the block itself — belongs to ``approvals``: ``hooks.py``
    is not one of the six files allowed to import ``hermes_cli``.
    """
    try:
        spawned = getattr(tick, "spawned", None)
        if not spawned or not board:
            return None
        from . import approvals
        result = approvals.check_review_loop(
            [str(row[0]) for row in spawned if row and row[0]], board)
        for tid in result.get("parked", ()):
            log.warning(
                "hermes-agent-synergia: card %s parked for a human after %s "
                "review rounds (limit %s) — review loop guard",
                tid, result["rounds"].get(tid), result["limit"])
    except Exception:                              # noqa: BLE001 — observer
        return None


def _log_suppression(tick) -> None:
    """Say out loud why a tick held ready work back.

    ``respawn_guarded`` never blocks a card and never fires a hook, so without
    this the only place the reason is written is ``hermes kanban tail``. Logged
    once per distinct reason set, so a guard that holds for hours costs one
    line, not one per tick.
    """
    global _LAST_SUPPRESSION
    try:
        if not getattr(tick, "respawn_guarded", None):
            return None
        from . import approvals
        line = approvals.suppression_line(tick)
        if not line or line == _LAST_SUPPRESSION:
            return None
        _LAST_SUPPRESSION = line
        log.info("hermes-agent-synergia: dispatcher holding ready work back: %s",
                 line)
    except Exception:                              # noqa: BLE001 — observer
        return None
