"""reconcile.py — desired vs observed -> ``[DriftItem]`` (spec §9).

S9 implements the **diff half** only: :func:`diff` is pure read — it
mutates nothing. ``apply`` (S10) is the only code that acts on items, and
the flag-gating of ``--reinstantiate`` / ``--link-env`` /
``--create-board`` / ``--create-workdir`` is *apply's* concern: per the
§9.2 table these items carry ``auto_fixable: False`` in the diff, and the
remedy string names the flag that lifts the gate (§9.3 "Instance creation
is never implicit").

Layering (§5.2, enforced by the import-lint test): this module imports
**no** ``hermes_*`` — every Hermes-state read goes through
:mod:`resolve` (the only reader), and the plugin's own modules
:mod:`catalog` / :mod:`storage` for catalog resolution and cross-team
ownership queries. The one non-Hermes filesystem touch is
``AGENTS.md`` inside the team's own ``workdir`` — plugin state, not
Hermes state.

The §9.2 catalogue is implemented row for row. Deliberately **not**
implemented, and asserted absent (§9.2 "Deliberately not a check"):
anything comparing ``SOUL.md``, ``skills/`` or the body of
``config.yaml`` against the blueprint. Once instantiated, the instance
belongs to its operator.

Check ids, severities and ``auto_fixable`` below mirror §9.2 exactly;
``info``-severity *skip* items (terminal.cwd / capacity ownership,
shared_board workdir) exist so a dry run can explain why a write was NOT
made ("logs ``info: terminal.cwd owned by team <other>``").

S10 adds the **apply half**: :func:`apply` walks the §9.2 catalogue in
§9.3's order (board → workdir → project → profile instances → per-member
config), batches every key destined for one ``config.yaml`` into exactly
**one** :func:`resolve.write_profile_config_keys` call per file, records
per-item ``applied: false`` on failure without aborting the run (§9.3
"Partial failure"), surfaces the §9.4 restart notice verbatim whenever a
profile config changed, and returns an :class:`ApplyReport` whose
``exit_code`` is 4 when unfixable drift remains (§8.2).

Neither ``--reinstantiate`` nor ``--link-env`` is implied by ``--apply``
(§9.3): the gated checks (``board.exists`` / ``workdir.exists`` /
``member.instance_missing`` / ``member.env_link``) are applied only when
their flag is passed.

Layering note: board/profile/project *writes* go through the sibling
module's own lazy substrate accessors (``resolve._kb()``,
``resolve._profiles()``, ``resolve._projects_db()``) so this module keeps
its promise of importing **no** ``hermes_*`` anywhere — the single-reader
wall stays intact, apply just borrows resolve's door.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path
from typing import Any, Optional

from . import catalog, catalog_store, model, provision, resolve, storage
from .errors import E_TEAM_CAPACITY_CONFLICT, E_TEAM_INVALID, TeamError

_LOG = logging.getLogger("hermes_agent_synergia")     # same logger name as resolve (S13 audit)

# F1: the plugin's own name — the F1 symlink basename and the
# ``plugins.enabled`` entry both key off it.
PLUGIN_NAME = "hermes-agent-synergia"

SEVERITIES = ("error", "warn", "info")


# ── §9.1 DriftItem ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DriftItem:
    """§9.1 verbatim: stable check id, typed severity, human remedy."""
    check: str          # stable id, e.g. "member.terminal_cwd"
    severity: str       # error | warn | info
    subject: str        # "profile:qa" | "board:project-x" | "team:project-x"
    observed: Any = None
    desired: Any = None
    auto_fixable: bool = False
    remedy: str = ""    # human sentence, or the exact command for manual items

    def to_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity,
                "subject": self.subject, "observed": self.observed,
                "desired": self.desired, "auto_fixable": self.auto_fixable,
                "remedy": self.remedy}


def _item(check: str, severity: str, subject: str, *, observed: Any = None,
          desired: Any = None, auto_fixable: bool = False,
          remedy: str = "") -> DriftItem:
    return DriftItem(check=check, severity=severity, subject=subject,
                     observed=observed, desired=desired,
                     auto_fixable=auto_fixable, remedy=remedy)


# ── cross-team ownership ────────────────────────────────────────────────────

def _primary_owner(reg, profile: str, exclude_slug: str) -> Optional[str]:
    """Slug of another team for which *profile* is ``primary``, if any."""
    for slug, t in reg.teams.items():
        if slug == exclude_slug:
            continue
        for m in t.members:
            if m.profile == profile and m.primary:
                return str(slug)
    return None


def _in_catalog(blueprint: str) -> bool:
    """Catalog membership for the *desired* side (dangling members have no
    observed block to read ``blueprint_in_catalog`` from). Never raises."""
    try:
        catalog.get(str(blueprint))
        return True
    except Exception:  # noqa: BLE001 — E_TEAM_NO_BLUEPRINT / catalog down ⇒ unknown
        return False


def _blueprint_description(blueprint: str) -> str:
    try:
        return str(catalog.get(str(blueprint)).description or "")
    except Exception:  # noqa: BLE001
        return ""


# ── diff ─────────────────────────────────────────────────────────────────────

def diff(team, observed=None) -> list:
    """Every §9.2 check for *team* against *observed*
    (``resolve.observe(team)`` when not given). Read-only, ordered
    (members in roster order, then board, workdir, orchestrator, project,
    tenant, policy, catalog). Returns ``[DriftItem]``."""
    if observed is None:
        observed = resolve.observe(team)
    reg = storage.load_or_empty()
    items: list = []

    workdir = os.path.expanduser(team.workdir or "")
    team_subject = f"team:{team.slug}"
    board_subject = f"board:{team.board}"
    member_profiles = {m.profile for m in team.members}

    # ── members ──────────────────────────────────────────────────────────────
    for m in team.members:
        subject = f"profile:{m.profile}"
        view = (observed.members or {}).get(m.profile) or {}
        exists = bool(view.get("exists"))
        tomb = resolve.profile_tombstoned(m.profile)

        if tomb:
            # §9.2: never force past a tombstone. This item OWNS the
            # member — exists/instance_missing would print a contradicting
            # remedy ("hermes profile create" vs. "never force").
            items.append(_item(
                "member.instance_tombstoned", "error", subject,
                observed="tombstone marker present", desired="live or absent",
                remedy="hermes profile create <name> (after clearing the "
                       "marker deliberately), then re-run sync — or rename "
                       "the member. Never force past a tombstone",
            ))
            continue

        if not exists:
            # Dangling reference: the two §9.2 rows whose detect condition
            # holds (exists is false), plus provenance. The two-key
            # dangling view carries no config facts, so the per-
            # config checks below are simply not evaluable.
            items.append(_item(
                "member.exists", "error", subject,
                observed="profile missing", desired="profile present",
                remedy=f"hermes profile create {m.profile}  (or "
                       f"hermes team remove-member {team.slug} {m.profile})",
            ))
            if m.managed:
                # §9.3: auto-fixable but flag-gated — a plain sync must
                # never create an agent.
                items.append(_item(
                    "member.instance_missing", "error", subject,
                    observed=f"managed instance of {m.blueprint or '?'} missing",
                    desired="re-instantiated from blueprint",
                    remedy=f"hermes team sync {team.slug} --apply "
                           "--reinstantiate",
                ))
            if m.blueprint and not _in_catalog(m.blueprint):
                items.append(_item(
                    "member.blueprint_unknown", "info", subject,
                    observed=f"blueprint {m.blueprint!r} not in catalog",
                    desired=None,
                    remedy="provenance only — the pack moved or was removed; "
                           "the profile itself is fine",
                ))
            continue

        # ── provenance only — the pack moved, the profile is fine ───────
        if m.blueprint and not view.get("blueprint_in_catalog"):
            items.append(_item(
                "member.blueprint_unknown", "info", subject,
                observed=f"blueprint {m.blueprint!r} not in catalog",
                desired=None,
                remedy="provenance only — the pack moved or was removed; "
                       "the profile itself is fine",
            ))

        # ── F1: the plugin must be loadable AND loaded in this profile ──
        link = resolve.profile_home(m.profile) / "plugins" / PLUGIN_NAME
        # is_dir() follows symlinks — a link (or bind-mounted dir) that
        # lands on a real directory satisfies scan_directory; a missing or
        # dangling entry means every policy tier of §6 is absent.
        if not link.is_dir():
            state = "missing"
            try:
                if link.is_symlink():
                    state = f"dangling symlink -> {os.readlink(link)}"
                elif link.exists():
                    state = "exists but is not a directory"
            except OSError:
                pass
            items.append(_item(
                "member.plugin_link", "error", subject,
                observed=state, desired=f"plugins/{PLUGIN_NAME} symlink",
                remedy=f"hermes team sync {team.slug} --apply   # (re)creates "
                       f"the per-profile plugin symlink (F1)",
                auto_fixable=True,
            ))
        try:
            cfg = resolve.read_profile_config(m.profile)
        except Exception:  # noqa: BLE001 — mirror of resolve._member_view:
            # a corrupt config degrades to {} here so the diff still runs;
            # the WRITE side (writer guard 2) is what refuses and reports it.
            cfg = {}
        enabled = (cfg.get("plugins") or {}).get("enabled") or []
        if PLUGIN_NAME not in enabled:
            items.append(_item(
                "member.plugin_enabled", "error", subject,
                observed=list(enabled), desired=list(enabled) + [PLUGIN_NAME],
                remedy=f"hermes team sync {team.slug} --apply   # appends "
                       f"{PLUGIN_NAME!r} to plugins.enabled (F1)",
                auto_fixable=True,
            ))

        # ── plane 1: terminal.cwd — owned by the profile's primary team ──
        if m.primary:
            if view.get("terminal_cwd") != workdir:
                # sentinels already normalise to None in the view (S7):
                # terminal.cwd "." counts as UNSET, i.e. drift (§2.6).
                items.append(_item(
                    "member.terminal_cwd", "error", subject,
                    observed=view.get("terminal_cwd"), desired=workdir,
                    remedy=f"hermes team sync {team.slug} --apply   # writes "
                           f"terminal.cwd via the surgical writer",
                    auto_fixable=True,
                ))
        else:
            owner = _primary_owner(reg, m.profile, team.slug)
            # The literal log line goes in `observed` — that is what the
            # dry run prints to explain the missing write.
            items.append(_item(
                "member.terminal_cwd", "info", subject,
                observed=(f"owned by team {owner}" if owner
                          else "no primary team"),
                desired=None,
                remedy=(f"plane 1 skipped here — team {owner} owns "
                        "terminal.cwd for this profile" if owner else
                        f"hermes team add-member {team.slug} {m.profile} "
                        "--primary   # a profile with no "
                        "primary team has plane 1 skipped entirely"),
            ))

        # ── capacity: the same ownership discipline ─────────────────────
        if m.capacity is not None:
            if not m.primary:
                owner = _primary_owner(reg, m.profile, team.slug)
                items.append(_item(
                    "member.capacity", "info", subject,
                    observed=(f"owned by team {owner}" if owner
                              else "no primary team"),
                    desired=None,
                    remedy=(f"capacity skipped here — team "
                            f"{owner} owns this profile's kanban keys"
                            if owner else
                            f"hermes team add-member {team.slug} "
                            f"{m.profile} --primary"),
                ))
            elif view.get("max_in_progress_per_profile") != m.capacity:
                items.append(_item(
                    "member.capacity", "info", subject,
                    observed=view.get("max_in_progress_per_profile"),
                    desired=m.capacity,
                    remedy=f"hermes team sync {team.slug} --apply   # writes "
                           "kanban.max_in_progress_per_profile",
                    auto_fixable=True,
                ))

        # ── toolsets: `team` everywhere, plus `kanban` for the orchestrator ──
        required = ["team"] + (["kanban"] if m.profile == team.orchestrator
                               else [])
        current = list(view.get("toolsets") or [])
        missing = [t for t in required if t not in current]
        if missing:
            items.append(_item(
                "member.toolsets", "warn", subject,
                observed=current, desired=current + missing,
                remedy=f"hermes team sync {team.slug} --apply   # appends "
                       f"{'/'.join(missing)} to toolsets",
                auto_fixable=True,
            ))

        # ── descriptions: only-when-empty, both flavours (F6) ────────────
        if view.get("description_empty"):
            role_sentence = (f"{team.display_name or team.slug} "
                             f"{m.role} (team {team.slug})")
            items.append(_item(
                "member.description", "warn", subject,
                observed="", desired=role_sentence,
                remedy=f"hermes profile update {m.profile} --description "
                       "…   # or hermes team sync --apply; the plugin never "
                       "overwrites a non-empty description (§9.2)",
                auto_fixable=True,
            ))
            if m.blueprint:
                bp_desc = _blueprint_description(m.blueprint)
                if bp_desc:
                    items.append(_item(
                        "member.profile_meta", "warn", subject,
                        observed="", desired=bp_desc,
                        remedy=f"hermes team sync {team.slug} --apply   # "
                               f"adopts the {m.blueprint!r} blueprint "
                               "description (description_auto stays False)",
                        auto_fixable=True,
                    ))

        # ── seeded .env placeholder -> symlink (flag-gated) ──────────────
        if view.get("env_is_placeholder") and (resolve.root_path() / ".env").is_file():
            items.append(_item(
                "member.env_link", "warn", subject,
                observed="seeded placeholder .env",
                desired=f"symlink to {str(resolve.root_path() / '.env')}",
                remedy=f"hermes team sync {team.slug} --apply --link-env   "
                       "# never copies a secret, links it (F7 c3)",
            ))

    # ── board ────────────────────────────────────────────────────────────────
    if not observed.board_exists:
        items.append(_item(
            "board.exists", "error", board_subject,
            observed="no board", desired=f"board {team.board!r}",
            remedy=f"hermes team sync {team.slug} --apply --create-board",
        ))
    else:
        meta = resolve.board_meta(team.board)
        # default_workdir: skipped when shared_board — emitted as an
        # info so the dry run explains the missing write.
        if team.shared_board:
            items.append(_item(
                "board.default_workdir", "info", board_subject,
                observed=meta.get("default_workdir"), desired=None,
                remedy="skipped: shared_board is true — another team may "
                       "own this board's defaults",
            ))
        elif meta.get("default_workdir") != workdir:
            items.append(_item(
                "board.default_workdir", "error", board_subject,
                observed=meta.get("default_workdir"), desired=workdir,
                remedy=f"hermes team sync {team.slug} --apply   # "
                       "kb.write_board_metadata(default_workdir=…)",
                auto_fixable=True,
            ))
        label = model.board_label(team)
        if meta.get("name") != label:
            items.append(_item(
                "board.display", "info", board_subject,
                observed=meta.get("name"), desired=label,
                remedy=f"hermes team sync {team.slug} --apply   # keeps "
                       "board.json name in step with display_name (board)",
                auto_fixable=True,
            ))
        found = resolve.project_for(team)
        mode = str(getattr(team.project, "mode", "none") or "none")
        if mode != "none" and found is not None \
                and meta.get("project_id") != found.get("id"):
            items.append(_item(
                "board.project_id", "warn", board_subject,
                observed=meta.get("project_id"), desired=found.get("id"),
                remedy=f"hermes team sync {team.slug} --apply   # links "
                       f"board {team.board} to project {found.get('slug')}",
                auto_fixable=True,
            ))
        # I1 on the read side: a foreign claim without shared_board on
        # both sides is an operator decision, never a fix.
        for slug, t in reg.teams.items():
            if slug == team.slug or t.board != team.board:
                continue
            if team.shared_board and bool(getattr(t, "shared_board", False)):
                continue
            items.append(_item(
                "board.exclusivity", "error", board_subject,
                observed=f"also claimed by team {slug!r}", desired=team.slug,
                remedy=f"give one team its own board, or set "
                       f"shared_board: true on both (I1)",
            ))

    # ── workdir ──────────────────────────────────────────────────────────────
    if not observed.workdir_exists:
        items.append(_item(
            "workdir.exists", "error", team_subject,
            observed=workdir or "(unset)", desired="directory present",
            remedy=f"hermes team sync {team.slug} --apply --create-workdir",
        ))
    elif (team.workspace_policy in ("worktree", "shared")
          and not resolve.workdir_has_commit(workdir)):
        # `worktree` teams dispatch per-task branches off HEAD, so a
        # `.git` without a commit is as broken as no repo. apply inits the
        # repo and snapshots what is already there.
        items.append(_item(
            "workdir.git", "error", team_subject,
            observed="no git repo with a commit", desired="git repo with HEAD",
            remedy=f"hermes team sync {team.slug} --apply",
            auto_fixable=True,
        ))
    if observed.workdir_exists and not (Path(workdir) / "AGENTS.md").is_file():
        items.append(_item(
            "workdir.agents_md", "info", team_subject,
            observed="no AGENTS.md", desired=str(Path(workdir) / "AGENTS.md"),
            remedy="hermes team sync <slug> --apply   # only when the "
                   "sync.write_agents_md setting is true (§9.2)",
            auto_fixable=bool(team.sync.write_agents_md),
        ))

    # ── orchestrator ─────────────────────────────────────────────────────────
    if team.orchestrator and team.orchestrator not in member_profiles:
        items.append(_item(
            "orchestrator.member", "error", team_subject,
            observed=team.orchestrator, desired="a member profile",
            remedy=f"hermes team add-member {team.slug} "
                   f"{team.orchestrator}   # I3: the orchestrator is a member",
        ))
    elif team.orchestrator and (observed.members or {}).get(
            team.orchestrator, {}).get("exists"):
        try:
            ocfg = resolve.read_profile_config(team.orchestrator)
        except Exception:  # noqa: BLE001 — mirror; the writer refuses instead
            ocfg = {}
        okan = ocfg.get("kanban") or {}
        want = {"kanban.orchestrator_profile": team.orchestrator,
                "kanban.default_assignee": team.orchestrator}
        got = {"kanban.orchestrator_profile": okan.get("orchestrator_profile"),
               "kanban.default_assignee": okan.get("default_assignee")}
        if got != want:
            items.append(_item(
                "orchestrator.config", "warn", f"profile:{team.orchestrator}",
                observed=got, desired=want,
                remedy=f"hermes team sync {team.slug} --apply   # one atomic "
                       "config write sets both kanban keys (§9.3)",
                auto_fixable=True,
            ))

    # ── project ──────────────────────────────────────────────────────────────
    mode = str(getattr(team.project, "mode", "none") or "none")
    if mode != "none" and resolve.project_for(team) is None:
        creating = mode == "create"
        items.append(_item(
            "project.resolves", "info", team_subject,
            observed=f"mode {mode!r}, no row in projects.db",
            desired=f"project {getattr(team.project, 'slug', None) or getattr(team.project, 'id', None)}",
            remedy=(f"hermes team sync {team.slug} --apply   # mode create "
                    "lets sync mint the projects.db row"
                    if creating else
                    "create the project first: hermes project create "
                    f"{getattr(team.project, 'slug', '<slug>')} --name …"),
            auto_fixable=creating,   # §9.2: ⚠ create only
        ))

    # ── tenant ───────────────────────────────────────────────────────────────
    # Unstamped = on this board but not under our tenant. Reliable only on
    # a dedicated board (our board, our tasks); on a shared board a
    # surplus is another team's and must not nag (no fix either way).
    if observed.board_exists and not team.shared_board:
        total = sum(resolve.task_counts(team.board, None).values())
        mine = sum((observed.counts or {}).values())
        if total > mine:
            items.append(_item(
                "tenant.unstamped", "warn", board_subject,
                observed=f"{total - mine} task(s) with no tenant stamp",
                desired=team.tenant or None,
                remedy=f"hermes team adopt {team.slug} --backfill-tenant",
            ))

    # ── policy hook (host install) ───────────────────────────────────────────
    if str(getattr(team.policy, "mode", "off") or "off") != "off":
        root_cfg = resolve.read_profile_config("default")
        root_enabled = (root_cfg.get("plugins") or {}).get("enabled") or []
        if PLUGIN_NAME not in root_enabled:
            items.append(_item(
                "policy.hook", "warn", team_subject,
                observed=f"root plugins.enabled={list(root_enabled)}",
                desired="contains hermes-agent-synergia",
                remedy="hermes plugins enable hermes-agent-synergia   # §9.2: a policy "
                       "tier the host never loads is decoration",
            ))

    # ── catalog ──────────────────────────────────────────────────────────────
    for src in catalog.sources():
        if not src.get("resolved"):
            items.append(_item(
                "catalog.source_missing", "info", team_subject,
                observed=f"{src.get('path')} ({src.get('error') or 'unresolved'})",
                desired="resolvable catalog root",
                remedy="hermes team profiles import <git-url|dir> into the "
                       "plugin's own store, or fix plugins.entries.hermes-agent-synergia."
                       "settings.catalog_paths (§9.2)",
            ))
    try:
        stray = catalog_store.orphans()["dirs_without_row"]
    except TeamError:
        stray = []      # an unreadable index already shows as catalog.source_missing
    if stray:
        # A crash between the payload swap and the index write leaves these;
        # rows without a dir are already dropped as catalog.source_missing.
        items.append(_item(
            "catalog.store_orphan", "info", team_subject,
            observed=f"{catalog_store.profiles_dir()}: {', '.join(stray)}",
            desired="every stored payload has a catalog.yaml row",
            remedy="hermes team profiles import <that dir> --replace to "
                   "register it, or delete the directory",
        ))

    return items


def observe_and_diff(team):
    """Convenience for the sync path: ``observe()`` with its §4.3 ``drift``
    slot filled by :func:`diff` (the ``Observed`` dataclass is frozen, so
    "filled" = a replaced copy)."""
    observed = resolve.observe(team)
    return dc_replace(observed, drift=diff(team, observed))


# ── §9.3 apply ───────────────────────────────────────────────────────────────

# §9.4 — the live-process notice, printed whenever this run rewrote a
# ``config.yaml`` a running gateway serves.
# What a dispatched worker needs before its first turn: a board to claim
# from, a workdir (with a commit to branch from under worktree), a profile
# carrying the plugin, and the orchestrator's kanban routing keys.
READY_CHECKS = frozenset({
    "board.exists", "workdir.exists", "workdir.git",
    "member.exists", "member.instance_missing", "member.instance_tombstoned",
    "member.plugin_enabled", "member.plugin_link",
    "orchestrator.member", "orchestrator.config",
})


def _has_model(cfg) -> bool:
    m = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(m, dict):
        m = m.get("default") or m.get("model")
    return bool(str(m or "").strip())


def readiness(team, items=None) -> dict:
    """Can *team* take a task now? ``blocking`` is the :data:`READY_CHECKS`
    drift (*items* reuses a diff already computed) plus one ``member.model``
    item per existing profile with no model — its workers would print "No
    inference provider configured" and exit before their first turn."""
    items = diff(team) if items is None else items
    blocking = [d for d in items if d.check in READY_CHECKS]
    for m in team.members:
        if not resolve.profile_exists(m.profile):
            continue
        try:
            cfg = resolve.read_profile_config(m.profile)
        except Exception:  # noqa: BLE001 — unreadable config: no model either
            cfg = {}
        if not _has_model(cfg):
            blocking.append(_item(
                "member.model", "error", f"profile:{m.profile}",
                observed=None, desired="a configured model",
                remedy=f"hermes -p {m.profile} model   # or set model in the "
                       f"root config.yaml, then: hermes team sync {team.slug} "
                       "--apply"))
    return {"ready": not blocking, "blocking": [d.to_dict() for d in blocking]}


# The gateway rebuilds each profile's terminal policy from config.yaml every
# turn (gateway/run.py profile scope), so no restart is needed; only a chat's
# frozen prompt and recorded session cwd outlive the write.
_RESTART_NOTICE = (
    "{n} profiles updated. New sessions and dispatched kanban workers use "
    "the new terminal.cwd now — no gateway restart needed.\n"
    "Open chats keep their old cwd: run /new in them to pick it up."
)

# §9.3 "Instance creation is never implicit" / bullet 1b: each gated check
# is fixable only under the flag its §9.2 remedy names. In the DIFF these
# items carry ``auto_fixable: False``; apply lifts the gate iff the flag was
# passed. Plain ``--apply`` reports them and creates neither an agent nor a
# credential path.
_GATED_CHECKS = {
    "board.exists": "create_board",
    "workdir.exists": "create_workdir",
    "member.instance_missing": "reinstantiate",
    "member.env_link": "link_env",
}

# batched config key -> the check id its Applied record carries, so one log
# vocabulary serves both the drift report and the apply audit (§13).
_KEY_CHECK = {
    "terminal.cwd": "member.terminal_cwd",
    "toolsets": "member.toolsets",
    "kanban.orchestrator_profile": "orchestrator.config",
    "kanban.default_assignee": "orchestrator.config",
    "kanban.max_in_progress_per_profile": "member.capacity",
    "plugins.enabled": "member.plugin_enabled",
    "skills.disabled": "member.skills",
    "model": "member.model",
    "provider": "member.model",
    "model.default": "member.model",
    "model.provider": "member.model",
    "model.base_url": "member.model",
}


@dataclass(frozen=True)
class Applied:
    """One acted-on item of an ``apply`` run — the write-half twin of
    :class:`DriftItem`: same ``check``/``subject`` vocabulary, plus the
    §9.3 outcome (``applied: false`` + the exception message on failure)."""
    check: str
    subject: str
    observed: Any = None
    desired: Any = None
    ok: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"check": self.check, "subject": self.subject,
                "observed": self.observed, "desired": self.desired,
                "applied": self.ok, "error": self.error}


@dataclass(frozen=True)
class ApplyReport:
    """Outcome of one ``sync --apply`` for one team (§8.2 exit semantics)."""
    slug: str
    applied: list = field(default_factory=list)
    drift: list = field(default_factory=list)     # residual DriftItems
    profiles_updated: int = 0                     # §9.4: config files changed
    restart_notice: Optional[str] = None          # §9.4 block, or None
    exit_code: int = 0                            # 0, or 4 (§8.2)

    def to_dict(self) -> dict:
        return {"slug": self.slug,
                "applied": [a.to_dict() for a in self.applied],
                "drift": [d.to_dict() for d in self.drift],
                "profiles_updated": self.profiles_updated,
                "restart_notice": self.restart_notice,
                "exit_code": self.exit_code}


@dataclass(frozen=True)
class _Opts:
    reinstantiate: bool = False
    link_env: bool = False
    create_board: bool = False
    create_workdir: bool = False
    only: Optional[frozenset] = None


def _gate(o: "_Opts", check: str, *, fixable: bool = True) -> bool:
    """May *check* be acted on this run? ``only=`` narrows, the §9.3 flags
    lift their gates, everything else must have been fixable in the diff."""
    if o.only is not None and check not in o.only:
        return False
    flag = _GATED_CHECKS.get(check)
    if flag is not None:
        return bool(getattr(o, flag))
    return bool(fixable)


def _profile_of(subject: str) -> str:
    return subject.split(":", 1)[1] if subject.startswith("profile:") else ""


# ── phase helpers (order is contractual, §9.3) ───────────────────────────────

def _agents_block(team, workdir: str) -> str:
    """The fenced AGENTS.md section, rendered from the registry (§9.2)."""
    members = ", ".join(f"{m.profile} ({m.role})" for m in team.members)
    return (
        "<!-- hermes-agent-synergia:begin -->\n"
        f"## Team: {team.display_name or team.slug} ({team.slug})\n"
        f"Board: {team.board}   Work directory: {workdir}"
        f"   Tenant: {team.tenant or '-'}\n"
        f"Members: {members}\n"
        f"Rules: create and assign cards only on board `{team.board}`, only "
        "to the members above.\n"
        "<!-- hermes-agent-synergia:end -->"
    )


def _apply_board_create(team, workdir: str, applied: list, forced: list) -> None:
    """Phase 1a: create the missing board. The substrate's delegated-child
    guard (``_assert_not_delegated_child_mutation`` → ``PermissionError``)
    is REPORTED as an unfixable :class:`DriftItem`, never propagated
    (§5.2.1, S10 bullet 4)."""
    subject = f"board:{team.board}"
    try:
        resolve.ensure_board(
            team.board,
            name=model.board_label(team),
            # Shared_board ⇒ the defaults belong to whoever got there first
            default_workdir=(None if team.shared_board else (workdir or None)),
        )
    except (OSError, ValueError) as exc:
        applied.append(Applied("board.exists", subject, observed="no board",
                               desired=f"board {team.board!r}", ok=False,
                               error=f"{type(exc).__name__}: {exc}"))
        forced.append(_item(
            "board.exists", "error", subject,
            observed=f"refused: {exc}", desired=f"board {team.board!r}",
            remedy="run `hermes team sync --apply --create-board` from the "
                   "operator shell — a delegated kanban child must not "
                   "create boards (§5.2.1)"))
        return
    applied.append(Applied("board.exists", subject, observed="no board",
                           desired=f"board {team.board!r}", ok=True))


def _apply_board_meta(team, by: dict, o: "_Opts", applied: list) -> None:
    """Phase 1b: board.json fixes — name/display and default_workdir —
    batched into ONE ``write_board_metadata`` call (board.json gets the same
    per-file batching §9.3 demands of config.yaml)."""
    updates: dict = {}
    recs: dict = {}
    for it in by.get("board.default_workdir", []):
        if it.auto_fixable and _gate(o, it.check, fixable=True):
            updates["default_workdir"] = it.desired
            recs["default_workdir"] = it
    for it in by.get("board.display", []):
        if it.auto_fixable and _gate(o, it.check, fixable=True):
            updates["name"] = it.desired
            recs["name"] = it
    for it in by.get("board.project_id", []):
        if it.auto_fixable and _gate(o, it.check, fixable=True):
            updates["project_id"] = it.desired
            recs["project_id"] = it
    if not updates:
        return
    subject = f"board:{team.board}"
    try:
        resolve._kb().write_board_metadata(team.board, **updates)
    except (OSError, ValueError) as exc:
        for key, it in recs.items():
            applied.append(Applied(it.check, it.subject, observed=it.observed,
                                   desired=it.desired, ok=False,
                                   error=f"{type(exc).__name__}: {exc}"))
        return
    for key, it in recs.items():
        applied.append(Applied(it.check, it.subject, observed=it.observed,
                               desired=it.desired, ok=True))


def _apply_workdir(team, workdir: str, applied: list) -> None:
    """Phase 2: create the shared work directory. §9.3 refusals honoured:
    a ``workdir`` inside ``<root>`` is never created."""
    subject = f"team:{team.slug}"
    ab = os.path.abspath(workdir) if workdir else ""
    root = str(resolve.root_path())
    if ab and (ab == root or ab.startswith(root + os.sep)):
        applied.append(Applied(
            "workdir.exists", subject, observed=workdir,
            desired="directory present", ok=False,
            error=f"{E_TEAM_INVALID}: refusing to create workdir "
                  f"{workdir!r} inside the Hermes root (§9.3 refusals)"))
        return
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as exc:
        applied.append(Applied("workdir.exists", subject, observed=workdir,
                               desired="directory present", ok=False,
                               error=f"{type(exc).__name__}: {exc}"))
        return
    applied.append(Applied("workdir.exists", subject, observed=workdir,
                           desired="directory present", ok=True))


def _apply_git(team, workdir: str, applied: list) -> None:
    """Phase 2: give a ``worktree`` team's workdir the HEAD its tasks branch
    off (:func:`resolve.ensure_worktree_repo`)."""
    subject = f"team:{team.slug}"
    try:
        resolve.ensure_worktree_repo(workdir)
    except TeamError as exc:
        applied.append(Applied("workdir.git", subject, observed=workdir,
                               desired="git repo with HEAD", ok=False,
                               error=f"{exc.code}: {exc.message}"))
        return
    applied.append(Applied("workdir.git", subject, observed=workdir,
                           desired="git repo with HEAD", ok=True))


def _apply_agents_md(team, workdir: str, o: "_Opts", applied: list) -> None:
    """Opt-in: append/repair the fenced ``hermes-agent-synergia:begin`` block,
    but only when ``sync.write_agents_md`` is true (§9.2) and the workdir
    exists. An existing block is left alone (idempotence)."""
    if not bool(getattr(team.sync, "write_agents_md", False)):
        return
    if not _gate(o, "workdir.agents_md"):
        return
    if not workdir or not os.path.isdir(workdir):
        return
    subject = f"team:{team.slug}"
    path = Path(workdir) / "AGENTS.md"
    begin = "<!-- hermes-agent-synergia:begin -->"
    block = _agents_block(team, workdir)
    observed = "no AGENTS.md"
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if begin in text:
                return                    # block already ours — touch nothing
            new_text = text.rstrip("\n") + "\n\n" + block + "\n"
            observed = "AGENTS.md without team block"
        else:
            new_text = block + "\n"
            observed = "no AGENTS.md"
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        applied.append(Applied("workdir.agents_md", subject,
                               observed=observed, desired=str(path),
                               ok=False, error=f"{type(exc).__name__}: {exc}"))
        return
    applied.append(Applied("workdir.agents_md", subject, observed=observed,
                           desired=str(path), ok=True))


def _apply_project(team, workdir: str, o: "_Opts", applied: list) -> None:
    """Phase 3: mint the projects.db row — ``mode: create`` only (§9.2
    "⚠ create only"). The board.json ``project_id`` stamp can only happen
    AFTER the row exists (ordering, §9.3), which is why it is a second,
    small board write instead of part of phase 1."""
    mode = str(getattr(team.project, "mode", "none") or "none")
    if mode != "create" or resolve.project_for(team) is not None:
        return
    if not _gate(o, "project.resolves"):
        return
    subject = f"team:{team.slug}"
    proj_slug = str(getattr(team.project, "slug", "") or team.slug)
    try:
        pdb = resolve._projects_db()
        with pdb.connect_closing() as conn:
            pid = pdb.create_project(
                conn, name=team.display_name or proj_slug, slug=proj_slug,
                folders=[workdir] if workdir else [])
        applied.append(Applied("project.resolves", subject,
                               observed="no row in projects.db",
                               desired=pid, ok=True))
    except Exception as exc:  # noqa: BLE001 — §9.3 partial failure
        applied.append(Applied("project.resolves", subject,
                               observed="no row in projects.db",
                               desired=f"project {proj_slug}", ok=False,
                               error=f"{type(exc).__name__}: {exc}"))
        return
    if resolve.board_exists(team.board):
        meta = resolve.board_meta(team.board)
        if meta.get("project_id") != pid:
            try:
                resolve._kb().write_board_metadata(team.board, project_id=pid)
                applied.append(Applied("board.project_id",
                                       f"board:{team.board}",
                                       observed=meta.get("project_id"),
                                       desired=pid, ok=True))
            except (OSError, ValueError) as exc:
                applied.append(Applied("board.project_id",
                                       f"board:{team.board}",
                                       observed=meta.get("project_id"),
                                       desired=pid, ok=False,
                                       error=f"{type(exc).__name__}: {exc}"))


def _apply_instantiate(team, it: DriftItem, applied: list) -> None:
    """Phase 4 (``--reinstantiate`` only): rebuild a deleted MANAGED instance
    from its blueprint via :func:`provision.instantiate` (F6 order is that
    function's contract). Adopted members never arrive here — the diff only
    emits ``member.instance_missing`` for ``managed: true``."""
    profile = _profile_of(it.subject)
    m = next((mm for mm in team.members if mm.profile == profile), None)
    if m is None:
        return
    try:
        entry = catalog.get(str(m.blueprint))
        provision.instantiate(entry, profile, role=m.role or "member")
        applied.append(Applied("member.instance_missing", it.subject,
                               observed=it.observed,
                               desired="re-instantiated from blueprint",
                               ok=True))
    except Exception as exc:  # noqa: BLE001 — §9.3 partial failure
        applied.append(Applied("member.instance_missing", it.subject,
                               observed=it.observed,
                               desired="re-instantiated from blueprint",
                               ok=False, error=f"{type(exc).__name__}: {exc}"))


def _fix_plugin_link(profile: str, it: DriftItem, applied: list) -> None:
    """F1: (re)create the per-profile plugin symlink, relative so the whole
    Hermes root stays relocatable (same convention provision.instantiate
    uses). A wrong-but-present entry is replaced, a real directory kept."""
    link = resolve.profile_home(profile) / "plugins" / PLUGIN_NAME
    try:
        if link.is_symlink() or link.exists():
            if link.is_dir():
                return                    # already a dir (or good link) — no-op
            link.unlink()
        elif link.parent.is_symlink():
            link.parent.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.join("..", "..", "..", "plugins", PLUGIN_NAME),
                   link)
        applied.append(Applied("member.plugin_link", it.subject,
                               observed=it.observed, desired=it.desired,
                               ok=True))
    except OSError as exc:
        applied.append(Applied("member.plugin_link", it.subject,
                               observed=it.observed, desired=it.desired,
                               ok=False, error=f"{type(exc).__name__}: {exc}"))


def _capacity_conflict(reg, slug: str, profile: str, want: Any,
                       current: Any) -> Optional[str]:
    """I6's enforcement INSIDE apply — where hand-edited registries
    (which bypass the CLI's I6 check) reach the disk. ``kanban.*`` on a
    shared profile is owned by whoever last wrote it: if the value now on
    disk equals ANOTHER primary team's declared capacity, this write is a
    flap-in-progress and is refused with E_TEAM_CAPACITY_CONFLICT. First
    writer wins; the second claimer is refused and both runs converge."""
    if current is None:
        return None
    for other_slug, t in reg.teams.items():
        if other_slug == slug:
            continue
        for mm in t.members:
            if (mm.profile == profile and mm.primary
                    and mm.capacity is not None and mm.capacity == current
                    and mm.capacity != want):
                return str(other_slug)
    return None


# ── phase 5: per-member config, batched (§9.3) ───────────────────────────────

def _apply_members(team, workdir: str, d2: list, o: "_Opts", applied: list,
                   updated: set) -> None:
    """Every key for one profile's ``config.yaml`` is collected first and
    committed in ONE :func:`resolve.write_profile_config_keys` call — never
    N writes to one file (§9.3). ``profile.yaml`` descriptions ride their own
    single :func:`write_profile_meta` call; a non-empty description never
    produces an item, so it is never overwritten (§9.2)."""
    reg = storage.load_or_empty()
    members_by_profile = {m.profile: m for m in team.members}
    batches: dict = {}
    meta_desc: dict = {}

    for it in d2:
        if it.check in ("board.exists", "board.default_workdir", "board.display",
                        "board.project_id", "workdir.exists", "workdir.git",
                        "workdir.agents_md", "project.resolves",
                        "member.instance_missing", "member.instance_tombstoned",
                        "member.exists", "member.blueprint_unknown",
                        "board.exclusivity", "tenant.unstamped",
                        "policy.hook", "catalog.source_missing",
                        "catalog.store_orphan"):
            continue            # phases 1-4 own these; they carry no config keys
        if not _gate(o, it.check, fixable=it.auto_fixable):
            continue
        profile = _profile_of(it.subject)
        if not profile:
            continue
        if it.check == "member.plugin_link":
            _fix_plugin_link(profile, it, applied)
        elif it.check == "member.env_link":
            try:
                env_path = resolve.profile_home(profile) / ".env"
                root_env = resolve.root_path() / ".env"
                if (env_path.is_file() and not env_path.is_symlink()
                        and root_env.is_file()):
                    # The diff just certified this REAL file as the seeded
                    # placeholder (the plugin's own footprint), so
                    # apply may replace it — provision.link_env hands off on
                    # any real file, and the placeholder is exactly the case
                    # the remedy flag was named for. Never a copy (§13).
                    env_path.unlink()
                provision.link_env(profile)
                applied.append(Applied(it.check, it.subject,
                                       observed=it.observed,
                                       desired=it.desired, ok=True))
            except Exception as exc:  # noqa: BLE001 — partial failure
                applied.append(Applied(it.check, it.subject,
                                       observed=it.observed,
                                       desired=it.desired, ok=False,
                                       error=f"{type(exc).__name__}: {exc}"))
        elif it.check in ("member.description", "member.profile_meta"):
            # profile.yaml, not config.yaml — only-when-empty (§9.2 F6)
            meta_desc[profile] = it.desired
        elif it.check == "member.terminal_cwd":
            # §9.3 ordering, re-checked NOW (phase 2 has had its chance):
            # never write a cwd pointing at a directory this run has yet to
            # create. The failure record explains the missing write.
            if workdir and os.path.isdir(workdir):
                batches.setdefault(profile, {})["terminal.cwd"] = it.desired
            else:
                applied.append(Applied(
                    it.check, it.subject, observed=it.observed,
                    desired=it.desired, ok=False,
                    error=f"{E_TEAM_INVALID}: refusing to write terminal.cwd "
                          f"while workdir {workdir!r} does not exist (§9.3 "
                          "ordering)"))
        elif it.check == "member.capacity":
            m = members_by_profile.get(profile)
            want = m.capacity if m is not None else it.desired
            other = _capacity_conflict(reg, team.slug, profile, want,
                                       it.observed)
            if other is not None:
                applied.append(Applied(
                    it.check, it.subject, observed=it.observed,
                    desired=it.desired, ok=False,
                    error=f"{E_TEAM_CAPACITY_CONFLICT}: "
                          f"kanban.max_in_progress_per_profile of "
                          f"{profile!r} is {it.observed!r} — declared by "
                          f"primary team {other!r}; refusing to overwrite "
                          f"with {want!r}"))
            else:
                batches.setdefault(profile, {})[
                    "kanban.max_in_progress_per_profile"] = it.desired
        elif it.check == "member.toolsets":
            batches.setdefault(profile, {})["toolsets"] = list(it.desired or [])
        elif it.check == "member.plugin_enabled":
            batches.setdefault(profile, {})["plugins.enabled"] = \
                list(it.desired or [PLUGIN_NAME])
        elif it.check == "orchestrator.config":
            batches.setdefault(profile, {}).update(dict(it.desired or {}))

    # ── S10a projections (bullet 2b): Member.skills / Member.model /
    # Member.provider. These have NO §9.2 row (by design — §9.2 "deliberately
    # not a check") — they are desired-state writes, projected when declared;
    # skills=None means "not managed by the team", so it is left alone.
    for m in team.members:
        profile = m.profile
        subject = f"profile:{profile}"
        if not resolve.profile_exists(profile):
            continue        # §9.3: config never precedes the profile
        if m.skills is not None and _gate(o, "member.skills"):
            try:
                batches.setdefault(profile, {})["skills.disabled"] = \
                    resolve.disabled_skills_for(profile, m.skills)
            except Exception as exc:  # noqa: BLE001 — partial failure
                applied.append(Applied("member.skills", subject,
                                       observed=None, desired=list(m.skills),
                                       ok=False,
                                       error=f"{type(exc).__name__}: {exc}"))
        extra = dict(getattr(m, "_extra", None) or {})
        model_val = extra.get("model")
        provider_val = extra.get("provider")
        base_url_val = extra.get("base_url")
        if model_val is not None and str(model_val).strip() \
                and _gate(o, "member.model"):
            # Section keys: a scalar ``model`` drops the profile's provider
            # and base_url, which hermes reads only from inside ``model:``.
            batch = batches.setdefault(profile, {})
            batch["model.default"] = model_val
            if provider_val is not None and str(provider_val).strip():
                batch["model.provider"] = provider_val
            if base_url_val is not None and str(base_url_val).strip():
                batch["model.base_url"] = base_url_val
        elif provider_val is not None and str(provider_val).strip():
            # The writer refuses provider-without-model in the same call
            # (S10a); report it here instead of discovering it at write time.
            applied.append(Applied("member.model", subject, observed=None,
                                   desired={"provider": provider_val},
                                   ok=False,
                                   error=f"{E_TEAM_INVALID}: provider without "
                                         "model is not a writable override "
                                         "(S10a)"))

    # ── one atomic config write per file (§9.3), after the pre-filter ────
    for profile, upd in batches.items():
        subject = f"profile:{profile}"
        path = resolve.profile_config_path(profile)
        try:
            parsed = resolve.read_profile_config(profile)
        except Exception:  # noqa: BLE001 — corrupt target: send it to the
            parsed = None  # writer, whose guard 2 is the one that refuses
        if parsed is not None:
            # §9.3 idempotence pre-filter (same rule as the writer's own):
            # drop every key already at its desired value. An all-quiet
            # batch skips the call entirely — nothing to commit, no write,
            # no mtime move.
            upd = {k: v for k, v in upd.items()
                   if resolve._parsed_value_at(parsed, k)
                   != (list(v) if isinstance(v, list) else v)}
        if not upd:
            continue
        before = path.read_bytes() if path.is_file() else None
        try:
            resolve.write_profile_config_keys(profile, upd)
        except Exception as exc:  # noqa: BLE001 — §9.3 partial failure:
            # this file is refused whole (all-or-nothing); its siblings run on.
            for k, v in upd.items():
                applied.append(Applied(_KEY_CHECK.get(k, "member.config"),
                                       subject, observed=None, desired=v,
                                       ok=False,
                                       error=f"{type(exc).__name__}: {exc}"))
            continue
        after = path.read_bytes() if path.is_file() else None
        if before != after:
            updated.add(profile)
        for k, v in upd.items():
            applied.append(Applied(_KEY_CHECK.get(k, "member.config"),
                                   subject, observed=None, desired=v,
                                   ok=True))

    # ── profile.yaml descriptions (F6: one write_profile_meta per profile) ──
    for profile, desc in meta_desc.items():
        subject = f"profile:{profile}"
        home = resolve.profile_home(profile)
        try:
            if not home.is_dir():
                raise FileNotFoundError(
                    f"profile directory does not exist: {home}")
            resolve._profiles().write_profile_meta(
                home, description=str(desc), description_auto=False)
            applied.append(Applied("member.description", subject,
                                   observed="", desired=desc, ok=True))
        except Exception as exc:  # noqa: BLE001 — partial failure
            applied.append(Applied("member.description", subject,
                                   observed="", desired=desc, ok=False,
                                   error=f"{type(exc).__name__}: {exc}"))


# ── apply() ───────────────────────────────────────────────────────────────────

def _apply_one(team, o: "_Opts") -> ApplyReport:
    """One team, §9.3's order: board → workdir → project → profile
    instances → per-member config. Every write site is guarded; every
    failure is a record, never an abort."""
    applied: list = []
    forced: list = []            # guard-refusal DriftItems for the residual
    updated: set = set()         # profiles whose config.yaml bytes changed
    workdir = os.path.expanduser(team.workdir or "")

    # ── phase 1: board ────────────────────────────────────────────────────
    obs = resolve.observe(team)
    d1 = diff(team, obs)
    by: dict = {}
    for it in d1:
        by.setdefault(it.check, []).append(it)
    if not obs.board_exists:
        if _gate(o, "board.exists"):
            _apply_board_create(team, workdir, applied, forced)
    else:
        _apply_board_meta(team, by, o, applied)

    # ── phase 2: workdir ──────────────────────────────────────────────────
    if not obs.workdir_exists:
        if _gate(o, "workdir.exists"):
            _apply_workdir(team, workdir, applied)
    _apply_agents_md(team, workdir, o, applied)
    # after AGENTS.md, so the initial snapshot carries it
    if (team.workspace_policy in ("worktree", "shared") and os.path.isdir(workdir)
            and _gate(o, "workdir.git")
            and not resolve.workdir_has_commit(workdir)):
        _apply_git(team, workdir, applied)

    # ── phase 3: project ──────────────────────────────────────────────────
    _apply_project(team, workdir, o, applied)

    # ── phase 4: profile instances (--reinstantiate only, never implicit) ─
    if o.reinstantiate:
        for it in by.get("member.instance_missing", []):
            _apply_instantiate(team, it, applied)

    # ── phase 5: per-member config (fresh diff: phases 1-4 moved the world) ─
    d2 = diff(team)
    _apply_members(team, workdir, d2, o, applied, updated)

    # ── report ────────────────────────────────────────────────────────────
    residual = diff(team) + forced
    notice = _RESTART_NOTICE.format(n=len(updated)) if updated else None
    failed = [a for a in applied if not a.ok]
    unfixable = [d for d in residual
                 if d.severity in ("error", "warn") and not d.auto_fixable]
    for a in applied:
        # §13 audit / bullet 6: every acted-on item, at INFO, with check,
        # subject, observed, desired (failures add the recorded error).
        _LOG.info("hermes-agent-synergia apply: check=%s subject=%s observed=%r "
                  "desired=%r applied=%s%s",
                  a.check, a.subject, a.observed, a.desired, a.ok,
                  f" error={a.error}" if a.error else "")
    return ApplyReport(slug=team.slug, applied=applied, drift=residual,
                       profiles_updated=len(updated), restart_notice=notice,
                       exit_code=4 if (failed or unfixable) else 0)


def apply(team, *, reinstantiate: bool = False, link_env: bool = False,
          create_board: bool = False, create_workdir: bool = False,
          only=None):
    """``sync --apply`` for one :class:`model.Team` (or an iterable of them,
    the ``sync --all --apply`` path — returns one :class:`ApplyReport` per
    team, in order). Fixes everything the §9.2 diff called fixable; the four
    §9.3-gated checks fire only under their own flag (§9.3 "Instance
    creation is never implicit", bullet 1b). Prints the §9.4 restart notice
    to stderr iff a served config changed. Never raises for a failing
    world: every failure is an ``applied: false`` record and the run
    continues (§9.3 "Partial failure")."""
    o = _Opts(
        reinstantiate=bool(reinstantiate), link_env=bool(link_env),
        create_board=bool(create_board), create_workdir=bool(create_workdir),
        only=None if only is None else frozenset(str(c) for c in only),
    )
    if isinstance(team, model.Team):
        report = _apply_one(team, o)
        if report.restart_notice:
            print(report.restart_notice, file=sys.stderr)
        return report
    return [apply(t, reinstantiate=reinstantiate, link_env=link_env,
                  create_board=create_board, create_workdir=create_workdir,
                  only=only)
            for t in team]


def would_write(team, *, reinstantiate: bool = False, link_env: bool = False,
                create_board: bool = False, create_workdir: bool = False,
                only=None) -> bool:
    """Would :func:`apply` with these options act on any drift item? The
    busy guard asks, so a no-op sync never stops or waits on running cards."""
    o = _Opts(
        reinstantiate=bool(reinstantiate), link_env=bool(link_env),
        create_board=bool(create_board), create_workdir=bool(create_workdir),
        only=None if only is None else frozenset(str(c) for c in only),
    )
    return any(_gate(o, d.check, fixable=d.auto_fixable) for d in diff(team))


def run_exit_code(reports) -> int:
    """§8.2: ``sync --apply`` exits ``4`` when it completed but at least one
    item failed or one unfixable item remains — the cli layer's mapping."""
    seq = reports if isinstance(reports, (list, tuple)) else [reports]
    return 4 if any(r.exit_code for r in seq) else 0
