"""catalog_store.py — the plugin's own writable catalog.

Authoring, directory / pack-repo / git imports, remove / hide, CAS, rollback,
and the CLI verbs over them. Git is never hit: ``_git_clone`` is patched to
copy a fixture tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

import pytest
import yaml

import hermes_constants

import catalog
import catalog_store
import cli
import storage
from errors import TeamError


@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()
    yield
    catalog._reset_caches_for_tests()
    storage._reset_caches_for_tests()


def _dist(path, name, *, soul=True):
    path.mkdir(parents=True, exist_ok=True)
    (path / "distribution.yaml").write_text(yaml.safe_dump({
        "name": name, "version": "0.1.0", "description": f"{name} does things.",
    }), encoding="utf-8")
    if soul:
        (path / "SOUL.md").write_text(f"# {name}\n", encoding="utf-8")
    skill = path / "skills" / "one"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: one\ndescription: first\n---\nbody\n",
                                    encoding="utf-8")
    return path


def _pack_repo(root, names=("agency-a", "agency-b", "agency-c")):
    root.mkdir(parents=True, exist_ok=True)
    (root / "packs.json").write_text(json.dumps({"packs": [{
        "name": "hermes-agency", "path": "hermes-agency",
        "manifest": "hermes-agency/agency.json"}]}), encoding="utf-8")
    (root / "hermes-agency").mkdir(exist_ok=True)
    (root / "hermes-agency" / "agency.json").write_text(json.dumps({"profiles": [
        {"name": n, "display_name": n.title(), "category": "qa"} for n in names]}),
        encoding="utf-8")
    for n in names:
        _dist(root / "hermes-agency" / "profiles" / n, n)
    (root / "recipes.json").write_text(json.dumps({"recipes": [
        {"id": "ab", "display_name": "AB",
         "tiers": {"minimal": ["agency-a"], "recommended": ["agency-a", "agency-b"]}},
        {"id": "ac", "display_name": "AC",
         "tiers": {"minimal": ["agency-a", "agency-c"]}},
    ]}), encoding="utf-8")
    return root


def _external_root(hermes_root, root):
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": [str(root)]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()


def _ids(**kw):
    return {e["id"] for e in catalog.entries(**kw)}


# ── contracts ─────────────────────────────────────────────────────────────────

def test_user_owned_exclude_matches_substrate():
    from hermes_cli import profile_distribution
    assert catalog_store.USER_OWNED_EXCLUDE == profile_distribution.USER_OWNED_EXCLUDE


@pytest.mark.parametrize("source,is_git", [
    ("github.com/Dadmin88/hermes-profile-packs", True),
    ("https://github.com/x/y", True),
    ("git@github.com:x/y.git", True),
    ("/root/workspace/packs", False),
    ("ext::sh -c id", False),
])
def test_git_url_detection(source, is_git):
    assert catalog_store._looks_like_git_url(source) is is_git


# ── author ────────────────────────────────────────────────────────────────────

def test_author_writes_payload_and_lists_first():
    res = catalog_store.author_entry(
        "my-reviewer", display_name="My Reviewer", category="qa",
        description="Reviews.", soul="# Role\nReview.",
        skills=[{"name": "wcag", "description": "WCAG", "body": "Check."}])
    home = catalog_store.profiles_dir() / "my-reviewer"
    assert yaml.safe_load((home / "distribution.yaml").read_text())["name"] == "my-reviewer"
    assert (home / "SOUL.md").read_text() == "# Role\nReview.\n"
    assert "name: wcag" in (home / "skills" / "wcag" / "SKILL.md").read_text()
    assert res["revision"] == 1 and res["warnings"] == []
    e = next(x for x in catalog.entries() if x["id"] == "my-reviewer")
    assert (e["local"], e["pack"], e["display_name"], e["description"],
            e["suggested_role"]) == (True, "local", "My Reviewer", "Reviews.", "reviewer")
    first = catalog.sources()[0]
    assert first["local"] is True and first["resolved"] is True and first["packs"] == 1


def test_author_without_soul_warns():
    res = catalog_store.author_entry("bare-one")
    assert res["warnings"] and "SOUL.md" in res["warnings"][0]


@pytest.mark.parametrize("bad", ["Default", "has space", "", "-lead"])
def test_author_rejects_invalid_ids(bad):
    with pytest.raises(TeamError) as ei:
        catalog_store.author_entry(bad, soul="x")
    assert ei.value.code == "E_TEAM_INVALID"


def test_author_duplicate_needs_replace():
    catalog_store.author_entry("x-one", soul="a")
    with pytest.raises(TeamError) as ei:
        catalog_store.author_entry("x-one", soul="b")
    assert ei.value.code == "E_TEAM_EXISTS"
    catalog_store.author_entry("x-one", soul="b", replace=True)
    assert (catalog_store.profiles_dir() / "x-one" / "SOUL.md").read_text() == "b\n"


def test_author_rejects_bad_and_duplicate_skills():
    with pytest.raises(TeamError):
        catalog_store.author_entry("s-one", skills=[{"name": "Bad Name"}])
    with pytest.raises(TeamError):
        catalog_store.author_entry("s-one", skills=[{"name": "a"}, {"name": "a"}])
    assert not (catalog_store.profiles_dir() / "s-one").exists()


# ── import ────────────────────────────────────────────────────────────────────

def test_import_single_distribution_skips_user_owned(tmp_path):
    src = _dist(tmp_path / "src" / "solo", "solo-agent")
    (src / ".env").write_text("SECRET=1", encoding="utf-8")
    (src / "memories").mkdir()
    (src / "memories" / "m.md").write_text("x", encoding="utf-8")
    plan = catalog_store.plan_import(str(src))
    row = plan["entries"][0]
    assert plan["kind"] == "profile" and row["id"] == "solo-agent"
    assert ".env" not in row["files"] and not any(f.startswith("memories") for f in row["files"])
    assert not (catalog_store.profiles_dir() / "solo-agent").exists()

    res = catalog_store.import_entries(str(src))
    home = catalog_store.profiles_dir() / "solo-agent"
    assert res["imported"] == ["solo-agent"]
    assert (home / "SOUL.md").is_file() and (home / "skills" / "one" / "SKILL.md").is_file()
    assert not (home / ".env").exists() and not (home / "memories").exists()
    assert catalog.get("solo-agent").local is True
    assert catalog_store.load_index()["entries"]["solo-agent"]["origin"] == str(src.resolve())


def test_import_single_with_name_rewrites_manifest(tmp_path):
    src = _dist(tmp_path / "solo", "solo-agent")
    catalog_store.import_entries(str(src), name="renamed")
    dist = yaml.safe_load((catalog_store.profiles_dir() / "renamed"
                           / "distribution.yaml").read_text())
    assert dist["name"] == "renamed"
    assert yaml.safe_load((src / "distribution.yaml").read_text())["name"] == "solo-agent"


def test_import_pack_repo_only_and_recipe_merge(tmp_path):
    repo = _pack_repo(tmp_path / "repo")
    plan = catalog_store.plan_import(str(repo), only="agency-a,agency-b")
    assert [e["id"] for e in plan["entries"]] == ["agency-a", "agency-b"]
    assert plan["recipes"] == ["ab"]

    res = catalog_store.import_entries(str(repo), only=["agency-a", "agency-b"])
    assert res["imported"] == ["agency-a", "agency-b"] and res["recipes"] == ["ab"]
    e = catalog.get("agency-a")
    assert (e.pack, e.display_name, e.category, e.local) == (
        "hermes-agency", "Agency-A", "qa", True)
    assert [r["id"] for r in catalog.recipes()] == ["ab"]

    catalog_store.import_entries(str(repo), only="agency-c")
    assert sorted(r["id"] for r in catalog.recipes()) == ["ab", "ac"]


def test_import_pack_unknown_only_id(tmp_path):
    repo = _pack_repo(tmp_path / "repo")
    with pytest.raises(TeamError) as ei:
        catalog_store.plan_import(str(repo), only="agency-zzz")
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"


def test_import_duplicate_needs_replace(tmp_path):
    repo = _pack_repo(tmp_path / "repo")
    catalog_store.import_entries(str(repo))
    with pytest.raises(TeamError) as ei:
        catalog_store.import_entries(str(repo))
    assert ei.value.code == "E_TEAM_EXISTS"
    res = catalog_store.import_entries(str(repo), replace=True)
    assert res["count"] == 3


def test_import_git_url_is_cloned_then_staged(monkeypatch, tmp_path):
    repo = _pack_repo(tmp_path / "upstream")
    calls = []

    def fake_clone(url, dest):
        calls.append(url)
        shutil.copytree(repo, dest)

    monkeypatch.setattr(catalog_store, "_git_clone", fake_clone)
    res = catalog_store.import_entries("https://github.com/example/packs")
    assert calls == ["https://github.com/example/packs"]
    assert res["count"] == 3 and res["source"] == "https://github.com/example/packs"
    assert (catalog_store.load_index()["entries"]["agency-a"]["origin"]
            == "https://github.com/example/packs")


def test_import_unresolvable_source():
    with pytest.raises(TeamError) as ei:
        catalog_store.plan_import("/definitely/not/here")
    assert ei.value.code == "E_TEAM_CATALOG_UNAVAILABLE"


def test_import_refuses_symlinks(tmp_path):
    src = _dist(tmp_path / "s", "linky")
    os.symlink("/etc/hostname", src / "skills" / "one" / "leak")
    with pytest.raises(TeamError) as ei:
        catalog_store.import_entries(str(src))
    assert ei.value.code == "E_TEAM_INVALID" and "symlink" in ei.value.message
    assert not (catalog_store.profiles_dir() / "linky").exists()


# ── adopt: a live root profile → a store entry ────────────────────────────────

def _live_profile(hermes_root, name, **extra):
    """A profile home under the hermes root, with user-owned state to prove
    ``USER_OWNED_EXCLUDE`` keeps it out of the store."""
    home = _dist(hermes_root / "profiles" / name, name)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / ".env").write_text("SECRET=1", encoding="utf-8")
    (home / "state.db").write_text("x", encoding="utf-8")
    (home / "sessions").mkdir()
    (home / "sessions" / "a.jsonl").write_text("{}", encoding="utf-8")
    for key, value in extra.items():
        (home / key).write_text(value, encoding="utf-8")
    return home


def test_adopt_copies_payload_without_user_owned_state(hermes_root):
    home = _live_profile(hermes_root, "live-reviewer")
    res = catalog_store.adopt_profile("live-reviewer")
    assert (res["profile"], res["adopted"], res["source"]) == (
        "live-reviewer", "live-reviewer", str(home))
    dest = catalog_store.profiles_dir() / "live-reviewer"
    assert (dest / "SOUL.md").is_file()
    assert (dest / "skills" / "one" / "SKILL.md").is_file()
    for excluded in (".env", "state.db", "sessions"):
        assert not (dest / excluded).exists(), excluded
    assert yaml.safe_load((dest / "distribution.yaml").read_text())["name"] \
        == "live-reviewer"
    e = catalog.get("live-reviewer")
    assert e.local is True and e.pack == "local"
    assert catalog_store.load_index()["entries"]["live-reviewer"]["origin"] \
        == "profile:live-reviewer"


def test_adopt_synthesises_manifest_for_hand_made_profile(hermes_root, profile):
    home = profile("hand-made")
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"display_name": "Hand Made", "description": "By hand."}),
        encoding="utf-8")
    res = catalog_store.adopt_profile("hand-made", name="handy")
    dist = yaml.safe_load((catalog_store.profiles_dir() / "handy"
                           / "distribution.yaml").read_text())
    assert dist["name"] == "handy" and dist["version"] == "0.1.0"
    assert dist["description"] == "By hand."
    assert "source" not in dist and "installed_at" not in dist
    assert res["warnings"] and "SOUL.md" in res["warnings"][0]
    assert catalog.get("handy").display_name == "Hand Made"
    assert not (catalog_store.profiles_dir() / "hand-made").exists()


def test_adopt_drops_instance_provenance(hermes_root):
    home = _live_profile(hermes_root, "installed-one")
    dist = yaml.safe_load((home / "distribution.yaml").read_text())
    dist.update({"source": "/opt/profile-hermes/x", "installed_at": "2026-01-01T00:00:00Z"})
    (home / "distribution.yaml").write_text(yaml.safe_dump(dist), encoding="utf-8")
    catalog_store.adopt_profile("installed-one")
    out = yaml.safe_load((catalog_store.profiles_dir() / "installed-one"
                          / "distribution.yaml").read_text())
    assert "source" not in out and "installed_at" not in out


def test_adopt_refuses_missing_and_tombstoned(hermes_root, profile):
    with pytest.raises(TeamError) as ei:
        catalog_store.adopt_profile("no-such-profile")
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"

    home = profile("husk-one")
    hermes_constants.mark_named_profile_deleted(home)
    with pytest.raises(TeamError) as ei:
        catalog_store.adopt_profile("husk-one")
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"
    assert "tombstone" in ei.value.hint
    assert not (catalog_store.profiles_dir() / "husk-one").exists()


def test_adopt_duplicate_needs_replace(hermes_root):
    _live_profile(hermes_root, "dup-one")
    catalog_store.adopt_profile("dup-one")
    with pytest.raises(TeamError) as ei:
        catalog_store.adopt_profile("dup-one")
    assert ei.value.code == "E_TEAM_EXISTS"
    (hermes_root / "profiles" / "dup-one" / "SOUL.md").write_text("# new\n",
                                                                 encoding="utf-8")
    catalog_store.adopt_profile("dup-one", replace=True)
    assert (catalog_store.profiles_dir() / "dup-one" / "SOUL.md").read_text() \
        == "# new\n"


def test_adopt_cas_conflict_writes_nothing(hermes_root):
    _live_profile(hermes_root, "cas-profile")
    with pytest.raises(TeamError) as ei:
        catalog_store.adopt_profile("cas-profile", expected_revision=9)
    assert ei.value.code == "E_TEAM_CONFLICT" and ei.value.exit_code == 3
    assert not (catalog_store.profiles_dir() / "cas-profile").exists()


def test_adopt_ignores_symlinks_outside_the_payload(hermes_root, tmp_path):
    """``sessions``/``cache`` legitimately hold links and are never copied, so
    only the payload subtrees are walked."""
    home = _live_profile(hermes_root, "linky")
    (home / "sessions" / "link").symlink_to(tmp_path)
    catalog_store.adopt_profile("linky")
    assert (catalog_store.profiles_dir() / "linky" / "SOUL.md").is_file()

    (home / "skills" / "one" / "escape").symlink_to(tmp_path)
    with pytest.raises(TeamError) as ei:
        catalog_store.adopt_profile("linky", name="linky-two")
    assert ei.value.code == "E_TEAM_INVALID"


def test_adopted_entry_shows_in_root_listing(hermes_root):
    _live_profile(hermes_root, "shown-one")
    assert catalog.root_profiles()[0]["adopted_as"] == ""
    catalog_store.adopt_profile("shown-one", name="shown-entry")
    assert catalog.root_profiles()[0]["adopted_as"] == "shown-entry"


# ── external roots: shadow refusal, hide / unhide ─────────────────────────────

def test_external_entries_are_read_only_but_hideable(hermes_root, tmp_path):
    _external_root(hermes_root, _pack_repo(tmp_path / "ext"))
    with pytest.raises(TeamError) as ei:
        catalog_store.author_entry("agency-a", soul="x")
    assert ei.value.code == "E_TEAM_EXISTS"
    with pytest.raises(TeamError) as ei:
        catalog_store.remove_entry("agency-a")
    assert "hide" in ei.value.remedy

    catalog_store.set_hidden("agency-a", True)
    assert "agency-a" not in _ids()
    assert "agency-a" in _ids(include_hidden=True)
    assert catalog.get("agency-a").hidden is True
    catalog_store.set_hidden("agency-a", False)
    assert "agency-a" in _ids()


def test_hide_unknown_id():
    with pytest.raises(TeamError) as ei:
        catalog_store.set_hidden("nope-nope", True)
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"


# ── edit ──────────────────────────────────────────────────────────────────────

def _index_row(eid):
    return catalog_store.load_index(use_cache=False)["entries"][eid]


def test_edit_fields_keeps_row_provenance_and_payload():
    catalog_store.author_entry("ed-one", display_name="Old", category="qa",
                               description="Old.", soul="# Old", author="me")
    home = catalog_store.profiles_dir() / "ed-one"
    (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
    added = _index_row("ed-one")["added_at"]
    res = catalog_store.update_entry("ed-one", display_name="New",
                                     category="engineering", description="New.",
                                     author="")
    assert res["changed"] == ["author", "category", "description", "display_name"]
    assert res["revision"] == 2
    row = _index_row("ed-one")
    assert row["added_at"] == added and row["updated_at"]
    assert row["origin"] == "authored"
    e = catalog.get("ed-one")
    assert (e.display_name, e.category, e.description, e.author) == (
        "New", "engineering", "New.", "")
    assert (home / "SOUL.md").read_text() == "# Old\n"
    assert (home / "config.yaml").read_text() == "model: x\n"
    assert yaml.safe_load((home / "distribution.yaml").read_text())["name"] == "ed-one"


def test_edit_soul_replace_then_clear():
    catalog_store.author_entry("ed-soul", display_name="Shown", soul="a")
    home = catalog_store.profiles_dir() / "ed-soul"
    catalog_store.update_entry("ed-soul", soul="b\n\n")
    assert (home / "SOUL.md").read_text() == "b\n"
    res = catalog_store.update_entry("ed-soul", soul="", display_name="")
    assert not (home / "SOUL.md").exists() and res["warnings"]
    assert catalog.get("ed-soul").display_name == "ed-soul"


def test_edit_skills_keeps_extra_files_and_frontmatter(tmp_path):
    catalog_store.author_entry("ed-sk", soul="x", skills=[
        {"name": "alpha", "description": "A", "body": "old"},
        {"name": "beta", "description": "B", "body": "b"}])
    alpha = catalog_store.profiles_dir() / "ed-sk" / "skills" / "alpha"
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: A\nallowed-tools: [terminal]\n---\n\nold\n",
        encoding="utf-8")
    (alpha / "helper.py").write_text("print(1)\n", encoding="utf-8")
    gamma = tmp_path / "gamma"
    gamma.mkdir()
    (gamma / "SKILL.md").write_text("---\nname: gamma\n---\ng\n", encoding="utf-8")
    res = catalog_store.update_entry(
        "ed-sk", skills=[{"name": "alpha", "description": "A2", "body": "new"}],
        skill_dirs=[gamma], remove_skills=["beta"])
    assert res["changed"] == ["skills"]
    skills = catalog_store.describe(str(alpha.parent.parent))["skills"]
    assert [(s["dir"], s["description"], s["body"], s["files"]) for s in skills] == [
        ("alpha", "A2", "new\n", 2), ("gamma", "", "g\n", 1)]
    assert "allowed-tools" in (alpha / "SKILL.md").read_text()


def test_edit_refusals_write_nothing():
    catalog_store.author_entry("ed-bad", soul="x",
                               skills=[{"name": "alpha", "description": "A"}])
    before = catalog_store.load_index(use_cache=False)["revision"]
    for kw in ({"remove_skills": ["ghost"]},
               {"remove_skills": ["../../ed-bad"]},
               {"skills": [{"name": "alpha"}], "remove_skills": ["alpha"]},
               {"skills": [{"name": "Bad Name"}]},
               {}):
        with pytest.raises(TeamError) as ei:
            catalog_store.update_entry("ed-bad", **kw)
        assert ei.value.code == "E_TEAM_INVALID", kw
    assert catalog_store.load_index(use_cache=False)["revision"] == before
    assert (catalog_store.profiles_dir() / "ed-bad" / "skills" / "alpha").is_dir()


def test_edit_refuses_external_and_unknown_entries(hermes_root, tmp_path):
    _external_root(hermes_root, _pack_repo(tmp_path / "ext"))
    with pytest.raises(TeamError) as ei:
        catalog_store.update_entry("agency-a", description="x")
    assert ei.value.code == "E_TEAM_INVALID"
    with pytest.raises(TeamError) as ei:
        catalog_store.update_entry("ghost-one", description="x")
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"


def test_edit_conflict_and_failed_index_write_keep_original(monkeypatch):
    catalog_store.author_entry("ed-cas", soul="original")
    home = catalog_store.profiles_dir() / "ed-cas"
    with pytest.raises(TeamError) as ei:
        catalog_store.update_entry("ed-cas", soul="new", expected_revision=9)
    assert ei.value.code == "E_TEAM_CONFLICT"
    assert (home / "SOUL.md").read_text() == "original\n"

    def boom(mutate, *, expected_revision=None):
        raise TeamError("E_TEAM_LOCKED", "held", hint="retry", remedy="retry")

    monkeypatch.setattr(catalog_store, "_write_index", boom)
    with pytest.raises(TeamError):
        catalog_store.update_entry("ed-cas", soul="new")
    assert (home / "SOUL.md").read_text() == "original\n"
    assert not [p for p in catalog_store.local_root().iterdir()
                if p.name.startswith((".staging-", ".trash-"))]


def test_describe_flags_truncated_previews(monkeypatch):
    catalog_store.author_entry("ed-big", soul="0123456789",
                               skills=[{"name": "alpha", "body": "0123456789"}])
    monkeypatch.setattr(catalog_store, "SOUL_PREVIEW_MAX", 8)
    d = catalog_store.describe(catalog.get("ed-big").source_path)
    assert d["soul"] == "01234567" and d["soul_truncated"] is True
    assert d["skills"][0]["body_truncated"] is True


# ── remove ────────────────────────────────────────────────────────────────────

def test_remove_deletes_entry_and_files():
    catalog_store.author_entry("my-x", soul="x")
    res = catalog_store.remove_entry("my-x")
    assert res["removed"] == "my-x"
    assert not (catalog_store.profiles_dir() / "my-x").exists()
    with pytest.raises(TeamError):
        catalog.get("my-x")


def test_remove_reports_team_refs_and_drops_recipes(tmp_path, capsys):
    catalog_store.import_entries(str(_pack_repo(tmp_path / "repo")))
    storage.registry_path().parent.mkdir(parents=True, exist_ok=True)
    storage.registry_path().write_text(yaml.safe_dump({
        "schema_version": 1, "revision": 1, "teams": {"px": {
            "slug": "px", "board": "px", "workdir": "/tmp/px",
            "orchestrator": "px-b",
            "members": [{"profile": "px-b", "role": "orchestrator",
                         "blueprint": "agency-b", "managed": True,
                         "primary": True}]}}}), encoding="utf-8")
    storage._reset_caches_for_tests()
    assert catalog_store.references("agency-b")["recipes"] == ["ab"]
    code, env, _ = _run("profiles", "remove", "agency-b", "--json", capsys=capsys)
    assert code == 1 and "drops recipe(s) ab" in env["error"]["message"]
    res = catalog_store.remove_entry("agency-b")
    assert res["team_refs"] == ["px/px-b"]
    assert res["dropped_recipes"] == ["ab"]
    assert [r["id"] for r in catalog.recipes()] == ["ac"]


def test_remove_unknown_local_entry():
    with pytest.raises(TeamError) as ei:
        catalog_store.remove_entry("ghost-one")
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"


# ── CAS, rollback, memo, degradation ──────────────────────────────────────────

def test_cas_conflict_writes_nothing():
    with pytest.raises(TeamError) as ei:
        catalog_store.author_entry("cas-one", soul="x", expected_revision=5)
    assert ei.value.code == "E_TEAM_CONFLICT" and ei.value.exit_code == 3
    assert not (catalog_store.profiles_dir() / "cas-one").exists()


def test_failed_index_write_restores_replaced_copy(monkeypatch):
    catalog_store.author_entry("keep-me", soul="original")

    def boom(mutate, *, expected_revision=None):
        raise TeamError("E_TEAM_LOCKED", "held", hint="retry", remedy="retry")

    monkeypatch.setattr(catalog_store, "_write_index", boom)
    with pytest.raises(TeamError):
        catalog_store.author_entry("keep-me", soul="new", replace=True)
    with pytest.raises(TeamError):
        catalog_store.author_entry("fresh-one", soul="x")
    home = catalog_store.profiles_dir()
    assert (home / "keep-me" / "SOUL.md").read_text() == "original\n"
    assert not (home / "fresh-one").exists()
    assert not [p for p in catalog_store.local_root().iterdir()
                if p.name.startswith((".staging-", ".trash-"))]


def test_memo_invalidates_on_store_write():
    assert "memo-one" not in _ids()
    catalog_store.author_entry("memo-one", soul="x")
    # author_entry's clash check forces its own rebuild, before the index
    # moves — so count from here.
    builds = catalog._BUILD_COUNT
    assert "memo-one" in _ids()
    assert catalog._BUILD_COUNT == builds + 1
    _ids()
    assert catalog._BUILD_COUNT == builds + 1


def test_corrupt_index_degrades_catalog_but_refuses_writes():
    catalog_store.index_path().parent.mkdir(parents=True, exist_ok=True)
    catalog_store.index_path().write_text("entries: [", encoding="utf-8")
    src = catalog.sources()[0]
    assert src["local"] is True and src["resolved"] is False
    catalog.entries()
    with pytest.raises(TeamError):
        catalog_store.load_index()


def test_orphans_and_describe():
    catalog_store.author_entry("desc-one", soul="# Soul",
                               skills=[{"name": "alpha", "description": "A"}])
    (catalog_store.profiles_dir() / "stray").mkdir()
    assert catalog_store.orphans() == {"dirs_without_row": ["stray"],
                                       "rows_without_dir": []}
    d = catalog_store.describe(catalog.get("desc-one").source_path)
    assert d["soul"] == "# Soul\n" and d["soul_truncated"] is False
    assert d["skills"] == [{"name": "alpha", "dir": "alpha", "description": "A",
                            "body": "", "body_truncated": False, "files": 1}]


# ── CLI ───────────────────────────────────────────────────────────────────────

def _run(*argv, capsys):
    p = argparse.ArgumentParser(prog="team")
    cli.setup_parser(p)
    code = cli.handle(p.parse_args(list(argv)))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_cli_add_list_show_hide_remove(tmp_path, capsys):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Role\nline\n", encoding="utf-8")
    code, env, _ = _run("profiles", "add", "cli-one", "--soul-file", str(soul),
                        "--category", "qa", "--json", capsys=capsys)
    assert code == 0 and env["data"]["id"] == "cli-one"

    code, env, _ = _run("profiles", "list", "--local", "--json", capsys=capsys)
    assert [e["id"] for e in env["data"]["entries"]] == ["cli-one"]
    assert env["data"]["revision"] == 1

    code, env, _ = _run("profiles", "show", "cli-one", "--json", capsys=capsys)
    assert env["data"]["soul"].startswith("# Role")
    assert env["data"]["referenced_by"] == {"teams": [], "recipes": []}

    code, env, _ = _run("profiles", "hide", "cli-one", "--json", capsys=capsys)
    assert code == 0 and env["data"]["hidden"] is True
    code, env, _ = _run("profiles", "list", "--json", capsys=capsys)
    assert "cli-one" not in {e["id"] for e in env["data"]["entries"]}
    code, env, _ = _run("profiles", "list", "--hidden", "--json", capsys=capsys)
    assert "cli-one" in {e["id"] for e in env["data"]["entries"]}

    code, env, _ = _run("profiles", "remove", "cli-one", "--json", capsys=capsys)
    assert code == 1 and env["error"]["code"] == "E_TEAM_INVALID"
    code, env, _ = _run("profiles", "remove", "cli-one", "--yes", "--json",
                        capsys=capsys)
    assert code == 0 and env["data"]["removed"] == "cli-one"


def test_cli_edit(tmp_path, capsys):
    catalog_store.author_entry("cli-ed", soul="old", skills=[{"name": "alpha"}])
    soul = tmp_path / "SOUL.md"
    soul.write_text("# New\n", encoding="utf-8")
    code, env, _ = _run("profiles", "edit", "cli-ed", "--soul-file", str(soul),
                        "--category", "qa", "--remove-skill", "alpha", "--json",
                        capsys=capsys)
    assert code == 0 and env["data"]["changed"] == ["category", "skills", "soul"]
    code, env, _ = _run("profiles", "show", "cli-ed", "--json", capsys=capsys)
    assert env["data"]["soul"] == "# New\n" and env["data"]["skills"] == []
    assert env["data"]["soul_truncated"] is False

    code, env, _ = _run("profiles", "edit", "cli-ed", "--json", capsys=capsys)
    assert code == 1 and env["error"]["code"] == "E_TEAM_INVALID"
    code, env, _ = _run("profiles", "edit", "cli-ed", "--category", "x",
                        "--expected-revision", "7", "--json", capsys=capsys)
    assert code == 3 and env["error"]["code"] == "E_TEAM_CONFLICT"

def test_cli_import_dry_run_then_real(tmp_path, capsys):
    repo = _pack_repo(tmp_path / "repo")
    code, env, _ = _run("profiles", "import", str(repo), "--dry-run", "--json",
                        capsys=capsys)
    assert code == 0 and env["data"]["dry_run"] is True and env["data"]["count"] == 3
    assert not catalog_store.index_path().exists()

    code, env, err = _run("profiles", "import", str(repo), "--only", "agency-a",
                          capsys=capsys)
    assert code == 0 and env is None and "imported 1 entry" in err


def test_cli_add_missing_soul_file(capsys):
    code, env, _ = _run("profiles", "add", "no-soul", "--soul-file", "/nope/SOUL.md",
                        "--json", capsys=capsys)
    assert code == 1 and env["error"]["code"] == "E_TEAM_INVALID"


def test_cli_cas_conflict_exits_3(capsys):
    code, env, _ = _run("profiles", "add", "z-one", "--expected-revision", "7",
                        "--json", capsys=capsys)
    assert code == 3 and env["error"]["code"] == "E_TEAM_CONFLICT"


def test_cli_list_root_then_adopt(hermes_root, capsys):
    _live_profile(hermes_root, "cli-live")
    code, env, _ = _run("profiles", "list", "--root", "--json", capsys=capsys)
    assert code == 0 and env["data"]["count"] == 1
    assert env["data"]["profiles"][0]["id"] == "cli-live"

    code, env, _ = _run("profiles", "list", "--json", capsys=capsys)
    assert env["data"]["entries"] == []

    code, env, _ = _run("profiles", "adopt", "cli-live", "--json", capsys=capsys)
    assert code == 0 and env["data"]["adopted"] == "cli-live"

    code, env, _ = _run("profiles", "list", "--json", capsys=capsys)
    assert [e["id"] for e in env["data"]["entries"]] == ["cli-live"]
    code, env, _ = _run("profiles", "list", "--root", "--json", capsys=capsys)
    assert env["data"]["profiles"][0]["adopted_as"] == "cli-live"


def test_cli_adopt_unknown_exits_1(capsys):
    code, env, _ = _run("profiles", "adopt", "nope-nope", "--json", capsys=capsys)
    assert code == 1 and env["error"]["code"] == "E_TEAM_NO_BLUEPRINT"


def test_cli_bare_profiles_lists(capsys):
    code, env, err = _run("profiles", capsys=capsys)
    assert code == 0 and env is None and "entr" in err


# ── bundled samples ──────────────────────────────────────────────────────────

_SAMPLE_IDS = sorted(d.name for d in catalog_store.SAMPLES_ROOT.iterdir()
                     if (d / "distribution.yaml").is_file())


def test_sample_dir_names_match_their_distribution_names():
    assert len(_SAMPLE_IDS) == 5
    for eid in _SAMPLE_IDS:
        dist = yaml.safe_load(
            (catalog_store.SAMPLES_ROOT / eid / "distribution.yaml").read_text())
        assert dist["name"] == eid


def test_seed_samples_fills_a_fresh_store_once():
    assert catalog_store.seed_samples() == _SAMPLE_IDS
    index = catalog_store.load_index(use_cache=False)
    assert index["seeded"] == _SAMPLE_IDS
    assert all(index["entries"][eid]["pack"] == catalog_store.SAMPLES_PACK
               for eid in _SAMPLE_IDS)
    assert all((catalog_store.profiles_dir() / eid / "SOUL.md").is_file()
               for eid in _SAMPLE_IDS)

    assert catalog_store.seed_samples() == []
    assert catalog_store.load_index(use_cache=False)["revision"] == index["revision"]


def test_seed_samples_never_brings_back_a_removed_sample():
    catalog_store.seed_samples()
    catalog_store.remove_entry("software_tester")

    assert catalog_store.seed_samples() == []
    assert "software_tester" not in catalog_store.load_index(use_cache=False)["entries"]


def test_seed_samples_leaves_a_user_owned_id_alone():
    catalog_store.author_entry("software_manager", soul="mine")

    added = catalog_store.seed_samples()

    assert "software_manager" not in added
    index = catalog_store.load_index(use_cache=False)
    assert index["entries"]["software_manager"]["pack"] == catalog_store.LOCAL_PACK
    assert "software_manager" in index["seeded"]
    assert (catalog_store.profiles_dir() / "software_manager" / "SOUL.md"
            ).read_text().strip() == "mine"
