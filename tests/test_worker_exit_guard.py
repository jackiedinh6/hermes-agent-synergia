"""The worker exit guard (``src/worker_exit_guard.py``).

The wrapper exists for one reason: upstream's non-quiet single-query path
returns 0 when credentials or agent init fail, so the dispatcher books a
*protocol violation* (clean exit while the card is still ``running``) and
spends one of its three streak slots on misleading retry guidance. The
wrapper turns that one case into rc 1 and must change nothing else — hence a
test per exit shape, driven by a fake hermes so no model, key or board is
involved.

The wrapper is run as a script (the way the dispatcher runs it), with
``$HERMES_REAL_BIN`` pointing at the fake; ``install()`` is exercised in-process
because it only edits ``os.environ``.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys

import pytest

_GUARD = pathlib.Path(__file__).resolve().parents[1] / "src" / "worker_exit_guard.py"
_MARKER = "─" * 40


def _fake_hermes(tmp_path, body: str) -> pathlib.Path:
    """A stand-in for the real hermes binary: a python script that prints and
    exits however the test needs."""
    p = tmp_path / "fake-hermes"
    p.write_text(f"#!/usr/bin/env python3\nimport os, signal, sys\n{body}\n",
                 encoding="utf-8")
    p.chmod(0o755)
    return p


def _run(fake, *args, task: str | None = "card-1", extra_env=None):
    env = dict(os.environ)
    env["HERMES_REAL_BIN"] = str(fake)
    env.pop("HERMES_KANBAN_TASK", None)
    if task:
        env["HERMES_KANBAN_TASK"] = task
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(_GUARD), *args],
                          capture_output=True, env=env, timeout=60)


# ── the one case the guard exists for ────────────────────────────────────────

def test_clean_exit_without_the_marker_becomes_rc_1(tmp_path):
    """No turn marker and rc 0 = the worker never reached the model."""
    fake = _fake_hermes(tmp_path, 'print("No API key configured"); sys.exit(0)')
    r = _run(fake, "--cli", "chat", "-q", "work kanban task card-1")
    assert r.returncode == 1
    assert b"No API key configured" in r.stdout


def test_marker_present_keeps_rc_0(tmp_path):
    fake = _fake_hermes(tmp_path, f'print({_MARKER!r}); print("done"); sys.exit(0)')
    r = _run(fake, "--cli", "chat", "-q", "work kanban task card-1")
    assert r.returncode == 0
    assert r.stdout.decode().splitlines() == [_MARKER, "done"]


def test_marker_split_across_reads_is_still_seen(tmp_path):
    """The marker may straddle a pipe read; the tail window must catch it."""
    fake = _fake_hermes(tmp_path, "\n".join([
        "big = 65536 - 20",
        'sys.stdout.write("x" * big)',
        'sys.stdout.write(%r)' % _MARKER,
        "sys.stdout.flush()",
        "sys.exit(0)",
    ]))
    r = _run(fake, "--cli", "chat", "-q", "work")
    assert r.returncode == 0


def test_marker_behind_ansi_colour_is_still_seen(tmp_path):
    """Rich prints the rule coloured; matching happens on stripped bytes."""
    fake = _fake_hermes(tmp_path, "\n".join([
        'sys.stdout.write("\\x1b[38;2;0;255;0m" + %r + "\\x1b[0m\\n")' % _MARKER,
        "sys.exit(0)",
    ]))
    r = _run(fake, "--cli", "chat", "-q", "work")
    assert r.returncode == 0
    assert b"\x1b[38;2;0;255;0m" in r.stdout        # relayed unchanged


# ── everything else passes through ───────────────────────────────────────────

@pytest.mark.parametrize("code", [1, 2, 3, 75, 200])
def test_nonzero_exit_codes_pass_through(tmp_path, code):
    """Including 75, the rate-limit sentinel the dispatcher must keep seeing."""
    fake = _fake_hermes(tmp_path, f"sys.exit({code})")
    assert _run(fake, "--cli", "chat").returncode == code


def test_signal_death_stays_signal_death(tmp_path):
    """The dispatcher classifies workers with ``os.WIFSIGNALED``; an exit code
    would read as ``nonzero_exit`` and book a failure."""
    fake = _fake_hermes(tmp_path,
                        "os.kill(os.getpid(), signal.SIGTERM)\nsignal.pause()")
    r = _run(fake, "--cli", "chat")
    assert r.returncode == -signal.SIGTERM


def test_stdout_is_relayed_byte_for_byte(tmp_path):
    blob = "".join(chr(c) for c in range(32, 127)) + "─ ✓ é\n"
    fake = _fake_hermes(tmp_path, "\n".join([
        f"sys.stdout.write({blob!r} * 700)",
        "sys.stdout.flush()",
        "sys.exit(3)",
    ]))
    r = _run(fake, "--cli", "chat")
    assert r.returncode == 3
    assert r.stdout == (blob * 700).encode("utf-8")


def test_stderr_is_inherited_not_reframed(tmp_path):
    fake = _fake_hermes(tmp_path, "\n".join([
        'sys.stderr.write("credential check failed\\n")',
        "sys.exit(0)",
    ]))
    r = _run(fake, "--cli", "chat")
    assert r.returncode == 1
    assert r.stderr == b"credential check failed\n"


# ── the fast path: not a worker, or already quiet ─────────────────────────────

def test_no_kanban_task_execs_straight_through(tmp_path):
    """A human/gateway invocation must not gain a process or a changed rc."""
    fake = _fake_hermes(tmp_path, 'print("no marker here"); sys.exit(0)')
    r = _run(fake, "--cli", "chat", task=None)
    assert r.returncode == 0
    assert r.stdout == b"no marker here\n"


def test_quiet_flag_execs_straight_through(tmp_path):
    """``-Q`` already exits non-zero on setup failure; nothing to guard."""
    fake = _fake_hermes(tmp_path, 'print("quiet"); sys.exit(0)')
    r = _run(fake, "-Q", "--cli", "chat")
    assert r.returncode == 0


def test_arguments_reach_the_real_binary_unchanged(tmp_path):
    fake = _fake_hermes(tmp_path, "\n".join([
        'print("|".join(sys.argv[1:]))',
        f'print({_MARKER!r})',
        "sys.exit(0)",
    ]))
    args = ["-p", "qa-2", "--cli", "--accept-hooks", "chat", "-q",
            "work kanban task card-1"]
    r = _run(fake, *args)
    assert r.returncode == 0
    assert r.stdout.decode().splitlines()[0] == "|".join(args)


# ── install(): what register() calls ─────────────────────────────────────────

@pytest.fixture
def guard_mod():
    from hermes_agent_synergia import worker_exit_guard
    return worker_exit_guard


def test_install_points_hermes_bin_at_the_wrapper(guard_mod, monkeypatch):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("HERMES_REAL_BIN", raising=False)
    path = guard_mod.install("wrapper")
    assert path and os.access(path, os.X_OK)
    assert os.environ["HERMES_BIN"] == path


def test_install_remembers_a_preexisting_hermes_bin(guard_mod, monkeypatch, tmp_path):
    real = tmp_path / "hermes"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.setenv("HERMES_BIN", str(real))
    monkeypatch.delenv("HERMES_REAL_BIN", raising=False)
    path = guard_mod.install("wrapper")
    assert os.environ["HERMES_REAL_BIN"] == str(real)
    assert os.environ["HERMES_BIN"] == path


def test_install_is_idempotent_so_wrappers_never_chain(guard_mod, monkeypatch):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("HERMES_REAL_BIN", raising=False)
    first = guard_mod.install("wrapper")
    monkeypatch.setenv("HERMES_REAL_BIN", "/usr/bin/hermes")
    second = guard_mod.install("wrapper")
    assert second == first
    assert os.environ["HERMES_REAL_BIN"] == "/usr/bin/hermes"   # untouched


def test_install_off_changes_nothing(guard_mod, monkeypatch):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    assert guard_mod.install("off") == ""
    assert "HERMES_BIN" not in os.environ


def test_shipped_wrapper_is_executable():
    """compose mounts ``src/`` read-only into the container, so the mode bit in
    git is the only thing that makes ``$HERMES_BIN`` runnable there."""
    assert os.access(_GUARD, os.X_OK), f"{_GUARD} lost its 0755 mode bit"
