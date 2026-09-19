"""S9 — reconcile.diff, the drift catalogue (§9.1/§9.2).

One test per catalogue row: each constructs exactly that drift in a temp
world (an instantiated orchestrator + an adopted verifier on a dedicated
board, everything else clean) and asserts the ``(check, severity,
auto_fixable)`` triple. The three skip rules are asserted
as *absence* of a fixable item plus *presence* of the corresponding
``info``. And the check that must NOT exist (§9.2 "Deliberately not a
check"): nothing compares SOUL.md, ``skills/`` or the body of
``config.yaml`` against the blueprint — edit all three and assert
``diff()`` still reports zero drift (the ``sync --dry-run`` CLI wiring of
this assertion is S11).

Everything lives under the conftest ``hermes_root`` tmp root; the live
``config/`` tree is never touched.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import types

import pytest
import yaml

import catalog
import catalog_store
import provision
import reconcile
import resolve
import storage
from hermes_cli import kanban_db as kb
from hermes_cli import profiles as hc_profiles
from hermes_cli import projects_db as pdb
from model import Member, Policy, Project, Team

from test_catalog import _make_pack_root


# ── staging helpers ──────────────────────────────────────────────────────────

def _stage_config(home, cfg):
    """Merge cfg into <home>/config.yaml (provision leaves a live file)."""
    p = home / "config.yaml"
    cur = {}
    if p.is_file():
        try:
            cur = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(cfg)
    p.write_text(yaml.safe_dump(cur, sort_keys=False), encoding="utf-8")


def _stage_member(profile, home, *, workdir, orchestrator=False,
                  description="does things", write_meta=True, f1_link=True,
                  strip_env=True):
    """Plane-1-clean + F1-complete + non-empty description: the per-member
    'no drift' baseline."""
    cfg = {
        "terminal": {"cwd": str(workdir)},
        "toolsets": ["team"] + (["kanban"] if orchestrator else []),
        "plugins": {"enabled": ["hermes-agent-synergia"]},
    }
    if orchestrator:
        cfg["kanban"] = {"orchestrator_profile": profile,
                         "default_assignee": profile}
    _stage_config(home, cfg)
    if write_meta:
        hc_profiles.write_profile_meta(home, description=description)
    if f1_link:
        link = home / "plugins" / "hermes-agent-synergia"
        if not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(os.path.join("..", "..", "..", "plugins", "hermes-agent-synergia"),
                       link)
    if strip_env:
        env = home / ".env"
        if env.is_file() and not env.is_symlink():
            env.unlink()   # a seeded placeholder is *staged drift*, not ambience


@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    """Per-test tmp root + cold caches (never the live registry/catalog)."""
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()
    yield


@pytest.fixture
def world(hermes_root, profile, tmp_path):
    """A fully-clean team ``teamx``: pack-instantiated orchestrator, adopted
    verifier, dedicated board, AGENTS.md, F1 everywhere. diff() == []."""
    pack_root = _make_pack_root(tmp_path / "packs")
    plugin_dir = hermes_root / "plugins" / "hermes-agent-synergia"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: hermes-agent-synergia\n",
                                            encoding="utf-8")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["hermes-agent-synergia"],
                     "entries": {"hermes-agent-synergia": {"settings": {
                         "catalog_paths": [str(pack_root)]}}}}},
        sort_keys=False), encoding="utf-8")

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "AGENTS.md").write_text("# Work in this repo\n", encoding="utf-8")
    kb.create_board("teamx-board", name="Team X (teamx-board)",
                    default_workdir=str(workdir))

    be = "teamx-backend-engineer"
    provision.instantiate(catalog.get("agency-backend-engineer"), be,
                          role="implementer")
    # instantiate already wrote the F1 symlink, plugins.enabled and a
    # non-empty profile.yaml description — only plane 1 / toolsets are ours.
    _stage_member(be, resolve.profile_home(be), workdir=workdir,
                  orchestrator=True, write_meta=False)

    qa_home = profile("qa", {})
    _stage_member("qa", qa_home, workdir=workdir)

    team = Team(
        slug="teamx", display_name="Team X", board="teamx-board",
        workdir=str(workdir), tenant="teamx", orchestrator=be,
        members=(
            Member(profile=be, role="implementer",
                   blueprint="agency-backend-engineer",
                   blueprint_source="pack:hermes-agency",
                   managed=True, primary=True),
            Member(profile="qa", role="verifier", primary=True),
        ),
    )
    return types.SimpleNamespace(
        team=team, root=hermes_root, workdir=workdir, be=be, qa=qa_home,
        pack_root=pack_root)


def checks(team) -> dict:
    """{check id: [DriftItem, ...]} over the full diff."""
    out: dict = {}
    for it in reconcile.diff(team):
        out.setdefault(it.check, []).append(it)
    return out


def only(got, check):
    """The single item for *check*, with its §9.2 triple pre-asserted."""
    items = got.get(check) or []
    assert len(items) == 1, f"{check}: expected 1 item, got {items!r}"
    return items[0]


# ── the clean world, and diff purity ────────────────────────────────────────

def test_clean_world_has_zero_drift(world):
    assert reconcile.diff(world.team) == []


def test_diff_is_pure_and_repeatable(world):
    once = [it.to_dict() for it in reconcile.diff(world.team)]
    assert [it.to_dict() for it in reconcile.diff(world.team)] == once
    # every item round-trips through the §9.1 dict shape
    rmtree = shutil.rmtree
    rmtree(resolve.profile_home("qa"))
    for d in (it.to_dict() for it in reconcile.diff(world.team)):
        assert set(d) == {"check", "severity", "subject", "observed",
                          "desired", "auto_fixable", "remedy"}


def test_observe_and_diff_fills_the_drift_slot(world):
    assert resolve.observe(world.team).drift == []      # observe never computes drift
    assert reconcile.observe_and_diff(world.team).drift == []
    shutil.rmtree(resolve.profile_home("qa"))
    filled = reconcile.observe_and_diff(world.team)
    assert [i.check for i in filled.drift] == ["member.exists"]


# ── member rows ─────────────────────────────────────────────────────────────

def test_member_exists(world):
    shutil.rmtree(resolve.profile_home("qa"))
    it = only(checks(world.team), "member.exists")
    assert (it.severity, it.auto_fixable) == ("error", False)
    assert it.subject == "profile:qa"
    assert "hermes profile create qa" in it.remedy


def test_member_instance_missing_only_under_reinstantiate(world):
    # adopted member deleted ⇒ only member.exists (never re-create an
    # adoption) ...
    shutil.rmtree(resolve.profile_home("qa"))
    got = checks(world.team)
    assert "member.exists" in got and "member.instance_missing" not in got
    # managed instance deleted ⇒ both rows; fixable only under the flag,
    # so the diff itself carries auto_fixable False (§9.3, gate = S10)
    shutil.rmtree(resolve.profile_home(world.be))
    it = only(checks(world.team), "member.instance_missing")
    assert (it.severity, it.auto_fixable) == ("error", False)
    assert it.subject == f"profile:{world.be}"
    assert "--reinstantiate" in it.remedy


def test_member_instance_tombstoned_owns_the_member(world):
    import hermes_constants
    hermes_constants.mark_named_profile_deleted(resolve.profile_home("qa"))
    got = checks(world.team)
    it = only(got, "member.instance_tombstoned")
    assert (it.severity, it.auto_fixable) == ("error", False)
    assert it.subject == "profile:qa"
    # no contradicting "just create it" row while the marker stands
    assert "member.exists" not in got
    assert "member.instance_missing" not in got


def test_member_blueprint_unknown_is_provenance_info(world):
    t = dataclasses.replace(
        world.team,
        members=tuple(
            dataclasses.replace(m, blueprint="agency-gone",
                                blueprint_source="pack:gone-pack")
            if m.profile == "qa" else m for m in world.team.members))
    it = only(checks(t), "member.blueprint_unknown")
    assert (it.severity, it.auto_fixable) == ("info", False)
    assert it.subject == "profile:qa"
    assert it.observed == "blueprint 'agency-gone' not in catalog"
    # the managed member's KNOWN blueprint stays silent
    assert [i.subject for i in checks(t).get("member.blueprint_unknown", [])] \
        == ["profile:qa"]


def test_member_terminal_cwd_mismatch(world, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    _stage_config(resolve.profile_home("qa"), {"terminal": {"cwd": str(other)}})
    it = only(checks(world.team), "member.terminal_cwd")
    assert (it.severity, it.auto_fixable) == ("error", True)
    assert (it.observed, it.desired) == (str(other), str(world.workdir))
    assert "surgical" in it.remedy


def test_member_terminal_cwd_sentinel_is_drift(world, profile):
    """§2.6: ``terminal.cwd: "."`` reads as UNSET (S7 sentinel rule) —
    which is drift for a primary member, not a match."""
    _stage_config(resolve.profile_home("qa"), {"terminal": {"cwd": "."}})
    it = only(checks(world.team), "member.terminal_cwd")
    assert it.observed is None and it.desired == str(world.workdir)


def test_member_toolsets_additive_only(world):
    _stage_config(resolve.profile_home("qa"), {"toolsets": ["browser"]})
    it = only(checks(world.team), "member.toolsets")
    assert (it.severity, it.auto_fixable) == ("warn", True)
    assert (it.observed, it.desired) == (["browser"], ["browser", "team"])
    # the orchestrator additionally needs `kanban` (restore qa first)
    _stage_config(resolve.profile_home("qa"), {"toolsets": ["browser", "team"]})
    _stage_config(resolve.profile_home(world.be), {"toolsets": ["team"]})
    it = only(checks(world.team), "member.toolsets")
    assert it.subject == f"profile:{world.be}" and it.desired == ["team", "kanban"]


def test_member_description_fires_only_when_empty(world):
    hc_profiles.write_profile_meta(resolve.profile_home("qa"), description="")
    it = only(checks(world.team), "member.description")
    assert (it.severity, it.auto_fixable) == ("warn", True)
    assert it.observed == ""
    assert it.subject == "profile:qa"
    # a non-empty description is NEVER touched, however stale (§9.2)
    hc_profiles.write_profile_meta(resolve.profile_home("qa"),
                                   description="operator prose, not mine")
    assert "member.description" not in checks(world.team)


def test_member_profile_meta_only_for_pack_members_with_blueprint(world):
    hc_profiles.write_profile_meta(resolve.profile_home(world.be),
                                   description="")
    got = checks(world.team)
    it = only(got, "member.profile_meta")
    assert (it.severity, it.auto_fixable) == ("warn", True)
    assert it.subject == f"profile:{world.be}"
    assert it.observed == ""
    assert it.desired == catalog.get("agency-backend-engineer").description
    # the adopted member's empty description never yields profile_meta —
    # there is no blueprint to source a description from
    hc_profiles.write_profile_meta(resolve.profile_home("qa"), description="")
    got2 = checks(world.team)
    assert all(i.subject == f"profile:{world.be}"
               for i in got2.get("member.profile_meta", []))
    assert any(i.subject == "profile:qa"
               for i in got2.get("member.description", []))


def test_member_capacity_mismatch_written_when_primary(world):
    t = dataclasses.replace(
        world.team,
        members=tuple(dataclasses.replace(m, capacity=3)
                      if m.profile == "qa" else m for m in world.team.members))
    it = only(checks(t), "member.capacity")
    assert (it.severity, it.auto_fixable) == ("info", True)   # I6
    assert (it.observed, it.desired) == (None, 3)
    assert "kanban.max_in_progress_per_profile" in it.remedy


def test_member_plugin_link_missing_and_dangling(world):
    link = resolve.profile_home("qa") / "plugins" / "hermes-agent-synergia"
    link.unlink()
    it = only(checks(world.team), "member.plugin_link")
    assert (it.severity, it.auto_fixable) == ("error", True)   # F1
    assert it.observed == "missing" and it.subject == "profile:qa"
    link.symlink_to("nowhere")
    assert only(checks(world.team), "member.plugin_link").observed \
        == "dangling symlink -> nowhere"


def test_member_plugin_enabled(world):
    _stage_config(resolve.profile_home("qa"), {"plugins": {"enabled": []}})
    it = only(checks(world.team), "member.plugin_enabled")
    assert (it.severity, it.auto_fixable) == ("error", True)   # F1
    assert (it.observed, it.desired) == ([], ["hermes-agent-synergia"])


def test_member_env_link_gated_on_root_env(world):
    env = resolve.profile_home(world.be) / ".env"
    env.write_text("KEY=seeded\n", encoding="utf-8")
    # no root .env ⇒ nothing to link to ⇒ no item (F7: a placeholder only
    # drifts when the real target exists)
    assert "member.env_link" not in checks(world.team)
    (world.root / ".env").write_text("KEY=root\n", encoding="utf-8")
    it = only(checks(world.team), "member.env_link")
    assert (it.severity, it.auto_fixable) == ("warn", False)   # --link-env gate
    assert "--link-env" in it.remedy
    assert it.observed == "seeded placeholder .env"
    assert str(world.root / ".env") in it.desired


# ── board rows ──────────────────────────────────────────────────────────────

def test_board_exists(world):
    shutil.rmtree(kb.board_dir("teamx-board"))
    it = only(checks(world.team), "board.exists")
    assert (it.severity, it.auto_fixable) == ("error", False)  # --create-board gate
    assert it.subject == "board:teamx-board"
    assert "--create-board" in it.remedy
    # a missing board suppresses every other board-plane row
    got = checks(world.team)
    assert not {"board.default_workdir", "board.display",
                "board.project_id", "tenant.unstamped"} & set(got)


def test_board_default_workdir_mismatch(world, tmp_path):
    kb.write_board_metadata("teamx-board", default_workdir=str(tmp_path))
    it = only(checks(world.team), "board.default_workdir")
    assert (it.severity, it.auto_fixable) == ("error", True)
    assert (it.observed, it.desired) == (str(tmp_path), str(world.workdir))


def test_skip_rule_board_default_workdir_shared(world):
    """``shared_board`` ⇒ the write is skipped and the dry run says so
    — info, never fixable, even while the board points elsewhere."""
    kb.write_board_metadata("teamx-board", default_workdir=str(world.workdir.parent))
    got = checks(dataclasses.replace(world.team, shared_board=True))
    it = only(got, "board.default_workdir")
    assert (it.severity, it.auto_fixable) == ("info", False)
    assert "shared_board" in it.remedy
    # and on a clean board the shared team emits the same explanatory info
    kb.write_board_metadata("teamx-board", default_workdir=str(world.workdir))
    assert only(checks(dataclasses.replace(
        world.team, shared_board=True)), "board.default_workdir").severity == "info"


def test_board_display(world):
    kb.write_board_metadata("teamx-board", name="Stale")
    it = only(checks(world.team), "board.display")
    assert (it.severity, it.auto_fixable) == ("info", True)
    assert (it.observed, it.desired) == ("Stale", "Team X (teamx-board)")


def test_board_project_id(world):
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="Proj X", slug="teamx",
                                 folders=[str(world.workdir)])
    t = dataclasses.replace(world.team, project=Project(mode="adopt", slug="teamx"))
    it = only(checks(t), "board.project_id")
    assert (it.severity, it.auto_fixable) == ("warn", True)
    assert (it.observed, it.desired) == (None, pid)
    # mode none ⇒ never a write request, even with a resolvable row
    assert "board.project_id" not in checks(
        dataclasses.replace(world.team, project=Project(mode="none")))


def test_board_exclusivity(world):
    other = Team(slug="other", display_name="Other", board="teamx-board",
                 workdir=str(world.workdir.parent / "other-wd"),
                 tenant="other", orchestrator="qa",
                 members=(Member(profile="qa", role="member", primary=True),))
    storage.upsert(other)
    try:
        it = only(checks(world.team), "board.exclusivity")
        # I1 on the read side: an operator decision, never a sync fix
        assert (it.severity, it.auto_fixable) == ("error", False)
        assert "other" in it.observed
        # one-sided shared still conflicts (I1: shared on BOTH records)
        assert only(checks(world.team), "board.exclusivity").severity == "error"
        # shared_board on BOTH sides ⇒ the exemption applies
        storage.upsert(dataclasses.replace(other, shared_board=True))
        got = checks(dataclasses.replace(world.team, shared_board=True))
        assert "board.exclusivity" not in got
    finally:
        storage.delete("other")
    assert "board.exclusivity" not in checks(world.team)


# ── workdir rows ────────────────────────────────────────────────────────────

def test_workdir_exists(world, tmp_path):
    t = dataclasses.replace(world.team, workdir=str(tmp_path / "not-yet"))
    it = only(checks(t), "workdir.exists")
    assert (it.severity, it.auto_fixable) == ("error", False)  # --create-workdir gate
    assert "--create-workdir" in it.remedy


def test_workdir_git_only_for_worktree_policy(world):
    assert "workdir.git" not in checks(world.team)   # scratch: no repo needed
    t = dataclasses.replace(world.team, workspace_policy="worktree")
    it = only(checks(t), "workdir.git")
    assert (it.severity, it.auto_fixable) == ("error", True)   # apply inits it
    assert it.remedy == f"hermes team sync {t.slug} --apply"
    (world.workdir / ".git").mkdir()                 # a repo without HEAD
    assert "workdir.git" in checks(t)                #   still cannot spawn
    shutil.rmtree(world.workdir / ".git")
    resolve.ensure_worktree_repo(str(world.workdir))
    assert "workdir.git" not in checks(t)


def test_workdir_agents_md_setting_gated(world):
    (world.workdir / "AGENTS.md").unlink()
    it = only(checks(world.team), "workdir.agents_md")
    assert (it.severity, it.auto_fixable) == ("info", False)
    assert "write_agents_md" in it.remedy
    t = dataclasses.replace(
        world.team,
        sync=dataclasses.replace(world.team.sync, write_agents_md=True))
    assert only(checks(t), "workdir.agents_md").auto_fixable is True


# ── orchestrator / project / tenant / policy / catalog rows ────────────────

def test_orchestrator_member(world):
    t = dataclasses.replace(world.team, orchestrator="stranger")
    it = only(checks(t), "orchestrator.member")
    assert (it.severity, it.auto_fixable) == ("error", False)   # I3
    assert "hermes team add-member teamx stranger" in it.remedy


def test_orchestrator_config(world):
    _stage_config(resolve.profile_home(world.be),
                  {"kanban": {"default_assignee": "qa"}})
    it = only(checks(world.team), "orchestrator.config")
    assert (it.severity, it.auto_fixable) == ("warn", True)     # one atomic write
    assert it.observed["kanban.default_assignee"] == "qa"
    assert it.desired == {"kanban.orchestrator_profile": world.be,
                          "kanban.default_assignee": world.be}
    assert "atomic" in it.remedy


def test_project_resolves(world):
    t = dataclasses.replace(world.team,
                            project=Project(mode="adopt", slug="nope"))
    it = only(checks(t), "project.resolves")
    assert (it.severity, it.auto_fixable) == ("info", False)    # §9.2: ⚠ create only
    assert "hermes project create" in it.remedy
    t2 = dataclasses.replace(world.team,
                             project=Project(mode="create", slug="fresh"))
    assert only(checks(t2), "project.resolves").auto_fixable is True
    # mode none never asks
    assert "project.resolves" not in checks(world.team)


def test_tenant_unstamped(world):
    with resolve._kanban_conn().connect_closing(board="teamx-board") as conn:
        kb.create_task(conn, title="forgotten", tenant=None,
                       initial_status="running")
    it = only(checks(world.team), "tenant.unstamped")
    assert (it.severity, it.auto_fixable) == ("warn", False)    # a CLI verb fixes it
    assert "--backfill-tenant" in it.remedy
    assert it.observed == "1 task(s) with no tenant stamp"
    # shared_board ⇒ the heuristic is unreliable (another team's tasks are
    # normal) ⇒ never nag, even unstamped
    assert "tenant.unstamped" not in checks(
        dataclasses.replace(world.team, shared_board=True))


def test_policy_hook(world):
    cfg = yaml.safe_load((world.root / "config.yaml").read_text(encoding="utf-8"))
    cfg["plugins"]["enabled"] = []
    (world.root / "config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    t = dataclasses.replace(world.team,
                            policy=dataclasses.replace(world.team.policy,
                                                       mode="enforce"))
    it = only(checks(t), "policy.hook")
    assert (it.severity, it.auto_fixable) == ("warn", False)
    assert "hermes plugins enable hermes-agent-synergia" in it.remedy
    # policy off ⇒ a hook the host never loads is not our problem
    assert "policy.hook" not in checks(
        dataclasses.replace(world.team,
                            policy=dataclasses.replace(world.team.policy,
                                                       mode="off")))


def test_catalog_source_missing(world):
    cfg = yaml.safe_load((world.root / "config.yaml").read_text(encoding="utf-8"))
    cfg["plugins"]["entries"]["hermes-agent-synergia"]["settings"]["catalog_paths"] = [
        str(world.pack_root), str(world.root / "gone")]
    (world.root / "config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    catalog._reset_caches_for_tests()
    it = only(checks(world.team), "catalog.source_missing")
    assert (it.severity, it.auto_fixable) == ("info", False)    # Info only
    assert "gone" in str(it.observed)
    assert "catalog_paths" in it.remedy


def test_catalog_store_orphan(world):
    (catalog_store.profiles_dir() / "stray").mkdir(parents=True)
    it = only(checks(world.team), "catalog.store_orphan")
    assert (it.severity, it.auto_fixable) == ("info", False)
    assert "stray" in str(it.observed)


# ── the skip rules — absence of fix + presence of info ─────────────────────

def _other_team(world, **member_kw):
    mk = {"role": "member"}
    mk.update(member_kw)
    return Team(slug="other", display_name="Other", board="other-board",
                workdir=str(world.workdir.parent / "other-wd"),
                tenant="other", orchestrator="qa",
                members=(Member(profile="qa", **mk),))


def test_skip_rule_terminal_cwd_owned_by_team(world):
    """Member of THIS team with ``primary: false``, primary elsewhere ⇒
    plane 1 belongs to that team: no fixable item, one info naming it."""
    storage.upsert(_other_team(world, primary=True))
    try:
        t = dataclasses.replace(
            world.team,
            members=tuple(dataclasses.replace(m, primary=False)
                          if m.profile == "qa" else m
                          for m in world.team.members))
        got = checks(t)
        # ... and the actual mismatch (qa's cwd points elsewhere) must NOT
        # produce a fixable terminal_cwd item
        _stage_config(resolve.profile_home("qa"),
                      {"terminal": {"cwd": str(world.workdir.parent)}})
        got = checks(t)
        assert not [i for i in got.get("member.terminal_cwd", [])
                    if i.auto_fixable]
        assert only(got, "member.terminal_cwd").severity == "info"
        assert only(got, "member.terminal_cwd").observed == "owned by team other"
    finally:
        storage.delete("other")


def test_skip_rule_capacity_owned_by_team(world):
    """Capacity is written only when set AND this team is the
    profile's primary — otherwise info, no write."""
    storage.upsert(_other_team(world, primary=True, capacity=3))
    try:
        t = dataclasses.replace(
            world.team,
            members=tuple(dataclasses.replace(m, primary=False, capacity=3)
                          if m.profile == "qa" else m
                          for m in world.team.members))
        got = checks(t)
        assert not [i for i in got.get("member.capacity", []) if i.auto_fixable]
        it = only(got, "member.capacity")
        assert (it.severity, it.auto_fixable) == ("info", False)
        assert it.observed == "owned by team other"
    finally:
        storage.delete("other")


def test_skip_rule_no_primary_anywhere(world):
    """A profile no team claims as primary ⇒ plane 1 AND
    capacity are skipped entirely; the remedy points at --primary."""
    t = dataclasses.replace(
        world.team,
        members=tuple(dataclasses.replace(m, primary=False, capacity=3)
                      if m.profile == "qa" else m
                      for m in world.team.members))
    _stage_config(resolve.profile_home("qa"),
                  {"terminal": {"cwd": str(world.workdir.parent)}})
    got = checks(t)
    assert not [i for i in got.get("member.terminal_cwd", []) if i.auto_fixable]
    assert not [i for i in got.get("member.capacity", []) if i.auto_fixable]
    tcwd = only(got, "member.terminal_cwd")
    assert (tcwd.severity, tcwd.auto_fixable) == ("info", False)
    assert tcwd.observed == "no primary team"
    assert "hermes team add-member teamx qa --primary" in tcwd.remedy
    cap = only(got, "member.capacity")
    assert (cap.severity, cap.auto_fixable) == ("info", False)
    assert cap.observed == "no primary team"


# ── the check that must NOT exist (§9.2) ───────────────────────────────────

def test_no_content_drift_soul_skills_config(world):
    """The instantiator does not create a master/pupil relationship: edit
    SOUL.md, add a skill, add a config key — diff() stays [] (asserted at
    the diff() level; the ``sync --dry-run`` CLI wiring is S11)."""
    assert reconcile.diff(world.team) == []
    be_home = resolve.profile_home(world.be)
    (be_home / "SOUL.md").write_text("I am a ghost now.\n", encoding="utf-8")
    skill = be_home / "skills" / "operator-only"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    _stage_config(be_home, {"model": "some-operator-choice"})
    assert reconcile.diff(world.team) == []


def test_remedy_and_shape_for_every_item(world):
    """§9.1: every item carries a human-readable remedy and a legal
    severity, whatever the row."""
    shutil.rmtree(resolve.profile_home("qa"))
    shutil.rmtree(kb.board_dir("teamx-board"))
    (world.workdir / "AGENTS.md").unlink()
    items = reconcile.diff(world.team)
    assert items, "the mutated world must report drift"
    for it in items:
        assert isinstance(it.remedy, str) and it.remedy, f"{it.check}: empty remedy"
        assert it.severity in reconcile.SEVERITIES
        assert isinstance(it.auto_fixable, bool)
