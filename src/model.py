"""Team records, validation, invariants — spec §4, pure (no I/O).

Layering (spec §5.2): this module performs **no filesystem access** and imports
nothing from ``hermes_cli``. The one allowed substrate call — profile-name
normalization — is inlined (``_normalize_profile_name``) with an equivalence
assertion in ``test_substrate_contract.py``, per plan S4.6's fallback, so the
model stays importable with zero substrate cost.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace as dc_replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .errors import (
    TeamError,
    E_TEAM_INVALID,
    E_TEAM_BOARD_TAKEN,
    E_TEAM_PRIMARY_CONFLICT,
    E_TEAM_CAPACITY_CONFLICT,
    E_TEAM_MANAGED_CONFLICT,
)

# ── copied regexes — spec §5.2.1: copy, never import kb._BOARD_SLUG_RE ──────
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_NAMES = frozenset({"hermes", "default", "test", "tmp", "root", "sudo"})

# Spec §2.6: file_tools treats these terminal.cwd values as "unset".
TERMINAL_CWD_SENTINELS = frozenset({"", ".", "./", "auto", "cwd"})

WORKSPACE_POLICIES = frozenset({"scratch", "worktree", "shared"})
POLICY_MODES = frozenset({"off", "advise", "enforce"})
DEFAULT_PROFILE_NAMING = "{team}-{short}"
PACK_NAMESPACES = ("agency-", "council-", "academy-")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_profile_name(name: str) -> str:
    """Copy of ``profiles.normalize_profile_name`` behaviour (equivalence asserted in CI)."""
    if not isinstance(name, str):
        name = str(name)
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    if stripped.casefold() == "default":
        return "default"
    return stripped.lower()


def _invalid(field_name: str, message: str, remedy: str = "hermes team create --help") -> TeamError:
    return TeamError(
        E_TEAM_INVALID,
        message,
        hint=f"Field {field_name!r} is invalid (spec §4.2).",
        remedy=remedy,
    )


def _parse_known(cls, data: Mapping):
    """Instantiate frozen dataclass *cls* from a mapping; unknown keys -> _extra."""
    known = {f.name for f in fields(cls) if f.name != "_extra"}
    kwargs = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return cls(**kwargs, _extra=extra)


def _dump_known(rec) -> dict:
    """Record -> plain dict: declared fields (None omitted) + unknown keys."""
    out: dict = {}
    for f in fields(rec):
        if f.name == "_extra":
            continue
        v = getattr(rec, f.name)
        if v is None:
            continue
        out[f.name] = v
    for k, v in rec._extra.items():
        out.setdefault(k, v)
    return out


# ── records (spec §4.1; unknown keys round-trip, §4.4) ──────────────────────

@dataclass(frozen=True)
class Member:
    profile: str = ""
    role: str = "member"
    primary: bool = False
    capacity: Optional[int] = None
    blueprint: Optional[str] = None
    blueprint_source: Optional[str] = None
    managed: bool = False
    skills: Optional[list] = None
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Member":
        return _parse_known(cls, data)

    def to_dict(self) -> dict:
        return _dump_known(self)


@dataclass(frozen=True)
class Project:
    mode: str = "none"
    slug: Optional[str] = None
    id: Optional[str] = None
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Project":
        return _parse_known(cls, data)

    def to_dict(self) -> dict:
        return _dump_known(self)


@dataclass(frozen=True)
class Policy:
    mode: str = "advise"
    allow_foreign_board: bool = False
    allow_non_member_assignee: bool = False
    guarded_tools: Optional[list] = None
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Policy":
        return _parse_known(cls, data)

    def to_dict(self) -> dict:
        return _dump_known(self)


@dataclass(frozen=True)
class SyncSwitches:
    terminal_cwd: bool = True
    board_default_workdir: bool = True
    profile_descriptions: bool = True
    orchestrator_config: bool = True
    toolsets: bool = True
    write_agents_md: bool = False
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "SyncSwitches":
        return _parse_known(cls, data)

    def to_dict(self) -> dict:
        return _dump_known(self)


@dataclass(frozen=True)
class Team:
    slug: str = ""
    display_name: str = ""
    description: str = ""
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    board: str = ""
    shared_board: bool = False
    workdir: str = ""
    workspace_policy: str = "scratch"
    tenant: str = ""
    tenant_stamping: str = "auto"
    profile_naming: str = DEFAULT_PROFILE_NAMING
    project: Project = field(default_factory=Project)
    orchestrator: str = ""
    members: tuple = ()
    policy: Policy = field(default_factory=Policy)
    sync: SyncSwitches = field(default_factory=SyncSwitches)
    metadata: Optional[dict] = None
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Team":
        d = dict(data)
        d["members"] = tuple(Member.from_dict(m) for m in d.get("members") or ())
        for key, sub in (("project", Project), ("policy", Policy), ("sync", SyncSwitches)):
            if isinstance(d.get(key), Mapping):
                d[key] = sub.from_dict(d[key])
        return _parse_known(cls, d)

    def to_dict(self) -> dict:
        out = _dump_known(self)
        out["members"] = [m.to_dict() for m in self.members]
        for key, rec in (("project", self.project), ("policy", self.policy), ("sync", self.sync)):
            val = rec.to_dict()
            if val:
                out[key] = val
        return out


_SLUG_JUNK_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(name: str) -> str:
    """Team slug derived from a free-form team name (the dashboard's single
    name field). Accents fold to ASCII; ``E_TEAM_INVALID`` when nothing
    slug-shaped is left."""
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(name or "")).encode(
        "ascii", "ignore").decode("ascii")
    slug = _SLUG_JUNK_RE.sub("-", folded.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")[:64].rstrip("-_")
    if not SLUG_RE.match(slug) or slug == "default":
        raise _invalid("name",
                       f"team name {name!r} gives no usable slug (got {slug!r})",
                       remedy="use letters or digits in the team name")
    return slug


# ── step labels: the one name a card is shown and branched under ────────────
#
# Room for ``step-<n>-`` plus a slug (``slugify`` caps at 64) plus the
# ``-<card id>`` a branch collision appends.
STEP_LABEL_MAX = 80
STEP_RE = re.compile(r"\bstep (\d+)\b", re.IGNORECASE)


def step_number(title) -> Optional[int]:
    """The step number a card title names, or None."""
    m = STEP_RE.search(str(title or ""))
    return int(m.group(1)) if m else None


def step_label(title, card_id) -> str:
    """The readable name of the card *title* belongs to:
    ``step-<n>-<title slug>``, the number from :data:`STEP_RE` and the slug
    from what is left of the title (:func:`slugify`).

    A title with no step number gives ``step-<slug>``; a title no slug can be
    made of gives ``step-<card id>``. Every surface shows this string, and
    ``resolve.step_branch_name`` branches a finished card under it, so the name
    an operator reads is the name in ``git branch``."""
    title = str(title or "")
    m = STEP_RE.search(title)
    prefix = f"step-{m.group(1)}" if m else "step"
    rest = f"{title[:m.start()]} {title[m.end():]}" if m else title
    try:
        slug = slugify(rest)
    except TeamError:
        slug = str(card_id)
    return f"{prefix}-{slug}"[:STEP_LABEL_MAX].rstrip("-_")


def _name_key(value) -> str:
    try:
        return slugify(value)
    except TeamError:
        return " ".join(str(value or "").split()).casefold()


def name_conflict(registry: "Registry", slug: str, display_name: str) -> Optional[str]:
    """Slug of another team whose name (display name, else slug) or slug
    matches this team's name or slug — team names are unique. Names compare
    by the slug they derive, so case, spacing and punctuation never tell two
    teams apart."""
    wanted = {_name_key(slug), _name_key(display_name or slug)}
    for other_slug, t in registry.teams.items():
        if other_slug == slug:
            continue
        if wanted & {_name_key(other_slug), _name_key(t.display_name or other_slug)}:
            return str(other_slug)
    return None


def board_label(team) -> str:
    """Kanban board ``name`` for a team board. The switcher shows only the
    name, so the slug rides along — two teams sharing a display_name must
    not render as two identical entries."""
    name = (team.display_name or "").strip()
    if not name or name == team.board:
        return team.board
    return f"{name} ({team.board})"


@dataclass(frozen=True)
class Registry:
    """Whole ``teams.yaml``. ``revision`` is the file-level CAS counter:
    every writer verifies it and writes ``revision + 1``. Per-team records
    carry their own ``revision`` too (§4.1)."""

    schema_version: int = 1
    revision: int = 0
    teams: Mapping = field(default_factory=dict)
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Registry":
        d = dict(data)
        d["teams"] = {slug: Team.from_dict(t) for slug, t in (d.get("teams") or {}).items()}
        return _parse_known(cls, d)

    def to_dict(self) -> dict:
        out = {"schema_version": self.schema_version, "revision": self.revision}
        for k, v in self._extra.items():
            out.setdefault(k, v)
        out["teams"] = {slug: t.to_dict() for slug, t in self.teams.items()}
        return out


# ── validation (spec §4.2 field contract) ────────────────────────────────────

def validate_team(team: Team, root) -> None:
    """Raise ``E_TEAM_INVALID`` unless the record satisfies the §4.2 contract.

    ``root`` is the Hermes root **passed in as an argument**: the model performs
    no I/O and must not resolve it itself (plan S4.3). All path comparisons are
    lexical.
    """
    if not SLUG_RE.match(team.slug or ""):
        raise _invalid("slug", f"team slug {team.slug!r} does not match {SLUG_RE.pattern}")
    if team.slug == "default":
        raise _invalid("slug", "no team may be named 'default' (I5)")
    if len(team.display_name or "") > 128:
        raise _invalid("display_name", "display_name exceeds 128 characters")

    wd = os.path.expanduser(team.workdir or "")
    if wd in TERMINAL_CWD_SENTINELS:
        raise _invalid(
            "workdir",
            f"workdir {team.workdir!r} is a sentinel value — a team needs a real directory",
        )
    if not os.path.isabs(wd):
        raise _invalid("workdir", f"workdir {team.workdir!r} is not absolute after expanduser")
    if ".." in wd.split("/"):
        raise _invalid("workdir", f"workdir {team.workdir!r} contains a '..' component")
    root_s = os.path.normpath(str(os.path.expanduser(str(root))))
    wd_s = os.path.normpath(wd)
    if wd_s == root_s or wd_s.startswith(root_s + "/"):
        raise _invalid(
            "workdir",
            f"workdir {team.workdir!r} is inside the Hermes root {root_s} — a team must "
            "not target the Hermes home",
        )

    if team.workspace_policy not in WORKSPACE_POLICIES:
        raise _invalid("workspace_policy",
                       f"workspace_policy {team.workspace_policy!r} not in {sorted(WORKSPACE_POLICIES)}",
                       remedy="set workspace_policy: worktree (or scratch) on this team's registry record")
    if len(team.tenant or "") > 64:
        raise _invalid("tenant", "tenant label exceeds 64 characters")
    if team.policy.mode not in POLICY_MODES:
        raise _invalid("policy.mode",
                       f"policy.mode {team.policy.mode!r} not in {sorted(POLICY_MODES)}")

    if not team.members:
        raise _invalid("members", "a team needs at least one member")
    profiles = [m.profile for m in team.members]
    if len(set(profiles)) != len(profiles):
        raise _invalid("members", "member profiles must be unique within a team")
    if team.orchestrator not in profiles:
        raise _invalid(
            "orchestrator",
            f"orchestrator {team.orchestrator!r} is not a member (I3)",
            remedy=f"hermes team add-member {team.slug} <orchestrator-profile>",
        )
    for m in team.members:
        if m.capacity is not None and (not isinstance(m.capacity, int) or m.capacity < 1):
            raise _invalid("members[].capacity",
                           f"member {m.profile!r} has capacity {m.capacity!r}; must be an integer >= 1")


# ── registry-wide invariants (I1, I2, I6, I7) ────────────────────────────────

def _conflicts(candidate: Team, registry: Registry):
    """Yield (other_slug, other_team, member) pairs sharing a profile with candidate."""
    by_profile = {}
    for other_slug, t in registry.teams.items():
        if other_slug == candidate.slug:
            continue
        for om in t.members:
            by_profile.setdefault(om.profile, []).append((other_slug, t, om))
    return by_profile


def check_invariants(registry: Registry, candidate: Team) -> list:
    """Registry-wide checks I1/I2/I6/I7 for *candidate* against *registry*.

    Returns a list of ``TeamError`` (possibly empty); I3/I5 are per-team and
    live in ``validate_team``.
    """
    errors: list = []
    others = {slug: t for slug, t in registry.teams.items() if slug != candidate.slug}

    # I1 — board exclusivity
    for slug, t in others.items():
        if t.board != candidate.board:
            continue
        if not (candidate.shared_board and t.shared_board):
            errors.append(TeamError(
                E_TEAM_BOARD_TAKEN,
                f"board {candidate.board!r} is already claimed by team {slug!r}",
                hint="One board per team unless every claimant sets shared_board: true (I1).",
                remedy=f"hermes team create {candidate.slug} --board <free-slug> --create-board"
                       "  # or pass --allow-shared-board on both teams",
            ))

    shared = _conflicts(candidate, registry)

    for m in candidate.members:
        hits = shared.get(m.profile, [])

        # I2 — a profile is primary in at most one team
        if m.primary:
            for slug, t, om in hits:
                if om.primary:
                    errors.append(TeamError(
                        E_TEAM_PRIMARY_CONFLICT,
                        f"profile {m.profile!r} is already primary in team {slug!r}",
                        hint="A profile is primary in at most one team (I2); the primary owns "
                             "terminal.cwd and the kanban.* keys for that profile.",
                        remedy=f"hermes team add-member {candidate.slug} {m.profile} --role {m.role}"
                               "  # add without --primary",
                    ))

        # I6 — capacity agreement across teams
        if m.capacity is not None:
            for slug, t, om in hits:
                if om.capacity is not None and om.capacity != m.capacity:
                    errors.append(TeamError(
                        E_TEAM_CAPACITY_CONFLICT,
                        f"profile {m.profile!r} declares capacity {m.capacity} here but "
                        f"{om.capacity} in team {slug!r}",
                        hint="kanban.max_in_progress_per_profile is one per-profile key — every "
                             "team declaring capacity for a profile must agree (I6).",
                        remedy=f"hermes team edit {slug}  # align the capacity, or drop it here",
                    ))

        # I7 — managed on at most one team
        if m.managed:
            for slug, t, om in hits:
                if om.managed:
                    errors.append(TeamError(
                        E_TEAM_MANAGED_CONFLICT,
                        f"profile {m.profile!r} is managed by team {slug!r} as well — two teams "
                        "cannot both claim the right to delete one profile",
                        hint="A profile is managed: true in at most one team (I7).",
                        remedy="hermes team doctor   # lists every managed member by owner",
                    ))

    return errors


# ── instance-name rendering ──────────────────────────────────────────────────

def render_instance_name(team_slug: str, entry_id: str, template: str) -> str:
    """Render + validate the profile name for a catalog entry (spec §2.9).

    ``{short}`` is the entry id with its pack namespace stripped
    (``agency-backend-engineer`` -> ``backend-engineer``). Raises
    ``E_TEAM_INVALID`` naming the rendered result when it would not be a legal
    profile id. Pure: no I/O.
    """
    short = entry_id
    for ns in PACK_NAMESPACES:
        if short.startswith(ns):
            short = short[len(ns):]
            break
    name = (template or DEFAULT_PROFILE_NAMING)
    name = name.replace("{team}", team_slug).replace("{short}", short)
    if not PROFILE_NAME_RE.match(name) or name in RESERVED_NAMES:
        raise TeamError(
            E_TEAM_INVALID,
            f"rendered instance name {name!r} is not a valid profile id (pattern "
            f"{PROFILE_NAME_RE.pattern}, reserved: {sorted(RESERVED_NAMES)})",
            hint=f"Check the profile_naming template against entry {entry_id!r}.",
            remedy="hermes team create ... --profile-naming '{team}-{short}'",
        )
    return name


# ── clone derivation — §8.1 copy/refuse table ────────────────────────────────

def derive_clone(src: Team, new_slug: str, overrides: Mapping | None = None) -> Team:
    """Derive the clone's record: the roster is reusable, the bindings never are.

    ``overrides`` keys: board, workdir, tenant, project, orchestrator,
    display_name, description, workspace_policy, drop_members,
    allow_shared_board, profile_naming.
    """
    ov = dict(overrides or {})
    drop = set(ov.get("drop_members") or ())

    members = []
    for m in src.members:
        if m.profile in drop:
            continue
        d = m.to_dict()
        d["primary"] = False   # recomputed; false wherever primary elsewhere (I2)
        d["managed"] = False   # I7 — the clone did not create these profiles
        members.append(Member.from_dict(d))

    display = ov.get("display_name") or (src.display_name or src.slug).replace(src.slug, new_slug)
    description = ov.get("description")
    if description is None:
        description = (src.description or "").replace(src.slug, new_slug)

    if ov.get("project"):
        project = Project(mode="adopt", slug=str(ov["project"]))
    else:
        project = Project(mode="none")  # a project is a per-board binding

    return Team(
        slug=new_slug,
        display_name=display,
        description=description,
        revision=1,                      # fresh
        created_at=now_iso(),
        updated_at=now_iso(),
        board=ov.get("board") or new_slug,   # NEVER copied
        shared_board=bool(ov.get("allow_shared_board")),
        workdir=ov.get("workdir") or "",     # NEVER copied; caller must require it
        workspace_policy=ov.get("workspace_policy", src.workspace_policy),
        tenant=ov.get("tenant") or new_slug,  # NEVER copied
        tenant_stamping=src.tenant_stamping,
        profile_naming=ov.get("profile_naming") or src.profile_naming or DEFAULT_PROFILE_NAMING,
        project=project,
        # `or`, not a default: a CLI override dict may carry an explicit
        # None for an unset flag, and unset must mean "copied" (S17).
        orchestrator=ov.get("orchestrator") or src.orchestrator,
        members=tuple(members),
        policy=Policy.from_dict(src.policy.to_dict()),
        sync=SyncSwitches.from_dict(src.sync.to_dict()),
        metadata=dict(src.metadata or {}),
        _extra={},
    )


def normalize(team: Team) -> Team:
    """Apply profile-name normalization to members and the orchestrator (§4.2)."""
    members = tuple(
        Member.from_dict({**m.to_dict(), "profile": _normalize_profile_name(m.profile)})
        for m in team.members
    )
    return dc_replace(team, members=members,
                      orchestrator=_normalize_profile_name(team.orchestrator))
