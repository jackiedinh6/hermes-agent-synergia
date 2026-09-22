"""S15 — hooks.py + the full §5.3 register(ctx).

Registration assertions go through **real discovery** (the house rule from
``plugins/AGENTS.md``: compat tests load plugins through the real discovery
path and assert behaviour, never source): the plugin directory is symlinked
into ``<HERMES_HOME>/plugins``, enabled in ``config.yaml``, and
``PluginManager.discover_and_load()`` runs the plugin's real ``register()``
against a recording ``PluginContext`` double — patched at
``hermes_cli.plugins.PluginContext``, the exact name the loader's lazy
``from hermes_cli.plugins import PluginContext`` inside
``_load_plugin_scoped`` binds. The recorder shows exactly which hooks a given
profile's registration produced; ``loaded.error is None`` and
``loaded.enabled`` prove ``register()`` itself ran clean.

The hot-path / cache / fail-open assertions call ``hooks.pre_tool_call``
directly — the ladder is a behaviour contract, not a wiring one, and §5.4
pins its cost:

* unguarded tool  → zero stat, zero parse, ``None``            (ladder step 1)
* guarded tool, warm cache → exactly one stat per call, zero re-parses
* garbage / missing registry → ``None``, logged **once**       (fail open, §5.4/§6.4)
* ``policy.decide`` raising → ``None`` (logged), never out     (§6.4 catch-all)
* 10,000 warm guarded calls: mean < 1 ms (measured, printed, asserted)

Directive translation is asserted in the shapes the substrate's
``_get_pre_tool_call_directive_details`` accepts (``hermes_cli/plugins.py``):
``{"action": "modify", "args": {...}}`` / ``{"action": "block", "message":
...}``.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time

import pytest
import yaml

from hermes_agent_synergia import hooks, policy, prompt
import storage

PLUGIN_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
NS_PREFIX = "hermes_plugins.hermes-agent-synergia"


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _teamhome(hermes_root, monkeypatch):
    """Registry under a per-test dir (the HERMES_TEAM_HOME env seam — top of
    storage's resolution order); hook caches dropped before and after every
    test so the module-level memo can never leak sideways."""
    reg_dir = hermes_root / "teamreg"
    monkeypatch.setenv("HERMES_TEAM_HOME", str(reg_dir))
    hooks.teardown()
    storage._reset_caches_for_tests()
    yield reg_dir
    hooks.teardown()
    storage._reset_caches_for_tests()


def _team(slug="px", board="px-board", mode="advise", *,
          members=(("qa", "orchestrator", True),), tenant="",
          orchestrator="qa") -> dict:
    return {
        "slug": slug, "display_name": slug, "description": "",
        "board": board, "workdir": "/tmp/whatever", "tenant": tenant,
        "orchestrator": orchestrator,
        "policy": {"mode": mode},
        "members": [{"profile": p, "role": r, "primary": prim}
                    for p, r, prim in members],
    }


def _write_teams(reg_dir, teams: dict) -> pathlib.Path:
    reg_dir = pathlib.Path(reg_dir)
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / "teams.yaml"
    path.write_text(yaml.safe_dump(
        {"schema_version": 1, "revision": 1, "teams": teams},
        sort_keys=False), encoding="utf-8")
    return path


class _Recorder:
    """Minimal ``PluginContext`` double: records every registration the real
    ``register(ctx)`` performs. ``profile_name`` is the §5.3 gate input —
    the real PluginContext derives it from HERMES_HOME (plugins.py :392);
    the double states it outright, per profile under test."""

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

    def _tool_override_allowed(self, tool_name):   # loader probes it on every load
        return False


def _discover(recorder: _Recorder):
    """Real discovery with the recording ctx: symlink the plugin dir into
    the temp HERMES_HOME, enable it, let PluginManager run the real
    ``register()``. Returns the LoadedPlugin — ``loaded.error is None`` is
    part of the assertion (a register() that raises fails here, loudly).

    Discovery loads the *whole* enabled set (bundled provider plugins too),
    so the patched factory is per-manifest: the recorder for hermes-agent-synergia, a
    swallow-everything sink for everything else."""
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
    if not link.exists():
        os.symlink(str(PLUGIN_SRC), str(link))
    (root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["hermes-agent-synergia"]}}), encoding="utf-8")

    pmod._reset_plugin_managers_for_tests()
    original_ctx = pmod.PluginContext
    pmod.PluginContext = _factory
    try:
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["hermes-agent-synergia"]
        assert loaded.error is None, loaded.error
        assert loaded.enabled
        return mgr, loaded
    finally:
        pmod.PluginContext = original_ctx
        for name in list(sys.modules):
            if name == NS_PREFIX or name.startswith(NS_PREFIX + "."):
                sys.modules.pop(name, None)


def _hook_names(recorder: _Recorder) -> list:
    return [name for name, _cb in recorder.hooks]


# ── §5.3 registration through real discovery ────────────────────────────────

def test_non_team_profile_registers_no_pre_tool_call(_teamhome):
    """'lonely' is on nobody's roster → register() must NOT wire
    pre_tool_call (§5.3: non-team profiles pay nothing on the hot path)."""
    _write_teams(_teamhome, {"px": _team()})
    rec = _Recorder("lonely")
    _discover(rec)
    assert "pre_tool_call" not in _hook_names(rec)
    # ... while the rest of §5.3 still registers:
    assert _hook_names(rec) == ["kanban_task_claimed", "kanban_task_blocked",
                                "kanban_task_completed", "on_kanban_dispatch_tick"]
    assert len(rec.tools) == 10
    assert {t["name"] for t in rec.tools} >= {"team_whoami", "team_list"}
    assert all(t["toolset"] == "team" for t in rec.tools)
    assert rec.cli_commands and rec.cli_commands[0]["name"] == "team"
    assert len(rec.prompt_sections) == 2
    section = rec.prompt_sections[0]
    assert section["id"] == "hermes-agent-synergia.contract"
    assert section["position"] == "after_memory"
    assert section["max_chars"] == 1000
    assert callable(section["content"])
    attachments = rec.prompt_sections[1]
    assert attachments["id"] == "hermes-agent-synergia.attachments"
    assert attachments["position"] == "after_memory"
    assert callable(attachments["content"])
    # the discovery-loaded plugin is its own module instance — compare the
    # unload callback by name, not identity
    assert [getattr(cb, "__name__", "") for cb in rec.unload_callbacks] == ["teardown"]


def test_team_member_with_policy_registers_exactly_one(_teamhome):
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    rec = _Recorder("qa")
    _discover(rec)
    pre = [cb for name, cb in rec.hooks if name == "pre_tool_call"]
    assert len(pre) == 1 and callable(pre[0])
    assert _hook_names(rec) == ["pre_tool_call", "kanban_task_claimed",
                                "kanban_task_blocked", "kanban_task_completed",
                                "on_kanban_dispatch_tick"]


def test_member_of_policy_off_team_registers_no_pre_tool_call(_teamhome):
    _write_teams(_teamhome, {"px": _team(mode="off")})
    rec = _Recorder("qa")
    _discover(rec)
    assert "pre_tool_call" not in _hook_names(rec)
    assert "kanban_task_claimed" in _hook_names(rec)


def test_policy_needed_direct_matrix(_teamhome):
    """policy_needed must not raise and answers the membership+mode question
    exactly (it gates the registrations above)."""
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    assert hooks.policy_needed("qa") is True
    assert hooks.policy_needed("lonely") is False
    assert hooks.policy_needed("") is False
    assert hooks.policy_needed(None) is False


# ── fail open (§5.4, §6.4) — direct hook calls ───────────────────────────────

def test_garbage_registry_fails_open_and_logs_once(_teamhome, caplog, monkeypatch):
    path = _teamhome / "teams.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("teams: {oops\n\tbroken: [", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    with caplog.at_level(logging.WARNING):
        assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None
        first = [r for r in caplog.records if "registry unreadable" in r.message]
        assert len(first) == 1
        # second call: the memo holds the fail-open value — nothing re-parsed,
        # so the failure must not re-log (log-once module flag, §5.4)
        assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None
    second = [r for r in caplog.records if "registry unreadable" in r.message]
    assert len(second) == 1


def test_first_ever_parse_failure_is_no_teams(_teamhome, monkeypatch):
    """Absent file and garbage file end the same way: None, no raise, and
    the hook lets nothing through (no teams ⇒ no policy to apply)."""
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    assert not (_teamhome / "teams.yaml").exists()
    assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None
    (_teamhome).mkdir(parents=True, exist_ok=True)
    (_teamhome / "teams.yaml").write_text("teams: [", encoding="utf-8")
    hooks.teardown()                        # forget the memoised "no registry"
    assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None


def test_decide_raising_returns_none(_teamhome, caplog, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="enforce")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    def _boom(*a, **k):
        raise RuntimeError("policy exploded")
    monkeypatch.setattr(policy, "decide", _boom)
    with caplog.at_level(logging.WARNING):
        assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None
    assert any("failed open" in r.message for r in caplog.records)


def test_hook_never_raises_even_when_registry_resolution_blows_up(monkeypatch):
    def _no_path():
        raise OSError("filesystem on fire")
    monkeypatch.setattr(storage, "registry_path", _no_path)
    hooks.teardown()
    assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is None
    assert hooks.policy_needed("qa") is False
    assert hooks.render_contract({"profile_name": "qa"}) == ""


# ── the cache (§5.4): one stat per call, parse only on change ────────────────

@pytest.fixture
def _counts(_teamhome, monkeypatch):
    """Path.stat wrapper counting only stats of THIS test's teams.yaml (the
    hook's single freshness stat — plus any a storage parse does), and a
    storage.load wrapper counting parses."""
    real_stat = pathlib.Path.stat
    target = str(_teamhome / "teams.yaml")
    stats: list = []

    def counting_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if str(self) == target:
            stats.append(True)
        return st

    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)

    parses: list = []
    real_load = storage.load

    def counting_load(*a, **k):
        parses.append(True)
        return real_load(*a, **k)

    monkeypatch.setattr(storage, "load", counting_load)
    return stats, parses


def test_exactly_one_stat_per_call_zero_reparse(_teamhome, _counts, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()
    stats, parses = _counts
    assert hooks.pre_tool_call("kanban_create", {"title": "t"}) is not None
    assert len(parses) == 1                     # the one legitimate parse
    warm = len(stats)
    n = 40
    for _ in range(n):
        assert hooks.pre_tool_call(
            "kanban_create", {"title": "t", "board": "px-board"}) is not None
    assert len(stats) - warm == n               # warm calls: one hook stat each
    assert len(parses) == 1                     # zero re-parses, same key


def test_cache_refreshes_on_file_change(_teamhome, _counts, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()
    _stats, parses = _counts
    hooks.pre_tool_call("kanban_create", {"title": "t"})
    assert len(parses) == 1
    _write_teams(_teamhome, {"px": _team(mode="advise", board="moved"),
                             "yy": _team(slug="yy", board="other")})
    hooks.pre_tool_call("kanban_create", {"title": "t"})
    assert len(parses) == 2                     # (mtime_ns, size) moved ⇒ re-parse once


def test_teardown_clears_caches(_teamhome, _counts, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()
    stats, parses = _counts
    hooks.pre_tool_call("kanban_create", {"title": "t"})
    base_stats, base_parses = len(stats), len(parses)
    hooks.teardown()                            # the ctx.on_unload seam
    hooks.pre_tool_call("kanban_create", {"title": "t"})
    assert len(parses) == base_parses + 1       # fresh re-parse...
    assert len(stats) > base_stats              # ...after a fresh stat pass


# ── the hot path (§5.4 ladder step 1) ───────────────────────────────────────

def test_unguarded_tool_zero_stat_zero_parse(_teamhome, _counts, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="enforce")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()
    stats, parses = _counts
    for tool in ("terminal", "write_file", "patch", "execute_code"):
        assert hooks.pre_tool_call(tool, {"command": "ls"}) is None
    assert stats == []                          # never a single stat
    assert parses == []                         # never a parse


def test_warm_guarded_cost_under_1ms(_teamhome, monkeypatch):
    """§5.4/§6.4 budget: 10,000 guarded calls over the warm ladder; mean per
    call must be < 1 ms. The measured number is printed (README / S15 DoD
    line) — run with -s to see it."""
    _write_teams(_teamhome, {"px": _team(mode="advise")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()
    args = {"title": "t", "board": "px-board"}
    for _ in range(50):                         # warm: parse + all lazies
        hooks.pre_tool_call("kanban_create", dict(args))
    n = 10_000
    t0 = time.perf_counter_ns()
    for _ in range(n):
        hooks.pre_tool_call("kanban_create", dict(args))
    mean_us = (time.perf_counter_ns() - t0) / n / 1000.0
    print(f"\nhermes-agent-synergia pre_tool_call: mean {mean_us:.1f} us/call "
          f"over {n} warm guarded calls")
    assert mean_us < 1000.0


# ── the contract section (§5.3) ─────────────────────────────────────────────

def test_render_contract_member_and_stranger(_teamhome, monkeypatch):
    _write_teams(_teamhome, {"px": _team(mode="advise", board="px-board")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    out = hooks.render_contract({"profile_name": "qa"})
    assert out.startswith("## Team: px (px)")
    assert "`qa`" in out and "px-board" in out
    assert 0 < len(out) <= prompt.MAX_CHARS
    assert hooks.render_contract({"profile_name": "stranger"}) == ""
    assert hooks.render_contract({}) == ""
    assert hooks.render_contract(None) == ""
    assert hooks.render_contract("not-a-mapping") == ""
    assert hooks.render_contract({"profile_name": None}) == ""


def test_render_contract_prefers_env_resolution(_teamhome, monkeypatch):
    """§6.3: a dispatched worker is identified by its team env, not its
    roster — HERMES_TEAM pins the contract even for a profile the team does
    not list."""
    _write_teams(_teamhome, {"px": _team(mode="advise", board="px-board"),
                             "other": _team(slug="other", board="ob")})
    monkeypatch.setenv("HERMES_TEAM", "px")
    out = hooks.render_contract({"profile_name": "stranger"})
    assert out.startswith("## Team: px (px)")


# ── the observer (§5.3): never raises, never a policy path ──────────────────

@pytest.mark.parametrize("payload", [
    {},
    {"task_id": None, "board": None, "assignee": None},
    {"task_id": 1, "board": object(), "assignee": ["weird"]},
    {"task_id": "t", "board": "b", "assignee": "qa", "run_id": "r",
     "profile_name": "qa", "telemetry_schema_version": 2},
])
def test_on_task_claimed_never_raises(payload):
    assert hooks.on_task_claimed(**payload) is None


# ── directive translation onto the host contract (§6.2) ─────────────────────

def _env_world(reg_dir, monkeypatch, mode):
    _write_teams(reg_dir, {"px": _team(mode=mode, board="px-board",
                                       orchestrator="qa")})
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    hooks.teardown()


def test_advise_wrong_board_becomes_modify(_teamhome, monkeypatch):
    _env_world(_teamhome, monkeypatch, "advise")
    ret = hooks.pre_tool_call("kanban_create",
                              {"title": "t", "board": "someone-elses-board"})
    assert isinstance(ret, dict)
    assert ret["action"] == "modify"
    args = ret["args"]
    assert isinstance(args, dict) and args     # host shallow-merges this
    assert args["board"] == "px-board"          # §6.2 row 1
    assert args["assignee"] == "qa"             # row 4: absent → orchestrator
    assert all("." not in k for k in args)      # whole top-level keys only


def test_advise_outsider_assignee_modify_carries_note_as_log(_teamhome, caplog,
                                                             monkeypatch):
    _env_world(_teamhome, monkeypatch, "advise")
    with caplog.at_level(logging.INFO):
        ret = hooks.pre_tool_call("kanban_create",
                                  {"title": "t", "board": "px-board",
                                   "assignee": "stranger"})
    assert ret["action"] == "modify"
    assert ret["args"]["assignee"] == "qa"      # redirected to orchestrator
    # the note has no slot in the modify contract, so it is LOGGED, not
    # smuggled into the args (§6.2 advise rows):
    assert any("stranger" in r.message and "redirected" in r.message
               for r in caplog.records)


def test_enforce_outsider_assignee_becomes_block(_teamhome, monkeypatch):
    _env_world(_teamhome, monkeypatch, "enforce")
    ret = hooks.pre_tool_call("kanban_create",
                              {"title": "t", "board": "px-board",
                               "assignee": "stranger"})
    assert isinstance(ret, dict)
    assert set(ret) == {"action", "message"}   # the exact guard shape
    assert ret["action"] == "block"
    msg = ret["message"]
    assert isinstance(msg, str) and msg
    assert "stranger" in msg and "px" in msg   # names team + violation...
    assert "add-member" in msg                # ...legal remedy (§5.6)


def test_enforce_foreign_board_on_link_becomes_block(_teamhome, monkeypatch):
    _env_world(_teamhome, monkeypatch, "enforce")
    ret = hooks.pre_tool_call("kanban_link",
                              {"task_id": "t1", "parent": "p1",
                               "board": "foreign"})
    assert ret is not None and ret["action"] == "block"
    assert "board='px-board'" in ret["message"]   # legal values named


def test_clean_call_returns_none(_teamhome, monkeypatch):
    _env_world(_teamhome, monkeypatch, "enforce")
    # right board + explicit tenant + member assignee: nothing to rewrite
    assert hooks.pre_tool_call(
        "kanban_list", {"board": "px-board", "tenant": "px"}) is None


# ── the kanban_task_blocked observer ─────────────────────────────────────────
#
# An observer must never raise and never block the worker: the card is already
# committed by the time it fires, so a fault here would cost the operator the
# ping and nothing else — but only if it stays swallowed.

def _blocked_card(board, tenant, kind="needs_input"):
    from hermes_cli import kanban_db, kanban_db_connect
    kanban_db.create_board(board, name=board)
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        tid = kanban_db.create_task(conn, title="decide", assignee="qa",
                                    tenant=tenant, initial_status="running",
                                    board=board)
        kanban_db.block_task(conn, tid, reason="A or B?", kind=kind)
    return tid


def test_on_task_blocked_is_a_noop_without_a_home_channel(_teamhome,
                                                          monkeypatch):
    from hermes_agent_synergia import approvals
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _blocked_card("px-board", "px")
    monkeypatch.setattr(approvals, "configured_home_channels", lambda: [])
    assert hooks.on_task_blocked(task_id=tid, board="px-board",
                                 assignee="qa", reason="A or B?") is None
    assert approvals.on_blocked(tid, "px-board")["platforms"] == []


def test_on_task_blocked_subscribes_the_configured_home_channels(_teamhome,
                                                                 monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_notify
    from hermes_agent_synergia import approvals
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _blocked_card("px-board", "px")
    monkeypatch.setattr(approvals, "configured_home_channels",
                        lambda: [{"platform": "telegram", "chat_id": "42",
                                  "thread_id": "", "name": "Home"}])
    hooks.on_task_blocked(task_id=tid, board="px-board", assignee="qa")
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px-board")) as conn:
        subs = kanban_db_notify.list_notify_subs(conn, tid)
    assert [s["platform"] for s in subs] == ["telegram"]


def test_on_task_blocked_ignores_a_block_that_is_not_a_human_gate(_teamhome,
                                                                  monkeypatch):
    from hermes_agent_synergia import approvals
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _blocked_card("px-board", "px", kind="capability")
    calls = []
    monkeypatch.setattr(approvals, "notify_home_channels",
                        lambda *a, **kw: calls.append(a) or ["telegram"])
    out = approvals.on_blocked(tid, "px-board")
    assert out["needs_input"] is False and calls == []


def test_on_task_blocked_never_raises(_teamhome, monkeypatch):
    from hermes_agent_synergia import approvals

    def _boom(*_a, **_kw):
        raise RuntimeError("board is on fire")

    monkeypatch.setattr(approvals, "on_blocked", _boom)
    assert hooks.on_task_blocked(task_id="t_x", board="px-board") is None


def test_on_task_completed_never_raises(_teamhome, monkeypatch):
    """The auto-merge observer runs git in the worker that just finished; a
    fault there must never surface as a failed completion."""
    from hermes_agent_synergia import approvals

    def _boom(*_a, **_kw):
        raise RuntimeError("git is on fire")

    monkeypatch.setattr(approvals, "on_completed", _boom)
    assert hooks.on_task_completed(task_id="t_x", board="px-board") is None


# ── the stuck lane: the on_kanban_dispatch_tick observer ─────────────────────
#
# The dispatcher's circuit breaker blocks a card with a raw UPDATE and fires no
# kanban_task_blocked, so the give-up is invisible to on_task_blocked. This
# tick observer is the only push the stuck lane has — and it fires every few
# seconds forever, so the empty case must cost nothing at all.

class _Tick:
    def __init__(self, auto_blocked):
        self.auto_blocked = auto_blocked


def _gave_up_card(board, tenant, *, error="protocol violation", failures=2):
    """A card parked the way ``kanban_db_dispatch._record_failure`` parks one:
    blocked, with ``block_kind`` left NULL."""
    from hermes_cli import kanban_db, kanban_db_connect
    kanban_db.create_board(board, name=board)
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        tid = kanban_db.create_task(conn, title="verify", assignee="qa",
                                    tenant=tenant, initial_status="running",
                                    board=board)
        conn.execute("UPDATE tasks SET status='blocked', consecutive_failures=?, "
                     "last_failure_error=? WHERE id=?", (failures, error, tid))
        kanban_db._append_event(conn, tid, "gave_up",
                                {"error": error, "failures": failures})
        conn.commit()
    return tid


def _subs(board, tid):
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_notify
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return [s["platform"] for s in kanban_db_notify.list_notify_subs(conn, tid)]


def _home(monkeypatch):
    from hermes_agent_synergia import approvals
    monkeypatch.setattr(approvals, "configured_home_channels",
                        lambda: [{"platform": "telegram", "chat_id": "42",
                                  "thread_id": "", "name": "Home"}])


def test_dispatch_tick_does_nothing_when_no_card_was_given_up(_teamhome,
                                                              monkeypatch):
    """The common case, thousands of times a day: no registry read, no DB."""
    from hermes_agent_synergia import approvals
    calls = []
    monkeypatch.setattr(approvals, "on_auto_blocked",
                        lambda *a, **kw: calls.append(a))
    assert hooks.on_dispatch_tick(result=_Tick([]), board="px-board") is None
    assert hooks.on_dispatch_tick(result=_Tick(None), board="px-board") is None
    assert calls == []


def test_dispatch_tick_pushes_a_giveup_to_the_home_channels(_teamhome,
                                                            monkeypatch):
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _gave_up_card("px-board", "px")
    _home(monkeypatch)
    assert hooks.on_dispatch_tick(result=_Tick([tid]), board="px-board") is None
    assert _subs("px-board", tid) == ["telegram"]


def test_dispatch_tick_pings_a_card_once_however_often_the_tick_repeats(
        _teamhome, monkeypatch):
    """``auto_blocked`` is the tick's view of the world, not a delta — the same
    id can come back on the next tick, and the operator must not be re-pinged."""
    from hermes_agent_synergia import approvals
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _gave_up_card("px-board", "px")
    seen = []
    monkeypatch.setattr(approvals, "on_auto_blocked",
                        lambda ids, board, **kw: seen.append(list(ids)) or
                        {"notified": ids})
    hooks.on_dispatch_tick(result=_Tick([tid]), board="px-board")
    hooks.on_dispatch_tick(result=_Tick([tid]), board="px-board")
    assert seen == [[tid]]


def test_dispatch_tick_skips_a_card_that_is_no_longer_blocked(_teamhome,
                                                              monkeypatch):
    """The ids are read back from the DB, so a card released between the give-up
    and the tick is not announced as stuck."""
    from hermes_agent_synergia import approvals
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _gave_up_card("px-board", "px")
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px-board")) as conn:
        kanban_db.unblock_task(conn, tid)
    _home(monkeypatch)
    out = approvals.on_auto_blocked([tid], "px-board")
    assert out["stuck"] == [] and out["notified"] == []
    assert _subs("px-board", tid) == []


def test_dispatch_tick_is_silent_when_notifications_are_off(_teamhome,
                                                            monkeypatch):
    from hermes_agent_synergia import approvals, cli as team_cli
    _write_teams(_teamhome, {"px": _team(board="px-board", tenant="px")})
    storage._reset_caches_for_tests()
    tid = _gave_up_card("px-board", "px")
    _home(monkeypatch)
    monkeypatch.setattr(team_cli, "_settings", lambda: {"approvals_notify": "off"})
    hooks.on_dispatch_tick(result=_Tick([tid]), board="px-board")
    assert _subs("px-board", tid) == []
    assert approvals.on_auto_blocked([tid], "px-board")["notify"] == "off"


def test_dispatch_tick_never_raises(_teamhome, monkeypatch):
    from hermes_agent_synergia import approvals

    def _boom(*_a, **_kw):
        raise RuntimeError("board is on fire")

    monkeypatch.setattr(approvals, "on_auto_blocked", _boom)
    assert hooks.on_dispatch_tick(result=_Tick(["t_x"]), board="px-board") is None
    assert hooks.on_dispatch_tick(result=None) is None
    assert hooks.on_dispatch_tick() is None
    assert hooks.on_dispatch_tick(result=object(), board=None) is None


# ── the dispatch tick's two new jobs ─────────────────────────────────────────
#
# Both ride the same observer, which fires every few seconds forever. The
# invariant that matters is what an IDLE tick costs: nothing at all.

class _FullTick:
    def __init__(self, *, auto_blocked=None, spawned=None, respawn_guarded=None):
        self.auto_blocked = auto_blocked
        self.spawned = spawned
        self.respawn_guarded = respawn_guarded


def test_dispatch_tick_makes_no_substrate_call_when_nothing_spawned(
        _teamhome, monkeypatch):
    from hermes_agent_synergia import approvals

    def _never(*_a, **_kw):
        raise AssertionError("an idle tick must not reach the substrate")

    monkeypatch.setattr(approvals, "check_review_loop", _never)
    monkeypatch.setattr(approvals, "suppression_line", _never)
    monkeypatch.setattr(approvals, "on_auto_blocked", _never)
    assert hooks.on_dispatch_tick(result=_FullTick(), board="px-board") is None
    assert hooks.on_dispatch_tick(
        result=_FullTick(spawned=[], respawn_guarded=[]),
        board="px-board") is None


def test_dispatch_tick_checks_the_review_loop_for_each_spawned_card(
        _teamhome, monkeypatch):
    """``spawned`` carries ``(task_id, assignee, workspace_path)`` triples —
    the guard must read the id out, not the triple."""
    from hermes_agent_synergia import approvals
    seen = []
    monkeypatch.setattr(
        approvals, "check_review_loop",
        lambda ids, board, **kw: seen.append((list(ids), board)) or
        {"parked": [], "rounds": {}, "limit": 3})
    hooks.on_dispatch_tick(
        result=_FullTick(spawned=[("t_a", "dev", "/w/a"), ("t_b", "qa", "/w/b")]),
        board="px-board")
    assert seen == [(["t_a", "t_b"], "px-board")]


def test_dispatch_tick_logs_the_suppression_reason_once(_teamhome, monkeypatch,
                                                        caplog):
    from hermes_agent_synergia import approvals
    monkeypatch.setattr(approvals, "suppression_line",
                        lambda _tick: "active_pr=1, rate_limited=2")
    hooks.teardown()
    tick = _FullTick(respawn_guarded=[("t_a", "active_pr")])
    with caplog.at_level("INFO"):
        hooks.on_dispatch_tick(result=tick, board="px-board")
        hooks.on_dispatch_tick(result=tick, board="px-board")
    said = [r for r in caplog.records if "holding ready work back" in r.message]
    assert len(said) == 1
    assert "active_pr=1, rate_limited=2" in said[0].getMessage()


def test_review_loop_guard_never_costs_the_giveup_push(_teamhome, monkeypatch):
    """Each job is isolated: a raising guard must not swallow the push that
    ran before it, nor the suppression line after it."""
    from hermes_agent_synergia import approvals
    pushed = []
    monkeypatch.setattr(approvals, "on_auto_blocked",
                        lambda ids, board, **kw: pushed.append(list(ids)) or
                        {"notified": [], "platforms": []})

    def _boom(*_a, **_kw):
        raise RuntimeError("events table is gone")

    monkeypatch.setattr(approvals, "check_review_loop", _boom)
    lines = []
    monkeypatch.setattr(approvals, "suppression_line",
                        lambda _t: lines.append(1) or "")
    hooks.teardown()
    assert hooks.on_dispatch_tick(
        result=_FullTick(auto_blocked=["t_x"], spawned=[("t_y", "dev", "/w")],
                         respawn_guarded=[("t_y", "active_pr")]),
        board="px-board") is None
    assert pushed == [["t_x"]]
    assert lines == [1]
