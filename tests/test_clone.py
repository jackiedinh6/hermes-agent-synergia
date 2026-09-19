"""S17 — ``team clone`` / ``export --relative`` / ``import`` (§5.7, §8.1).

The copy/refuse table is the spec (§8.1): the roster (members, roles,
capacity, blueprint provenance) and the switches (policy / workspace_policy /
sync / metadata) are copied; every BINDING is re-declared — board, workdir,
tenant, project — and ``<src>`` is left byte-unchanged.
Every copied member lands ``primary: false`` — ``model.derive_clone`` line
``d["primary"] = False   # recomputed; false wherever primary elsewhere (I2)``
— and ``managed: false`` (I7). ``--instantiate`` is the one exception to
both flags: it re-runs S6b provisioning under the CLONE's naming template,
so the clone owns fresh ``<new>-*`` profiles.

Live-stack verification (implement.md S17, G3b) is the Verifier's job; every
live assertion is replicated in-process here. Two textual divergences, both
substance-preserving, are flagged at their tests:

* the live check reads ``show py --json`` fields off ``data`` directly; the
  CLI envelope nests the record under ``data["team"]`` — same fields.
* ``sync <new> --json`` → ``drift == []`` cannot hold RAW for a shared-roster
  clone: ``reconcile.diff`` emits an unconditional INFO ``member.terminal_cwd``
  skip note per non-primary member (reconcile.py:257-271), plus INFO
  ``member.capacity`` and INFO ``workdir.agents_md`` (§9.2 opt-in).
  Those INFO items ARE the zero-drift contract for shared members — the
  tests therefore assert the substance: nothing error/warn, and everything
  remaining is an INFO skip annotation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

import pytest
import yaml

import cli
import storage
from model import Team
from test_catalog import _make_pack_root


# ── harness (test_cli_sync / test_cli_registry house style) ──────────────────

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
    """Writable workdir paths — outside the Hermes root (guard 5)."""
    def _w(name):
        return str(hermes_root.parent / ("work-" + name))
    return _w


def run(parser, *argv, capsys):
    """Parse + dispatch; returns (exit_code, envelope|None, stderr)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)   # must parse as exactly one object
    err = out.err
    if code and envelope and envelope.get("error"):
        e = envelope["error"]           # surface JSON-mode failures in `err`
        err += f"\n{e.get('code')}: {e.get('message')}"
    return code, envelope, err


def _seed_team(slug, *, members, **kw):
    """Registry seed through the single writer (storage.upsert) — for the
    tests that don't exercise the create pipeline itself."""
    d = {"slug": slug, "display_name": slug, "board": slug,
         "workdir": "/tmp/none", "tenant": slug, "members": members}
    d.update(kw)
    d.setdefault("orchestrator", members[0]["profile"])
    return storage.upsert(Team.from_dict(d))


_SEED = """# hermes profile config
terminal:
  cwd: '.'
toolsets: []
plugins:
  enabled: []
"""


@pytest.fixture
def pack(hermes_root, tmp_path):
    """Synthetic pack root (test_catalog layout) + plugin install dir + a root
    config that ENABLES the plugin — the test_provision.py shape, so
    instantiate works and catalog/policy checks stay silent."""
    root = _make_pack_root(tmp_path / "packs")
    (hermes_root / "plugins" / "hermes-agent-synergia").mkdir(parents=True, exist_ok=True)
    (hermes_root / "plugins" / "hermes-agent-synergia" / "plugin.yaml").write_text(
        "name: hermes-agent-synergia\n", encoding="utf-8")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["hermes-agent-synergia"],
                     "entries": {"hermes-agent-synergia":
                                 {"settings": {"catalog_paths": [str(root)]}}}}},
        sort_keys=False), encoding="utf-8")
    import catalog
    catalog._reset_caches_for_tests()
    return root


@pytest.fixture
def px_world(profile, W):
    """Team ``px`` (board project-x, workdir W(px), tenant px) on two seeded
    profiles — the shape of the live team the S17 live check clones."""
    profile("qa")
    profile("be")
    _seed_team("px", board="project-x", workdir=W("px"), tenant="px",
               members=[{"profile": "qa", "role": "orchestrator",
                         "primary": True},
                        {"profile": "be", "role": "implementer",
                         "primary": True, "capacity": 2}],
               policy={"mode": "enforce"}, workspace_policy="worktree")
    return "px"


def _err_code(human_err: str) -> str:
    """Error code from the human (stderr) rendering ``CODE: message``."""
    return human_err.split("\n", 1)[0].split(":", 1)[0].strip()


def _actionable(drift: list) -> list:
    """The substance of the live check's ``drift==[]`` (test_cli_sync's
    _real_drift, same idea): everything that is not an INFO skip note."""
    return [d for d in drift if d["severity"] != "info"]


# ── clone: bindings (live check, in-process) ─────────────────────────────────

def test_clone_bindings_replicate_live_check(px_world, parser, W, capsys):
    code, env, err = run(
        parser, "clone", "px", "py",
        "--board", "project-y", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]   # live check reads data directly; envelope nests
    assert t["board"] == "project-y" and t["tenant"] == "py"
    assert t["project"]["mode"] == "none"
    assert all(not m["primary"] for m in t["members"]), t["members"]
    assert env["data"]["cloned_from"] == "px"
    # roster copied, bindings never: names/roles/capacity preserved;
    # managed forced false (I7) — these profiles are shared, not owned
    assert {m["profile"] for m in t["members"]} == {"qa", "be"}
    be = next(m for m in t["members"] if m["profile"] == "be")
    assert be["capacity"] == 2 and be["role"] == "implementer"
    assert all(m["managed"] is False for m in t["members"])
    assert t["orchestrator"] == "qa"           # copied unless --orchestrator
    assert t["shared_board"] is False
    # policy.* / workspace_policy are switches, not bindings — copied
    assert t["policy"]["mode"] == "enforce"
    assert t["workspace_policy"] == "worktree"
    assert t["display_name"] == "py"           # slug substituted (§8.1)
    assert t["revision"] == 1                  # fresh record (§8.1 row 9)
    # live check row 7: px keeps its revision (identical show before/after)
    code, env2, _ = run(parser, "show", "px", "--json", capsys=capsys)
    assert code == 0 and env2["data"]["team"]["revision"] == 1
    assert env2["data"]["team"]["board"] == "project-x"


def test_clone_board_and_tenant_default_to_new_slug(px_world, parser, W,
                                                    capsys):
    code, env, err = run(
        parser, "clone", "px", "py", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    assert t["board"] == "py" and t["tenant"] == "py"   # live check: tenant py
    assert t["project"]["mode"] == "none"


def test_clone_tenant_and_project_overrides(px_world, parser, W, capsys):
    code, env, err = run(
        parser, "clone", "px", "py", "--tenant", "team-pee",
        "--project", "second", "--board", "project-y", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    assert t["tenant"] == "team-pee"
    assert t["project"] == {"mode": "adopt", "slug": "second"}


def test_clone_requires_workdir(px_world, parser, capsys):
    code, _env, err = run(parser, "clone", "px", "py", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_INVALID"
    assert "never copied" in err


def test_clone_missing_src_is_not_found(parser, W, capsys):
    code, _env, err = run(parser, "clone", "ghost", "py",
                          "--workdir", W("py"), capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_NOT_FOUND"


def test_clone_onto_existing_slug_refused(px_world, parser, W, capsys):
    _seed_team("py", board="board-py", workdir=W("py"),
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, _env, err = run(
        parser, "clone", "px", "py", "--workdir", W("py2"),
        "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_EXISTS"   # create is not an editor


# ── clone: the I-rows the G3b gate names ─────────────────────────────────────

def test_clone_board_taken_refused_registry_untouched(px_world, parser, W,
                                                      capsys):
    _seed_team("other", board="project-y", workdir=W("other"),
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, _env, err = run(
        parser, "clone", "px", "py", "--board", "project-y", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_BOARD_TAKEN"          # I1
    assert set(storage.load().teams) == {"px", "other"}    # registry untouched


def test_clone_shared_board_allowed(px_world, parser, W, capsys):
    _seed_team("other", board="project-y", workdir=W("other"),
               shared_board=True,
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, env, err = run(
        parser, "clone", "px", "py", "--board", "project-y", "--create-board",
        "--allow-shared-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    assert env["data"]["team"]["shared_board"] is True


def test_clone_capacity_conflict_I6(px_world, parser, W, capsys):
    # px declares capacity 2 for be; team cy declares 3 → the clone of that
    # roster cannot agree (I6).
    _seed_team("cy", board="board-cy", workdir=W("cy"),
               members=[{"profile": "be", "role": "implementer",
                         "primary": False, "capacity": 3}])
    code, _env, err = run(
        parser, "clone", "px", "py", "--workdir", W("py"),
        "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_CAPACITY_CONFLICT"
    assert "cy" in err


def test_clone_drop_member_rechecks_I3(px_world, parser, W, capsys):
    # dropping the orchestrator without naming a new one → I3 refusal...
    code, _env, err = run(
        parser, "clone", "px", "py", "--drop-member", "qa",
        "--workdir", W("py"), "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_INVALID"
    assert "orchestrator" in err
    # ...and lands once the new orchestrator is named (§8.1 orchestrator row)
    code, env, err = run(
        parser, "clone", "px", "py", "--drop-member", "qa",
        "--orchestrator", "be", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    assert {m["profile"] for m in t["members"]} == {"be"}
    assert t["orchestrator"] == "be"


# ── clone: managed/primary discipline (§8.1 rows 1-2) ────────────────────────

def test_clone_shares_profiles_forces_managed_false(pack, parser, W, capsys):
    code, env, err = run(
        parser, "create", "px",
        "--from", "agency-backend-engineer:orchestrator",
        "--board", "board-b1", "--create-board",
        "--workdir", W("px"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    src = env["data"]["team"]["members"][0]
    assert src["managed"] is True and src["primary"] is True   # create stamps
    code, env, err = run(
        parser, "clone", "px", "py", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    m = env["data"]["team"]["members"][0]
    # shared profile, verbatim name, managed forced false (I7), primary
    # recomputed false (I2), blueprint provenance copied (§8.1 row 1)
    assert m["profile"] == src["profile"] == "px-backend-engineer"
    assert m["managed"] is False and m["primary"] is False
    assert m["blueprint"] == "agency-backend-engineer"
    assert m["blueprint_source"] == src["blueprint_source"]
    assert env["data"]["instantiated"] == []   # no --instantiate: no instances
    # <src> still owns its instance
    code, env2, _ = run(parser, "show", "px", "--json", capsys=capsys)
    sm = env2["data"]["team"]["members"][0]
    assert sm["managed"] is True and sm["primary"] is True


def test_clone_instantiate_gives_the_clone_its_own_profiles(pack, parser, W,
                                                            hermes_root,
                                                            capsys):
    code, env, err = run(
        parser, "create", "px",
        "--from", "agency-backend-engineer:orchestrator",
        "--board", "board-b2", "--create-board",
        "--workdir", W("px"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    src_name = env["data"]["team"]["members"][0]["profile"]
    code, env, err = run(
        parser, "clone", "px", "py", "--instantiate",
        "--board", "board-b3", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    m = t["members"][0]
    # the clone's OWN profile, from the clone's own naming template (§5.7):
    assert m["profile"] == "py-backend-engineer" != src_name
    assert m["managed"] is True and m["primary"] is True   # the clone created it
    assert t["orchestrator"] == "py-backend-engineer"      # rename mapped
    assert env["data"]["instantiated"] == ["py-backend-engineer"]
    assert (hermes_root / "profiles" / "py-backend-engineer"
            / "config.yaml").is_file()
    # <src> still points at its own instance, managed as before
    code, env2, _ = run(parser, "show", "px", "--json", capsys=capsys)
    assert env2["data"]["team"]["members"][0]["profile"] == src_name
    assert env2["data"]["team"]["members"][0]["managed"] is True


def test_clone_instantiate_refuses_member_without_blueprint(px_world, parser,
                                                            W, capsys):
    code, _env, err = run(
        parser, "clone", "px", "py", "--instantiate",
        "--workdir", W("py"), "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_NO_BLUEPRINT"
    assert set(storage.load().teams) == {"px"}             # refused pre-write


# ── clone: <src> byte-unchanged (live md5 check) ─────────────────────────────

def _tree_snapshot(root):
    """relpath → (mtime_ns, sha256) for every file under *root*."""
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                data = fh.read()
            st = os.stat(p)
            out[os.path.relpath(p, str(root))] = (
                st.st_mtime_ns, hashlib.sha256(data).hexdigest())
    return out


def _raw_team_doc(hermes_root, slug):
    """The *slug* sub-document of teams.yaml, as canonical YAML text."""
    reg = yaml.safe_load(
        (hermes_root / "plugin-data" / "hermes-agent-synergia" / "teams.yaml")
        .read_text(encoding="utf-8"))
    t = reg["teams"].get(slug)
    return None if t is None else yaml.safe_dump(t, sort_keys=True)


@pytest.fixture
def settled_px(hermes_root, pack, profile, parser, W, capsys):
    """Clean world: px created through the CLI and ``sync --apply``-settled
    (board, workdir, profiles, orchestrator config all reconciled) — the
    precondition the live stack's px is in when the clone runs. Returns the
    hermes root."""
    for name in ("qa", "be"):
        profile(name, _SEED)
    code, env, err = run(
        parser, "create", "px",
        "--member", "qa:orchestrator", "--member", "be:implementer:2",
        "--board", "project-x", "--create-board",
        "--workdir", W("px"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "sync", "px", "--apply", "--json",
                         capsys=capsys)
    assert code == 0, err
    assert _actionable(env["data"]["drift"]) == [], env["data"]["drift"]
    return hermes_root


def test_clone_leaves_src_byte_unchanged(settled_px, parser, W, capsys):
    hermes_root = settled_px
    before = {
        "teams_doc": _raw_team_doc(hermes_root, "px"),
        "profiles": _tree_snapshot(hermes_root / "profiles"),
        "workdir": _tree_snapshot(hermes_root.parent / "work-px"),
    }
    revision_before = storage.load().teams["px"].revision

    code, env, err = run(          # live flags: reconcile RUNS on the clone
        parser, "clone", "px", "py",
        "--board", "project-y", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--json", capsys=capsys)
    assert code == 0, err

    assert _raw_team_doc(hermes_root, "px") == before["teams_doc"]  # byte-ident
    assert _tree_snapshot(hermes_root / "profiles") == before["profiles"]
    assert _tree_snapshot(hermes_root.parent / "work-px") == before["workdir"]
    assert storage.load().teams["px"].revision == revision_before
    # ...and the clone itself did land
    assert set(storage.load().teams) == {"px", "py"}


def test_sync_clone_zero_actionable_drift(settled_px, parser, W, capsys):
    """Replicates the live check ``team sync py`` → ``drift == []``.

    A shared-roster clone can never show a RAW-empty drift list: each
    non-primary member carries the INFO ``member.terminal_cwd`` skip note
    and a capacity-declaring non-primary member the INFO
    ``member.capacity`` note — reconcile.py:257-287 emits them
    unconditionally, and ``sync --json`` carries DriftItems verbatim. The
    live stack's own drift list carries them too; what the check means is
    that NOTHING error/warn remains. Asserted here, plus that the info set
    is only ever the three known skip annotations."""
    code, env, err = run(
        parser, "clone", "px", "py",
        "--board", "project-y", "--create-board",
        "--workdir", W("py"), "--create-workdir",
        "--json", capsys=capsys)
    assert code == 0, err
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "sync", "py", "--json", capsys=capsys)
    assert code == 0
    drift = env["data"]["drift"]
    assert _actionable(drift) == []
    assert {d["check"] for d in drift} <= {
        "member.terminal_cwd", "member.capacity", "workdir.agents_md"}
    assert any(d["check"] == "member.terminal_cwd" for d in drift)


# ── export / --relative ───────────────────────────────────────────────────────

def _exportable_px(W):
    _seed_team("px", board="project-x", workdir=W("px"),
               members=[{"profile": "qa", "role": "orchestrator",
                         "primary": True}],
               metadata={"notes_dir": W("px") + "/notes", "home": "/opt/other"})


def test_export_to_stdout_is_the_yaml_document(parser, W, capsys):
    _exportable_px(W)
    args = parser.parse_args(["export", "px"])
    assert cli.handle(args) == 0
    out = capsys.readouterr()                  # read exactly once
    assert out.err == ""                       # the document is the output
    doc = yaml.safe_load(out.out)
    assert doc["slug"] == "px" and doc["board"] == "project-x"
    assert doc["members"][0]["profile"] == "qa"
    assert doc["workdir"] == W("px")           # absolute without --relative


def test_export_to_file_writes_and_notes(parser, W, tmp_path, capsys):
    _exportable_px(W)
    f = str(tmp_path / "px.yaml")
    code, _env, err = run(parser, "export", "px", "-o", f, capsys=capsys)
    assert code == 0 and "exported to" in err
    doc = yaml.safe_load(open(f, encoding="utf-8").read())
    assert doc["workdir"] == W("px")


def test_export_relative_rewrites_only_workdir_rooted_paths(parser, W,
                                                            tmp_path, capsys):
    _exportable_px(W)
    f = str(tmp_path / "px.yaml")
    assert run(parser, "export", "px", "-o", f, "--relative",
               capsys=capsys)[0] == 0
    doc = yaml.safe_load(open(f, encoding="utf-8").read())
    assert doc["workdir"] == "."                       # the root itself
    assert doc["metadata"]["notes_dir"] == "./notes"   # under the root
    assert doc["metadata"]["home"] == "/opt/other"     # elsewhere: untouched


def test_export_unknown_team(parser, capsys):
    code, _env, err = run(parser, "export", "ghost", capsys=capsys)
    assert code == 1 and "E_TEAM_NOT_FOUND" in err


# ── import: the board-claim gap, closed ──────────────────────────────────────

@pytest.fixture
def exported_px(parser, W, tmp_path, capsys):
    """px (orchestrator qa on board project-x) exported to a file, then
    deleted — a fresh host where the file is all the registry has of px.
    Deliberately does NOT seed the qa profile: adoption tests opt in."""
    _exportable_px(W)
    f = str(tmp_path / "px.yaml")
    assert run(parser, "export", "px", "-o", f, "--relative",
               capsys=capsys)[0] == 0
    storage.delete("px")
    return f


def test_import_roundtrip_bindings_from_flags_not_file(profile, parser, W,
                                                       exported_px, capsys):
    profile("qa")                               # adopted member must exist
    code, env, err = run(
        parser, "import", exported_px, "--rename", "py",
        "--board", "fresh-board", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    assert t["slug"] == "py" and t["board"] == "fresh-board"
    assert t["tenant"] == "py"                  # default: <new> (not the file's)
    assert t["project"]["mode"] == "none"       # bindings are never imported
    m = t["members"][0]
    assert (m["profile"] == "qa" and m["primary"] is False
            and m["managed"] is False)
    assert env["data"]["imported_from"] == exported_px


def test_import_proposal_board_accepted_when_free(profile, parser, W,
                                                  exported_px, capsys):
    """No --board: the file's board is a PROPOSAL — accepted when free (§8.1)."""
    profile("qa")
    code, env, err = run(
        parser, "import", exported_px, "--rename", "py", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    assert env["data"]["team"]["board"] == "project-x"   # the proposal, honored


def test_import_neither_board_nor_freeing_rename_refused(profile, parser, W,
                                                        exported_px, capsys):
    """The exported board is a proposal, not a silent
    binding. A team already claims it, nothing frees it → E_TEAM_BOARD_TAKEN
    naming the file, registry untouched — never two teams on one board."""
    profile("qa")
    _seed_team("other", board="project-x", workdir=W("other"),
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, _env, err = run(
        parser, "import", exported_px, "--rename", "py",
        "--workdir", W("py"), "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_BOARD_TAKEN"
    assert "hint:" in err and "remedy:" in err    # §7.3 envelope discipline
    assert "project-x" in err and exported_px in err
    assert set(storage.load().teams) == {"other"}         # nothing landed


def test_import_board_flag_bypasses_proposal_gate(profile, parser, W,
                                                 exported_px, capsys):
    profile("qa")
    _seed_team("other", board="project-x", workdir=W("other"),
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, env, err = run(
        parser, "import", exported_px, "--rename", "py",
        "--board", "other-board", "--create-board",
        "--tenant", "imp-tenant",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    t = env["data"]["team"]
    assert t["board"] == "other-board" and t["tenant"] == "imp-tenant"


def test_import_allow_shared_board_with_shared_owner(profile, parser, W,
                                                     exported_px, capsys):
    profile("qa")
    _seed_team("other", board="project-x", workdir=W("other"),
               shared_board=True,
               members=[{"profile": "ghost1", "role": "orchestrator"}])
    code, env, err = run(
        parser, "import", exported_px, "--allow-shared-board", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", "--json",
        capsys=capsys)
    assert code == 0, err
    assert env["data"]["team"]["board"] == "project-x"
    assert env["data"]["team"]["shared_board"] is True


def test_import_requires_workdir(parser, exported_px, capsys):
    code, _env, err = run(parser, "import", exported_px, capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_INVALID"
    assert "never copied" in err


def test_import_rejects_non_team_document(tmp_path, parser, W, capsys):
    f = tmp_path / "junk.yaml"
    f.write_text("hello: world\n", encoding="utf-8")
    code, _env, err = run(parser, "import", str(f), "--workdir", W("j"),
                          capsys=capsys)
    assert code == 1 and _err_code(err) == "E_TEAM_INVALID"


def test_import_adopted_member_without_profile_refused(parser, W, exported_px,
                                                       capsys):
    """Roster profiles are never imported: an adoption needs a real profile
    (import has no --create-missing)."""
    code, _env, err = run(
        parser, "import", exported_px, "--rename", "py",
        "--board", "b9", "--create-board",
        "--workdir", W("py"), "--create-workdir", "--no-sync", capsys=capsys)
    assert code == 1
    assert _err_code(err) == "E_TEAM_NO_PROFILE"
    assert set(storage.load().teams) == set()             # nothing landed
