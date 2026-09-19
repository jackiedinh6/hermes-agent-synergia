"""resolve.py — the only reader of Hermes state (spec §5.2 layering).

S7 implements the READ HALF of Appendix A: ``observe(team) -> Observed``,
``profile_config_path``, ``read_profile_config``, ``board_meta``,
``project_for``, ``task_counts`` (plus the S6 call sites ``profiles_exist``,
``board_exists``, ``ensure_board``, ``create_profile_for`` which stay as-is).
S8 adds the WRITE half: ``write_profile_config_keys`` — the five guards,
in order; the plugin's only ``config.yaml`` writer (§13).
S10a widens that writer's allow-list to nine keys (deviation 8) and adds
:func:`installed_skill_names` / :func:`disabled_skills_for`, the read seam S10
uses to project ``Member.skills`` onto a member's ``skills.disabled``.

Everything hermes_cli-touching lazy-imports inside the function so importing
the plugin never pays ``hermes_cli``'s import cost (spec §5.2).

§5.2.1 hazard mitigations — one named mitigation per row of the hazard
table; each is also commented at its use site:

* Private symbols ............ only ``kb.board_exists / read_board_metadata /
  create_board / write_board_metadata`` are used; slug validation comes from
  the COPY in ``model.SLUG_RE`` (:func:`_check_slug`) — never the
  underscore-private ``kb._normalize_board_slug`` / ``kb._BOARD_SLUG_RE``.
  The two deliberate exceptions are the S10a seam (``web_server_profiles._profile_scope``
  + ``skills_tool._find_all_skills``, named at their use site in
  :func:`installed_skill_names`): deviation 8 requires the *profile-scoped*
  installed-skill set, and the public surfaces offer no scoped read.
* ``load_config()`` cache ... :func:`read_profile_config` goes through the
  substrate's ``read_user_config_raw(path)`` — uncached, exact, and safe for
  a later write round-trip. Nothing here ever mutates a
  ``load_config_readonly()`` result.
* ``plugin_data_dir()`` ...... NOT imported. This module resolves the Hermes
  root only through ``hermes_constants``/``profiles`` (the registry is
  host-global; profile-following data dirs are storage.py's non-concern).
* ``default_workdir`` kind-scoped :func:`observe` RECORDS
  ``board_default_workdir`` but never infers task placement from it alone
  (it only applies for workspace_kind dir/worktree).
* ``terminal.cwd`` sentinels :func:`_member_view` maps any
  ``model.TERMINAL_CWD_SENTINELS`` value to ``None`` ("unset"), never echoes
  the literal, and nothing here writes one.
* Compat window .............. the only imports are ``hermes_cli.kanban_db``,
  ``hermes_cli.kanban_db_connect``, ``hermes_cli.projects_db``,
  ``hermes_cli.profiles``, ``hermes_cli.config``, ``hermes_constants``,
  ``utils`` (modern paths; no re-export shims, no name imported through
  ``profiles``/``profile_distribution``'s PLUGIN-COMPAT blocks).

Every read validates and degrades — a dangling member yields
``{"exists": false, "managed": false}`` (§4.3), never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import model
from .errors import (
    E_TEAM_DRIFT_UNFIXED, E_TEAM_INVALID, E_TEAM_MANAGED, E_TEAM_NO_PROFILE,
    TeamError,
)

# §4.3's ``counts`` block is exactly these six buckets, in this order.
# triage/scheduled/archived are deliberately absent — the envelope shape is
# frozen by §4.3 and consumers (dashboard, doctor) key off these six.
COUNT_STATUSES = ("todo", "ready", "running", "review", "done", "blocked")


# ── substrate accessors (lazy; §5.2 import discipline) ──────────────────────

def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _kanban_conn():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _profiles():
    from hermes_cli import profiles
    return profiles


def _projects_db():
    from hermes_cli import projects_db
    return projects_db


def _config():
    from hermes_cli import config
    return config


def _utils():
    """The substrate's shared ``utils`` (allow-listed; S8: atomic_write_text,
    the temp+fsync+rename primitive the write pipeline commits through)."""
    import utils
    return utils


def _skills_tool():
    """``tools.skills_tool`` — S10a reads the installed-skill set through it
    (see :func:`installed_skill_names`)."""
    from tools import skills_tool
    return skills_tool


def _profile_scope():
    """``hermes_cli.web_server_profiles._profile_scope`` — the substrate's own
    profile seam: retargets ``HERMES_HOME`` **and** rebinds
    ``skills_tool.SKILLS_DIR`` for the duration of the call."""
    from hermes_cli.web_server_profiles import _profile_scope as scope
    return scope


def _essential_skills():
    """``agent.skill_utils.ESSENTIAL_SKILLS`` — the names that can never be
    disabled (``hermes-agent`` is the agent's operating manual)."""
    from agent.skill_utils import ESSENTIAL_SKILLS
    return ESSENTIAL_SKILLS


# ── private-symbol mitigation (hazard: Private symbols) ─────────────────────

def _check_slug(board: str) -> str:
    """Validate a board slug with OUR copied ``model.SLUG_RE`` — hazard
    §5.2.1 "Private symbols": we never call ``kb._normalize_board_slug`` or
    read ``kb._BOARD_SLUG_RE``; one bad slug in the registry produces a
    TeamError naming the field, not a ``ValueError`` from private code."""
    board = str(board or "")
    if not model.SLUG_RE.match(board):
        raise TeamError(
            E_TEAM_INVALID,
            f"board slug {board!r} is not a valid kanban slug",
            hint="Slugs match ^[a-z0-9][a-z0-9\\-_]{0,63}$ (copied from the substrate).",
            remedy="hermes team update <team> --board <valid-slug>",
        )
    return board


# ── S6 call sites (unchanged) ───────────────────────────────────────────────

def profile_home(profile: str) -> Path:
    return _profiles().get_profile_dir(profile)


def profile_exists(profile: str) -> bool:
    return _profiles().profile_exists(profile)


def profile_tombstoned(profile: str) -> bool:
    """True when the profile dir exists but carries the deleted-profile
    marker (public ``hermes_constants.named_profile_is_deleted`` — the
    marker is a hermes_constants concept, not a ``profiles`` one).
    An unreadable home ⇒ ``False`` (no evidence of a tombstone)."""
    import hermes_constants
    try:
        return bool(hermes_constants.named_profile_is_deleted(
            profile_home(profile)))
    except Exception:  # noqa: BLE001
        return False


def root_path() -> Path:
    """Host-global Hermes root (``hermes_constants.get_default_hermes_root``
    — never ``plugin_data_dir()``, which follows ``HERMES_HOME`` and
    flips per profile). Needed by reconcile for the root ``.env``
    and root ``config.yaml`` (policy hook) reads, keeping those
    ``hermes_*`` touches behind the single-reader wall (§5.2)."""
    import hermes_constants
    return hermes_constants.get_default_hermes_root()


def default_model() -> dict:
    """Root ``config.yaml``'s main model as ``{model, provider, base_url}``
    (blank values dropped) — what a roster member without its own model is
    stamped with. Raw file, not ``load_config``: ``${VAR}`` references are
    copied as written so the profile expands them the same way."""
    try:
        raw = yaml.safe_load((root_path() / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    cfg = raw.get("model") if isinstance(raw, dict) else None
    if isinstance(cfg, str):
        cfg = {"default": cfg}
    if not isinstance(cfg, dict):
        return {}
    picked = {"model": cfg.get("default") or cfg.get("model"),
              "provider": cfg.get("provider"),
              "base_url": cfg.get("base_url")}
    return {k: str(v).strip() for k, v in picked.items()
            if isinstance(v, (str, int, float)) and not isinstance(v, bool)
            and str(v).strip()}


def profiles_exist(profiles) -> list:
    """Return the member profiles that do NOT exist on disk (dangling)."""
    return [p for p in profiles if not profile_exists(p)]


def create_profile_for(profile: str, *, clone_from: Optional[str] = None,
                       clone_config: bool = False,
                       description: Optional[str] = None) -> Path:
    """``profiles.create_profile``, guarded: never ``clone_all``."""
    return _profiles().create_profile(
        profile, clone_from=clone_from, clone_all=False,
        clone_config=clone_config, description=description,
    )


def board_exists(board: str) -> bool:
    return _kb().board_exists(board)


def ensure_board(board: str, *, name: Optional[str] = None,
                 default_workdir: Optional[str] = None) -> dict:
    """Create the board if missing (mkdir -p semantics, spec §5.5).

    The delegated-child mutation guard (``_assert_not_delegated_child_mutation``)
    is caught and reported as drift by S10 — S7 adds that; S6 is CLI-only.
    """
    kb = _kb()
    return kb.create_board(board, name=name, default_workdir=default_workdir)


# ── S7 read-half readers ────────────────────────────────────────────────────

def board_meta(board: str) -> dict:
    """Board metadata (public ``kb.read_board_metadata`` — it never raises;
    a missing board yields the substrate's synthesized entry). Slug validated
    against the copied ``model.SLUG_RE`` first (hazard: Private symbols)."""
    return _kb().read_board_metadata(_check_slug(board))


def profile_config_path(profile: str) -> Path:
    """``<profile-home>/config.yaml`` — home via public
    ``profiles.get_profile_dir`` (never a hand-joined ``<root>/profiles``)."""
    return _profiles().get_profile_dir(profile) / "config.yaml"


def read_profile_config(profile: str) -> dict:
    """Raw profile ``config.yaml`` exactly as written, or ``{}`` when absent.

    Hazard §5.2.1 "``load_config()`` cache": this goes through
    ``hermes_cli.config.read_user_config_raw(path)`` — uncached, no defaults
    overlay, no variable expansion — because anything read here may be
    destined for a surgical write round-trip. We NEVER mutate a
    ``load_config_readonly()`` result (that would corrupt the shared
    in-process cache for every other plugin). A malformed YAML raises; that
    is the point for write round-trips (callers that must not crash —
    :func:`observe` — catch it and degrade).
    """
    return _config().read_user_config_raw(profile_config_path(profile))


def _read_distribution_yaml(profile_home: Path) -> dict:
    """Installed ``distribution.yaml`` (provenance stamped by
    ``install_distribution``); ``{}`` when absent/unreadable — reading it must
    never break an observe."""
    import yaml
    try:
        data = yaml.safe_load(
            (profile_home / "distribution.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a dangling/unreadable pack file is info, not a crash
        return {}


def _env_is_placeholder(profile_home: Path) -> bool:
    """A fresh instance gets a seeded placeholder ``.env`` while this
    deployment symlinks profiles to the root ``.env``. Observed truth: a REAL
    file (not a symlink) is the placeholder; a symlink (or no file) is not."""
    env = profile_home / ".env"
    return env.is_file() and not env.is_symlink()


def _blueprint_in_catalog(blueprint: Optional[str]) -> bool:
    """catalog lookup for §4.3 ``blueprint_in_catalog`` — catalog.py is
    filesystem-only (no hermes_cli), so this stays inside the layering rule.
    false ⇒ the pack moved or the catalog is unavailable: info, not drift
    (§4.3 footnote). Never raises."""
    if not blueprint:
        return False
    try:
        from . import catalog
        catalog.get(str(blueprint))
        return True
    except Exception:  # noqa: BLE001 — E_TEAM_NO_BLUEPRINT / E_TEAM_CATALOG_UNAVAILABLE ⇒ False
        return False


# ── doctor readers: sprawl, orphans and the F8 gateway hazard (S6.5) ─────────
#
# These exist so `team doctor` never reaches for the filesystem itself: §5.2
# keeps every Hermes-state read behind this module.

PLUGIN_LINK_NAME = "hermes-agent-synergia"

# Every named profile's gateway defaults to this port and there is NO
# allocator (substrate `profiles.py:_maybe_register_gateway_service` spells it
# out: `API_SERVER_PORT`, else `platforms.api_server.extra.port`, else 8642).
# Two members that both leave it unset bind the same port — plan F8.
DEFAULT_API_SERVER_PORT = 8642


def profile_inventory() -> list:
    """Live named profiles as plain dicts (public ``profiles.list_profiles``,
    which already skips tombstones and invalid ids). ``default`` is dropped:
    it is the root, never a team member. Never raises."""
    try:
        infos = _profiles().list_profiles()
    except Exception:  # noqa: BLE001 — an unreadable profiles root is "none"
        return []
    out = []
    for info in infos:
        if getattr(info, "is_default", False):
            continue
        out.append({
            "name": info.name,
            "gateway_running": bool(getattr(info, "gateway_running", False)),
        })
    return out


def plugin_link_present(profile: str) -> bool:
    """``<profile-home>/plugins/hermes-agent-synergia`` is a symlink.

    This is the F1 plumbing **only this plugin installs** — a pack install
    (`install_distribution`) never creates it — so it is the marker that
    separates "an instance we provisioned" from "a profile the operator
    installed by hand"."""
    try:
        return (profile_home(profile) / "plugins" / PLUGIN_LINK_NAME).is_symlink()
    except Exception:  # noqa: BLE001
        return False


def distribution_source(profile: str) -> str:
    """The ``source:`` provenance stamped on install, or ``""``."""
    try:
        return str(_read_distribution_yaml(profile_home(profile)).get("source") or "")
    except Exception:  # noqa: BLE001
        return ""


def profile_footprint_bytes(profile: str) -> int:
    """Bytes on disk under the profile home — instance
    sprawl is *measured*, not hidden.

    Symlinks count as zero: the common one is F7's ``.env`` pointing at the
    root, and charging a team for the root's bytes would be a lie."""
    total = 0
    try:
        home = profile_home(profile)
    except Exception:  # noqa: BLE001
        return 0
    for dirpath, _dirnames, filenames in os.walk(home, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.islink(full):
                total += st.st_size
    return total


def _env_var(home: Path, key: str) -> Optional[str]:
    """One ``KEY=value`` from a profile's ``.env``; symlinks are FOLLOWED on
    purpose (that is the file the gateway would load). ``None`` when absent."""
    try:
        text = (home / ".env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def _explicit_gateway_port(profile: str) -> Optional[int]:
    """The port this profile *declares*, config first then ``.env``; ``None``
    when it just takes the default."""
    try:
        cfg = read_profile_config(profile)
    except Exception:  # noqa: BLE001 — malformed config declares nothing
        cfg = {}
    node: Any = cfg
    for part in ("platforms", "api_server", "extra", "port"):
        node = node.get(part) if isinstance(node, dict) else None
    for candidate in (node, _env_var(profile_home(profile), "API_SERVER_PORT")):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.strip().isdigit():
            return int(candidate.strip())
    return None


def gateway_facts(profile: str) -> dict:
    """``{port, explicit}`` for the F8 collision check.

    ``explicit`` is True when the profile names its own port — the only way
    two gateways in one team can coexist, since there is no allocator."""
    port = _explicit_gateway_port(profile)
    return {"port": DEFAULT_API_SERVER_PORT if port is None else port,
            "explicit": port is not None}


def _member_view(member, team: "model.Team") -> dict:
    """One member's §4.3 resolved block — full shape when the profile is on
    disk, the two-key dangling form when it is not."""
    # Hazard §5.2.1: every read validates, nothing crashes.
    if not profile_exists(member.profile):
        # §4.3 pins the resolved view of a dangling reference to exactly
        # exists/managed-false: never re-instantiated, never deleted.
        return {"exists": False, "managed": False}

    home = profile_home(member.profile)
    try:
        cfg = read_profile_config(member.profile)
    except Exception:  # noqa: BLE001 — malformed config degrades to {}
        cfg = {}
    try:
        meta = _profiles().read_profile_meta(home)
    except Exception:  # noqa: BLE001 — read_profile_meta never raises, belt and braces
        meta = {"description": ""}
    dist = _read_distribution_yaml(home)

    # Hazard §5.2.1 "terminal.cwd sentinels": "", ".", "./", "auto", "cwd"
    # all mean UNSET — report None, never echo the literal, never write one.
    raw_cwd = (cfg.get("terminal") or {}).get("cwd")
    terminal_cwd = None if raw_cwd in model.TERMINAL_CWD_SENTINELS else raw_cwd
    matches = bool(
        terminal_cwd
        and team.workdir
        and os.path.expanduser(str(terminal_cwd)) == os.path.expanduser(team.workdir)
    )
    description = str(meta.get("description") or "")
    return {
        "exists": True,
        "managed": bool(member.managed),
        "blueprint": member.blueprint,
        "blueprint_in_catalog": _blueprint_in_catalog(member.blueprint),
        "distribution_source": (str(dist.get("source") or "") or None),
        "distribution_installed_at": (str(dist.get("installed_at") or "") or None),
        "terminal_cwd": terminal_cwd,
        "terminal_cwd_matches": matches,
        "toolsets": list(cfg.get("toolsets") or []),
        "description": description,
        "description_empty": not description,
        "env_is_placeholder": _env_is_placeholder(home),
        "max_in_progress_per_profile": (cfg.get("kanban") or {}).get(
            "max_in_progress_per_profile"),
    }


def project_for(team: "model.Team") -> Optional[dict]:
    """The team's linked project record (public ``projects_db.get_project``,
    hazard: Compat window — modern module path only), or ``None``.

    ``project.mode == "none"`` (or no id/slug) is not a lookup request; a
    link whose row has gone missing degrades to ``None`` (dangling
    references never crash a read)."""
    proj = getattr(team, "project", None)
    mode = str(getattr(proj, "mode", "none") or "none")
    if mode in ("", "none"):
        return None
    id_or_slug = getattr(proj, "id", None) or getattr(proj, "slug", None)
    if not id_or_slug:
        return None
    pdb = _projects_db()
    try:
        with pdb.connect_closing() as conn:
            found = pdb.get_project(conn, str(id_or_slug))
    except Exception:  # noqa: BLE001 — unreadable projects DB ⇒ unlinked view
        return None
    return found.to_dict() if found is not None else None


def task_counts(board: str, tenant: Optional[str] = None) -> dict:
    """Six §4.3 status buckets for *board*, scoped to ``tenant`` when
    given; every bucket present, missing board ⇒ all zeros.

    Read-only by construction (plus the layering rule): we gate on the DB
    FILE existing because ``kanban_db_connect.connect()`` creates/migrates on
    first open — a reader must never mint a board directory."""
    zero = {status: 0 for status in COUNT_STATUSES}
    try:
        kb = _kb()
        board = _check_slug(board)
    except TeamError:
        return zero
    db_path = kb.kanban_db_path(board)
    if not db_path.is_file():
        return zero
    tasks = []
    try:
        with _kanban_conn().connect_closing(db_path=db_path) as conn:
            # public list_tasks; archived rows excluded by the default.
            tasks = kb.list_tasks(conn, tenant=tenant or None)
    except Exception:  # noqa: BLE001 — an unreadable board reads as no tasks
        return zero
    counts = dict(zero)
    for task in tasks:
        status = getattr(task, "status", None)
        if status in counts:
            counts[status] += 1
    return counts


def _creator_task_id(events) -> Optional[str]:
    for event in events:
        if event.kind == "created":
            payload = event.payload if isinstance(event.payload, dict) else {}
            return payload.get("creator_task_id") or None
    return None


def task_forest(board: str, tenant: Optional[str] = None) -> dict:
    """Every tenant card on *board*, grouped under the started task (root
    card) it descends from: ``{root_id: {"tasks": [Task, ...], "parents":
    {id: [parent ids]}, "summaries": {id: summary}}}``.

    A child reaches its root through ``task_links``. A card created without
    ``parents`` from inside a worker still names that worker's card as
    ``creator_task_id`` in its ``created`` event; the creator stands in as
    its parent. A card with neither is a root. Each group is in execution
    order: root first, then by start time, cards not yet started last.

    Read-only, same gate as :func:`task_counts` — a missing board DB reads
    as ``{}`` and is never minted."""
    db_path = _board_db(board)
    if db_path is None:
        return {}
    kb = _kb()
    try:
        with _kanban_conn().connect_closing(db_path=db_path) as conn:
            by_id = {t.id: t for t in kb.list_tasks(conn, tenant=tenant or None)}
            graph = kb.task_graph_contexts(conn, list(by_id))
            parents = {
                tid: [p["id"] for p in graph.get(tid, {}).get("parents", [])
                      if p["id"] in by_id]
                for tid in by_id}
            for tid, pids in parents.items():
                if not pids:
                    creator = _creator_task_id(kb.list_events(conn, tid))
                    if creator in by_id and creator != tid:
                        pids.append(creator)
            summaries = kb.latest_summaries(conn, list(by_id))
    except Exception:  # noqa: BLE001 — an unreadable board reads as no tasks
        return {}

    def root_of(tid: str) -> str:
        seen = set()
        while parents[tid] and tid not in seen:
            seen.add(tid)
            tid = min(parents[tid], key=lambda p: (by_id[p].created_at, p))
        return tid

    groups: dict = {}
    for tid in by_id:
        groups.setdefault(root_of(tid), []).append(by_id[tid])
    forest = {}
    for rid, tasks in groups.items():
        tasks.sort(key=lambda t: (t.id != rid, t.started_at is None,
                                  t.started_at or t.created_at,
                                  t.created_at, t.id))
        ids = [t.id for t in tasks]
        forest[rid] = {
            "tasks": tasks,
            "parents": {i: parents[i] for i in ids},
            "summaries": {i: summaries[i] for i in ids if summaries.get(i)},
        }
    return forest


# Per-card comment cap: a long-running card accumulates operator notes and
# agent replies, and the chat thread only ever renders the recent end of it.
_COMMENTS_MAX = 200


def task_comments(board: str, task_ids, *, limit: int = _COMMENTS_MAX) -> dict:
    """``{task_id: [{id, author, body, created_at}, ...]}`` for *task_ids*.

    The newest ``limit`` per card, oldest first — a thread reads forwards but
    an old card's history is not worth shipping in full. Same gate as
    :func:`task_forest`: a missing or unreadable board DB reads as ``{}`` and
    is never minted."""
    ids = [t for t in dict.fromkeys(task_ids or ()) if t]
    if not ids:
        return {}
    db_path = _board_db(board)
    if db_path is None:
        return {}
    kb = _kb()
    out: dict = {}
    try:
        with _kanban_conn().connect_closing(db_path=db_path) as conn:
            for tid in ids:
                rows = kb.list_comments(conn, tid)
                out[tid] = [
                    {"id": c.id, "author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in (rows[-limit:] if limit and limit > 0 else rows)
                ]
    except Exception:  # noqa: BLE001 — an unreadable board reads as no comments
        return {}
    return out


# ── busy guard: running cards, stop, resume ─────────────────────────────────

def _board_db(board: str):
    """The board's DB path, or None when it does not exist (never minted)."""
    try:
        kb = _kb()
        board = _check_slug(board)
    except TeamError:
        return None
    db_path = kb.kanban_db_path(board)
    return db_path if db_path.is_file() else None


def running_cards(board: str, assignee: Optional[str] = None) -> list:
    """Cards in ``running`` on *board* (optionally one assignee's), claimed
    or not: a claim-less running row is requeued and respawned within a tick."""
    db_path = _board_db(board)
    if db_path is None:
        return []
    kb = _kb()
    try:
        with _kanban_conn().connect_closing(db_path=db_path) as conn:
            tasks = kb.list_tasks(conn, status="running", assignee=assignee)
    except Exception:  # noqa: BLE001 — an unreadable board reads as no tasks
        return []
    return [{"id": t.id, "title": t.title, "assignee": t.assignee,
             "worker_pid": t.worker_pid} for t in tasks]


def stop_cards(board: str, ids, reason: str, *, signal_fn=None) -> dict:
    """Block each still-running card, then kill its worker after the commit.

    Block, not reclaim: reclaim requeues to ``ready`` and the dispatcher may
    respawn the card before the caller's change lands; an explicit block is
    sticky. Same write-then-kill order as the kanban dashboard's
    ``_set_status_direct``."""
    stopped, not_killed = [], []
    db_path = _board_db(board)
    if db_path is None:
        return {"stopped": stopped, "not_killed": not_killed}
    kb = _kb()
    kills = []
    with _kanban_conn().connect_closing(db_path=db_path) as conn:
        for tid in ids:
            task = kb.get_task(conn, tid)
            if task is None or task.status != "running":
                continue
            pid, lock = task.worker_pid, task.claim_lock
            if kb.block_task(conn, tid, reason=reason):
                stopped.append(tid)
                if pid:
                    kills.append((tid, pid, lock))
    for tid, pid, lock in kills:
        info = kb._terminate_reclaimed_worker(pid, lock, signal_fn=signal_fn)
        if not info.get("terminated"):
            not_killed.append(tid)
    return {"stopped": stopped, "not_killed": not_killed}


def resume_cards(board: str, ids) -> list:
    """Unblock the cards a stop blocked, so they re-run under the new setup."""
    resumed = []
    db_path = _board_db(board)
    if db_path is None or not ids:
        return resumed
    kb = _kb()
    with _kanban_conn().connect_closing(db_path=db_path) as conn:
        for tid in ids:
            task = kb.get_task(conn, tid)
            if task is not None and task.status == "blocked" \
                    and kb.unblock_task(conn, tid):
                resumed.append(tid)
    return resumed


# ── observe() ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Observed:
    """§4.3 ``_resolved`` in one object — computed, never stored (§4.3)."""
    board_exists: bool
    board_db_path: Optional[str]
    board_default_workdir: Optional[str]
    board_project_id: Optional[str]
    workdir_exists: bool
    workdir_is_git_repo: bool
    members: dict
    counts: dict
    managed_profile_count: int
    drift: list = field(default_factory=list)   # reconcile.diff() fills it (S9)

    def to_dict(self) -> dict:
        """Exactly the §4.3 field set, in order."""
        return {
            "board_exists": self.board_exists,
            "board_db_path": self.board_db_path,
            "board_default_workdir": self.board_default_workdir,
            "board_project_id": self.board_project_id,
            "workdir_exists": self.workdir_exists,
            "workdir_is_git_repo": self.workdir_is_git_repo,
            "members": self.members,
            "counts": self.counts,
            "managed_profile_count": self.managed_profile_count,
            "drift": self.drift,
        }


def observe(team: "model.Team") -> Observed:
    """The §4.3 ``_resolved`` snapshot for *team*. Never raises for a valid
    team whose world moved underneath it; the only raise surface is a
    registry-corrupt board slug, which the caller's envelope already maps."""
    kb = _kb()
    # Hazard §5.2.1 "Private symbols": validation via copied model.SLUG_RE.
    slug_valid = bool(team.board) and bool(model.SLUG_RE.match(team.board))
    board_present = bool(slug_valid and kb.board_exists(team.board))
    meta = kb.read_board_metadata(team.board) if slug_valid else {}

    # Hazard §5.2.1 "default_workdir inheritance is kind-scoped": we RECORD
    # it; drift logic (S9) must never infer task placement from it alone.
    workdir = os.path.expanduser(team.workdir or "")
    workdir_exists = bool(workdir) and os.path.isdir(workdir)
    git_repo = bool(workdir) and os.path.exists(os.path.join(workdir, ".git"))

    return Observed(
        board_exists=board_present,
        board_db_path=str(meta.get("db_path")) if board_present else None,
        board_default_workdir=meta.get("default_workdir"),
        board_project_id=meta.get("project_id"),
        workdir_exists=workdir_exists,
        workdir_is_git_repo=git_repo,
        members={m.profile: _member_view(m, team) for m in team.members},
        counts=task_counts(team.board, team.tenant or None),
        # Sprawl is measured, not hidden.
        managed_profile_count=sum(1 for m in team.members if m.managed),
        drift=[],
    )


def workdir_has_commit(workdir: str) -> bool:
    # The dispatcher branches every worktree task off HEAD
    # (`git worktree add -b wt/<id> <path> HEAD`), so a bare `git init` with
    # no commit fails spawn exactly like a plain directory does.
    import subprocess
    if not workdir or not os.path.isdir(workdir):
        return False
    try:
        r = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    # A workdir nested in another repo borrows that repo's HEAD, but the
    # dispatcher would then `git worktree add` the WHOLE outer repo at the
    # workdir itself — only a repo toplevel anchors <workdir>/.worktrees/<id>.
    return r.returncode == 0 and _git_toplevel(workdir) == os.path.realpath(workdir)


def _git_toplevel(path: str) -> str:
    """Real path of the repo toplevel containing *path*, or ``""``."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    top = r.stdout.strip() if r.returncode == 0 else ""
    return os.path.realpath(top) if top else ""


def _exclude_from_outer_repo(outer: str, workdir: str) -> None:
    """Best-effort: list a nested workdir repo in the outer repo's
    ``info/exclude`` so the outer repo's status stays clean."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", outer, "rev-parse", "--git-path",
                            "info/exclude"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return
        path = os.path.join(outer, r.stdout.strip())
        entry = "/" + os.path.relpath(os.path.realpath(workdir), outer) + "/"
        lines = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        if entry in lines:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except (OSError, subprocess.SubprocessError):
        return


def ensure_worktree_repo(workdir: str) -> bool:
    """Give a ``worktree`` team's *workdir* the HEAD its tasks branch off:
    ``git init`` when there is no repo, then commit whatever is already there
    so each worker's worktree starts with the project. True iff a commit was
    made; raises :class:`TeamError` when git refuses."""
    import subprocess
    if workdir_has_commit(workdir):
        return False

    def git(*args, check=True, timeout=60):
        r = subprocess.run(["git", "-C", workdir, *args],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise subprocess.SubprocessError(
                (r.stderr or r.stdout).strip()[-500:]
                or f"git {args[-1]} exited {r.returncode}")
        return r

    try:
        if not os.path.exists(os.path.join(workdir, ".git")):
            outer = _git_toplevel(workdir)
            git("init", "-q")
            if outer and outer != os.path.realpath(workdir):
                _exclude_from_outer_repo(outer, workdir)
        git("add", "-A", timeout=300)
        cfg = ["-c", "commit.gpgsign=false"]
        # A fresh container has no identity and `commit` would refuse; a
        # configured one is left alone.
        if git("config", "user.email", check=False).returncode != 0:
            cfg += ["-c", "user.name=hermes-agent-synergia",
                    "-c", "user.email=hermes-agent-synergia@localhost"]
        git(*cfg, "commit", "-q", "--allow-empty",
            "-m", "hermes-agent-synergia: initial snapshot")
    except (OSError, subprocess.SubprocessError) as exc:
        raise TeamError(
            E_TEAM_INVALID,
            f"could not initialize git repo in workdir {workdir}: {exc}",
            hint="workspace_policy worktree needs a git repo with at least "
                 "one commit.",
            remedy=f"git -C {workdir} init && git -C {workdir} add -A && "
                   f"git -C {workdir} commit --allow-empty -m init",
        ) from exc
    return True


def require_worktree_workdir(team: "model.Team") -> None:
    """Refuse a ``worktree`` team whose workdir cannot anchor a worktree —
    otherwise the card is created and dies as spawn_failed -> gave_up."""
    if team.workspace_policy not in ("worktree", "shared"):
        return
    workdir = os.path.expanduser(team.workdir or "")
    if workdir_has_commit(workdir):
        return
    raise TeamError(
        E_TEAM_DRIFT_UNFIXED,
        f"team {team.slug}: workdir {workdir or '(unset)'} is not a git repo "
        "with a commit; worktree tasks branch off HEAD and would fail to spawn",
        hint="workspace_policy worktree needs a git repo with at least one "
             f"commit; `hermes team sync {team.slug} --apply` initializes it",
        remedy=f"git -C {workdir} init && git -C {workdir} commit "
               "--allow-empty -m init",
    )


def prepare_worktree_workdir(team: "model.Team") -> None:
    """Task-create gate: init the repo a team created before create-time
    init never got, then refuse only what still cannot anchor a worktree
    (a missing workdir). Read-only callers use :func:`require_worktree_workdir`."""
    if team.workspace_policy in ("worktree", "shared"):
        workdir = os.path.expanduser(team.workdir or "")
        if os.path.isdir(workdir):
            ensure_worktree_repo(workdir)
    require_worktree_workdir(team)


# ── S10a read seam: the profile's installed skills (deviation 8) ───────────

def installed_skill_names(profile: str) -> list:
    """Every skill name installed in *profile*'s home, sorted, regardless of
    its disabled state — the universe ``skills.disabled`` is a complement of.

    Deliberate private-symbol use (hazard §5.2.1 "Private symbols"): the set
    only exists per-profile, and the substrate reaches it exactly this way —
    ``web_server_profiles._profile_scope`` retargets ``HERMES_HOME`` *and*
    rebinds ``skills_tool.SKILLS_DIR`` (bound at import time) for the call,
    then ``_find_all_skills(skip_disabled=True)`` scans that tree. No public
    surface takes a profile. Deviation 8's other half — writing the result —
    must NOT use ``skills_config.save_disabled_skills``: it ends in
    ``save_config()``, which re-serialises the whole 2 167-line commented
    document (:func:`write_profile_config_keys` is the only writer).
    """
    with _profile_scope()(profile):
        found = _skills_tool()._find_all_skills(skip_disabled=True)
    return sorted({str(s.get("name") or "").strip()
                   for s in found if isinstance(s, dict)} - {""})


def disabled_skills_for(profile: str, enabled) -> list:
    """The ``skills.disabled`` value for "this member gets *enabled*".

    There is no ``skills.enabled`` key anywhere in the substrate, so an
    enabled selection is stored as its COMPLEMENT over the installed set
    (§4.2 ``Member.skills``, deviation 8) — the same shape
    ``skills_config`` persists. ``ESSENTIAL_SKILLS`` is subtracted, exactly as
    :func:`hermes_cli.skills_config.save_disabled_skills` subtracts it: those
    names cannot be disabled from any surface. The writer subtracts them a
    second time, so the invariant holds whichever caller shows up.
    """
    keep = {str(name).strip() for name in (enabled or ()) if str(name).strip()}
    return sorted(set(installed_skill_names(profile)) - keep
                  - set(_essential_skills()))


# ── S8 write half (Appendix A) ─────────────────────────────────────────────
#
# ``write_profile_config_keys`` is the plugin's ONLY writer of another
# profile's ``config.yaml`` (§13: "Arbitrary config writes ... refuse").
# The five guards, in order:
#
#   1. refuse when the install is package-manager-managed   -> E_TEAM_MANAGED
#   2. fail closed on a corrupt/unreadable target           (never overwrite)
#   3. copy ``config.yaml.hermes-agent-synergia.bak`` before the FIRST modification
#   4. allow-list of nine dotted keys, nothing else
#   5. never write ``terminal.cwd`` unless absolute and non-sentinel (§2.6)
#
# The pipeline: ``set_hermes_home_override(profile_home)`` →
# ``read_user_config_raw`` (uncached, exact — hazard §5.2.1 "load_config()
# cache"; we never touch the ``load_config_readonly()`` cache) → our own
# nested setter → ONE atomic replace per file per run (§9.3 per-file
# batching: the function takes a dict of updates, never one key).
#
# The nested setter is LINE-SURGICAL by necessity. ``utils.atomic_yaml_write``
# (the step's literal pipeline word) re-serialises the whole document, which
# on the substrate's 2 167-line commented template would destroy every
# comment and rewrite every line — violating the writer's purpose ("touches only
# the keys it sets") and §9.3's idempotence (byte-identical unchanged
# files). We instead edit the original text in place and commit it through
# ``utils.atomic_write_text`` — the substrate's shared temp+fsync+rename
# primitive, i.e. the same atomicity guarantee, without the re-serialise.

# Guard 4 — the closed allow-list, in canonical write order (the sixth key is
# the §0.2/F1 addition: which marketplace plugins a profile loads; the last
# three are the S10a/deviation-8 addition — a member's skill set and pinned
# model — which is what §4.2 ``Member.skills`` / ``Member.provider`` project
# onto. Order is load-bearing: ``_apply_updates`` sorts by index, so the
# nested ``skills.disabled`` lands inside an existing ``skills:`` block (or
# creates one) BEFORE the two top-level scalar lines are appended at EOF).
WRITABLE_CONFIG_KEYS = (
    "terminal.cwd",
    "toolsets",
    "kanban.orchestrator_profile",
    "kanban.default_assignee",
    "kanban.max_in_progress_per_profile",
    "plugins.enabled",
    "skills.disabled",
    "model",
    "provider",
    # Section form of the pinned model: hermes reads provider/base_url only
    # from inside ``model:``, so a scalar pair cannot reach a custom endpoint.
    "model.default",
    "model.provider",
    "model.base_url",
)

# Keys whose value must be a list of non-empty strings (the writer renders
# block sequences; ``skills.disabled`` is the substrate's own shape — see
# :func:`disabled_skills_for`, there is no ``skills.enabled``).
_LIST_KEYS = ("toolsets", "plugins.enabled", "skills.disabled")

# S10a keys holding a model override pair. A ``provider`` with no ``model``
# in the SAME call is refused, mirroring the substrate's
# ``kanban_db._validate_model_override``: a bare provider would re-resolve the
# profile's model against another backend — the mismatch the override exists
# to kill.
_MODEL_KEY = "model"
_PROVIDER_KEY = "provider"
_MODEL_SECTION_KEYS = ("model.default", "model.provider", "model.base_url")

# Guard 3 — the backup name is fixed and obvious; ``cp -p`` semantics.
CONFIG_BAK_SUFFIX = ".hermes-agent-synergia.bak"

_LOG = logging.getLogger("hermes_agent_synergia")

# ``^section:`` — a section/key line owns column 0; we match the prefix so
# inline values (``toolsets: []``, ``terminal: {cwd: "."}``) are found too.
_TOP_RE = lambda name: re.compile(rf"^{name}:")  # noqa: E731


def _inline_value(line: str) -> Optional[str]:
    """Inline value text after ``key:``, or ``None`` when the line is bare /
    comment-only. Trailing ``# comment`` is stripped like the YAML spec's
    flow-comment rule (good enough for these six allow-listed shapes)."""
    rest = line.split(":", 1)[1] if ":" in line else ""
    rest = re.sub(r"[ \t]+#.*$", "", rest).strip()
    return rest or None


def _dump_scalar(value: Any) -> str:
    """One YAML scalar on one line (block style, unicode kept)."""
    text = yaml.safe_dump(value, default_flow_style=False, width=1_000_000,
                         allow_unicode=True).strip()
    if "\n" in text:  # multi-line scalar (newlines in a path ⇒ quote it)
        text = json.dumps(value)
    return text


def _render_key(key: str, value: Any, indent: str) -> list:
    """Render ``key: value`` as text lines at *indent* (sequences become
    block items one indent deeper — the substrate's own house style)."""
    if isinstance(value, (list, tuple)) and not value:
        # An EMPTY sequence has no block to render: ``key:`` alone reparses as
        # ``None`` and the reparse guard below would refuse the write outright.
        # Flow style is the substrate's own spelling of the same value
        # (``toolsets: []``) and the shape ``save_config`` emits for
        # ``skills.disabled: []`` — a member allowed every installed skill.
        return [f"{indent}{key}: {json.dumps(list(value))}\n"]
    if isinstance(value, (list, tuple)):
        lines = [f"{indent}{key}:\n"]
        lines.extend(f"{indent}  - {_dump_scalar(item)}\n" for item in value)
        return lines
    return [f"{indent}{key}: {_dump_scalar(value)}\n"]


def _block_end(lines: list, start: int) -> int:
    """First line after the block opened at *start* (first column-0 line)."""
    i = start + 1
    while i < len(lines) and lines[i][:1] in (" ", "\t"):
        i += 1
    return i


def _is_filler(text: str) -> bool:
    """A blank or comment line — no YAML value of its own, so it belongs to
    whichever block it sits inside."""
    stripped = text.strip()
    return not stripped or stripped.startswith("#")


def _value_resume(lines: list, start: int, key_indent: int) -> Optional[int]:
    """Index of the line that RESUMES the value after the filler run at
    *start*, or ``None`` when the run ends the value.

    Only a deeper line (or a same-indent sequence item) can resume it: the next
    shallower-or-equal real line is another key, and the comment/blank run in
    front of that key belongs to THAT key (that is how every section banner in
    the substrate template stays with its section)."""
    i = start
    while i < len(lines):
        text = lines[i]
        if _is_filler(text):
            i += 1
            continue
        indent = len(text) - len(text.lstrip(" \t"))
        dash = text.lstrip(" \t")
        if indent > key_indent or (
                indent == key_indent
                and (dash.startswith(("- ", "-\t")) or dash.rstrip("\r\n") == "-")):
            return i
        return None
    return None


def _value_end(lines: list, key_idx: int, key_indent: int) -> int:
    """First line after the mapping value that begins at *key_idx*: the key
    line plus any deeper-indented lines and same-indent ``- `` items.

    S10a generalisation: a blank/comment RUN is inside the value when the value
    RESUMES after it. The substrate's own templates separate a section's
    sub-keys with blank lines and comment banners, so stopping at the first
    blank line orphaned everything below the replacement — invalid YAML, and
    the reparse guard then refused the write outright, which made the S10a
    ``model`` key (a SECTION with ~300 commented sub-keys on the shipped
    template) impossible to write at all. A filler run that ends the value
    (next real line is another key) is left exactly where it is."""
    i = key_idx + 1
    while i < len(lines):
        text = lines[i]
        indent = len(text) - len(text.lstrip(" \t"))
        if indent > key_indent:
            i += 1
            continue
        # A block sequence may keep its items at the KEY's own indent (that
        # is what ``yaml.safe_dump`` emits, so it is the shape of every
        # config the substrate itself wrote): a same-column ``- `` line under
        # the key is part of the value, not the next key. Test the STRIPPED
        # text — comparing the raw prefix only ever matched column-0 lists,
        # which left a nested ``plugins.enabled`` replacement stranded above
        # its own orphaned items (invalid YAML, caught by the reparse guard
        # below, but only after the edit was pointless).
        dash = text.lstrip(" \t")
        if indent == key_indent and (dash.startswith(("- ", "-\t"))
                                     or dash.rstrip("\r\n") == "-"):
            i += 1
            continue
        if _is_filler(text):
            resume = _value_resume(lines, i, key_indent)
            if resume is not None:
                i = resume          # the run is INSIDE the value: skip into it
                continue
        break
    return i


def _set_in_text(lines: list, section: Optional[str], key: str, value: Any) -> list:
    """Replace (or create) one dotted key inside *lines*, byte-preserving
    everywhere else. ``section`` is ``None`` for the top-level ``toolsets``.

    Deliberately NOT ruamel: per-file batching (§9.3) demands ONE replace
    for a whole dict of updates, and ``utils.atomic_roundtrip_yaml_update``
    is a private per-key writer that would mean one write (and one mtime
    bump) per key."""
    top_key = section or key
    top_re = _TOP_RE(top_key)
    idx = next((i for i, t in enumerate(lines) if top_re.match(t.rstrip("\n"))), None)
    if idx is None:  # create the section at EOF (one append ⇒ one hunk)
        out = list(lines)
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        if out and out[-1].strip():
            out.append("\n")
        if section is None:
            out.extend(_render_key(key, value, ""))
        else:
            out.append(f"{section}:\n")
            out.extend(_render_key(key, value, "  "))
        return out

    block_end = _block_end(lines, idx)
    inline = _inline_value(lines[idx].rstrip("\n"))
    if section is None:
        return (lines[:idx] + _render_key(key, value, "")
                + lines[_value_end(lines, idx, 0):])
    if inline and not inline.startswith(("{", "[")):
        # A section line carrying a scalar (``model: gpt``) becomes a block
        # holding the key. (A flow-mapping section line,
        # ``terminal: {cwd: "."}``, is merged below instead.)
        return (lines[:idx] + [f"{section}:\n"] + _render_key(key, value, "  ")
                + lines[_value_end(lines, idx, 0):])
    if inline:  # flow mapping on the section line: merge, then re-block it
        try:
            merged = yaml.safe_load(inline)
        except Exception:  # noqa: BLE001 — junk inline value: replace, don't merge
            merged = None
        if isinstance(merged, dict):
            merged[key] = value
            out = [f"{section}:\n"]
            out.extend(_render_key(k, v, "  ") for k, v in merged.items())
            return lines[:idx] + out + lines[block_end:]
        return (lines[:idx] + [f"{section}:\n"] + _render_key(key, value, "  ")
                + lines[_value_end(lines, idx, 0):])
    # section present: find or add the nested key inside the block. Prefix
    # match with a lookahead so inline values (``cwd: "."  # comment``) are
    # found and replaced whole, and ``cwdx:`` never matches ``cwd:``.
    sub_re = re.compile(r"^([ \t]+)" + re.escape(key) + r"(?=:)")
    for i in range(idx + 1, block_end):
        m = sub_re.match(lines[i].rstrip("\n"))
        if m:
            return (lines[:i] + _render_key(key, value, m.group(1))
                    + lines[_value_end(lines, i, len(m.group(1))):])
    # key absent inside an existing section: insert at the block's content end
    indent = "  "
    for i in range(idx + 1, block_end):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            indent = re.match(r"^[ \t]+", lines[i]).group(0)
            break
    insert_at = idx + 1
    for i in range(idx + 1, block_end):
        if lines[i].strip():
            insert_at = i + 1
    return lines[:insert_at] + _render_key(key, value, indent) + lines[insert_at:]


def _apply_updates(text: str, updates: dict) -> str:
    """Apply *updates* to the document text in canonical allow-list order."""
    lines = text.splitlines(keepends=True)
    for dotted in sorted(updates, key=WRITABLE_CONFIG_KEYS.index):
        parts = dotted.split(".")
        section, key = (None, parts[0]) if len(parts) == 1 else (parts[0], parts[1])
        lines = _set_in_text(lines, section, key, updates[dotted])
    return "".join(lines)


def _parsed_value_at(parsed: dict, dotted: str) -> Any:
    node: Any = parsed
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def write_profile_config_keys(profile: str, updates: dict) -> None:
    """Surgically write *updates* (dotted allow-list keys) into ``profile``'s
    ``config.yaml``. The plugin's only config writer; every guard applies.

    Raises :class:`TeamError`: ``E_TEAM_MANAGED`` (guard 1),
    ``E_TEAM_INVALID`` (unknown key / bad value / refused target),
    ``E_TEAM_NO_PROFILE`` (tombstoned home). A no-op update set writes
    nothing at all (§9.3 idempotence); otherwise exactly ONE atomic replace
    happens after the ``.hermes-agent-synergia.bak`` copy (guard 3).
    """
    import hermes_constants

    # Guard 1 — a package-manager-managed install is the host's, not ours.
    if _config().is_managed():
        raise TeamError(
            E_TEAM_MANAGED,
            f"refusing to write {profile!r}'s config: this Hermes install is "
            "package-manager-managed (read-only)",
            hint="Guard 1: hermes_cli.config.is_managed() is true.",
            remedy="Manage profile configs through your package manager.",
        )

    if not isinstance(updates, dict):
        raise TeamError(E_TEAM_INVALID, "updates must be a dict of dotted keys",
                        hint="write_profile_config_keys takes a DICT of updates "
                             "— one atomic replace per file per run (§9.3).",
                        remedy="Pass write_profile_config_keys(profile, {'terminal.cwd': ...}).")
    if not updates:
        return

    # Guard 4 — allow-list only. Validate EVERY key before any mutation
    # (all-or-nothing: one bad key writes nothing).
    for dotted in updates:
        if dotted not in WRITABLE_CONFIG_KEYS:
            raise TeamError(
                E_TEAM_INVALID,
                f"refusing to write config key {dotted!r}: not in the allow-list",
                hint=f"Guard 4 — writable keys are exactly: "
                     f"{', '.join(WRITABLE_CONFIG_KEYS)}.",
                remedy="hermes team sync manages exactly these keys.",
            )
    # Value shapes we will have to render — reject nonsense before the disk.
    # Values are SANITISED INTO A NEW DICT, never in place: the caller's dict
    # is not ours to mutate, and the ESSENTIAL_SKILLS subtraction has to reach
    # the idempotence filter and the renderer below, which read ``updates``.
    clean: dict = {}
    for dotted, value in updates.items():
        if dotted in _LIST_KEYS:
            if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item for item in value):
                raise TeamError(
                    E_TEAM_INVALID,
                    f"{dotted} must be a list of non-empty strings, got {value!r}",
                    hint="The writer renders block sequences; anything else is a bug.",
                    remedy="Check the team blueprint's toolset/plugin lists.")
            if dotted == "skills.disabled":
                # Deviation 8: the value is a DENY list, so a member's skill
                # selection arrives already complemented (see
                # :func:`disabled_skills_for`). ESSENTIAL_SKILLS is subtracted
                # here too — the same veto
                # ``skills_config.save_disabled_skills`` applies — so no caller
                # can smuggle ``hermes-agent`` into a member's deny list.
                essential = set(_essential_skills())
                value = [item for item in value if item not in essential]
        elif dotted in (_MODEL_KEY, _PROVIDER_KEY) + _MODEL_SECTION_KEYS:
            # The model-override pair: a non-empty scalar each (a bool would
            # render as ``model: true``, a container would not round-trip).
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) \
                    or not str(value).strip():
                raise TeamError(
                    E_TEAM_INVALID,
                    f"{dotted} must be a non-empty scalar, got {value!r}",
                    hint="Deviation 8 pins one model per member; the value is a "
                         "scalar model/provider id, never a mapping or a list.",
                    remedy="Set the member's model on the team/blueprint, e.g. "
                          "hermes team member set <profile> --model <id>.")
        elif dotted == "terminal.cwd":
            # Guard 5 (§2.6, hazard "terminal.cwd sentinels"): absolute, and
            # never one of the sentinel strings — the substrate's own
            # set_config_values applies the same veto, but we fail with OUR
            # code so the caller can map it.
            cwd = value
            if not isinstance(cwd, str) or not cwd.strip():
                raise TeamError(
                    E_TEAM_INVALID,
                    f"terminal.cwd must be a non-empty absolute path, got {cwd!r}",
                    hint="Sentinels ('', '.', './', 'auto', 'cwd') and relative "
                         "paths are never writable (guard 5, §2.6).",
                    remedy="Point the team at an absolute workdir: hermes team create --workdir /abs/path")
            if cwd.strip() in model.TERMINAL_CWD_SENTINELS or \
                    not os.path.isabs(os.path.expanduser(cwd.strip())):
                raise TeamError(
                    E_TEAM_INVALID,
                    f"terminal.cwd={cwd!r} is not an absolute, non-sentinel path",
                    hint="Guard 5 / §2.6: a sentinel cwd means 'inherit', "
                         "which the plugin must never write.",
                    remedy="Use an absolute workdir (hermes team update --workdir /abs/path).")
        clean[dotted] = value
    # Deviation 8, mirroring the substrate's own
    # ``kanban_db._validate_model_override``: a bare ``provider`` would
    # re-resolve the profile's model against another backend — exactly the
    # mismatch the override pair exists to kill — so the two travel together
    # and the pair is checked while NOTHING has been written yet.
    if _PROVIDER_KEY in clean and _MODEL_KEY not in clean:
        raise TeamError(
            E_TEAM_INVALID,
            f"{_PROVIDER_KEY!r} requires {_MODEL_KEY!r} in the same write",
            hint="Deviation 8 / kanban_db._validate_model_override: a provider "
                 "override without a model override re-resolves the profile's "
                 "own model against another backend.",
            remedy="Write both together: {'model': ..., 'provider': ...}.")
    if any(k in clean for k in _MODEL_SECTION_KEYS[1:]) \
            and "model.default" not in clean:
        raise TeamError(
            E_TEAM_INVALID,
            "'model.provider'/'model.base_url' require 'model.default' in "
            "the same write",
            hint="Same rule as the scalar pair: a provider or endpoint without "
                 "its model re-resolves the profile's own model elsewhere.",
            remedy="Write them together: {'model.default': ..., "
                   "'model.provider': ...}.")
    updates = clean

    # The profile path resolves BEFORE any home override (hazard §5.2.1
    # "plugin_data_dir()/profile resolution": under an override
    # ``get_profile_dir`` would point inside the profile itself).
    try:
        path = profile_config_path(profile)
    except Exception as exc:  # noqa: BLE001 — hostile profile name
        raise TeamError(E_TEAM_INVALID, f"unusable profile name {profile!r}: {exc}",
                        hint="Profile names go through the substrate's validator.",
                        remedy="Create the profile first: hermes team create ...")
    profile_home = path.parent

    # Guard 7 (listed last by the step, but it must precede guard 3 because
    # guard 3 touches the filesystem): never resurrect a tombstoned profile
    # (§9.3 refusals). Public hermes_constants API; a ValueError for
    # root/default names is the substrate's own veto, surfaced.
    try:
        hermes_constants.assert_named_profile_home_live(profile_home)
    except ValueError as exc:
        raise TeamError(E_TEAM_INVALID, f"refusing to write {profile!r}: {exc}",
                        hint="assert_named_profile_home_live vetoes root/default.",
                        remedy="Team members are named profiles.")
    except FileNotFoundError as exc:
        raise TeamError(
            E_TEAM_NO_PROFILE,
            f"refusing to write {profile!r}: {exc}",
            hint="The profile home is missing or tombstoned; writing would "
                 "resurrect a deliberately deleted profile (§9.3).",
            remedy="hermes team sync re-provisions managed members.",
        )

    # Guard 2 — fail closed on a corrupt/unreadable target (missing is fine:
    # that is the create path; the substrate treats a missing stat as OK).
    try:
        _config().require_readable_config_before_write(path)
    except RuntimeError as exc:
        raise TeamError(
            E_TEAM_INVALID,
            f"refusing to write {str(path)!r}: {exc}",
            hint="Guard 2: a corrupt or unreadable config is never overwritten.",
            remedy="Repair or remove the profile, then hermes team sync.",
        )

    # Guard 6 — the pipeline, under a context-local home override so the
    # substrate resolves everything inside this profile home (§5.2.1 cache
    # hazard: read_user_config_raw is uncached and exact).
    token = hermes_constants.set_hermes_home_override(profile_home)
    try:
        current = _config().read_user_config_raw(path)   # {} when absent
        # §9.3 idempotence: drop keys already at the desired value; if
        # nothing is left, this run writes NOTHING (no mtime change).
        changed = {d: v for d, v in updates.items()
                   if _parsed_value_at(current, d) != (list(v) if isinstance(v, list) else v)}
        if not changed:
            _LOG.info("hermes-agent-synergia: %s config already current (%s) — no write",
                      profile, ", ".join(sorted(updates)))
            return
        original = path.read_bytes() if path.is_file() else None
        text = original.decode("utf-8") if original is not None else ""
        new_text = _apply_updates(text, changed)
        # Belt and braces: the surgical text must PARSE BACK to the intent
        # before anything is committed (fail closed, still zero writes).
        try:
            reparsed = yaml.safe_load(new_text) or {}
        except yaml.YAMLError as exc:
            # Fail closed with OUR code: a surgical edit that would not parse
            # never reaches the disk, and the caller gets an envelope instead
            # of a raw parser traceback.
            raise TeamError(
                E_TEAM_INVALID,
                f"internal: surgical edit of {', '.join(sorted(changed))} would "
                f"not re-parse ({exc.__class__.__name__}); nothing was written",
                hint="The writer declined to corrupt the file (guard 2).",
                remedy="Report this config shape; the writer needs a fallback.",
            ) from exc
        for dotted, want in changed.items():
            got = _parsed_value_at(reparsed, dotted)
            if got != (list(want) if isinstance(want, list) else want):
                raise TeamError(
                    E_TEAM_INVALID,
                    f"internal: surgical edit of {dotted!r} would yield {got!r}, not {want!r}",
                    hint="The text editor declined to corrupt the file; nothing was written.",
                    remedy="Report this config shape; the writer needs a fallback.")
        # Guard 3 — copy the pre-change file BEFORE the first modification,
        # and never overwrite an existing .bak (it holds the pristine bytes).
        if original is not None:
            bak = path.with_name(path.name + CONFIG_BAK_SUFFIX)
            if not bak.exists():
                bak.write_bytes(original)
        _utils().atomic_write_text(path, new_text, preserve_mode=True)
        # §13 audit: every applied change is logged at INFO, naming subject
        # and keys (the drift record itself is rendered by the cli layer).
        _LOG.info("hermes-agent-synergia: applied to %r: %s (backup: %s)",
                  profile, ", ".join(sorted(changed)),
                  path.name + CONFIG_BAK_SUFFIX if original is not None else "n/a (created)")
    finally:
        hermes_constants.reset_hermes_home_override(token)
