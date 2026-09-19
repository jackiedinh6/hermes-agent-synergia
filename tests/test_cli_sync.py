"""S11 — ``team sync`` / ``set-board`` / ``set-workdir`` (spec §9, §8.2).

The reconciler is reachable through the CLI, and the DRY RUN IS THE DEFAULT:

* a bare ``sync`` on a broken team is a pure report — the whole tree is
  snapshotted (path → mtime_ns + sha256) before and after and must match;
* ``--apply`` then ``--apply`` again is byte-identical (idempotence witness
  from S10, now asserted through the CLI);
* ``--json`` prints exactly one ``{"ok": true, "data": {…}}`` object on
  stdout with the DriftItems verbatim (§7.3); every human line — the
  ``severity check subject observed → desired remedy`` table included —
  goes to stderr;
* exit codes (§8.2): drift alone is a report → 0; ``--apply`` that leaves
  unfixable drift → 4; unknown team → 1; registry lock → 3;
* ``set-board`` / ``set-workdir`` mutate the record through ``storage.upsert``
  (revision bump asserted) and re-run the reconcile report path;
  ``set-workdir`` refuses sentinels and relative paths via the same
  ``model.validate_team`` the create path honors;
* ``create`` without ``--no-sync`` reconciles for real — the always-False
  ``_stub_reconcile`` is gone (payload key is ``reconciled``, a report).

Profiles are seeded with the S10 block-style ``_SEED`` document: the flow
text ``"{}"`` is refused by the line editor (it cannot re-parse a
foreign document shape), and these tests assert WRITES, so they need a
document the writer accepts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil

import pytest
import yaml

import cli
import storage
from test_reconcile_apply import _SEED


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
    """Parse + dispatch; returns (exit_code, stdout_json_or_None, stderr)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)   # must parse as exactly one object
    return code, envelope, out.err


def _tree(root) -> dict:
    """Every file under *root*: relpath → (mtime_ns, sha256) — write witness."""
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                data = fh.read()
            out[os.path.relpath(p, str(root))] = (
                os.stat(p).st_mtime_ns, hashlib.sha256(data).hexdigest())
    return out


# ── the broken team, built through the CLI itself ────────────────────────────

def _install_plugin(hermes_root):
    """Minimal plugin install so the F1 symlink has a real target — and the
    root config enables the plugin, else ``diff`` carries the manual
    ``policy.hook`` warn (§9.2: "a policy tier the host never loads is
    decoration") which no ``--apply`` may fix and which would turn every
    apply test into exit 4."""
    d = hermes_root / "plugins" / "hermes-agent-synergia"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text("name: hermes-agent-synergia\n", encoding="utf-8")
    (hermes_root / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-agent-synergia\n", encoding="utf-8")
    return d


def _real_drift(env_data) -> list:
    """Non-info drift items (info lines are notes, not findings)."""
    return [d for d in env_data["drift"] if d["severity"] != "info"]


def _mk_team(hermes_root, profile, parser, W, capsys, slug="teamx",
             members=("qa", "pm"), extra=()):
    """Register *slug* (qa = adopted orchestrator, pm = adopted PRIMARY via
    add-member) through the CLI with ``--no-sync`` — the team is DRIFTED by
    construction (sentinel seed doc, no F1 link, no description)."""
    _install_plugin(hermes_root)
    for name in members:
        profile(name, _SEED)
    code, env, err = run(
        parser, "create", slug,
        "--member", f"{members[0]}:orchestrator",
        "--board", slug, "--create-board",
        "--workdir", W(slug), "--create-workdir",
        "--no-sync", *extra, "--json", capsys=capsys)
    assert code == 0, err
    if len(members) > 1:
        for m in members[1:]:
            code, env, err = run(parser, "add-member", slug, m,
                                 "--role", "reviewer", "--primary",
                                 "--json", capsys=capsys)
            assert code == 0, err
    return env["data"]["team"]


# ── bullet 1: the dry run is the default and writes NOTHING ─────────────────

def test_sync_dry_run_writes_nothing(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    before = _tree(hermes_root)
    code, env, err = run(parser, "sync", "teamx", capsys=capsys)
    assert code == 0                       # §8.2: drift alone is a REPORT
    assert _tree(hermes_root) == before    # not one byte moved
    # and the drift was REPORTED — the table is not an empty shrug
    assert "member.plugin_enabled" in err
    assert "member.plugin_link" in err


# ── bullet 1/6: --json is exactly one object on stdout; table → stderr ──────

def test_sync_json_envelope_shape(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "sync", "teamx", "--json", capsys=capsys)
    assert code == 0
    assert env is not None and env["ok"] is True
    data = env["data"]
    assert data["team"] == "teamx"
    assert data["dry_run"] is True
    assert isinstance(data["drift"], list) and data["drift"]
    assert data["applied"] == []
    assert data["restart_notice"] is None
    d = data["drift"][0]
    assert set(d) == {"check", "severity", "subject", "observed", "desired",
                      "auto_fixable", "remedy"}          # §9.1 verbatim
    assert "member." not in err                # json mode: NO human text


def test_sync_human_table_to_stderr(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, _env, err = run(parser, "sync", "teamx", capsys=capsys)
    assert code == 0
    assert "→" in err                        # unicode arrow (CLI is unicode)
    assert "severity" not in err             # aligned rows, no faked header
    assert "--apply" in err                   # remedy column, last in the row
    assert "teamx" in err


# ── bullet 2: --apply fixes; the second apply is byte-identical ─────────────

def test_apply_then_apply_is_byte_identical(hermes_root, profile, parser, W,
                                            capsys):
    wd = W("teamx")
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "sync", "teamx", "--apply", "--json",
                         capsys=capsys)
    assert code == 0
    data = env["data"]
    assert data["dry_run"] is False
    assert _real_drift(data) == []           # everything fixable WAS fixed
    assert data["applied"]                   # and the report says so
    assert data["restart_notice"] and \
        "no gateway restart needed" in data["restart_notice"]
    assert "no gateway restart needed" in err   # bullet 5: verbatim
    qa_cfg = hermes_root / "profiles" / "qa" / "config.yaml"
    with open(qa_cfg, "rb") as fh:
        parsed = yaml.safe_load(fh.read())
    assert "hermes-agent-synergia" in parsed["plugins"]["enabled"]  # actually written
    first = _tree(hermes_root)

    storage._reset_caches_for_tests()
    code, env, err = run(parser, "sync", "teamx", "--apply", "--json",
                         capsys=capsys)
    assert code == 0
    assert env["data"]["applied"] == []      # nothing left to do
    assert env["data"]["restart_notice"] is None   # §9.4: iff a config changed
    assert "profiles updated" not in err
    assert _tree(hermes_root) == first       # byte-identical INCLUDING mtime


# ── bullet 2: exit 4 on unfixable + --apply (and 0 for the same dry) ────────

def test_apply_exit4_on_unfixable_residue(hermes_root, profile, parser, W,
                                          capsys):
    wd = W("teamx")
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    shutil.rmtree(wd)                        # workdir.exists: gated, so unfixable
    code, _env, _err = run(parser, "sync", "teamx", capsys=capsys)
    assert code == 0                         # dry: an error is still a report
    code, env, err = run(parser, "sync", "teamx", "--apply", "--json",
                         capsys=capsys)
    assert code == 4                         # §8.2: unfixable residue
    assert any(d["check"] == "workdir.exists" for d in env["data"]["drift"])


def test_apply_create_missing_lifts_the_gate(hermes_root, profile, parser, W,
                                             capsys):
    wd = W("teamx")
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    shutil.rmtree(wd)
    code, env, _err = run(parser, "sync", "teamx", "--apply",
                          "--create-missing", "--json", capsys=capsys)
    assert code == 0
    assert os.path.isdir(wd)
    assert not any(d["check"] == "workdir.exists" for d in env["data"]["drift"])


# ── R3: the remedy strings print --create-board/--create-workdir; sync obeys ─

def test_sync_accepts_the_printed_remedies_verbatim(hermes_root, profile,
                                                   parser, W, capsys):
    """The §9.2 table's remedy for a missing workdir is
    ``… --apply --create-workdir`` — an operator pasting it must not get
    exit 2. Same for --create-board; --create-missing keeps working."""
    wd = W("teamx")
    team = _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    shutil.rmtree(wd)
    code, env, err = run(parser, "sync", "teamx", capsys=capsys)
    assert code == 0
    assert "--create-workdir" in err          # the remedy the table PRINTS
    code, env, _err = run(parser, "sync", "teamx", "--apply",
                          "--create-workdir", "--json", capsys=capsys)
    assert code == 0                          # …and the parser ACCEPTS
    assert os.path.isdir(wd)
    # the board gate stands alone too
    run(parser, "set-board", "teamx", "board9", "--json", capsys=capsys)
    storage._reset_caches_for_tests()
    code, env, _err = run(parser, "sync", "teamx", "--apply",
                          "--create-board", "--json", capsys=capsys)
    assert code == 0
    assert not any(d["check"] == "board.exists" for d in env["data"]["drift"])


# ── bullet 2/3: exit 1 unknown team; exit 3 locked registry ─────────────────

def test_sync_unknown_team_exit1(hermes_root, parser, capsys):
    code, env, _err = run(parser, "sync", "nope", "--json", capsys=capsys)
    assert code == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"


def test_locked_registry_is_exit3(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    path = storage.registry_path()
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, env, _err = run(parser, "set-board", "teamx", "board9",
                              "--json", capsys=capsys)
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert code == 3                         # E_TEAM_LOCKED is the write path
    assert env["error"]["code"] == "E_TEAM_LOCKED"


# ── bullet 3: --only filters by check name (comma list) ─────────────────────

def test_only_filters_the_run(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, env, _err = run(parser, "sync", "teamx", "--only",
                          "member.plugin_link", "--apply", "--json",
                          capsys=capsys)
    assert code == 0
    assert env["data"]["applied"]            # the link WAS fixed
    assert all(a["check"] == "member.plugin_link"
               for a in env["data"]["applied"])
    qa_cfg = hermes_root / "profiles" / "qa" / "config.yaml"
    with open(qa_cfg, "rb") as fh:
        parsed = yaml.safe_load(fh.read())
    assert parsed["plugins"]["enabled"] == []   # config: NOT touched
    # and the REPORT view filters too
    code, env, _err = run(parser, "sync", "teamx", "--only", "member.toolsets",
                          "--json", capsys=capsys)
    assert all(d["check"] == "member.toolsets" for d in env["data"]["drift"])


def test_only_empty_list_is_invalid(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    code, env, _err = run(parser, "sync", "teamx", "--only", ",,",
                          "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"


# ── bullet 4: set-board / set-workdir upsert + re-reconcile ──────────────────

def test_set_board_upserts_and_resyncs(hermes_root, profile, parser, W,
                                       capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    before = storage.load().teams["teamx"]
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "set-board", "teamx", "board7", "--json",
                         capsys=capsys)
    assert code == 0
    assert env["data"]["team"] == "teamx"
    after = storage.load().teams["teamx"]    # the ONLY writer was used
    assert after.board == "board7"
    assert after.revision == before.revision + 1
    # re-sync: the moved board does not exist → drift is REPORTED
    assert any(d["check"] == "board.exists" for d in env["data"]["drift"])


def test_set_workdir_moves_record(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    before = storage.load().teams["teamx"]
    new = W("elsewhere")
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "set-workdir", "teamx", new, "--json",
                         capsys=capsys)
    assert code == 0
    after = storage.load().teams["teamx"]
    assert after.workdir == new
    assert after.revision == before.revision + 1
    assert any(d["check"] == "workdir.exists"
               for d in env["data"]["drift"])   # re-run: dir absent, reported


@pytest.mark.parametrize("bad", [".", "./", "auto", "cwd", "",
                                 "relative/dir", "~/x/../escape"])
def test_set_workdir_refuses_sentinel_or_relative(hermes_root, profile,
                                                  parser, W, capsys, bad):
    _mk_team(hermes_root, profile, parser, W, capsys)
    before = storage.load().teams["teamx"]
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "set-workdir", "teamx", bad, "--json",
                         capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"
    after = storage.load().teams["teamx"]    # refused BEFORE the write
    assert after.workdir == before.workdir
    assert after.revision == before.revision


def test_set_workdir_apply_moves_every_projection(hermes_root, profile, parser,
                                                  W, capsys):
    from hermes_cli import kanban_db as kb
    _mk_team(hermes_root, profile, parser, W, capsys)
    new = os.path.join(W("teamx"), "app1")      # a folder inside the old root
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "set-workdir", "teamx", new, "--apply",
                         "--json", capsys=capsys)
    assert code == 0, err
    data = env["data"]
    assert data["changed"] is True and data["dry_run"] is False
    assert data["workdir"] == new
    assert os.path.isdir(new)
    assert storage.load().teams["teamx"].workdir == new
    assert kb.read_board_metadata("teamx").get("default_workdir") == new
    with open(hermes_root / "profiles" / "pm" / "config.yaml", "rb") as fh:
        assert yaml.safe_load(fh.read())["terminal"]["cwd"] == new
    assert "no gateway restart needed" in data["restart_notice"]


def test_set_workdir_same_path_is_noop(hermes_root, profile, parser, W,
                                       capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    before = storage.load().teams["teamx"]
    storage._reset_caches_for_tests()
    code, env, err = run(parser, "set-workdir", "teamx", W("teamx"),
                         "--json", capsys=capsys)
    assert code == 0, err
    assert env["data"]["changed"] is False
    assert storage.load().teams["teamx"].revision == before.revision


def test_set_board_bad_slug_exit1(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, env, _err = run(parser, "set-board", "teamx", "Bad Board!",
                          "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"


def test_set_board_unknown_team_exit1(hermes_root, parser, capsys):
    code, env, _err = run(parser, "set-board", "ghost", "b", "--json",
                          capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"


# ── bullet 3: create without --no-sync reconciles (no more stub) ─────────────

def test_create_reconciles_without_no_sync(hermes_root, profile, parser, W,
                                           capsys):
    _install_plugin(hermes_root)
    _with_model(profile("qa", _SEED))
    wd = W("live")
    code, env, err = run(parser, "create", "live",
                         "--member", "qa:orchestrator",
                         "--board", "live", "--create-board",
                         "--workdir", wd, "--create-workdir",
                         "--json", capsys=capsys)
    assert code == 0, err
    assert env["data"]["ready"] == {"ready": True, "blocking": []}
    rep = env["data"]["reconciled"]
    assert rep is not None and rep["slug"] == "live"
    assert rep["applied"]                    # the stub always returned False
    storage._reset_caches_for_tests()
    qa_cfg = hermes_root / "profiles" / "qa" / "config.yaml"
    with open(qa_cfg, "rb") as fh:
        parsed = yaml.safe_load(fh.read())
    assert "hermes-agent-synergia" in parsed["plugins"]["enabled"]
    # …and the follow-up sync is a clean report
    code, env, _err = run(parser, "sync", "live", "--json", capsys=capsys)
    assert code == 0
    assert _real_drift(env["data"]) == []


def _with_model(home):
    with open(home / "config.yaml", "a", encoding="utf-8") as fh:
        fh.write("model:\n  default: test-model\n")
    return home


def test_create_without_a_model_is_not_ready_exit_4(hermes_root, profile,
                                                    parser, W, capsys):
    """Everything sync can fix is fixed; a member with no model is what is
    left, and create says so (exit 4, record kept) instead of leaving a team
    whose workers exit before their first turn."""
    _install_plugin(hermes_root)
    profile("qa", _SEED)
    code, env, err = run(parser, "create", "nomodel",
                         "--member", "qa:orchestrator",
                         "--workdir", W("nomodel"), "--json", capsys=capsys)
    assert code == 4, err
    ready = env["data"]["ready"]
    assert ready["ready"] is False
    assert [(b["check"], b["subject"]) for b in ready["blocking"]] == [
        ("member.model", "profile:qa")]
    storage._reset_caches_for_tests()
    assert "nomodel" in storage.load().teams


def test_no_sync_create_still_reports_drift_on_first_sync(hermes_root, profile,
                                                          parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    storage._reset_caches_for_tests()
    code, env, _err = run(parser, "sync", "teamx", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["drift"]              # created --no-sync ⇒ drift seen


# ── R1: the FIRST claim of a profile owns plane 1 ───────────────────────────

def test_adopted_first_claim_is_primary(hermes_root, profile, parser, W,
                                        capsys):
    team = _mk_team(hermes_root, profile, parser, W, capsys)
    qa = next(m for m in team["members"] if m["profile"] == "qa")
    assert qa["primary"] is True       # demo of this world: first --member qa
    # …and the claim is on disk, not just in the create echo
    stored = storage.load().teams["teamx"]
    assert next(m for m in stored.members
                if m.profile == "qa").primary is True


def test_second_team_shares_claimed_profile_as_nonprimary(hermes_root, profile,
                                                          parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)   # teamx claims qa
    storage._reset_caches_for_tests()
    code, env, err = run(
        parser, "create", "second", "--member", "qa:orchestrator",
        "--board", "second", "--create-board",
        "--workdir", W("second"), "--create-workdir",
        "--no-sync", "--json", capsys=capsys)
    assert code == 0, err or json.dumps(env)
    qa = next(m for m in env["data"]["team"]["members"]
              if m["profile"] == "qa")
    assert qa["primary"] is False      # Teamx claimed it first (I2 holds)
    storage._reset_caches_for_tests()
    teamx = storage.load().teams["teamx"]
    assert next(m for m in teamx.members
                if m.profile == "qa").primary is True   # unchanged owner


# ── wiring ───────────────────────────────────────────────────────────────────

def test_sync_all_reports_every_team(hermes_root, profile, parser, W, capsys):
    _mk_team(hermes_root, profile, parser, W, capsys)
    _mk_team(hermes_root, profile, parser, W, capsys, slug="second",
             members=("qa2", "pm2"))
    storage._reset_caches_for_tests()
    code, env, _err = run(parser, "sync", "--all", "--json", capsys=capsys)
    assert code == 0
    assert [t["team"] for t in env["data"]["teams"]] == ["second", "teamx"]
    assert all(t["dry_run"] for t in env["data"]["teams"])


def test_sync_requires_team_or_all(hermes_root, parser, capsys):
    code, _env, err = run(parser, "sync", capsys=capsys)
    assert code == 2
    assert "usage" in err
    code, _env, err = run(parser, "sync", "teamx", "--all", capsys=capsys)
    assert code == 2
