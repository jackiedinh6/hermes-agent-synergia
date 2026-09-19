"""S16 — integration: policy on a REAL board (spec §14.4, sequence §5.6).

The rewrite and the refusal both happen for real here: a temp ``HERMES_HOME``,
a real SQLite kanban board under it, and the plugin loaded through **real
discovery** (the house rule from ``plugins/AGENTS.md`` and the S15 pattern in
``test_hooks.py``: symlink into ``<HERMES_HOME>/plugins``, enable in
``config.yaml``, ``PluginManager.discover_and_load()`` runs the plugin's real
``register()`` against a recording ``PluginContext`` double). The hook and
tool handlers exercised below are the *registered* objects the loader handed
the host — not direct module calls — so the wiring under test is the wiring a
session would actually get.

The seven §14.4 bullets this step verifies (implement.md S16, lines
1289-1299), in order:

1. ``team create --create-board --create-workdir`` (in-process
   ``cli.handle``) → board dir, ``board.json`` and the workdir exist;
2. ``team_create_task`` → the created row on the REAL board DB carries the
   record-bound ``board`` / ``assignee`` / ``tenant`` / ``workspace_kind``;
3. ``kanban_create`` with a WRONG board **through the registered hook** → the
   dispatcher simulation (shallow-merge the ``modify`` args, then call the
   substrate's real ``kanban_create`` handler) lands the row on the team
   board — T2 rewrite, no retry (exactly one row, wrong board never opened);
4. ``policy.mode: enforce`` + non-member assignee → the hook returns the
   block directive and **no row is created** (row count unchanged);
5. two teams, one shared board → ``team_status`` for one tenant excludes the
   other team's cards (§6.3 stamping pays off);
6. ``team_sync`` (the tool) never writes — whole-tree snapshot (path → size +
   sha256) before and after is byte-identical;
7. disabling the plugin (``plugins.enabled`` without ``hermes-agent-synergia``,
   re-discover) removes the tools AND the hook and leaves the board dir,
   ``board.json`` and every card byte-identical (success criterion 5).

Deviations / notes:
* §14.4's ``team clone`` bullet is not part of the S16 Verify list
  (implement.md S16 lists the seven above); clone is exercised by its own
  step, so it is not reproduced here.
* Tree snapshots hash file CONTENT (sha256) and record size, and deliberately
  ignore mtimes: a rewrite that preserves content is still a write we would
  catch, while an mtime touched by a read-only open would be a false alarm
  (same witness choice as ``test_tools._tree_fingerprint``).
* The dispatcher simulation for bullet 3 is the minimal honest one: call the
  registered ``pre_tool_call`` hook, shallow-merge its ``args`` directive
  (the host contract, ``plugins.py``), and invoke the substrate's real
  ``kanban_create`` handler once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

import pytest
import yaml

import cli
import storage

PLUGIN_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
NS_PREFIX = "hermes_plugins.hermes-agent-synergia"


# ── world fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_world(hermes_root, monkeypatch):
    """Temp HERMES_HOME (conftest) + drop every session identity the host
    shell or a previous test could leak into the hook's env snapshot or the
    substrate's board resolution. Caches dropped before and after."""
    for var in (
        "HERMES_PROFILE",
        "HERMES_KANBAN_DB",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_KEY",
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_KANBAN_TASK",
    ):
        monkeypatch.delenv(var, raising=False)
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
    """Legal workdir paths — outside the Hermes root (guard 5)."""
    def _w(name):
        return str(hermes_root.parent / ("work-" + name))
    return _w


# ── real discovery (the S15 pattern from test_hooks.py) ──────────────────────

class _Recorder:
    """Minimal ``PluginContext`` double: records every registration the real
    ``register(ctx)`` performs. ``profile_name`` is the §5.3 gate input."""

    def __init__(self, profile: str):
        self.profile_name = profile
        self.tools: list = []
        self.hooks: list = []
        self.cli_commands: list = []
        self.prompt_sections: list = []
        self.unload_callbacks: list = []

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_cli_command(self, **kw):
        self.cli_commands.append(kw)

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))

    def register_system_prompt_section(self, **kw):
        self.prompt_sections.append(kw)

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)

    def _tool_override_allowed(self, tool_name):   # loader probes on every load
        return False


def _discover(recorder: _Recorder, *, expect_loaded: bool = True):
    """Real discovery with the recording ctx (copied from the S15 helper):
    symlink the plugin dir into the temp HERMES_HOME, enable it in the root
    config, and let ``PluginManager.discover_and_load()`` run the plugin's
    real ``register()``. Returns ``(manager, recorder)``. The enabled set is
    *rewritten* here, so call this BEFORE any tree snapshot that must be
    stable. With ``expect_loaded=False`` the plugin dir stays linked but the
    config does not enable hermes-agent-synergia — the disable scenario of bullet 7."""
    import hermes_cli.plugins as pmod

    class _Sink:
        """Ctx double for unrelated plugins: every register_* is a no-op."""
        profile_name = "sink"

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return lambda *a, **k: None

    def _factory(manifest, manager):
        if str(getattr(manifest, "name", "")).strip() == "hermes-agent-synergia":
            return recorder
        return _Sink()

    root = pathlib.Path(os.environ["HERMES_HOME"])
    link = root / "plugins" / "hermes-agent-synergia"
    if not link.is_symlink() and not link.exists():
        os.symlink(str(PLUGIN_SRC), str(link))
    enabled = ["hermes-agent-synergia"] if expect_loaded else []
    (root / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": enabled}}), encoding="utf-8")

    pmod._reset_plugin_managers_for_tests()
    original_ctx = pmod.PluginContext
    pmod.PluginContext = _factory
    try:
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        if expect_loaded:
            loaded = mgr._plugins["hermes-agent-synergia"]
            assert loaded.error is None, loaded.error
            assert loaded.enabled
        return mgr, recorder
    finally:
        pmod.PluginContext = original_ctx
        for name in list(sys.modules):
            if name == NS_PREFIX or name.startswith(NS_PREFIX + "."):
                sys.modules.pop(name, None)


def _handler(rec: _Recorder, tool_name: str):
    """The handler the real ``register()`` handed the host for *tool_name*."""
    for kw in rec.tools:
        if kw.get("name") == tool_name:
            return kw["handler"]
    raise AssertionError(f"{tool_name} not registered by discovery")


def _call(rec: _Recorder, tool_name: str, args: dict) -> dict:
    """Invoke a registered handler exactly as the substrate would."""
    return json.loads(_handler(rec, tool_name)(dict(args)))


def _pre_tool_call(rec: _Recorder):
    """The registered ``pre_tool_call`` callback (must exist)."""
    cbs = [cb for name, cb in rec.hooks if name == "pre_tool_call"]
    assert len(cbs) == 1, f"expected one pre_tool_call, got {len(cbs)}"
    return cbs[0]


# ── team creation through the CLI ────────────────────────────────────────────

def run(parser, *argv, capsys):
    """Parse + dispatch; returns (exit_code, stdout_json_or_None, stderr)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)
    return code, envelope, out.err


def _create_team(hermes_root, profile, parser, W, capsys, slug, *,
                 members, board=None, policy_mode="advise",
                 workspace_policy=None, create_board=True,
                 allow_shared=False):
    """Seed the member profiles, then ``hermes team create … --no-sync``."""
    for name, _role in members:
        profile(name)
    argv = ["create", slug]
    for name, role in members:
        argv += ["--member", f"{name}:{role}"]
    argv += ["--board", board or slug,
             "--workdir", W(slug), "--create-workdir",
             "--policy", policy_mode]
    if create_board:
        argv.append("--create-board")
    if workspace_policy:
        argv += ["--workspace-policy", workspace_policy]
    if allow_shared:
        argv.append("--allow-shared-board")
    argv += ["--no-sync", "--json"]
    code, env, err = run(parser, *argv, capsys=capsys)
    assert code == 0, err or json.dumps(env)
    storage._reset_caches_for_tests()
    return env["data"]["team"]


# ── substrate witnesses ──────────────────────────────────────────────────────

def _task_row(board: str, task_id: str):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return kanban_db.get_task(conn, task_id)


def _row_count(board: str) -> int:
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.is_file():
        return 0
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        return int(conn.execute("select count(*) from tasks").fetchone()[0])


def _tree_snapshot(root) -> dict:
    """Every file under *root*: relpath → (size, sha256) — write witness.
    Content, not mtime (see module docstring). Symlinked dirs (the plugin
    link) are not descended by os.walk's default."""
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            p = pathlib.Path(dirpath) / name
            try:
                data = p.read_bytes()
            except OSError:
                data = b"<unreadable>"
            out[str(p.relative_to(root))] = (len(data),
                                            hashlib.sha256(data).hexdigest())
    return out


# ── bullet 1: create --create-board --create-workdir ─────────────────────────

def test_team_create_cli_makes_board_dir_board_json_and_workdir(
        hermes_root, profile, parser, W, capsys):
    """§14.4.1 — the CLI provisions for real: the board directory with its
    ``board.json`` exists on disk and the team workdir was created."""
    from hermes_cli import kanban_db as kb

    team = _create_team(hermes_root, profile, parser, W, capsys, "s16a",
                        members=[("qa", "orchestrator"),
                                 ("qa-2", "implementer")])
    assert team["board"] == "s16a"

    board_dir = kb.board_dir("s16a")
    assert board_dir.is_dir()
    board_json = board_dir / "board.json"
    assert board_json.is_file()
    meta = json.loads(board_json.read_text(encoding="utf-8"))
    assert meta.get("slug", "s16a") == "s16a"
    assert kb.board_exists("s16a") is True

    workdir = W("s16a")
    assert os.path.isdir(workdir)
    assert team["workdir"] == workdir


# ── bullet 2: team_create_task row bindings ─────────────────────────────────

def test_team_create_task_row_binds_board_assignee_tenant_workspace(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """§14.4.2 — the tool takes no board/tenant args: the row on the REAL
    SQLite board carries the record's board, the roster-valid assignee, the
    team tenant, and the workspace_kind mapped from the record."""
    team = _create_team(hermes_root, profile, parser, W, capsys, "s16b",
                        members=[("qa", "orchestrator"),
                                 ("qa-2", "implementer")],
                        workspace_policy="worktree")
    rec = _Recorder("qa")
    _discover(rec)
    monkeypatch.setenv("HERMES_PROFILE", "qa")

    env = _call(rec, "team_create_task",
                {"title": "integrate policy", "body": "S16",
                 "assignee": "qa-2"})
    assert env["ok"] is True, env
    data = env["data"]
    assert data["board"] == "s16b"
    assert data["tenant"] == "s16b"
    assert data["assignee"] == "qa-2"

    row = _task_row("s16b", data["task_id"])
    assert row is not None                      # found on the TEAM board
    assert row.assignee == "qa-2"
    assert row.tenant == "s16b"                  # stamped from the record
    assert row.workspace_kind == "worktree"
    assert row.workspace_path == team["workdir"]
    assert _row_count("s16b") == 1


# ── bullet 3: wrong board through the hook → rewrite, no retry ───────────────

def test_hook_rewrites_wrong_board_and_row_lands_on_team_board(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """§5.6 end to end — model asks for the wrong board; the REGISTERED hook
    answers ``modify``; the dispatcher simulation (shallow merge + one real
    substrate ``kanban_create`` call) lands exactly one row on the team
    board. The foreign board is never even opened."""
    from tools import kanban_tools
    from hermes_cli import kanban_db as kb

    _create_team(hermes_root, profile, parser, W, capsys, "s16c",
                 members=[("qa", "orchestrator"),
                          ("qa-2", "implementer")],
                 policy_mode="advise")
    rec = _Recorder("qa")
    _discover(rec)
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hook = _pre_tool_call(rec)

    args = {"title": "fan out", "assignee": "qa-2",
            "board": "someone-elses-board"}
    ret = hook("kanban_create", dict(args))
    assert isinstance(ret, dict) and ret["action"] == "modify"
    patch = ret["args"]
    assert patch["board"] == "s16c"              # T2 rewrite target
    assert patch["tenant"] == "s16c"             # tenant stamped in the same
    assert "assignee" not in patch               # member: left alone

    merged = {**args, **patch}                   # the host's shallow merge
    out = json.loads(kanban_tools._handle_create(dict(merged)))
    assert out.get("ok") is True, out            # the tool ran ONCE, changed
    row = _task_row("s16c", out["task_id"])
    assert row is not None                       # the row IS the team board
    assert row.tenant == "s16c"
    assert row.assignee == "qa-2"
    assert _row_count("s16c") == 1               # one row, no retry
    assert not kb.kanban_db_path("someone-elses-board").exists()


def test_hook_binds_team_workspace_on_raw_kanban_create(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """A raw ``kanban_create`` with no workspace args on a worktree team lands
    a worktree row anchored at the team workdir — not a scratch row, which
    the substrate deletes on completion."""
    from tools import kanban_tools

    team = _create_team(hermes_root, profile, parser, W, capsys, "s16w",
                        members=[("qa", "orchestrator"),
                                 ("qa-2", "implementer")],
                        workspace_policy="worktree")
    rec = _Recorder("qa")
    _discover(rec)
    monkeypatch.setenv("HERMES_PROFILE", "qa")

    args = {"title": "implement", "assignee": "qa-2", "board": "s16w"}
    ret = _pre_tool_call(rec)("kanban_create", dict(args))
    assert isinstance(ret, dict) and ret["action"] == "modify"

    out = json.loads(kanban_tools._handle_create({**args, **ret["args"]}))
    assert out.get("ok") is True, out
    row = _task_row("s16w", out["task_id"])
    assert row.workspace_kind == "worktree"
    assert row.workspace_path == team["workdir"]


# ── bullet 4: enforce + non-member assignee → block, zero writes ─────────────

def test_enforce_non_member_assignee_blocks_and_creates_no_row(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """§5.6 enforce branch — the registered hook refuses with a message that
    names team, violation, legal values and remedy, and the board gains no
    row (the dispatcher never runs the tool; we don't either)."""
    _create_team(hermes_root, profile, parser, W, capsys, "s16d",
                 members=[("qa", "orchestrator"),
                          ("qa-2", "implementer")],
                 policy_mode="enforce")
    rec = _Recorder("qa")
    _discover(rec)
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hook = _pre_tool_call(rec)

    # one legal card first, so "no row created" has a baseline of exactly 1
    env = _call(rec, "team_create_task",
                {"title": "legal", "assignee": "qa-2"})
    assert env["ok"] is True, env
    baseline = _row_count("s16d")
    assert baseline == 1

    ret = hook("kanban_create", {"title": "illegal",
                                 "assignee": "some-outsider",
                                 "board": "s16d"})
    assert isinstance(ret, dict)
    assert ret["action"] == "block"
    assert set(ret) == {"action", "message"}
    msg = ret["message"]
    assert "some-outsider" in msg                # names the violation
    assert "s16d" in msg                         # names the team
    assert "qa-2" in msg                         # names the legal values
    assert "add-member" in msg                   # names the remedy (§5.6)

    assert _row_count("s16d") == baseline        # NOT one row was created


# ── bullet 5: shared board, two teams → tenant scoping in team_status ────────

def test_team_status_on_shared_board_excludes_other_team_rows(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """§14.4.5 — two teams share ONE board DB; each team's ``team_status``
    sees only its own tenant's cards (the §6.3 stamp is the fence)."""
    _create_team(hermes_root, profile, parser, W, capsys, "alpha",
                 members=[("qa", "orchestrator"), ("qa-2", "implementer")],
                 board="shared-s16e", allow_shared=True)
    _create_team(hermes_root, profile, parser, W, capsys, "beta",
                 members=[("dev", "orchestrator")],
                 board="shared-s16e", allow_shared=True,
                 create_board=False)              # the board already exists
    rec = _Recorder("qa")
    _discover(rec)

    monkeypatch.setenv("HERMES_PROFILE", "qa")
    a1 = _call(rec, "team_create_task",
               {"title": "alpha-one", "assignee": "qa-2"})["data"]["task_id"]
    a2 = _call(rec, "team_create_task",
               {"title": "alpha-two", "assignee": "qa"})["data"]["task_id"]
    monkeypatch.setenv("HERMES_PROFILE", "dev")
    b1 = _call(rec, "team_create_task",
               {"title": "beta-one", "assignee": "dev"})["data"]["task_id"]
    assert _row_count("shared-s16e") == 3        # ONE shared SQLite board

    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call(rec, "team_status", {})
    assert env["ok"] is True, env
    data = env["data"]
    assert data["board"] == "shared-s16e"
    assert sum(data["counts"].values()) == 2     # alpha's tenant only
    ids = {c["id"] for c in data["open_cards"]}
    assert ids == {a1, a2}
    assert b1 not in ids                         # beta's card is invisible

    # …and the mirror: beta sees only its own row
    monkeypatch.setenv("HERMES_PROFILE", "dev")
    env2 = _call(rec, "team_status", {})
    assert sum(env2["data"]["counts"].values()) == 1
    assert [c["id"] for c in env2["data"]["open_cards"]] == [b1]


# ── bullet 6: team_sync (tool) never writes ─────────────────────────────────

def test_team_sync_tool_tree_is_byte_identical_before_and_after(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """§14.4.6 — the whole HERMES_HOME tree (every file: size + sha256) is
    the witness: a dry-run ``team_sync`` may report drift, but not one byte
    may move (no path to ``reconcile.apply``)."""
    _create_team(hermes_root, profile, parser, W, capsys, "s16f",
                 members=[("qa", "orchestrator"),
                          ("qa-2", "implementer")])
    rec = _Recorder("qa")
    _discover(rec)
    monkeypatch.setenv("HERMES_PROFILE", "qa")

    env = _call(rec, "team_create_task", {"title": "seed", "assignee": "qa"})
    assert env["ok"] is True, env

    before = _tree_snapshot(hermes_root)
    rep = _call(rec, "team_sync", {})
    after = _tree_snapshot(hermes_root)

    assert rep["ok"] is True, rep
    assert rep["data"]["dry_run"] is True
    assert rep["data"]["nothing_written"] is True
    assert isinstance(rep["data"]["drift"], list)
    assert after == before                       # not one byte moved


# ── bullet 7: disable → tools+hook gone, board+cards unchanged ───────────────

def test_disabling_plugin_removes_tools_and_hook_and_leaves_board_untouched(
        hermes_root, profile, parser, W, capsys, monkeypatch):
    """Success criterion 5. Phase A: enabled discovery registers ten tools
    and the pre_tool_call hook (cards exist on the real board). Phase B: the
    plugin is linked but NOT in ``plugins.enabled`` → re-discover yields zero
    hermes-agent-synergia registrations, and the board subtree + registry are hash-for-
    hash the same."""
    _create_team(hermes_root, profile, parser, W, capsys, "s16g",
                 members=[("qa", "orchestrator"),
                          ("qa-2", "implementer")])
    rec_a = _Recorder("qa")
    _discover(rec_a)
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call(rec_a, "team_create_task",
                {"title": "durable", "assignee": "qa-2"})
    assert env["ok"] is True, env

    # enabled registration surface (phase A)
    assert len(rec_a.tools) == 10
    assert "pre_tool_call" in [name for name, _cb in rec_a.hooks]

    from hermes_cli import kanban_db as kb
    board_state_before = {
        "tree": _tree_snapshot(hermes_root / "kanban"),
        "board_json": (kb.board_dir("s16g") / "board.json").read_bytes(),
        "registry": storage.registry_path().read_bytes(),
        "row_count": _row_count("s16g"),
    }

    # disable: drop hermes-agent-synergia from plugins.enabled, re-discover for real
    root = pathlib.Path(os.environ["HERMES_HOME"])
    (root / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": []}}), encoding="utf-8")
    storage._reset_caches_for_tests()
    mgr_b, rec_b = _discover(_Recorder("qa"), expect_loaded=False)

    # tools gone, hook gone — register() never ran for the disabled plugin
    assert rec_b.tools == []
    assert rec_b.hooks == []
    assert rec_b.cli_commands == []
    assert rec_b.prompt_sections == []
    loaded_b = mgr_b._plugins.get("hermes-agent-synergia")
    if loaded_b is not None:
        assert not loaded_b.enabled

    board_state_after = {
        "tree": _tree_snapshot(hermes_root / "kanban"),
        "board_json": (kb.board_dir("s16g") / "board.json").read_bytes(),
        "registry": storage.registry_path().read_bytes(),
        "row_count": _row_count("s16g"),
    }
    assert board_state_after == board_state_before   # board+cards unchanged
