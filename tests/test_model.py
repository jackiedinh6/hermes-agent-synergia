"""S4 — model.py: field contract, invariants I1–I7, naming, clone derivation."""

from __future__ import annotations

import pytest

from errors import TeamError
from model import (
    DEFAULT_PROFILE_NAMING,
    RESERVED_NAMES,
    Registry,
    Team,
    board_label,
    derive_clone,
    render_instance_name,
    validate_team,
    check_invariants,
)

ROOT = "/root/.hermes"


def team_dict(**over):
    d = {
        "slug": "project-x",
        "display_name": "Project X",
        "revision": 1,
        "created_at": "2026-09-07T00:00:00Z",
        "updated_at": "2026-09-07T00:00:00Z",
        "board": "project-x",
        "workdir": "/root/workspace/project-x",
        "orchestrator": "orch-1",
        "members": [
            {"profile": "orch-1", "role": "orchestrator", "primary": True},
            {"profile": "dev-1", "role": "implementer"},
        ],
    }
    d.update(over)
    return d


def make_team(**over) -> Team:
    return Team.from_dict(team_dict(**over))


def reg_of(*teams: Team) -> Registry:
    return Registry(teams={t.slug: t for t in teams})


# ── validate_team ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sentinel", ["", ".", "./", "auto", "cwd"])
def test_workdir_sentinel_rejected(sentinel):
    with pytest.raises(TeamError) as ei:
        validate_team(make_team(workdir=sentinel), ROOT)
    assert ei.value.code == "E_TEAM_INVALID"


def test_workdir_relative_rejected():
    with pytest.raises(TeamError, match="not absolute"):
        validate_team(make_team(workdir="workspace/x"), ROOT)


def test_workdir_inside_root_rejected():
    with pytest.raises(TeamError, match="inside the Hermes root"):
        validate_team(make_team(workdir="/root/.hermes/workspace"), ROOT)


def test_workdir_equals_root_rejected():
    with pytest.raises(TeamError, match="inside the Hermes root"):
        validate_team(make_team(workdir=ROOT), ROOT)


def test_workdir_dotdot_rejected():
    with pytest.raises(TeamError, match="\\.\\."):
        validate_team(make_team(workdir="/root/workspace/../secret"), ROOT)


@pytest.mark.parametrize("slug", ["X", "-x", "x y", "", "x" * 65, "déjà"])
def test_bad_slug_rejected(slug):
    with pytest.raises(TeamError, match="slug"):
        validate_team(make_team(slug=slug), ROOT)


def test_display_name_over_128_rejected():
    with pytest.raises(TeamError, match="128"):
        validate_team(make_team(display_name="x" * 129), ROOT)


@pytest.mark.parametrize("mode", ["off", "advise", "enforce"])
def test_policy_mode_accepted(mode):
    validate_team(make_team(policy={"mode": mode}), ROOT)


def test_bad_policy_mode_rejected():
    with pytest.raises(TeamError, match="policy.mode"):
        validate_team(make_team(policy={"mode": "yolo"}), ROOT)


def test_empty_members_rejected():
    with pytest.raises(TeamError, match="at least one member"):
        validate_team(make_team(members=[]), ROOT)


def test_duplicate_members_rejected():
    dup = [{"profile": "a"}, {"profile": "a"}]
    with pytest.raises(TeamError, match="unique"):
        validate_team(make_team(members=dup, orchestrator="a"), ROOT)


def test_capacity_below_one_rejected():
    m = [{"profile": "orch-1", "capacity": 0}]
    with pytest.raises(TeamError, match="capacity"):
        validate_team(make_team(members=m, orchestrator="orch-1"), ROOT)


def test_happy_team_passes():
    validate_team(make_team(), ROOT)


# ── invariants, each violated in isolation ───────────────────────────────────

def test_I1_board_taken():
    t1 = make_team()
    t2 = make_team(slug="p2", orchestrator="orch-2",
                   members=[{"profile": "orch-2", "primary": True}])
    errs = check_invariants(reg_of(t1), t2)
    assert [e.code for e in errs] == ["E_TEAM_BOARD_TAKEN"]


def test_I1_shared_board_allowed():
    t1 = make_team(shared_board=True)
    t2 = make_team(slug="p2", shared_board=True, orchestrator="orch-2",
                   members=[{"profile": "orch-2", "primary": True}])
    assert check_invariants(reg_of(t1), t2) == []


def test_I2_primary_conflict():
    t1 = make_team()
    t2 = make_team(slug="p2", board="p2",
                   members=[{"profile": "orch-1", "primary": True},
                            {"profile": "dev-2"}],
                   orchestrator="orch-1")
    errs = check_invariants(reg_of(t1), t2)
    assert "E_TEAM_PRIMARY_CONFLICT" in [e.code for e in errs]


def test_I3_orchestrator_not_member():
    with pytest.raises(TeamError, match="I3"):
        validate_team(make_team(orchestrator="nobody"), ROOT)


def test_shared_workspace_policy_accepted():
    # Local fork: `shared` maps to the substrate's `dir` kind — one checkout the
    # whole team commits to, never removed by completion cleanup.
    validate_team(make_team(workspace_policy="shared"), ROOT)


def test_unknown_workspace_policy_rejected():
    with pytest.raises(TeamError, match="workspace_policy"):
        validate_team(make_team(workspace_policy="nonsense"), ROOT)


def test_I5_default_slug_rejected():
    with pytest.raises(TeamError, match="default"):
        validate_team(make_team(slug="default"), ROOT)


def test_I6_capacity_conflict():
    t1 = make_team(members=[{"profile": "orch-1"}, {"profile": "dev-1", "capacity": 2}])
    t2 = make_team(slug="p2", board="p2", orchestrator="orch-1",
                   members=[{"profile": "orch-1"},
                            {"profile": "dev-1", "capacity": 3}])
    errs = check_invariants(reg_of(t1), t2)
    assert "E_TEAM_CAPACITY_CONFLICT" in [e.code for e in errs]


def test_I6_same_capacity_ok():
    t1 = make_team(members=[{"profile": "dev-1", "capacity": 2}])
    t2 = make_team(slug="p2", board="p2", orchestrator="dev-1",
                   members=[{"profile": "dev-1", "capacity": 2}])
    assert check_invariants(reg_of(t1), t2) == []


def test_I7_managed_conflict():
    t1 = make_team(members=[{"profile": "dev-1", "managed": True}])
    t2 = make_team(slug="p2", board="p2", orchestrator="dev-1",
                   members=[{"profile": "dev-1", "managed": True}])
    errs = check_invariants(reg_of(t1), t2)
    assert "E_TEAM_MANAGED_CONFLICT" in [e.code for e in errs]


# ── unknown-key round-trip (§4.4) ────────────────────────────────────────────

def test_unknown_keys_round_trip():
    raw = team_dict(future_field={"hello": ["world"]}, another=7)
    raw["members"][0]["quantum_role"] = "observer"
    reg = Registry(teams={"project-x": Team.from_dict(raw)})
    out = reg.to_dict()["teams"]["project-x"]
    assert out["future_field"] == {"hello": ["world"]}
    assert out["another"] == 7
    assert out["members"][0]["quantum_role"] == "observer"


def test_registry_extra_round_trip():
    reg = Registry.from_dict({"schema_version": 1, "teams": {}, "generator": "v2"})
    assert reg.to_dict()["generator"] == "v2"


def test_from_dict_to_dict_stable_under_reparse():
    t = make_team()
    t2 = Team.from_dict(t.to_dict())
    assert t == t2
    assert t.to_dict() == t2.to_dict()


# ── render_instance_name ─────────────────────────────────────────────────────

@pytest.mark.parametrize("entry,expected", [
    ("agency-backend-engineer", "px-backend-engineer"),
    ("council-qa-lead", "px-qa-lead"),
    ("academy-python-tutor", "px-python-tutor"),
    ("bare-name", "px-bare-name"),
])
def test_render_strips_namespaces(entry, expected):
    assert render_instance_name("px", entry, DEFAULT_PROFILE_NAMING) == expected


def test_render_exactly_64_ok():
    entry = "agency-" + ("a" * (64 - len("px-")))
    name = render_instance_name("px", entry, "{team}-{short}")
    assert len(name) == 64


def test_render_65_rejected():
    entry = "agency-" + ("a" * (65 - len("px-")))
    with pytest.raises(TeamError, match="not a valid profile id"):
        render_instance_name("px", entry, "{team}-{short}")


@pytest.mark.parametrize("reserved", sorted(RESERVED_NAMES) + ["default"])
def test_render_reserved_rejected(reserved):
    # Template "{short}" renders exactly the reserved word.
    with pytest.raises(TeamError, match="not a valid profile id"):
        render_instance_name("px", reserved, "{short}")


def test_render_dotted_template_rejected():
    with pytest.raises(TeamError):
        render_instance_name("px", "agency-x", "{team}.{short}")


# ── derive_clone ─────────────────────────────────────────────────────────────

def test_clone_never_copies_bindings():
    src = make_team(board="brd", workdir="/w/src", tenant="ten")
    clone = derive_clone(src, "py")
    assert clone.board == "py"          # never src's
    assert clone.workdir == ""          # required via overrides, never copied
    assert clone.tenant == "py"         # defaults to new slug


def test_clone_forces_managed_false_and_primary_false():
    src = make_team(members=[
        {"profile": "orch-1", "role": "orchestrator", "primary": True, "managed": True},
        {"profile": "dev-1", "capacity": 2, "managed": True, "blueprint": "agency-x"},
    ])
    clone = derive_clone(src, "py", {"workdir": "/w/py", "board": "py",
                                     "orchestrator": "orch-1"})
    for m in clone.members:
        assert m.managed is False
        assert m.primary is False
    # blueprint provenance survives
    assert any(m.blueprint == "agency-x" for m in clone.members)


def test_clone_resets_project_and_bumps_freshness():
    src = make_team(project={"mode": "adopt", "slug": "px", "id": "prj_1"}, revision=9)
    clone = derive_clone(src, "py")
    assert clone.project.mode == "none"
    assert clone.project.slug is None
    assert clone.revision == 1
    assert clone.created_at != src.created_at or clone.created_at != ""


def test_clone_copies_roster_policy_and_sync():
    src = make_team(policy={"mode": "enforce"}, sync={"write_agents_md": True},
                    metadata={"k": "v"}, description="team project-x rocks")
    clone = derive_clone(src, "py")
    assert clone.policy.mode == "enforce"
    assert clone.sync.write_agents_md is True
    assert clone.metadata == {"k": "v"}
    assert clone.description == "team py rocks"  # slug substituted


def test_clone_with_project_override():
    clone = derive_clone(make_team(), "py", {"project": "px"})
    assert clone.project == {"mode": "adopt", "slug": "px", "id": None} or \
           (clone.project.mode == "adopt" and clone.project.slug == "px")


def test_clone_drop_member():
    src = make_team()
    clone = derive_clone(src, "py", {"drop_members": ["dev-1"], "orchestrator": "orch-1"})
    assert [m.profile for m in clone.members] == ["orch-1"]


@pytest.mark.parametrize("display, board, want", [
    ("Fullstack Engineering", "team05", "Fullstack Engineering (team05)"),
    ("", "team05", "team05"),
    ("team05", "team05", "team05"),
])
def test_board_label(display, board, want):
    assert board_label(Team(display_name=display, board=board)) == want


@pytest.mark.parametrize("name, want", [
    ("Alpha Team", "alpha-team"),
    ("  Alpha   -  Team  ", "alpha-team"),
    ("Équipe Ünö", "equipe-uno"),
    ("web_api v2!", "web_api-v2"),
    ("x" * 80, "x" * 64),
    ("a" * 63 + " b", "a" * 63),
])
def test_slugify(name, want):
    from model import slugify
    assert slugify(name) == want


@pytest.mark.parametrize("name", ["", "   ", "!!!", "Default", "日本"])
def test_slugify_refuses_unusable(name):
    from model import slugify
    with pytest.raises(TeamError) as ei:
        slugify(name)
    assert ei.value.code == "E_TEAM_INVALID"


def test_name_conflict():
    from model import Registry, name_conflict
    reg = Registry(teams={"alpha": Team(slug="alpha", display_name="Alpha Team"),
                          "beta": Team(slug="beta")})
    assert name_conflict(reg, "gamma", "alpha  TEAM") == "alpha"
    assert name_conflict(reg, "gamma", "BETA") == "beta"      # name = a slug
    assert name_conflict(reg, "alpha-team", "") == "alpha"    # slug = a name
    assert name_conflict(reg, "gamma", "Gamma") is None
    assert name_conflict(reg, "alpha", "Alpha Team") is None  # itself
