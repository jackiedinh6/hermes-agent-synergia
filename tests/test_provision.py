"""S6b — provision.py: the only creator/deleter of profiles.

Runs against a synthetic pack root (like test_catalog.py). The §14.7
provisioning assertions — ``seed_profile_skills`` call counter at zero,
``create_alias`` pinned false, provenance stamped, ``profile.yaml`` non-empty
(F6), plugin symlink + enable (F1) — are the point of this file.
"""

from __future__ import annotations

import argparse
import json

import pytest
import yaml

import catalog
import cli
import provision
import storage
from errors import TeamError
from test_catalog import _make_pack_root


@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()
    yield
    catalog._reset_caches_for_tests()
    storage._reset_caches_for_tests()


@pytest.fixture
def pack(hermes_root, tmp_path):
    """Synthetic pack root + a real ``plugins/hermes-agent-synergia`` install dir so
    the F1 symlink resolves."""
    root = _make_pack_root(tmp_path / "packs")
    (hermes_root / "plugins" / "hermes-agent-synergia").mkdir(parents=True, exist_ok=True)
    (hermes_root / "plugins" / "hermes-agent-synergia" / "plugin.yaml").write_text(
        "name: hermes-agent-synergia\n", encoding="utf-8")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": [str(root)]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    return root


@pytest.fixture
def entry(pack):
    return catalog.get("agency-backend-engineer")


# ── counters for the §2.9 trap assertions ────────────────────────────────────

@pytest.fixture
def install_spy(monkeypatch):
    """Record install_distribution kwargs; assert seed_profile_skills never runs."""
    from hermes_cli import profile_distribution as dist
    from hermes_cli import profiles as prof
    calls = {"install": [], "seed": 0}
    real = dist.install_distribution

    def spy(source, **kw):
        calls["install"].append(kw)
        return real(source, **kw)

    def no_seed(*a, **k):
        calls["seed"] += 1
        raise AssertionError("seed_profile_skills must never be called (F6)")

    monkeypatch.setattr(dist, "install_distribution", spy)
    monkeypatch.setattr(prof, "seed_profile_skills", no_seed, raising=False)
    return calls


# ── render / plan ─────────────────────────────────────────────────────────────

def test_render_name_default_template(entry):
    assert provision.render_name("project-x", entry) == \
        "project-x-backend-engineer"
    assert provision.render_name("px", entry, "{team}-{short}") == "px-backend-engineer"


def test_plan_writes_nothing(entry, hermes_root):
    profiles_dir = hermes_root / "profiles"
    before = sorted(p.name for p in profiles_dir.iterdir())
    p = provision.plan(entry, "px-backend-engineer")
    assert p["name"] == "px-backend-engineer"
    assert p["existing"] is False
    assert p["file_count"] >= 1
    assert p["target_dir"].endswith("profiles/px-backend-engineer")
    assert sorted(q.name for q in profiles_dir.iterdir()) == before   # untouched


# ── instantiate (F6/F1 order is contractual) ─────────────────────────────────

def test_instantiate_full_contract(entry, hermes_root, install_spy):
    m = provision.instantiate(entry, "px-backend-engineer", role="implementer")
    home = hermes_root / "profiles" / "px-backend-engineer"
    assert home.is_dir()
    # provenance stamped by the substrate
    dist_y = yaml.safe_load((home / "distribution.yaml").read_text())
    assert dist_y["source"] == entry.source_path
    assert dist_y["name"] == "px-backend-engineer"
    # F6: profile.yaml carries the description the picker/decomposer read
    meta = yaml.safe_load((home / "profile.yaml").read_text())
    assert meta["description"] == entry.description
    assert meta["display_name"] == entry.display_name
    assert meta["description_auto"] is False
    # F1: per-profile plugin symlink + plugins.enabled
    link = home / "plugins" / "hermes-agent-synergia"
    assert link.is_symlink()
    assert link.readlink() == __import__("pathlib").Path(
        "..", "..", "..", "plugins", "hermes-agent-synergia")
    assert link.resolve() == (hermes_root / "plugins" / "hermes-agent-synergia")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert "hermes-agent-synergia" in cfg["plugins"]["enabled"]
    # §2.9 traps: no seed, no alias
    assert install_spy["seed"] == 0
    assert install_spy["install"] and install_spy["install"][0]["create_alias"] is False
    assert install_spy["install"][0]["force"] is False
    # returned member
    assert m.blueprint == "agency-backend-engineer"
    assert m.blueprint_source == "pack:hermes-agency"
    assert m.managed is True and m.primary is True
    assert m.role == "implementer"


def test_instantiate_existing_foreign_profile_raises(entry, hermes_root, profile):
    profile("px-backend-engineer")             # hand-made, no provenance
    with pytest.raises(TeamError) as ei:
        provision.instantiate(entry, "px-backend-engineer")
    assert ei.value.code == "E_TEAM_PROFILE_EXISTS"
    assert "--name" in ei.value.remedy


def test_instantiate_tombstone_never_forced(entry, hermes_root):
    import hermes_constants
    home = hermes_root / "profiles" / "px-backend-engineer"
    home.mkdir(parents=True)
    hermes_constants.mark_named_profile_deleted(home)
    with pytest.raises(TeamError) as ei:
        provision.instantiate(entry, "px-backend-engineer")
    assert ei.value.code == "E_TEAM_INVALID"
    assert "tombstone" in ei.value.message
    # tombstone lives at <profiles>/.deleted/<name> and survived untouched
    assert hermes_constants.profile_tombstone_path(home).exists()


def test_instantiate_resume_adopts_matching_provenance(entry, hermes_root):
    """§5.5 resumable create: re-run adopts the instance it already made."""
    import hermes_constants
    home = hermes_root / "profiles" / "px-backend-engineer"
    home.mkdir(parents=True)
    (home / "distribution.yaml").write_text(yaml.safe_dump(
        {"name": "px-backend-engineer", "source": entry.source_path}),
        encoding="utf-8")
    m = provision.instantiate(entry, "px-backend-engineer", role="implementer")
    assert m.managed is True
    assert (home / "plugins" / "hermes-agent-synergia").is_symlink()   # F1 still applied


def test_instantiate_seeds_root_model(entry, hermes_root):
    """install_distribution ships no model block; without the root's the
    instance has no provider and its kanban workers exit before a turn."""
    root_cfg = hermes_root / "config.yaml"
    cfg = yaml.safe_load(root_cfg.read_text())
    cfg["model"] = {"default": "m1", "provider": "custom",
                    "base_url": "http://llm:8000/v1", "api_key": "unused"}
    root_cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    provision.instantiate(entry, "px-backend-engineer")
    home = hermes_root / "profiles" / "px-backend-engineer"
    prof = yaml.safe_load((home / "config.yaml").read_text())
    assert prof["model"] == {"default": "m1", "provider": "custom",
                             "base_url": "http://llm:8000/v1"}
    assert "hermes-agent-synergia" in prof["plugins"]["enabled"]


def test_instantiate_without_root_model_writes_none(entry, hermes_root):
    provision.instantiate(entry, "px-backend-engineer")
    home = hermes_root / "profiles" / "px-backend-engineer"
    assert "model" not in yaml.safe_load((home / "config.yaml").read_text())


def test_instantiate_entry_without_description(pack, hermes_root):
    """Regression: an authored entry carries no ``description`` — author_entry
    omits empty fields from distribution.yaml — so ``Entry.description`` is "".
    The old truthiness-based reader fell through to the mapping branch and
    raised ``'Entry' object has no attribute 'get'``, which cli rewrapped as
    E_TEAM_PROVISION_FAILED for every locally-authored blueprint."""
    import catalog_store
    catalog_store.author_entry("bare-one", soul="# Role\nWork.")
    catalog._reset_caches_for_tests()
    bare = catalog.get("bare-one")
    assert bare.description == ""

    m = provision.instantiate(bare, "px-bare-one", role="implementer")

    assert m.blueprint == "bare-one" and m.managed is True
    home = hermes_root / "profiles" / "px-bare-one"
    assert home.is_dir()
    meta = yaml.safe_load((home / "profile.yaml").read_text())
    assert meta["description"] == ""
    assert meta["display_name"] == "bare-one"
    assert (home / "plugins" / "hermes-agent-synergia").is_symlink()


def test_instantiate_accepts_mapping_entry(entry, hermes_root):
    """The mapping branch the reader exists to serve: ``to_dict()`` in, same
    result as the dataclass out."""
    row = entry.to_dict()
    assert provision.render_name("px", row) == provision.render_name("px", entry)
    assert (provision.plan(row, "px-backend-engineer")
            == provision.plan(entry, "px-backend-engineer"))

    m = provision.instantiate(row, "px-backend-engineer", role="implementer")

    assert m.blueprint == entry.id
    assert m.blueprint_source == f"pack:{entry.pack}"
    meta = yaml.safe_load(
        (hermes_root / "profiles" / "px-backend-engineer" / "profile.yaml").read_text())
    assert meta["description"] == entry.description
    assert meta["display_name"] == entry.display_name


# ── delete: gated on managed ─────────────────────────────────────────────────

# ── plugins.enabled goes through the ONE config writer (S8) ─────────────────

def test_enable_refuses_corrupt_config_and_leaves_it_intact(hermes_root, profile):
    """Regression: the hand-rolled writer fell back to ``{}`` on a YAML error
    and rewrote the file down to ``plugins.enabled`` alone — a 2 167-line
    profile config replaced by three lines. The writer refuses instead."""
    home = profile("victim", "model: gpt\ntoolsets: [a, b]\nbroken: [unclosed\n")
    before = (home / "config.yaml").read_bytes()
    with pytest.raises(TeamError) as ei:
        provision._append_enabled("victim")
    assert ei.value.code == "E_TEAM_INVALID"
    assert (home / "config.yaml").read_bytes() == before
    assert not (home / "config.yaml.hermes-agent-synergia.bak").exists()


def test_enable_preserves_the_rest_of_the_config_and_backs_it_up(hermes_root, profile):
    home = profile("keeper", {"model": "gpt", "toolsets": ["a"],
                              "terminal": {"cwd": "/w/x"}})
    provision._append_enabled("keeper")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["plugins"]["enabled"] == ["hermes-agent-synergia"]
    assert cfg["model"] == "gpt" and cfg["toolsets"] == ["a"]
    assert cfg["terminal"]["cwd"] == "/w/x"
    # Guard 3: the pristine bytes are kept before the first modification.
    assert (home / "config.yaml.hermes-agent-synergia.bak").is_file()


def test_enable_appends_without_dropping_other_plugins(hermes_root, profile):
    home = profile("keeper", {"plugins": {"enabled": ["other-plugin"]}})
    provision._append_enabled("keeper")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["plugins"]["enabled"] == ["other-plugin", "hermes-agent-synergia"]


def test_enable_is_idempotent(hermes_root, profile):
    home = profile("keeper", {"plugins": {"enabled": ["hermes-agent-synergia"]}})
    before = (home / "config.yaml").stat().st_mtime_ns
    provision._append_enabled("keeper")
    assert (home / "config.yaml").stat().st_mtime_ns == before


def test_enable_refused_on_a_managed_install(hermes_root, profile, monkeypatch):
    """Guard 1 now applies here too — it did not before."""
    from hermes_cli import config as hconfig
    home = profile("keeper", {"model": "gpt"})
    before = (home / "config.yaml").read_bytes()
    monkeypatch.setattr(hconfig, "is_managed", lambda *a, **k: True)
    with pytest.raises(TeamError) as ei:
        provision._append_enabled("keeper")
    assert ei.value.code == "E_TEAM_MANAGED"
    assert (home / "config.yaml").read_bytes() == before


def test_delete_refuses_adopted(hermes_root, profile):
    profile("qa")
    member = __import__("model").Member.from_dict({"profile": "qa"})
    with pytest.raises(TeamError) as ei:
        provision.delete(member)
    assert ei.value.code == "E_TEAM_INVALID"
    assert "adopted" in ei.value.message
    assert (hermes_root / "profiles" / "qa").is_dir()


def test_delete_managed_goes(entry, hermes_root):
    m = provision.instantiate(entry, "px-backend-engineer")
    provision.delete(m)
    import resolve
    assert not resolve.profile_exists("px-backend-engineer")


# ── link_env: symlink only ───────────────────────────────────────────────────

def test_link_env_symlinks_not_copies(entry, hermes_root):
    m = provision.instantiate(entry, "px-backend-engineer")
    root_env = hermes_root / ".env"
    root_env.write_text("API_KEY=rotating\n", encoding="utf-8")
    provision.link_env(m.profile)
    link = hermes_root / "profiles" / m.profile / ".env"
    assert link.is_symlink()
    assert root_env.read_text() in ("API_KEY=rotating\n",)
    root_env.write_text("API_KEY=rotated\n", encoding="utf-8")
    assert link.read_text() == "API_KEY=rotated\n"      # live symlink, not a copy


def test_link_env_no_root_env_is_noop(entry, hermes_root):
    m = provision.instantiate(entry, "px-backend-engineer")
    provision.link_env(m.profile)                       # must not raise
    assert not (hermes_root / "profiles" / m.profile / ".env").exists()


# ── CLI wiring ────────────────────────────────────────────────────────────────

def _parser():
    p = argparse.ArgumentParser(prog="team")
    cli.setup_parser(p)
    return p


def run(parser, *argv, capsys):
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    env = json.loads(out.out) if args.json and out.out.strip() else None
    return code, env, out.err


def _w(hermes_root, name):
    return str(hermes_root.parent / ("pw-" + name))


def test_cli_dry_run_from_writes_nothing(pack, entry, hermes_root, profile, capsys):
    profile("qa")
    before = sorted(p.name for p in (hermes_root / "profiles").iterdir())
    code, env, err = run(_parser(), "create", "px",
                         "--from", "agency-backend-engineer:implementer:2",
                         "--member", "qa:verifier", "--orchestrator", "qa",
                         "--board", "px", "--create-board",
                         "--workdir", _w(hermes_root, "px"), "--create-workdir",
                         "--dry-run", "--json", capsys=capsys)
    assert code == 0, err
    assert env["data"]["dry_run"] is True
    assert len(env["data"]["plans"]) == 1
    assert env["data"]["plans"][0]["name"] == "px-backend-engineer"
    assert env["data"]["plans"][0]["file_count"] >= 1
    assert sorted(p.name for p in (hermes_root / "profiles").iterdir()) == before
    assert not storage.load().teams                      # registry untouched


def test_cli_create_from_real(pack, hermes_root, profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px",
                         "--from", "agency-backend-engineer:implementer:2",
                         "--from", "agency-code-reviewer:reviewer",
                         "--member", "qa:verifier", "--orchestrator", "qa",
                         "--board", "px", "--create-board",
                         "--workdir", _w(hermes_root, "px"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    team = env["data"]["team"]
    members = {m["profile"]: m for m in team["members"]}
    assert members["px-backend-engineer"]["managed"] is True
    assert members["px-backend-engineer"]["blueprint"] == "agency-backend-engineer"
    assert members["px-backend-engineer"]["capacity"] == 2
    assert members["px-code-reviewer"]["managed"] is True
    assert members["qa"]["managed"] is False
    assert env["data"]["instantiated"] == ["px-backend-engineer",
                                           "px-code-reviewer"]
    # profiles really on disk with provenance
    dy = yaml.safe_load((hermes_root / "profiles" / "px-backend-engineer"
                         / "distribution.yaml").read_text())
    assert dy["source"] == entry_dir(entry_id="agency-backend-engineer",
                                      hermes_root=hermes_root)
    # --installed filter now finds it
    code, env, _ = run(_parser(), "profiles", "list", "--installed", "--json",
                       capsys=capsys)
    ids = {e["id"] for e in env["data"]["entries"]}
    assert "agency-backend-engineer" in ids


def entry_dir(entry_id, hermes_root):
    """source_path of a catalog entry (resolved through the settings)."""
    return catalog.get(entry_id).source_path


def test_cli_name_override(pack, hermes_root, profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px2",
                         "--from", "agency-backend-engineer",
                         "--name", "custom-be",
                         "--member", "qa:orchestrator",
                         "--board", "px2", "--create-board",
                         "--workdir", _w(hermes_root, "px2"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    profiles_made = [m["profile"] for m in env["data"]["team"]["members"]]
    assert "custom-be" in profiles_made
    assert (hermes_root / "profiles" / "custom-be").is_dir()
    assert not (hermes_root / "profiles" / "px2-backend-engineer").exists()


def test_cli_provision_failure_keeps_created(pack, hermes_root, profile,
                                             capsys):
    from hermes_cli import profile_distribution as dist
    real = dist.install_distribution
    made = []

    def flaky(source, **kw):
        r = real(source, **kw)
        if len(made) >= 1:
            raise dist.DistributionError("simulated mid-create failure")
        made.append(kw.get("name"))
        return r

    # patch by hand: monkeypatch.undo() would also revert HERMES_HOME
    dist.install_distribution = flaky
    profile("qa")
    try:
        code, env, err = run(_parser(), "create", "px3",
                             "--from", "agency-backend-engineer:implementer",
                             "--from", "agency-code-reviewer:reviewer",
                             "--member", "qa:verifier", "--orchestrator", "qa",
                             "--board", "px3", "--create-board",
                             "--workdir", _w(hermes_root, "px3"),
                             "--create-workdir", "--json", capsys=capsys)
    finally:
        dist.install_distribution = real
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_PROVISION_FAILED"
    # the first instance is KEPT (resumable create)
    assert (hermes_root / "profiles" / "px3-backend-engineer").is_dir()
    # ...and the identical re-run (unpatched now) adopts them and finishes
    code, env, err = run(_parser(), "create", "px3",
                         "--from", "agency-backend-engineer:implementer",
                         "--from", "agency-code-reviewer:reviewer",
                         "--member", "qa:verifier", "--orchestrator", "qa",
                         "--board", "px3", "--create-board",
                         "--workdir", _w(hermes_root, "px3"), "--create-workdir",
                         "--json", capsys=capsys)
    # 4 = created but not ready: this bare root configures no model
    assert code in (0, 4), (err, env)
    assert env["data"]["team"]["slug"] == "px3"


def test_cli_destroy_purges_managed_profiles(pack, hermes_root, profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px4",
                         "--from", "agency-backend-engineer",
                         "--member", "qa:verifier", "--orchestrator", "qa",
                         "--board", "px4", "--create-board",
                         "--workdir", _w(hermes_root, "px4"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    # deleting managed profiles (the default) requires --yes
    code, env, _ = run(_parser(), "destroy", "px4", "--json", capsys=capsys)
    assert code == 1 and "--yes" in env["error"]["message"]
    assert _live(hermes_root, "px4-backend-engineer")      # refused = untouched
    # with --yes: managed profile deleted, adopted kept and reported skipped
    code, env, err = run(_parser(), "destroy", "px4", "--yes", "--json",
                         capsys=capsys)
    assert code == 0, err
    assert not _live(hermes_root, "px4-backend-engineer")  # managed gone
    assert (hermes_root / "profiles" / "qa").is_dir()      # adopted survives
    assert "skipped" in err


def test_cli_destroy_keep_profiles(pack, hermes_root, profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px6",
                         "--from", "agency-backend-engineer",
                         "--member", "qa:verifier", "--orchestrator", "qa",
                         "--board", "px6", "--create-board",
                         "--workdir", _w(hermes_root, "px6"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    code, env, err = run(_parser(), "destroy", "px6", "--yes", "--keep-profiles",
                         "--json", capsys=capsys)
    assert code == 0, err
    assert _live(hermes_root, "px6-backend-engineer")


def _live(hermes_root, name):
    import hermes_constants
    home = hermes_root / "profiles" / name
    return home.is_dir() and not hermes_constants.named_profile_is_deleted(home)


def test_cli_remove_member_delete_profile_adopted_skipped(pack, hermes_root,
                                                         profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px5",
                         "--from", "agency-backend-engineer",
                         "--member", "qa:verifier",
                         "--orchestrator", "px5-backend-engineer",
                         "--board", "px5", "--create-board",
                         "--workdir", _w(hermes_root, "px5"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    code, env, err = run(_parser(), "remove-member", "px5", "qa",
                         "--delete-profile", "--json", capsys=capsys)
    assert code == 0
    assert (hermes_root / "profiles" / "qa").is_dir()       # skip, not delete
    assert "adopted" in err


def test_cli_from_recipe_tier(pack, hermes_root, profile, capsys):
    profile("qa")
    code, env, err = run(_parser(), "create", "px6",
                         "--from-recipe", "software-delivery", "--tier", "minimal",
                         "--member", "qa:orchestrator",
                         "--board", "px6", "--create-board",
                         "--workdir", _w(hermes_root, "px6"), "--create-workdir",
                         "--no-sync", "--json", capsys=capsys)
    assert code == 0, err
    assert sorted(env["data"]["instantiated"]) == ["px6-backend-engineer"]
    # recipe tier roles come from suggested_role
    be = next(m for m in env["data"]["team"]["members"]
              if m["profile"] == "px6-backend-engineer")
    assert be["role"] == "implementer"        # engineering category
