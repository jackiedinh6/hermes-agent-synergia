"""S6 — cli.py registry commands: parsing, envelope, exit codes, create sequence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess

import pytest
import yaml

import cli
import storage
from errors import TeamError
from model import Team


@pytest.fixture(autouse=True)
def _fresh_storage():
    storage._reset_caches_for_tests()
    yield
    storage._reset_caches_for_tests()


@pytest.fixture
def parser():
    p = argparse.ArgumentParser(prog="team")
    cli.setup_parser(p)
    return p


@pytest.fixture
def W(hermes_root):
    """Writable workdir paths — outside the Hermes root, unlike /w/..."""
    def _w(name):
        return str(hermes_root.parent / ("work-" + name))
    return _w


# A synced create in a bare test root exits 4: the record is written, but the
# members have no model and the plugin is not installed, so it is not ready.
_NOT_READY = 4


def run(parser, *argv, capsys):
    """Parse + dispatch; returns (exit_code, stdout_json_or_None, stderr_text)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if args.json and out.out.strip():
        envelope = json.loads(out.out)   # must parse as exactly one object
    return code, envelope, out.err


# ── member-spec parsing ──────────────────────────────────────────────────────

def test_parse_member_bare():
    assert cli.parse_member_spec("qa") == {"profile": "qa"}


def test_parse_member_role_capacity():
    assert cli.parse_member_spec("dev:implementer:2") == {
        "profile": "dev", "role": "implementer", "capacity": 2}


def test_parse_member_empty_profile():
    with pytest.raises(TeamError) as ei:
        cli.parse_member_spec(":role")
    assert ei.value.code == "E_TEAM_INVALID"


def test_parse_member_bad_capacity():
    with pytest.raises(TeamError) as ei:
        cli.parse_member_spec("dev:impl:x")
    assert "not an integer" in ei.value.message


def test_parse_member_too_many_fields():
    with pytest.raises(TeamError):
        cli.parse_member_spec("a:b:c:d")


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_empty_json(hermes_root, parser, capsys):
    code, env, err = run(parser, "list", "--json", capsys=capsys)
    assert code == 0
    assert env == {"ok": True, "data": {"teams": []}}
    assert err == ""


# ── create ───────────────────────────────────────────────────────────────────

@pytest.fixture
def team_profiles(profile):
    profile("qa")
    profile("agency-orchestrator")
    profile("agency-dev")


def test_create_smoke_and_registry_shape(hermes_root, team_profiles, parser, capsys, W):
    code, env, err = run(
        parser, "create", "demo",
        "--member", "qa:verifier",
        "--member", "agency-orchestrator:orchestrator",
        "--member", "agency-dev:implementer:2",
        "--orchestrator", "agency-orchestrator",
        "--board", "demo", "--create-board",
        "--workdir", W("demo"), "--create-workdir",
        "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    team = env["data"]["team"]
    assert team["slug"] == "demo"
    assert team["board"] == "demo"
    assert team["orchestrator"] == "agency-orchestrator"
    assert team["policy"]["mode"] == "advise"
    assert {m["profile"] for m in team["members"]} == {
        "qa", "agency-orchestrator", "agency-dev"}
    dev = next(m for m in team["members"] if m["profile"] == "agency-dev")
    assert dev["capacity"] == 2
    assert dev["role"] == "implementer"
    # teams.yaml is human-legible YAML
    raw = (hermes_root / "plugin-data" / "hermes-agent-synergia" / "teams.yaml").read_text()
    assert "teams:" in raw and "schema_version" in raw
    yaml.safe_load(raw)["teams"]["demo"]["slug"] == "demo"


def test_create_worktree_inits_git_with_snapshot(hermes_root, team_profiles,
                                                 parser, capsys, W):
    wd = W("wt")
    os.makedirs(wd, exist_ok=True)
    with open(os.path.join(wd, "app.py"), "w") as f:
        f.write("print(1)\n")
    argv = ("create", "wt", "--member", "qa:orchestrator",
            "--board", "wt", "--create-board", "--workdir", wd,
            "--workspace-policy", "worktree", "--no-sync", "--json")
    code, _, err = run(parser, *argv, "--dry-run", capsys=capsys)
    assert code == 0, err
    assert not os.path.exists(os.path.join(wd, ".git"))    # dry-run writes nothing
    code, _, err = run(parser, *argv, capsys=capsys)
    assert code == 0, err
    files = subprocess.run(
        ["git", "-C", wd, "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()
    assert files == ["app.py"]


def test_create_defaults_to_worktree(hermes_root, team_profiles, parser,
                                     capsys, W):
    wd = W("dflt")
    code, env, err = run(parser, "create", "dflt", "--member", "qa:orchestrator",
                         "--board", "dflt", "--create-board", "--workdir", wd,
                         "--create-workdir", "--no-sync", "--json",
                         capsys=capsys)
    assert code == 0, err
    assert env["data"]["team"]["workspace_policy"] == "worktree"
    head = subprocess.run(["git", "-C", wd, "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode == 0, head.stderr


def test_create_shared_policy_accepted(parser):
    args = parser.parse_args(["create", "t", "--member", "qa:orchestrator",
                              "--workspace-policy", "shared"])
    assert args.workspace_policy == "shared"


def test_create_unknown_policy_rejected(parser):
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["create", "t", "--member", "qa:orchestrator",
                           "--workspace-policy", "nonsense"])
    assert exc.value.code == 2


def test_create_worktree_failure_falls_back_to_scratch(
        hermes_root, team_profiles, parser, capsys, W, monkeypatch):
    import resolve

    def refuse(workdir):
        raise TeamError("E_TEAM_INVALID", "git missing", hint="h", remedy="r")

    monkeypatch.setattr(resolve, "ensure_worktree_repo", refuse)
    code, env, err = run(parser, "create", "fb", "--member", "qa:orchestrator",
                         "--board", "fb", "--create-board", "--workdir", W("fb"),
                         "--create-workdir", "--no-sync", "--json",
                         capsys=capsys)
    assert code == 0, err
    assert env["data"]["team"]["workspace_policy"] == "scratch"
    assert "git missing" in env["data"]["warning"]
    assert "WARNING:" in err
    storage._reset_caches_for_tests()
    assert storage.load().teams["fb"].workspace_policy == "scratch"


def test_create_requires_existing_profiles(hermes_root, profile, parser, capsys, W):
    profile("orch")
    code, env, err = run(
        parser, "create", "t1", "--member", "orch:orchestrator",
        "--member", "ghost", "--workdir", W("t1"), "--board", "b1",
        "--create-board", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NO_PROFILE"
    assert "ghost" in env["error"]["message"]
    assert storage.load().teams == {}          # registry NOT written


def test_create_missing_creates_profile(hermes_root, team_profiles, parser, capsys, W):
    code, env, err = run(
        parser, "create", "t2", "--member", "agency-orchestrator:orchestrator",
        "--member", "brandnew", "--create-missing",
        "--board", "t2", "--create-board",
        "--workdir", W("t2"), "--create-workdir", "--json", capsys=capsys)
    assert code in (0, _NOT_READY), err
    home = hermes_root / "profiles" / "brandnew"
    assert (home / "config.yaml").exists() or (home / "SOUL.md").exists()


def test_create_dry_run_writes_nothing(hermes_root, team_profiles, parser, capsys, W):
    code, env, err = run(
        parser, "create", "demo",
        "--member", "qa", "--member", "agency-orchestrator:orchestrator",
        "--board", "demo", "--workdir", W("demo"),
        "--dry-run", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["dry_run"] is True
    assert not storage.registry_path().exists()


def test_create_i1_board_taken(hermes_root, team_profiles, parser, capsys, W):
    code, _, err = run(
        parser, "create", "one",
        "--member", "agency-orchestrator:orchestrator", "--member", "qa:verifier",
        "--workdir", W("one"), "--create-workdir",
        "--board", "shared-b", "--create-board", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    code, env, _ = run(
        parser, "create", "two",
        "--member", "agency-orchestrator:orchestrator", "--member", "agency-dev",
        "--workdir", W("two"), "--create-workdir",
        "--board", "shared-b", "--no-sync", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_BOARD_TAKEN"


def test_create_orchestrator_must_be_member(hermes_root, team_profiles, parser, capsys, W):
    code, env, _ = run(
        parser, "create", "t3", "--member", "qa", "--member", "agency-dev",
        "--board", "t3", "--create-board", "--workdir", W("t3"),
        "--create-workdir", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"
    assert "orchestrator" in env["error"]["message"]


def test_create_duplicate_name_refused(hermes_root, team_profiles, parser,
                                      capsys, W):
    """Team names are unique ignoring case and spacing — a clash is refused
    before any write, like a clashing slug."""
    code, _, err = run(
        parser, "create", "one", "--member", "agency-orchestrator:orchestrator",
        "--display-name", "Alpha Team", "--workdir", W("one"),
        "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    for argv in (("two", "--display-name", "alpha   TEAM"),
                 ("two", "--display-name", "ONE"),
                 ("alpha-team",)):
        code, env, _ = run(
            parser, "create", *argv, "--member", "qa:orchestrator",
            "--workdir", W("two"), "--no-sync", "--json", capsys=capsys)
        assert code == 1, argv
        assert env["error"]["code"] == "E_TEAM_EXISTS", argv
    assert not os.path.isdir(W("two"))
    assert set(storage.load().teams) == {"one"}


def test_create_makes_board_and_workdir_without_flags(hermes_root, team_profiles,
                                                     parser, capsys, W):
    from hermes_cli import kanban_db
    code, env, err = run(
        parser, "create", "nf", "--member", "agency-orchestrator:orchestrator",
        "--workdir", W("nf"), "--json", capsys=capsys)
    assert code in (0, _NOT_READY), err
    assert os.path.isdir(W("nf"))
    assert kanban_db.kanban_db_path("nf").is_file()
    assert env["data"]["reconciled"] is not None       # the sync ran
    assert set(env["data"]["ready"]) == {"ready", "blocking"}


# ── show ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def demo(team_profiles, parser, capsys, W):
    code, env, err = run(
        parser, "create", "demo", "--member", "qa",
        "--member", "agency-orchestrator:orchestrator",
        "--board", "demo", "--create-board",
        "--workdir", W("demo"), "--create-workdir",
        "--workspace-policy", "scratch", "--json", capsys=capsys)
    assert code in (0, _NOT_READY), err
    return "demo"


def test_create_existing_slug_refused(hermes_root, demo, parser, capsys, W):
    """`create` is not an editor (Appendix C E_TEAM_EXISTS).

    Regression: this used to fall through to ``upsert``, which merges — so a
    re-run silently replaced a live team's roster and bindings, exit 0.
    """
    before = storage.load().teams["demo"].to_dict()
    code, env, _ = run(
        parser, "create", "demo",
        "--member", "agency-dev:implementer",
        "--orchestrator", "agency-dev",
        "--board", "demo", "--workdir", W("demo"),
        "--display-name", "CLOBBER", "--no-sync", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_EXISTS"
    assert "demo" in env["error"]["message"]
    assert env["error"]["remedy"]
    storage._reset_caches_for_tests()
    assert storage.load().teams["demo"].to_dict() == before   # untouched


def test_create_existing_slug_refused_before_any_write(hermes_root, demo, parser,
                                                       capsys, W):
    """The refusal precedes provisioning and board/workdir creation."""
    code, _, _ = run(
        parser, "create", "demo", "--member", "qa",
        "--orchestrator", "qa", "--board", "brand-new-board", "--create-board",
        "--workdir", W("never"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 1
    import os
    assert not os.path.isdir(W("never"))
    assert not (hermes_root / "kanban" / "boards" / "brand-new-board").exists()


def test_show_round_trip_and_missing(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "show", "demo", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["team"]["slug"] == "demo"
    assert env["data"]["team"]["_resolved"]["members"]["qa"]["exists"] is True
    code, env, _ = run(parser, "show", "nope", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"


# ── add/remove member ────────────────────────────────────────────────────────

def test_add_member_capacity(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "add-member", "demo", "agency-dev",
                       "--role", "implementer", "--capacity", "3",
                       "--json", capsys=capsys)
    assert code == 0
    m = next(x for x in env["data"]["team"]["members"]
             if x["profile"] == "agency-dev")
    assert m["capacity"] == 3
    code, env, _ = run(parser, "add-member", "demo", "agency-dev", "--json",
                       capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"


def test_add_member_nonexistent_profile(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "add-member", "demo", "ghost", "--json",
                       capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NO_PROFILE"


def test_remove_member_round_trip(hermes_root, demo, parser, capsys):
    run(parser, "add-member", "demo", "agency-dev", "--json", capsys=capsys)
    code, env, _ = run(parser, "remove-member", "demo", "agency-dev", "--json",
                       capsys=capsys)
    assert code == 0
    assert all(x["profile"] != "agency-dev"
               for x in env["data"]["team"]["members"])


def test_remove_orchestrator_refused(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "remove-member", "demo", "agency-orchestrator",
                       "--json", capsys=capsys)
    assert code == 1
    assert "orchestrator" in env["error"]["message"]


def test_remove_member_not_member(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "remove-member", "demo", "ghost", "--json",
                       capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NOT_MEMBER"


# ── destroy ──────────────────────────────────────────────────────────────────

def test_destroy_requires_yes_for_purge(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "destroy", "demo", "--purge-board", "--json",
                       capsys=capsys)
    assert code == 1
    assert "--yes" in env["error"]["message"]
    assert "demo" in storage.load().teams          # refused = unchanged


def test_destroy_deletes_record_only(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "destroy", "demo", "--yes", "--json",
                       capsys=capsys)
    assert code == 0
    assert storage.load().teams == {}
    assert (hermes_root / "profiles" / "qa").is_dir()   # profiles untouched


# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_reports_paths_and_ownership(hermes_root, demo, parser, capsys):
    code, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    data = env["data"]
    assert data["registry_path"] == str(
        hermes_root / "plugin-data" / "hermes-agent-synergia" / "teams.yaml")
    assert data["hermes_root"] == str(hermes_root)
    assert not any(f["check"] == "primary_none" and "qa" in f["message"]
                   for f in data["findings"])     # Demo's --member qa is
    # the FIRST claim, so it is primary — the old always-False hardcode made
    # every adopted profile look orphaned here.
    assert next(m for m in storage.load().teams["demo"].members
                if m.profile == "qa").primary is True


def test_doctor_flags_I2_conflict(hermes_root, demo, parser, capsys):
    run(parser, "add-member", "demo", "agency-dev", "--primary", "--json",
        capsys=capsys)
    # A second team claiming primary on the same profile, injected straight to
    # storage (bypasses CLI guards) — doctor must surface the I2 violation.
    storage.upsert(Team.from_dict({
        "slug": "other", "board": "other", "workdir": "/w/other",
        "orchestrator": "agency-dev",
        "members": [{"profile": "agency-dev", "role": "orchestrator",
                     "primary": True}]}))
    code, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    assert any(f["check"] == "I2" for f in env["data"]["findings"])


def test_doctor_flags_worktree_workdir_without_commit(hermes_root, demo, parser,
                                                      capsys, W):
    t = storage.load().teams["demo"]
    storage.upsert(Team.from_dict({**t.to_dict(), "workspace_policy": "worktree"}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    hit = [f for f in env["data"]["findings"] if f["check"] == "workdir.git"]
    assert len(hit) == 1 and hit[0]["severity"] == "error"
    assert "git" in hit[0]["remedy"]

    subprocess.run(["git", "-C", W("demo"), "init", "-q"], check=True)
    subprocess.run(["git", "-C", W("demo"), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "--allow-empty", "-m",
                    "init"], check=True)
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert not any(f["check"] == "workdir.git" for f in env["data"]["findings"])


# ── doctor: the S6.5 reports that S6a/S6b add (§5.5, F8) ─────────────────────

def _instance(hermes_root, name, *, source="/packs/x", link=True):
    """A profile that looks like something this plugin provisioned: pack
    provenance + the F1 per-profile symlink only this plugin installs."""
    home = hermes_root / "profiles" / name
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / "distribution.yaml").write_text(
        yaml.safe_dump({"name": name, "source": source}), encoding="utf-8")
    if link:
        (hermes_root / "plugins" / "hermes-agent-synergia").mkdir(parents=True, exist_ok=True)
        (home / "plugins" / "hermes-agent-synergia").symlink_to(
            "../../../plugins/hermes-agent-synergia")
    return home


def test_doctor_reports_role_drift_across_teams(hermes_root, demo, parser, capsys):
    """A member whose role differs across teams is `info`, and that
    report is the Q6 trigger detector."""
    storage.upsert(Team.from_dict({
        "slug": "other", "board": "other", "workdir": "/w/other",
        "orchestrator": "qa",
        "members": [{"profile": "qa", "role": "reviewer"}]}))
    code, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    drift = [f for f in env["data"]["findings"] if f["check"] == "role_drift"]
    assert len(drift) == 1
    assert "qa" in drift[0]["message"]
    assert "member" in drift[0]["message"] and "reviewer" in drift[0]["message"]
    assert drift[0]["severity"] == "info"


def test_doctor_lists_shared_profiles_with_primary_owner(hermes_root, demo,
                                                         parser, capsys):
    """Every multi-team profile with its primary owner.

    R1 fix: demo's ``--member qa`` was the FIRST claim, so qa is
    primary in demo and the second team can only share it (primary: false).
    The old expectation — ``other`` owning the profile a plain ``--member``
    adopted first — encoded the always-False hardcode.
    """
    storage.upsert(Team.from_dict({
        "slug": "other", "board": "other", "workdir": "/w/other",
        "orchestrator": "qa",
        "members": [{"profile": "qa", "role": "member", "primary": False}]}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    shared = {s["profile"]: s for s in env["data"]["shared_profiles"]}
    assert shared["qa"]["teams"] == ["demo", "other"]
    assert shared["qa"]["primary"] == "demo"


def test_doctor_measures_managed_footprint(hermes_root, profile, parser, capsys):
    """Instance sprawl is measured, not hidden."""
    home = _instance(hermes_root, "px-be")
    (home / "SOUL.md").write_text("x" * 4096, encoding="utf-8")
    storage.upsert(Team.from_dict({
        "slug": "px", "board": "px", "workdir": "/w/px", "orchestrator": "px-be",
        "members": [{"profile": "px-be", "role": "implementer",
                     "managed": True, "primary": True}]}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    data = env["data"]
    assert data["managed_profile_count"] == 1
    assert data["managed_footprint_bytes"] >= 4096
    assert data["managed_instances"][0]["profile"] == "px-be"
    assert data["managed_instances"][0]["team"] == "px"


def test_doctor_footprint_ignores_symlinked_env(hermes_root, parser, capsys):
    """F7's .env points at the root — charging the team for it would be a lie."""
    home = _instance(hermes_root, "px-be")
    root_env = hermes_root / ".env"
    root_env.write_text("y" * 8192, encoding="utf-8")
    (home / ".env").symlink_to(root_env)
    storage.upsert(Team.from_dict({
        "slug": "px", "board": "px", "workdir": "/w/px", "orchestrator": "px-be",
        "members": [{"profile": "px-be", "managed": True}]}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert env["data"]["managed_footprint_bytes"] < 8192


def test_doctor_flags_unclaimed_instance_after_interrupted_create(
        hermes_root, parser, capsys):
    """§5.5: the registry write is LAST, so a failed create leaves instances
    with no team record at all — the evidence is the F1 plumbing."""
    _instance(hermes_root, "px-code-reviewer")
    code, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    orphans = [f for f in env["data"]["findings"]
               if f["check"] == "instance_unclaimed"]
    assert len(orphans) == 1
    assert "px-code-reviewer" in orphans[0]["message"]
    assert "re-run" in orphans[0]["remedy"]
    assert env["data"]["unclaimed_instances"][0]["profile"] == "px-code-reviewer"


def test_doctor_flags_unclaimed_by_naming_template(hermes_root, demo, parser,
                                                   capsys, profile):
    """The other signal: the name matches a live team's naming template."""
    profile("demo-ghost")
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    orphans = [f for f in env["data"]["findings"]
               if f["check"] == "instance_unclaimed"]
    assert [o["message"].split("'")[1] for o in orphans] == ["demo-ghost"]
    assert "naming template" in orphans[0]["message"]


def test_doctor_ignores_claimed_and_hand_made_profiles(hermes_root, demo, parser,
                                                       capsys, profile):
    """A pack profile the operator installed (provenance, no symlink) and a
    hand-made one (symlink, no provenance) are NOT instances of ours."""
    _instance(hermes_root, "agency-installed", link=False)
    profile("handmade")
    (hermes_root / "profiles" / "handmade" / "plugins").mkdir(parents=True)
    (hermes_root / "plugins" / "hermes-agent-synergia").mkdir(parents=True, exist_ok=True)
    (hermes_root / "profiles" / "handmade" / "plugins" / "hermes-agent-synergia").symlink_to(
        "../../../plugins/hermes-agent-synergia")
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert env["data"]["unclaimed_instances"] == []


def test_doctor_warns_on_gateway_port_collision(hermes_root, parser, capsys):
    """F8: there is no port allocator, so two declared gateways collide."""
    for name in ("px-a", "px-b"):
        home = _instance(hermes_root, name)
        (home / ".env").write_text("API_SERVER_PORT=8642\n", encoding="utf-8")
    storage.upsert(Team.from_dict({
        "slug": "px", "board": "px", "workdir": "/w/px", "orchestrator": "px-a",
        "members": [{"profile": "px-a", "managed": True},
                    {"profile": "px-b", "managed": True}]}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    warns = [f for f in env["data"]["findings"] if f["check"] == "gateway_port"]
    assert len(warns) == 1
    assert "8642" in warns[0]["message"]
    assert "px-a" in warns[0]["message"] and "px-b" in warns[0]["message"]


def test_doctor_quiet_when_ports_differ_or_undeclared(hermes_root, parser, capsys):
    """Every profile *could* start a gateway; warning about that is noise."""
    _instance(hermes_root, "px-a")                       # declares nothing
    home_b = _instance(hermes_root, "px-b")
    (home_b / ".env").write_text("API_SERVER_PORT=9001\n", encoding="utf-8")
    storage.upsert(Team.from_dict({
        "slug": "px", "board": "px", "workdir": "/w/px", "orchestrator": "px-a",
        "members": [{"profile": "px-a", "managed": True},
                    {"profile": "px-b", "managed": True}]}))
    _, env, _ = run(parser, "doctor", "--json", capsys=capsys)
    assert [f for f in env["data"]["findings"] if f["check"] == "gateway_port"] == []


# ── --json contract (spec §8.2) ──────────────────────────────────────────────

def test_json_single_object_stdout(hermes_root, demo, capsys):
    p = argparse.ArgumentParser(prog="team")
    cli.setup_parser(p)
    code = cli.handle(p.parse_args(["show", "demo", "--json"]))
    captured = capsys.readouterr()
    assert code == 0
    envelope = json.loads(captured.out)      # raises if not exactly one object
    assert envelope["ok"] is True
    assert "_human" not in envelope["data"]


def test_create_secrets_flag_warns(hermes_root, profile, parser, capsys, W):
    profile("orch")
    code, env, err = run(
        parser, "create", "t9", "--member", "orch:orchestrator",
        "--member", "newbie", "--create-missing", "--clone-secrets",
        "--board", "t9", "--create-board", "--workdir", W("t9"),
        "--create-workdir", "--json", capsys=capsys)
    assert code in (0, _NOT_READY), err
    assert "--clone-secrets" in err


def test_no_subcommand_is_usage(hermes_root, parser, capsys):
    args = parser.parse_args([])
    assert cli.handle(args) == 2


# ── S19 1c: the registry write is CAS-guarded on every path ─────────────────

def test_create_registry_write_is_cas_guarded(hermes_root, profile, parser,
                                              capsys, W, monkeypatch):
    """Fix 1c: ``_cmd_create``'s step-11 ``storage.upsert`` passes the
    step-5 snapshot's revision — never ``None``. A concurrent committer
    landing between the snapshot (step 5) and the write (step 11) must
    surface as E_TEAM_CONFLICT (exit 3), not a silently lost update
    (g1). The race is injected at the step-6→8 seam (ensure_board, a
    call every create makes after the snapshot); ``check_invariants``
    is short-circuited on an empty registry, so it is not a reliable
    seam."""
    import model
    import resolve
    profile("qa")
    storage.upsert(model.Team.from_dict({
        "slug": "other", "board": "other", "workdir": str(W("other")),
        "orchestrator": "qa",
        "members": [{"profile": "qa", "role": "orchestrator"}]}))
    first = storage.load().revision
    real = resolve.ensure_board

    def racing(board, **kw):
        # a second writer commits right after our snapshot
        storage.upsert(model.Team.from_dict({
            "slug": "late", "board": "late", "workdir": str(W("late")),
            "orchestrator": "qa",
            "members": [{"profile": "qa", "role": "orchestrator"}]}))
        return real(board, **kw)

    monkeypatch.setattr(resolve, "ensure_board", racing)
    code, env, err = run(
        parser, "create", "t10", "--member", "qa:orchestrator",
        "--workdir", W("t10"), "--create-workdir",
        "--board", "b10", "--create-board",
        "--no-sync", "--json", capsys=capsys)
    assert code == 3, (code, env, err)         # E_TEAM_CONFLICT
    assert env["error"]["code"] == "E_TEAM_CONFLICT"
    # the 409-shape message names BOTH revisions (g1)
    assert str(first) in env["error"]["message"]
    assert str(first + 1) in env["error"]["message"]
    reg = storage.load()
    assert "late" in reg.teams                 # the concurrent write stands
    assert "t10" not in reg.teams              # ours did not happen


def test_upsert_expected_revision_is_enforced(hermes_root):
    """The unit beneath fix 1c: ``storage.upsert`` honours the revision
    it is handed — ``None`` skips the check (the old create path), an
    explicit stale value raises E_TEAM_CONFLICT and writes nothing."""
    import model
    t = model.Team.from_dict({
        "slug": "a", "board": "a", "workdir": "/w/a", "orchestrator": "qa",
        "members": [{"profile": "qa", "role": "orchestrator"}]})
    storage.upsert(t)                          # None → no CAS
    assert storage.load().revision == 1
    before = (hermes_root / "plugin-data" / "hermes-agent-synergia"
              / "teams.yaml").read_bytes()
    with pytest.raises(TeamError) as ei:
        storage.upsert(t, expected_revision=0)  # stale → conflict
    assert ei.value.code == "E_TEAM_CONFLICT"
    assert (hermes_root / "plugin-data" / "hermes-agent-synergia"
            / "teams.yaml").read_bytes() == before   # nothing written
