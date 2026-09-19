"""Busy guard: a team change refuses, stops or waits while team cards run."""

from __future__ import annotations

import subprocess
import time

import pytest

import reconcile
import resolve
import storage
from test_cli_registry import (  # noqa: F401 — fixtures
    W, _fresh_storage, demo, parser, run, team_profiles,
)


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _card(board, *, assignee=None, status="running", pid=None, lock=None):
    kb = _kb()
    with resolve._kanban_conn().connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title=f"card-{assignee}", assignee=assignee,
                             initial_status="running")
        conn.execute("UPDATE tasks SET status = ?, worker_pid = ?, claim_lock = ?"
                     " WHERE id = ?", (status, pid, lock, tid))
        conn.commit()
    return tid


def _set_status(board, tid, status):
    with resolve._kanban_conn().connect_closing(board=board) as conn:
        conn.execute("UPDATE tasks SET status = ?, worker_pid = NULL,"
                     " claim_lock = NULL WHERE id = ?", (status, tid))
        conn.commit()


def _status(board, tid):
    with resolve._kanban_conn().connect_closing(board=board) as conn:
        return _kb().get_task(conn, tid).status


@pytest.fixture
def worker():
    """A live host-local 'worker' process and a claim lock that owns it."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc, _kb()._host_prefix() + "busy-test"
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


# ── resolve helpers ─────────────────────────────────────────────────────────

def test_running_cards_filters_board_and_assignee(hermes_root):
    a = _card("b1", assignee="a")
    b = _card("b1", assignee="b")
    _card("b1", assignee="a", status="ready")
    _card("b2", assignee="a")
    assert {c["id"] for c in resolve.running_cards("b1")} == {a, b}
    assert [c["id"] for c in resolve.running_cards("b1", "a")] == [a]


def test_running_cards_missing_board_mints_nothing(hermes_root):
    assert resolve.running_cards("ghost-board") == []
    assert not _kb().board_dir("ghost-board").exists()


def test_stop_cards_blocks_then_kills_worker(hermes_root, worker):
    proc, lock = worker
    tid = _card("b1", assignee="a", pid=proc.pid, lock=lock)
    idle = _card("b1", assignee="a", status="ready")
    out = resolve.stop_cards("b1", [tid, idle], "stopped by test")
    assert out == {"stopped": [tid], "not_killed": []}
    assert _status("b1", tid) == "blocked"
    assert _status("b1", idle) == "ready"
    assert proc.wait(timeout=5) is not None


def test_stop_cards_never_signals_another_hosts_worker(hermes_root, worker):
    proc, _lock = worker
    tid = _card("b1", assignee="a", pid=proc.pid, lock="other-host:1:x")
    out = resolve.stop_cards("b1", [tid], "stopped by test")
    assert out == {"stopped": [tid], "not_killed": [tid]}
    assert proc.poll() is None


def test_resume_cards_returns_stopped_card_to_ready(hermes_root):
    tid = _card("b1", assignee="a")
    resolve.stop_cards("b1", [tid], "stopped by test")
    assert resolve.resume_cards("b1", [tid]) == [tid]
    assert _status("b1", tid) == "ready"
    assert resolve.resume_cards("b1", [tid]) == []      # no longer blocked


def test_would_write_honours_only(hermes_root, demo):
    team = storage.load().teams["demo"]
    assert reconcile.would_write(team, only=frozenset({"no.such_check"})) is False


# ── destroy ─────────────────────────────────────────────────────────────────

def test_destroy_refuses_while_a_card_runs(hermes_root, demo, parser, capsys):
    tid = _card("demo", assignee="qa")
    code, env, _ = run(parser, "destroy", "demo", "--yes", "--json", capsys=capsys)
    assert code == 3
    err = env["error"]
    assert err["code"] == "E_TEAM_BUSY"
    assert tid in err["message"]
    assert "--on-busy stop" in err["remedy"]
    assert "demo" in storage.load().teams
    assert _status("demo", tid) == "running"


def test_destroy_stop_kills_the_worker_then_destroys(hermes_root, demo, parser,
                                                     capsys, worker):
    proc, lock = worker
    tid = _card("demo", assignee="qa", pid=proc.pid, lock=lock)
    code, env, _ = run(parser, "destroy", "demo", "--yes", "--on-busy", "stop",
                       "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["stopped"] == [tid]
    assert "demo" not in storage.load().teams
    assert _status("demo", tid) == "blocked"
    assert proc.wait(timeout=5) is not None


def test_destroy_wait_applies_once_the_card_finishes(hermes_root, demo, parser,
                                                     capsys, monkeypatch):
    tid = _card("demo", assignee="qa")
    polls = []

    def _card_finishes(_seconds):
        polls.append(_seconds)
        _set_status("demo", tid, "done")

    monkeypatch.setattr(time, "sleep", _card_finishes)
    code, env, err = run(parser, "destroy", "demo", "--yes", "--on-busy", "wait",
                         "--json", capsys=capsys)
    assert code == 0
    assert polls == [5]
    assert "waiting for 1 running card(s)" in err
    assert "stopped" not in env["data"]
    assert "demo" not in storage.load().teams


def test_destroy_wait_times_out(hermes_root, demo, parser, capsys):
    _card("demo", assignee="qa")
    code, env, _ = run(parser, "destroy", "demo", "--yes", "--on-busy", "wait",
                       "--wait-timeout", "0", "--json", capsys=capsys)
    assert code == 3
    assert "still busy after 0s" in env["error"]["message"]
    assert "demo" in storage.load().teams


# ── remove-member ───────────────────────────────────────────────────────────

def test_remove_member_counts_only_that_members_cards(hermes_root, demo, parser,
                                                      capsys):
    run(parser, "add-member", "demo", "agency-dev", "--json", capsys=capsys)
    _card("demo", assignee="qa")
    code, _env, _ = run(parser, "remove-member", "demo", "agency-dev", "--json",
                        capsys=capsys)
    assert code == 0


def test_remove_member_refuses_then_stops(hermes_root, demo, parser, capsys):
    run(parser, "add-member", "demo", "agency-dev", "--json", capsys=capsys)
    tid = _card("demo", assignee="agency-dev")
    code, env, _ = run(parser, "remove-member", "demo", "agency-dev", "--json",
                       capsys=capsys)
    assert code == 3 and env["error"]["code"] == "E_TEAM_BUSY"
    assert "agency-dev" in {m.profile for m in storage.load().teams["demo"].members}
    code, env, _ = run(parser, "remove-member", "demo", "agency-dev",
                       "--on-busy", "stop", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["stopped"] == [tid]
    assert _status("demo", tid) == "blocked"


# ── set-workdir / sync: stop, apply, resume ─────────────────────────────────

def test_set_workdir_refuses_then_stops_and_resumes(hermes_root, demo, parser,
                                                    capsys, W):
    tid = _card("demo", assignee="qa")
    before = storage.load().teams["demo"].workdir
    code, env, _ = run(parser, "set-workdir", "demo", W("moved"), "--apply",
                       "--json", capsys=capsys)
    assert code == 3 and env["error"]["code"] == "E_TEAM_BUSY"
    assert storage.load().teams["demo"].workdir == before
    code, env, _ = run(parser, "set-workdir", "demo", W("moved"), "--apply",
                       "--on-busy", "stop", "--json", capsys=capsys)
    assert code in (0, 4)
    assert env["data"]["stopped"] == [tid]
    assert env["data"]["resumed"] == [tid]
    assert storage.load().teams["demo"].workdir == W("moved")
    assert _status("demo", tid) == "ready"


def test_set_workdir_without_apply_is_not_guarded(hermes_root, demo, parser,
                                                  capsys, W):
    _card("demo", assignee="qa")
    code, _env, _ = run(parser, "set-workdir", "demo", W("moved"), "--json",
                        capsys=capsys)
    assert code == 0


def test_sync_apply_guarded_only_when_it_would_write(hermes_root, demo, parser,
                                                     capsys, monkeypatch):
    tid = _card("demo", assignee="qa")
    monkeypatch.setattr(reconcile, "would_write", lambda *a, **k: False)
    code, _env, _ = run(parser, "sync", "demo", "--apply", "--json", capsys=capsys)
    assert code != 3
    monkeypatch.setattr(reconcile, "would_write", lambda *a, **k: True)
    code, env, _ = run(parser, "sync", "demo", "--apply", "--json", capsys=capsys)
    assert code == 3 and env["error"]["code"] == "E_TEAM_BUSY"
    code, env, _ = run(parser, "sync", "demo", "--apply", "--on-busy", "stop",
                       "--json", capsys=capsys)
    assert code != 3
    assert env["data"]["stopped"] == [tid]
    assert env["data"]["resumed"] == [tid]
    assert _status("demo", tid) == "ready"


def test_sync_dry_run_never_guarded(hermes_root, demo, parser, capsys,
                                    monkeypatch):
    _card("demo", assignee="qa")
    monkeypatch.setattr(reconcile, "would_write", lambda *a, **k: True)
    code, _env, _ = run(parser, "sync", "demo", "--json", capsys=capsys)
    assert code == 0


def test_sync_all_skips_a_busy_team(hermes_root, demo, parser, capsys,
                                    monkeypatch):
    _card("demo", assignee="qa")
    monkeypatch.setattr(reconcile, "would_write", lambda *a, **k: True)
    code, env, _ = run(parser, "sync", "--all", "--apply", "--json", capsys=capsys)
    assert code == 3
    assert env["data"]["teams"] == []
    assert [s["team"] for s in env["data"]["skipped_busy"]] == ["demo"]
