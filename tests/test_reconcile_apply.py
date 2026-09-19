"""S10 — reconcile.apply: ordering, batching, idempotence (§9.3/§9.4).

One test per Verify bullet of the step plan (§14.3), each run against a
deliberately DIRTY world (stale board metadata, a drifted adopted member):

* fresh team → drift → ``--apply`` → zero drift, and the second apply is a
  proven no-op — idempotence asserted by ``mtime_ns`` **and** byte comparison
  on every touched file (the spec calls this a core property, not a nicety);
* per-file batching (one ``write_profile_config_keys`` call per file, never N);
* the gates — plain apply reports ``member.instance_missing`` / ``member.env_link``
  and creates NEITHER (asserted as absence of a write);
* ordering — ``terminal.cwd`` never written pointing at a workdir this run has
  yet to create; capacity conflicts (I6 bypassed by a hand-edited registry)
  refused with ``E_TEAM_CAPACITY_CONFLICT`` and the flap regression pinned;
* refusals (managed install, unreadable config) record ``applied: false`` and
  write nothing; the delegated-child guard becomes a DriftItem, never a raise;
* the §9.4 restart notice appears when and only when a config changed.

The staging helpers come from the S9 diff suite unchanged — the apply half
must close exactly the drift the diff half found, nothing more, nothing less.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import types

import pytest
import yaml

import catalog
import provision
import reconcile
import resolve
import storage
from errors import E_TEAM_CAPACITY_CONFLICT, E_TEAM_MANAGED
from hermes_cli import kanban_db as kb
from hermes_cli import profiles as hc_profiles
from model import Member, Project, Team

from test_catalog import _make_pack_root
from test_reconcile_diff import _stage_config, _stage_member


# ── the dirty world ───────────────────────────────────────────────────────────

def _snap(paths) -> dict:
    """{path: (mtime_ns, sha256)} — the §14.3 idempotence witness."""
    out = {}
    for p in (str(x) for x in paths):
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                data = fh.read()
            out[p] = (os.stat(p).st_mtime_ns, hashlib.sha256(data).hexdigest())
        else:
            out[p] = None
    return out


# A block-style seed document (the flow text "{}" is re-serialised by yaml,
# and the line editor deliberately refuses to reshape a foreign document
# shape it cannot re-parse — test_config_writer proves the writer on real
# block-style templates, so the apply world seeds ones too). The sentinel
# cwd, the empty toolsets and the empty plugins.enabled are the staged drift.
_SEED = """\
# hermes profile config
terminal:
  cwd: '.'
toolsets: []
plugins:
  enabled: []
"""


def _raw_profile(profile, name):
    """A freshly created profile holding the seed document (drifted by
    construction: sentinel cwd, empty toolsets/plugins, no description)."""
    return profile(name, _SEED)


@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    """Per-test tmp root + cold caches (never the live registry/catalog)."""
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()
    yield


@pytest.fixture
def world(hermes_root, profile, tmp_path):
    """Team ``teamx`` with staged drift: the orchestrator is instantiated
    and clean; ``qa`` is a raw profile (no F1 link, no cwd, no toolsets, no
    description); the board's ``default_workdir`` is stale on purpose. The
    ONLY drift: board.default_workdir + everything qa touches."""
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
    # stale on purpose: default_workdir points one level up
    kb.create_board("teamx-board", name="Team X (teamx-board)",
                    default_workdir=str(tmp_path))

    be = "teamx-backend-engineer"
    provision.instantiate(catalog.get("agency-backend-engineer"), be,
                          role="implementer")
    _stage_member(be, resolve.profile_home(be), workdir=workdir,
                  orchestrator=True, write_meta=False)

    qa_home = _raw_profile(profile, "qa")   # seed doc: drifted on purpose

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
        pack_root=pack_root, tmp=tmp_path)


def _touched_files(world):
    """Every file an apply of *world* may write (§14.3 snapshot set)."""
    be_home = resolve.profile_home(world.be)
    return [
        resolve.profile_config_path("qa"),
        resolve.profile_home("qa") / "profile.yaml",
        resolve.profile_config_path(world.be),
        be_home / "profile.yaml",
        kb.board_metadata_path("teamx-board"),
    ]


def _spy_writes(monkeypatch) -> list:
    """Record every write_profile_config_keys call (delegating for real)."""
    calls: list = []
    real = resolve.write_profile_config_keys

    def spy(profile, updates):
        calls.append((profile, dict(updates)))
        return real(profile, updates)

    monkeypatch.setattr(resolve, "write_profile_config_keys", spy)
    return calls


def _applied(report, check):
    return [a for a in report.applied if a.check == check]


def _refusals(report):
    return [a for a in report.applied if not a.ok]


# ── fresh team → apply → zero drift (§9.3 happy path) ────────────────────────

def test_apply_clears_every_drift(world):
    assert reconcile.diff(world.team), "world must start drifted"
    report = reconcile.apply(world.team)
    assert report.drift == [], [d.to_dict() for d in report.drift]
    assert all(a.ok for a in report.applied)
    assert report.exit_code == 0
    assert report.slug == "teamx"
    # the writes actually landed where the diff pointed
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert cfg["terminal"]["cwd"] == str(world.workdir)
    assert cfg["plugins"]["enabled"] == ["hermes-agent-synergia"]
    assert cfg["toolsets"] == ["team"]
    meta = yaml.safe_load(
        (resolve.profile_home("qa") / "profile.yaml").read_text(
            encoding="utf-8"))
    assert meta["description"] == "Team X verifier (team teamx)"
    assert meta["description_auto"] is False
    # one board write carried the metadata fix
    assert kb.read_board_metadata("teamx-board")["default_workdir"] \
        == str(world.workdir)


# ── idempotence: mtime_ns AND bytes on every touched file ────────────────────

def test_idempotence_mtime_and_bytes(world):
    reconcile.apply(world.team)
    snap1 = _snap(_touched_files(world))
    second = reconcile.apply(world.team)
    snap2 = _snap(_touched_files(world))
    assert snap1 == snap2, "second --apply touched a file"
    # nothing was even attempted: the diff is empty before the plan
    assert second.applied == []
    assert second.profiles_updated == 0
    assert second.restart_notice is None
    assert second.exit_code == 0


def test_batching_one_call_per_file_per_run(world, monkeypatch):
    calls = _spy_writes(monkeypatch)
    reconcile.apply(world.team)
    qa_calls = [c for c in calls if c[0] == "qa"]
    assert len(qa_calls) == 1, f"expected ONE call for qa, got {calls!r}"
    # every drifted qa key rode in that single atomic batch
    assert {"terminal.cwd", "plugins.enabled", "toolsets"} \
        <= set(qa_calls[0][1])
    # the clean orchestrator received no write at all
    assert [c for c in calls if c[0] == world.be] == []
    # second run: nothing to commit → not even a call
    calls.clear()
    reconcile.apply(world.team)
    assert calls == []


# ── the §9.4 restart notice: iff a config changed ────────────────────────────

def test_restart_notice_iff_config_changed(world, capsys):
    first = reconcile.apply(world.team)
    err = capsys.readouterr().err
    assert first.restart_notice is not None
    assert first.profiles_updated == 1                 # only qa's config moved
    assert first.restart_notice in err
    # the gateway re-reads terminal.cwd per turn: no restart, /new for chats
    assert "Open chats keep their old cwd: run /new in them to pick it up." \
        in err
    # the n is the count of PROFILES whose config moved, on line 1 verbatim
    # (the stream itself can carry unrelated stderr noise between tests)
    assert first.restart_notice.splitlines()[0] == (
        "1 profiles updated. New sessions and dispatched kanban workers use "
        "the new terminal.cwd now — no gateway restart needed.")
    # nothing changed the second time → nothing is printed
    capsys.readouterr()
    second = reconcile.apply(world.team)
    err2 = capsys.readouterr().err
    assert second.restart_notice is None
    assert "profiles updated" not in err2


# ── gates: plain apply creates neither an agent nor a credential path ────────

def test_reinstantiate_never_implicit(world):
    shutil.rmtree(resolve.profile_home(world.be))
    report = reconcile.apply(world.team)                  # no flag
    assert not resolve.profile_exists(world.be)           # absence of a write
    assert _applied(report, "member.instance_missing") == []
    assert [d for d in report.drift
            if d.check == "member.instance_missing"]      # reported...
    assert report.exit_code == 4
    # ...and ONLY the flag creates it, in the same run, converging to zero
    r2 = reconcile.apply(world.team, reinstantiate=True)
    assert resolve.profile_exists(world.be)
    assert r2.drift == [], [d.to_dict() for d in r2.drift]
    assert [a for a in r2.applied
            if a.check == "member.instance_missing" and a.ok]


def test_env_link_never_implicit(world):
    env = resolve.profile_home(world.be) / ".env"
    env.write_text("KEY=seeded\n", encoding="utf-8")
    (world.root / ".env").write_text("KEY=root\n", encoding="utf-8")
    report = reconcile.apply(world.team)                  # no flag
    assert env.is_file() and not env.is_symlink()         # absence of a write
    assert _applied(report, "member.env_link") == []
    assert [d for d in report.drift if d.check == "member.env_link"]
    # the flag converts it — symlink, never a copy (§13)
    r2 = reconcile.apply(world.team, link_env=True)
    assert env.is_symlink()
    assert os.path.realpath(env) == os.path.realpath(world.root / ".env")
    assert [a for a in r2.applied
            if a.check == "member.env_link" and a.ok]


def test_workdir_never_implicit_and_orders_the_cwd_write(world, tmp_path):
    """§9.3 ordering: with the workdir missing and no ``--create-workdir``,
    ``terminal.cwd`` is NOT written — a plain apply never points a member at
    a directory the run has yet to create."""
    t = dataclasses.replace(world.team, workdir=str(tmp_path / "not-yet"))
    report = reconcile.apply(t)
    assert not os.path.isdir(str(tmp_path / "not-yet"))
    qa_cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    # sentinel kept: cwd write withheld (§9.3 ordering)
    assert qa_cfg["terminal"]["cwd"] == "."
    assert not [a for a in report.applied
                if a.check == "member.terminal_cwd" and a.ok]
    assert [d for d in report.drift if d.check == "workdir.exists"]
    # with the flag: dir first, THEN the cwd, zero residual
    r2 = reconcile.apply(t, create_workdir=True)
    assert os.path.isdir(str(tmp_path / "not-yet"))
    qa_cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert qa_cfg["terminal"]["cwd"] == str(tmp_path / "not-yet")
    # no error/warn drift left; the only residue is the informational
    # workdir.agents_md row (the setting is off — an info, never a fix)
    hard = [d.to_dict() for d in r2.drift if d.severity in ("error", "warn")]
    assert hard == [], hard
    assert all(d.check == "workdir.agents_md" for d in r2.drift)


def test_worktree_workdir_git_initialized_implicitly(world):
    """workdir.git is not a §9.3-gated check: a plain apply inits the repo
    and snapshots the files already in the workdir."""
    (world.workdir / "app.py").write_text("print(1)\n")
    t = dataclasses.replace(world.team, workspace_policy="worktree")
    assert [d for d in reconcile.diff(t) if d.check == "workdir.git"]
    report = reconcile.apply(t)
    git = [a for a in report.applied if a.check == "workdir.git"]
    assert len(git) == 1 and git[0].ok, [a.to_dict() for a in git]
    assert resolve.workdir_has_commit(str(world.workdir))
    assert not [d for d in report.drift if d.check == "workdir.git"]
    assert report.exit_code == 0
    again = reconcile.apply(t)
    assert not [a for a in again.applied if a.check == "workdir.git"]


def test_workdir_inside_root_refused(world):
    t = dataclasses.replace(world.team,
                            workdir=str(world.root / "workspace-x"))
    report = reconcile.apply(t, create_workdir=True)
    bad = _applied(report, "workdir.exists")
    assert bad and not bad[0].ok
    assert "refusing to create workdir" in bad[0].error
    assert not os.path.isdir(str(world.root / "workspace-x"))


# ── board-create × delegated-child guard (§5.2.1, bullet 4) ─────────────────

def test_board_create_delegated_child_guard_becomes_drift(world, monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    shutil.rmtree(kb.board_dir("teamx-board"))
    report = reconcile.apply(world.team, create_board=True)   # must NOT raise
    guard = [d for d in report.drift
             if d.check == "board.exists" and not d.auto_fixable
             and "delegate_task" in str(d.observed)]
    assert guard, [d.to_dict() for d in report.drift]
    # the run CONTINUED: the member plane was served despite the guard
    assert [a for a in report.applied
            if a.check == "member.terminal_cwd" and a.ok]
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert cfg["terminal"]["cwd"] == str(world.workdir)


# ── terminal.cwd + capacity skipped for primary: false ───────────────────────

def test_primary_false_skips_cwd_and_capacity_bytes(world, tmp_path):
    """With ``primary: false`` here and a primary claim on
    team ``other``, plane 1 AND capacity are skipped — proven by the target
    config.yaml not moving one byte, while both info items name the owner."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _stage_member("qa", world.qa, workdir=elsewhere)   # F1-clean, cwd wrong
    _stage_config(world.qa,
                  {"kanban": {"max_in_progress_per_profile": 3}})
    other = Team(slug="other", display_name="Other", board="other-board",
                 workdir=str(world.workdir), tenant="other",
                 members=(Member(profile="qa", role="member", primary=True,
                                 capacity=3),))
    storage.upsert(other)
    try:
        t = dataclasses.replace(
            world.team,
            members=tuple(
                dataclasses.replace(m, primary=False, capacity=2)
                if m.profile == "qa" else m for m in world.team.members))
        before = _snap([resolve.profile_config_path("qa")])
        report = reconcile.apply(t)
        # NOT ONE byte moved — despite the cwd mismatch and the capacity
        # mismatch this team declares
        assert _snap([resolve.profile_config_path("qa")]) == before
        infos = {(d.check, d.observed) for d in report.drift
                 if d.check in ("member.terminal_cwd", "member.capacity")}
        assert ("member.terminal_cwd", "owned by team other") in infos
        assert ("member.capacity", "owned by team other") in infos
    finally:
        storage.delete("other")


def test_capacity_conflict_second_write_refused_flap_pinned(world):
    """I6 bypassed by a hand-edited registry (storage.save, the CLI-only
    invariant check skipped): two primary teams, one shared qa, capacities
    2 vs 3. First writer wins, second refused E_TEAM_CAPACITY_CONFLICT — and
    the sync-all-apply FLAP is dead: the second round touches no file."""
    a = dataclasses.replace(
        world.team,
        members=tuple(dataclasses.replace(m, capacity=2)
                      if m.profile == "qa" else m
                      for m in world.team.members))
    b = Team(slug="other", display_name="Other", board="other-board",
             workdir=str(world.workdir), tenant="other",
             members=(Member(profile="qa", role="member", primary=True,
                             capacity=3),))
    kb.create_board("other-board", name="Other",
                    default_workdir=str(world.workdir))
    reg = storage.load_or_empty()
    storage.save(dataclasses.replace(
        reg, teams={**dict(reg.teams), "teamx": a, "other": b}))
    storage._reset_caches_for_tests()

    ra, rb = reconcile.apply([a, b])              # sync --all --apply pass 1
    assert ra.exit_code == 0, [a.to_dict() for a in ra.applied]
    refused = [x for x in rb.applied
               if not x.ok and E_TEAM_CAPACITY_CONFLICT in (x.error or "")]
    assert refused, [x.to_dict() for x in rb.applied]
    assert refused[0].subject == "profile:qa"
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert cfg["kanban"]["max_in_progress_per_profile"] == 2   # first writer
    assert rb.exit_code == 4                                    # §8.2

    snap1 = _snap(_touched_files(world)
                  + [kb.board_metadata_path("other-board")])
    ra2, rb2 = reconcile.apply([a, b])            # pass 2: the flap regression
    snap2 = _snap(_touched_files(world)
                  + [kb.board_metadata_path("other-board")])
    assert snap1 == snap2, "sync --all --apply round 2 touched a file"
    assert [x for x in rb2.applied
            if not x.ok and E_TEAM_CAPACITY_CONFLICT in (x.error or "")]


# ── shared_board skips default_workdir (info, no write) ─────────────────────

def test_shared_board_default_workdir_skipped(world):
    t = dataclasses.replace(world.team, shared_board=True)
    before = _snap([kb.board_metadata_path("teamx-board")])
    report = reconcile.apply(t)
    assert _applied(report, "board.default_workdir") == []
    assert _snap([kb.board_metadata_path("teamx-board")]) == before
    info = [d for d in report.drift if d.check == "board.default_workdir"]
    assert info and info[0].severity == "info"
    assert "shared_board" in info[0].remedy


# ── F6: a non-empty description is never overwritten ─────────────────────────

def test_nonempty_description_never_overwritten(world):
    hc_profiles.write_profile_meta(resolve.profile_home("qa"),
                                   description="operator prose, not mine")
    before = _snap([resolve.profile_home("qa") / "profile.yaml"])
    reconcile.apply(world.team)
    assert _snap([resolve.profile_home("qa") / "profile.yaml"]) == before
    meta = yaml.safe_load(
        (resolve.profile_home("qa") / "profile.yaml").read_text(
            encoding="utf-8"))
    assert meta["description"] == "operator prose, not mine"


# ── refusals: managed install and unreadable config ─────────────────────────

def test_managed_install_refuses_and_reports(world, monkeypatch):
    import hermes_cli.config as hc_config
    monkeypatch.setattr(hc_config, "is_managed", lambda: True)
    before = _snap([resolve.profile_config_path("qa")])
    report = reconcile.apply(world.team)
    bad = _refusals(report)
    assert bad, "managed install must produce applied:false records"
    assert all(a.check.startswith("member.") for a in bad)
    assert all(E_TEAM_MANAGED in a.error for a in bad)
    assert _snap([resolve.profile_config_path("qa")]) == before   # nothing written
    assert report.exit_code == 4


def test_unreadable_config_refuses_and_reports(world):
    cfg = resolve.profile_config_path("qa")
    cfg.write_text("{{{{ not yaml :::\n  - [", encoding="utf-8")
    before = _snap([cfg])
    report = reconcile.apply(world.team)          # must not raise
    bad = _refusals(report)
    assert bad and all("refusing to write" in a.error for a in bad)
    assert _snap([cfg]) == before                 # fail closed, zero writes
    assert report.exit_code == 4


# ── three members, one workdir (plane 1) ─────────────────────────────────────

def test_three_members_one_workdir(world, profile):
    rev_home = _raw_profile(profile, "rev")
    t = dataclasses.replace(
        world.team,
        members=tuple(world.team.members) + (
            Member(profile="rev", role="reviewer", primary=True),))
    reconcile.apply(t)
    for who in ("qa", "rev", world.be):
        cfg = yaml.safe_load(
            resolve.profile_config_path(who).read_text(encoding="utf-8"))
        assert cfg["terminal"]["cwd"] == str(world.workdir)   # byte for byte
    files = _touched_files(world) + [
        resolve.profile_config_path("rev"),
        resolve.profile_home("rev") / "profile.yaml"]
    snap1 = _snap(files)
    reconcile.apply(t)
    assert _snap(files) == snap1                  # second apply touches none


# ── S10a projections: skills / model / provider (bullet 2b) ──────────────────

def test_skills_projection_complement_with_essential_veto(world, monkeypatch):
    monkeypatch.setattr(resolve, "installed_skill_names",
                        lambda profile: sorted(["arxiv", "github", "pdf",
                                                "hermes-agent"]))
    t = dataclasses.replace(
        world.team,
        members=tuple(
            dataclasses.replace(m, skills=["github", "arxiv"])
            if m.profile == "qa" else m
            for m in world.team.members))
    report = reconcile.apply(t)
    assert all(a.ok for a in report.applied)
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    # complement over the installed set, ESSENTIAL_SKILLS never disabled
    assert cfg["skills"]["disabled"] == ["pdf"]
    assert "hermes-agent" not in cfg["skills"]["disabled"]


def test_skills_none_leaves_skills_block_untouched(world, monkeypatch):
    """``skills: None`` means "not managed by the team", NOT "no skills":
    no ``skills.disabled`` key ever enters qa's batch, and the operator's
    own skills block survives the batched write of the OTHER keys."""
    calls = _spy_writes(monkeypatch)
    cfg_path = resolve.profile_config_path("qa")
    cfg_path.write_text(yaml.safe_dump(
        {"skills": {"disabled": ["mine"]}}, sort_keys=False),
        encoding="utf-8")
    report = reconcile.apply(world.team)          # qa member: skills=None
    assert all(a.ok for a in report.applied)
    assert all("skills.disabled" not in upd for _, upd in calls)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["skills"]["disabled"] == ["mine"]  # not managed ⇒ not touched


def test_model_provider_written_only_when_declared(world, monkeypatch):
    calls = _spy_writes(monkeypatch)
    t = dataclasses.replace(
        world.team,
        members=tuple(
            dataclasses.replace(
                m, _extra={"model": "glm-5", "provider": "openai",
                           "base_url": "http://llm:8000/v1"})
            if m.profile == world.be else m
            for m in world.team.members))
    reconcile.apply(t)
    be_cfg = yaml.safe_load(
        resolve.profile_config_path(world.be).read_text(encoding="utf-8"))
    assert be_cfg["model"] == {"default": "glm-5", "provider": "openai",
                               "base_url": "http://llm:8000/v1"}
    qa_cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert "model" not in qa_cfg and "provider" not in qa_cfg
    # the pair travels in the member's own batched call
    be_calls = [c for c in calls if c[0] == world.be]
    assert len(be_calls) == 1 and be_calls[0][1]["model.default"] == "glm-5"


def test_provider_without_model_refused(world):
    t = dataclasses.replace(
        world.team,
        members=tuple(dataclasses.replace(m, _extra={"provider": "openai"})
                      if m.profile == "qa" else m
                      for m in world.team.members))
    before = _snap([resolve.profile_config_path("qa")])
    report = reconcile.apply(t)
    bad = [a for a in report.applied
           if a.check == "member.model" and not a.ok]
    assert bad and "provider without model" in bad[0].error
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert "provider" not in cfg                  # refused before the batch


# ── project phase: mode create mints the row and stamps the board ────────────

def test_project_create_mints_row_and_stamps_board(world):
    t = dataclasses.replace(world.team,
                            project=Project(mode="create", slug="teamx"))
    report = reconcile.apply(t)
    found = resolve.project_for(t)
    assert found is not None
    assert kb.read_board_metadata("teamx-board")["project_id"] == found["id"]
    assert report.drift == [], [d.to_dict() for d in report.drift]
    snap = _snap(_touched_files(world)
                 + [kb.board_metadata_path("teamx-board")])
    reconcile.apply(t)
    assert _snap(_touched_files(world)
                 + [kb.board_metadata_path("teamx-board")]) == snap


# ── the opt-in AGENTS.md block is idempotent ─────────────────────────────────

def test_agents_md_block_opt_in_and_idempotent(world):
    (world.workdir / "AGENTS.md").unlink()
    t = dataclasses.replace(
        world.team,
        sync=dataclasses.replace(world.team.sync, write_agents_md=True))
    reconcile.apply(t)
    text = (world.workdir / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- hermes-agent-synergia:begin -->" in text
    assert "teamx" in text and str(world.workdir) in text
    snap = _snap([world.workdir / "AGENTS.md"])
    reconcile.apply(t)
    assert _snap([world.workdir / "AGENTS.md"]) == snap
    # setting off ⇒ the file is left absent (info row, no fix)
    (world.workdir / "AGENTS.md").unlink()
    report = reconcile.apply(world.team)          # world.team: setting False
    assert not (world.workdir / "AGENTS.md").exists()
    assert [d for d in report.drift if d.check == "workdir.agents_md"]


# ── only= narrowing + the §8.2 exit-4 surface ────────────────────────────────

def test_only_narrows_the_apply(world):
    report = reconcile.apply(world.team, only=["member.terminal_cwd"])
    assert {a.check for a in report.applied} == {"member.terminal_cwd"}
    cfg = yaml.safe_load(
        resolve.profile_config_path("qa").read_text(encoding="utf-8"))
    assert cfg["terminal"]["cwd"] == str(world.workdir)
    assert cfg["plugins"]["enabled"] == []        # everything else untouched
    assert [d for d in report.drift
            if d.check == "member.plugin_enabled"]


def test_run_exit_code_surfaces_4(world):
    import types as _t
    ok = _t.SimpleNamespace(exit_code=0)
    bad = _t.SimpleNamespace(exit_code=4)
    assert reconcile.run_exit_code(ok) == 0
    assert reconcile.run_exit_code([ok, ok]) == 0
    assert reconcile.run_exit_code([ok, bad]) == 4


# ── §13 audit: every applied item logs at INFO, in full ──────────────────────

def test_applied_items_logged_at_info(world, caplog):
    with caplog.at_level(logging.INFO, logger="hermes_agent_synergia"):
        reconcile.apply(world.team)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "hermes_agent_synergia"
             and "hermes-agent-synergia apply:" in r.getMessage()]
    for probe in ("check=member.terminal_cwd subject=profile:qa",
                  "check=member.plugin_enabled subject=profile:qa"):
        assert any(probe in ln and "observed=" in ln and "desired=" in ln
                   and "applied=True" in ln for ln in lines), lines
