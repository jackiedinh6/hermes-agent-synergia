"""S6a — catalog.py: pack resolution, entry shape, degradation, role hints.

Spec §14.7 catalog section. A synthetic two-pack root is built in
tmp_path and pointed at via ``catalog_paths``; the real ``/opt/profile-hermes``
assertion belongs to the live-stack G0 gate.
"""

from __future__ import annotations

import argparse
import json

import pytest
import yaml

import hermes_constants

import catalog
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


# ── synthetic pack root ───────────────────────────────────────────────────────

def _make_pack_root(root, *, recipes=True):
    """``root/`` laid out like ``/opt/profile-hermes``: packs.json,
    hermes-agency/agency.json, hermes-agency/profiles/<name>/distribution.yaml.

    ``agency-ghost`` is named by the manifest but has no directory — the
    drop-and-record-info case.
    """
    root.mkdir(parents=True, exist_ok=True)
    entries = [
        ("agency-backend-engineer", "Backend Engineer", "engineering"),
        ("agency-code-reviewer", "Code Reviewer", "engineering"),
        ("agency-qa-tester", "QA Tester", "qa"),
        ("agency-ghost", "Ghost", "no-such-dir"),
    ]
    (root / "packs.json").write_text(json.dumps({
        "schema_version": 1,
        "packs": [{"name": "hermes-agency", "path": "hermes-agency",
                   "namespace": "agency-*",
                   "manifest": "hermes-agency/agency.json"}],
    }), encoding="utf-8")
    (root / "hermes-agency").mkdir(exist_ok=True)
    (root / "hermes-agency" / "agency.json").write_text(json.dumps({
        "schema_version": 2, "name": "hermes-agency",
        "distribution": {"profile_root": "profiles"},
        "profiles": [{"name": n, "display_name": d, "category": c}
                     for n, d, c in entries],
    }), encoding="utf-8")
    for name, _, _ in entries:
        if name == "agency-ghost":
            continue                      # manifest names it, directory absent
        pdir = root / "hermes-agency" / "profiles" / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "distribution.yaml").write_text(yaml.safe_dump({
            "name": name, "version": "0.1.0",
            "description": f"{name} does things.",
            "author": "Kyle French", "license": "AGPL-3.0-only",
        }), encoding="utf-8")
    if recipes:
        (root / "recipes.json").write_text(json.dumps({
            "schema_version": 1,
            "tiers": ["minimal", "recommended", "expanded"],
            "recipes": [
                {"id": "software-delivery", "display_name": "Software Delivery Team",
                 "pack": "agency", "description": "ship software",
                 "keywords": ["software", "ship"],
                 "tiers": {"minimal": ["agency-backend-engineer"],
                           "recommended": ["agency-backend-engineer",
                                           "agency-code-reviewer"],
                           "expanded": ["agency-backend-engineer"]}},
                {"id": "security-review", "display_name": "Security Review",
                 "pack": "agency", "description": "review", "keywords": ["security"],
                 "tiers": {"minimal": ["agency-code-reviewer"]}},
            ],
        }), encoding="utf-8")
    return root


@pytest.fixture
def pack_root(hermes_root, tmp_path):
    root = _make_pack_root(tmp_path / "packs")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": [str(root)]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    return root


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


# ── shape / resolution order ─────────────────────────────────────────────────

def test_sources_resolution(pack_root):
    srcs = [s for s in catalog.sources() if not s["local"]]
    assert len(srcs) == 1
    assert srcs[0]["resolved"] is True
    assert srcs[0]["packs"] == 3            # ghost dropped


def test_entry_shape_and_distribution_merge(pack_root):
    e = catalog.get("agency-backend-engineer")
    d = e.to_dict()
    for key in ("id", "pack", "display_name", "category", "description",
                "version", "author", "license", "source_path",
                "installed_as", "suggested_role"):
        assert key in d, key
    assert d["display_name"] == "Backend Engineer"       # from manifest
    assert d["description"] == "agency-backend-engineer does things."  # from dist yaml
    assert d["version"] == "0.1.0"
    assert d["author"] == "Kyle French"
    assert d["license"] == "AGPL-3.0-only"
    assert d["source_path"].endswith("hermes-agency/profiles/agency-backend-engineer")
    assert d["pack"] == "hermes-agency"


def test_manifest_entry_absent_dir_dropped_with_info(pack_root):
    ids = {e["id"] for e in catalog.entries()}
    assert "agency-ghost" not in ids
    cat = catalog.load()
    assert any(code == "catalog.source_missing" and "agency-ghost" in msg
               for code, msg in cat.infos)


# ── role hints ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entry_id,cat,expected", [
    ("agency-qa-tester", "qa", "verifier"),               # category
    ("agency-code-reviewer", "engineering", "reviewer"),  # name glob wins over category
    ("agency-backend-engineer", "engineering", "implementer"),
    ("agency-orchestrator", "engineering", "orchestrator"),
    ("agency-technical-lead", "lead", "orchestrator"),
    ("agency-ux-researcher", "design", "researcher"),
    ("agency-data-analyst", "data", "researcher"),
    ("agency-copywriter", "marketing", "member"),         # "*writer*" is a glob…
])
def test_suggested_role_builtin(pack_root, entry_id, cat, expected):
    assert catalog.suggested_role(entry_id, cat) == expected


def test_suggested_role_settings_override(hermes_root, tmp_path):
    root = _make_pack_root(tmp_path / "packs")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia": {"settings": {
            "catalog_paths": [str(root)],
            "role_hints": {"*backend*": "lead", "qa": "gatekeeper"}}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    assert catalog.suggested_role("agency-backend-engineer", "engineering") == "lead"
    assert catalog.suggested_role("agency-qa-tester", "qa") == "gatekeeper"


# ── degradation: never raises ────────────────────────────────────────────────

def test_missing_root_degrades_to_empty(hermes_root, profile):
    """An unreachable root degrades to an empty catalog, not to the profiles
    root: there is no tier C, so a live profile is never an entry."""
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": ["/nonexistent/packs"]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    profile("qa")
    srcs = [s for s in catalog.sources() if not s["local"]]
    assert srcs[0]["resolved"] is False
    cat = catalog.load()
    assert any(code == "catalog.source_missing" for code, _ in cat.infos)
    assert catalog.entries() == []


def test_malformed_packs_json_drops_only_that_source(hermes_root, tmp_path):
    good = _make_pack_root(tmp_path / "packs")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "packs.json").write_text("::: not json :::", encoding="utf-8")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": [str(bad), str(good)]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    srcs = [s for s in catalog.sources() if not s["local"]]
    assert srcs[0]["resolved"] is False
    assert srcs[1]["resolved"] is True
    assert len(catalog.entries()) == 3


def test_get_unknown_raises_near_matches(pack_root):
    with pytest.raises(TeamError) as ei:
        catalog.get("agency-backend-enginer")           # one letter off
    assert ei.value.code == "E_TEAM_NO_BLUEPRINT"
    assert "agency-backend-engineer" in ei.value.hint


# ── root profiles are not catalog entries (no tier C) ────────────────────────

def test_root_profile_absent_from_entries_present_in_root_profiles(pack_root, profile):
    profile("solo-helper")
    assert "solo-helper" not in {e["id"] for e in catalog.entries()}
    rows = catalog.root_profiles()
    assert [r["id"] for r in rows] == ["solo-helper"]
    assert rows[0]["adopted_as"] == ""
    assert rows[0]["has_soul"] is False


def test_root_profiles_skip_tombstoned(pack_root, profile):
    home = profile("ghost-helper")
    hermes_constants.mark_named_profile_deleted(home)
    assert catalog.root_profiles() == []


def test_deleted_dir_alone_is_not_a_profile(pack_root, hermes_root):
    (hermes_root / "profiles" / ".deleted").mkdir(parents=True, exist_ok=True)
    assert catalog.root_profiles() == []
    assert catalog.load().entries                      # load() still works
    assert ".deleted" not in {e["id"] for e in catalog.entries()}


def test_installed_index_ignores_tombstoned_home(pack_root, profile, hermes_root):
    """A husk left behind when ``rmtree`` lost a race with the gateway keeps its
    ``distribution.yaml``; the tombstone is what makes it stop counting."""
    home = profile("demo-backend-engineer")
    e = catalog.get("agency-backend-engineer")
    (home / "distribution.yaml").write_text(
        yaml.safe_dump({"name": "demo-backend-engineer", "source": e.source_path}),
        encoding="utf-8")
    assert catalog.get("agency-backend-engineer").installed_as \
        == ("demo-backend-engineer",)
    hermes_constants.mark_named_profile_deleted(home)
    assert catalog.get("agency-backend-engineer").installed_as == ()


# ── installed_as ─────────────────────────────────────────────────────────────

def test_installed_as_from_provenance(pack_root, profile, hermes_root):
    profile("demo-backend-engineer")
    e = catalog.get("agency-backend-engineer")
    (hermes_root / "profiles" / "demo-backend-engineer" / "distribution.yaml")\
        .write_text(yaml.safe_dump({"name": "demo-backend-engineer",
                                    "source": e.source_path}), encoding="utf-8")
    assert catalog.get("agency-backend-engineer").installed_as == ("demo-backend-engineer",)


def test_installed_not_stale_after_removal(pack_root, profile, hermes_root):
    profile("demo-backend-engineer")
    e = catalog.get("agency-backend-engineer")
    (hermes_root / "profiles" / "demo-backend-engineer" / "distribution.yaml")\
        .write_text(yaml.safe_dump({"source": e.source_path}), encoding="utf-8")
    assert catalog.get("agency-backend-engineer").installed_as
    import shutil
    shutil.rmtree(hermes_root / "profiles" / "demo-backend-engineer")
    assert catalog.get("agency-backend-engineer").installed_as == ()


# ── memo ─────────────────────────────────────────────────────────────────────

def test_memo_hits_until_manifest_changes(pack_root):
    catalog._reset_caches_for_tests()
    base = catalog._BUILD_COUNT
    catalog.load()
    catalog.load()
    assert catalog._BUILD_COUNT - base == 1
    mpath = pack_root / "hermes-agency" / "agency.json"
    mpath.write_text(mpath.read_text() + " ", encoding="utf-8")   # size changes
    catalog.load()
    assert catalog._BUILD_COUNT - base == 2


# ── recipes ──────────────────────────────────────────────────────────────────

def test_recipes_shape(pack_root):
    rs = catalog.recipes()
    assert {r["id"] for r in rs} == {"software-delivery", "security-review"}
    sd = next(r for r in rs if r["id"] == "software-delivery")
    assert set(sd["tiers"]) == {"minimal", "recommended", "expanded"}
    assert sd["keywords"] == ["software", "ship"]


# ── CLI (§8.2 on stdout only) ────────────────────────────────────────────────

def test_cli_profiles_list_json(pack_root, capsys):
    code, env, _ = run(_parser(), "profiles", "list", "--json", capsys=capsys)
    assert code == 0
    assert env["ok"] is True
    assert {e["id"] for e in env["data"]["entries"]} == {
        "agency-backend-engineer", "agency-code-reviewer", "agency-qa-tester"}
    assert "_human" not in env["data"]


def test_cli_profiles_list_filters(pack_root, capsys):
    code, env, _ = run(_parser(), "profiles", "list", "--pack", "hermes-agency",
                       "--category", "engineering", "--json", capsys=capsys)
    assert {e["id"] for e in env["data"]["entries"]} == {
        "agency-backend-engineer", "agency-code-reviewer"}
    code, env, _ = run(_parser(), "profiles", "list", "--role", "reviewer",
                       "--json", capsys=capsys)
    assert [e["id"] for e in env["data"]["entries"]] == ["agency-code-reviewer"]
    code, env, _ = run(_parser(), "profiles", "list", "--search", "qa",
                       "--json", capsys=capsys)
    assert [e["id"] for e in env["data"]["entries"]] == ["agency-qa-tester"]


def test_cli_profiles_show_and_sources(pack_root, capsys):
    code, env, _ = run(_parser(), "profiles", "show",
                       "agency-backend-engineer", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["entry"]["display_name"] == "Backend Engineer"
    code, env, _ = run(_parser(), "profiles", "sources", "--json", capsys=capsys)
    assert code == 0 and env["data"]["sources"][0]["resolved"] is True


def test_cli_profiles_show_missing_exit1(pack_root, capsys):
    code, env, _ = run(_parser(), "profiles", "show", "nope-nothing", "--json",
                       capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NO_BLUEPRINT"


def test_cli_recipes_list(pack_root, capsys):
    code, env, _ = run(_parser(), "recipes", "list", "--json", capsys=capsys)
    assert code == 0
    assert {r["id"] for r in env["data"]["recipes"]} == {
        "software-delivery", "security-review"}


def test_cli_human_mode_writes_stderr_only(pack_root, capsys):
    args = _parser().parse_args(["profiles", "list"])
    code = cli.handle(args)
    out = capsys.readouterr()
    assert code == 0
    assert out.out == ""
    assert "3 entries" in out.err


# ── registry commands survive a dead catalog ─────────────────────────────────

def test_registry_works_with_unresolvable_catalog(hermes_root):
    import model
    storage.upsert(model.Team.from_dict({
        "slug": "t", "board": "t", "workdir": "/w/t", "orchestrator": "dev",
        "members": [{"profile": "dev"}]}))
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"entries": {"hermes-agent-synergia":
                      {"settings": {"catalog_paths": ["/nowhere"]}}}}}),
        encoding="utf-8")
    catalog._reset_caches_for_tests()
    p = argparse.ArgumentParser(prog="team")
    cli.setup_parser(p)
    code = cli.handle(p.parse_args(["show", "t", "--json"]))
    assert code == 0


# ── layering (S21 preview): catalog.py imports no hermes_cli ────────────────

def test_catalog_imports_no_hermes_cli():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "src" / "catalog.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        assert not (s.startswith("import hermes_cli")
                    or s.startswith("from hermes_cli")), line
