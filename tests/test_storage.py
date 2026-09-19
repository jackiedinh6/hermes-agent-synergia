"""S5 — storage.py: CAS, lock, fail-open read, host-global resolution (plan §14.2)."""

from __future__ import annotations

import os
import threading

import pytest
import yaml

import hermes_constants
import storage
from errors import TeamError, TeamRegistryError
from model import Registry, Team


@pytest.fixture(autouse=True)
def _fresh_storage():
    storage._reset_caches_for_tests()
    yield
    storage._reset_caches_for_tests()


def _team(slug, member="dev-1", revision=1):
    return Team.from_dict({
        "slug": slug, "board": slug, "workdir": f"/w/{slug}",
        "orchestrator": member, "revision": revision,
        "members": [{"profile": member}],
    })


# ── round-trip ───────────────────────────────────────────────────────────────

def test_round_trip_preserves_unknown_keys(hermes_root):
    storage.upsert(_team("alpha"))
    path = storage.registry_path()
    raw = yaml.safe_load(path.read_text())
    raw["teams"]["alpha"]["future_field"] = {"x": [1, 2]}
    raw["some_future_top_key"] = 9
    path.write_text(yaml.safe_dump(raw))
    storage._reset_caches_for_tests()
    reg = storage.load()
    assert reg.to_dict()["teams"]["alpha"]["future_field"] == {"x": [1, 2]}
    assert reg.to_dict()["some_future_top_key"] == 9
    storage.save(reg)
    again = storage.load()
    assert again.to_dict()["teams"]["alpha"]["future_field"] == {"x": [1, 2]}
    assert again.to_dict()["some_future_top_key"] == 9


# ── CAS ──────────────────────────────────────────────────────────────────────

def test_cas_two_writers_second_conflicts(hermes_root):
    reg = storage.load()
    # Writer 1 and writer 2 both snapshot revision 0.
    t1, t2 = _team("one"), _team("two")
    teams1 = dict(reg.teams, one=t1)
    teams2 = dict(reg.teams, two=t2)
    storage.save(Registry(teams=teams1), expected_revision=reg.revision)
    with pytest.raises(TeamError) as ei:
        storage.save(Registry(teams=teams2), expected_revision=reg.revision)
    assert ei.value.code == "E_TEAM_CONFLICT"
    # "carrying both revisions"
    assert "expected revision 0" in ei.value.message
    assert "found 1" in ei.value.message
    # exactly one mutation on disk
    on_disk = storage.load().teams
    assert set(on_disk) == {"one"}


def test_cas_message_names_both_revisions(hermes_root):
    storage.upsert(_team("a"))
    stale = storage.load().revision
    storage.upsert(_team("b"))
    with pytest.raises(TeamError) as ei:
        storage.upsert(_team("c"), expected_revision=stale)
    assert ei.value.code == "E_TEAM_CONFLICT"


def test_per_team_revision_bumps_on_upsert(hermes_root):
    out = storage.upsert(_team("a"))
    assert out.revision == 1
    out2 = storage.upsert(_team("a", member="dev-2"))
    assert out2.revision == 2
    assert out2.members[0].profile == "dev-2"


# ── lock ─────────────────────────────────────────────────────────────────────

def test_lock_contention_raises_locked(hermes_root):
    storage.upsert(_team("a"))
    import fcntl
    path = storage.registry_path()
    blocker = open(path.with_name(path.name + ".lock"), "a+")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(TeamError) as ei:
            storage.upsert(_team("b"))
        assert ei.value.code == "E_TEAM_LOCKED"
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()
    # after release, writes work again
    storage.upsert(_team("b"))
    assert "b" in storage.load().teams


# ── corrupt file: fail-open ──────────────────────────────────────────────────

def test_corrupt_yaml_load_raises_or_empty_last_good(hermes_root):
    storage.upsert(_team("a"))
    storage.upsert(_team("b"))            # second write copies .bak
    path = storage.registry_path()
    assert path.with_name(path.name + ".bak").is_file()
    good = storage.load()
    path.write_text("::: not yaml :::\n\t- broken", encoding="utf-8")
    storage._PARSE_MEMO.clear()
    with pytest.raises(TeamRegistryError):
        storage.load()
    fallback = storage.load_or_empty()
    assert set(fallback.teams) == set(good.teams)   # last good parse


def test_first_ever_failure_logs_once_and_returns_empty(hermes_root, caplog):
    path = storage.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[[[((", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING):
        reg = storage.load_or_empty()
        assert reg.teams == {}
        reg2 = storage.load_or_empty()
        assert reg2.teams == {}
    warns = [r for r in caplog.records if "registry unreadable" in r.getMessage()]
    assert len(warns) == 1


# ── host-global resolution ───────────────────────────────────────────────────

def test_registry_dir_same_inode_from_profile_home(hermes_root):
    """Regression test: HERMES_HOME=<root> vs <root>/profiles/qa."""
    storage.upsert(_team("a"))            # creates plugin-data/hermes-agent-synergia
    path_root = storage.registry_dir()
    inode_root = path_root.stat().st_ino

    monkey = os.environ.copy()
    try:
        os.environ["HERMES_HOME"] = str(hermes_root / "profiles" / "qa")
        hermes_constants._default_hermes_root_memo = None
        hermes_constants.reset_hermes_home_key_cache()
        path_prof = storage.registry_dir()
        assert path_prof == path_root      # same directory
        assert path_prof.stat().st_ino == inode_root
    finally:
        os.environ.clear()
        os.environ.update(monkey)
        hermes_constants._default_hermes_root_memo = None
        hermes_constants.reset_hermes_home_key_cache()


def test_env_beats_setting_beats_default(hermes_root, monkeypatch, tmp_path):
    # default: <root>/plugin-data/hermes-agent-synergia
    assert storage.registry_dir() == hermes_root / "plugin-data" / "hermes-agent-synergia"
    # config setting beats default
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"registry_dir": str(tmp_path / "conf-dir")}}}}}),
        encoding="utf-8")
    assert storage.registry_dir() == tmp_path / "conf-dir"
    # env beats config
    monkeypatch.setenv("HERMES_TEAM_HOME", str(tmp_path / "env-dir"))
    assert storage.registry_dir() == tmp_path / "env-dir"


# ── misc API ─────────────────────────────────────────────────────────────────

def test_delete_unknown_team(hermes_root):
    storage.upsert(_team("a"))
    with pytest.raises(TeamError) as ei:
        storage.delete("nope")
    assert ei.value.code == "E_TEAM_NOT_FOUND"
    storage.delete("a")
    assert storage.load().teams == {}


def test_index_by_profile(hermes_root):
    storage.upsert(_team("a", member="qa"))
    storage.upsert(_team("b", member="qa"))
    idx = storage.index_by_profile()
    assert sorted(idx["qa"]) == ["a", "b"]


def test_missing_file_loads_empty(hermes_root):
    assert storage.load().teams == {}
    assert storage.load_or_empty().teams == {}
