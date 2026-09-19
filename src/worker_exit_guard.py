#!/usr/bin/env python3
"""worker_exit_guard.py — make a dead-on-arrival kanban worker exit non-zero.

The substrate's non-quiet single-query path (``cli._run_single_query_mode``)
returns 0 even when the turn never started: ``chat()`` bails out early if
``_ensure_runtime_credentials()`` or ``_init_agent()`` fails, prints the reason,
and returns None. The dispatcher sees a clean exit while the card is still
``running`` and books a *protocol violation* — three of those in a row and the
card is parked with retry guidance that has nothing to do with the real cause
(a missing API key, an unreachable endpoint, a bad model pin).

No plugin hook can change a child process's exit code, so this wrapper is
installed as ``$HERMES_BIN`` by the plugin's ``register()``; the dispatcher
re-reads that variable on every spawn (``kanban_db_dispatch._resolve_hermes_argv``).

What it does, and nothing else:

* Not a dispatcher worker (``HERMES_KANBAN_TASK`` unset), or already in the
  fully-quiet branch (``-Q``, which exits non-zero on its own): ``execv`` the
  real hermes. No extra process, no behaviour change.
* Otherwise: run the real hermes, relay its stdout byte-for-byte (stderr is
  inherited and untouched), and watch for the turn-start marker — the 40-char
  ``─`` rule ``cli_chat_turn_mixin.chat()`` prints only after credentials AND
  agent init have both succeeded. Exit code 0 with no marker means the worker
  never reached the model: exit 1 instead. Every other exit code, including the
  rate-limit sentinel 75, passes through untouched; a worker killed by a
  signal makes the wrapper die of the same signal, so the dispatcher still
  classifies it as ``signaled`` and not as an ordinary non-zero exit.

Signals are forwarded to the child, and the child gets ``PR_SET_PDEATHSIG`` on
Linux so the dispatcher's ``SIGTERM`` → ``SIGKILL`` escalation on this wrapper's
pid cannot leave an orphaned hermes behind.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys

#: Printed by ``hermes_cli/cli_chat_turn_mixin.py`` immediately before the turn
#: runs — i.e. only once the session really has credentials and an agent.
MARKER = "─" * 40
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
_SETUP_FAILED_EXIT = 1
_READ_SIZE = 65536


def _real_argv() -> list:
    """The real hermes invocation: ``$HERMES_REAL_BIN`` (set by the plugin when
    it installed this wrapper), else ``hermes`` on PATH, else the module form —
    the same ladder as ``_resolve_hermes_argv``, minus the wrapper itself."""
    env_bin = (os.environ.get("HERMES_REAL_BIN") or "").strip()
    if env_bin:
        if os.sep in env_bin or (os.altsep and os.altsep in env_bin):
            return [os.path.abspath(env_bin)]
        found = shutil.which(env_bin)
        if found:
            return [found]
    found = shutil.which("hermes")
    if found and os.path.realpath(found) != os.path.realpath(__file__):
        return [found]
    return [sys.executable, "-m", "hermes_cli.main"]


def _pdeathsig():
    """Best-effort: die with the wrapper, so a SIGKILL here cannot orphan the
    worker. Linux only; a failure is not worth losing the run over."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _die_by_signal(signum: int) -> int:
    """Die of *signum* ourselves, so the dispatcher's ``os.WIFSIGNALED`` check
    still classifies the worker as ``signaled`` (an ordinary exit code would
    read as ``nonzero_exit`` and book a failure instead)."""
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        signal.pause()
    except Exception:
        pass
    return 128 + signum


def _guard(argv: list) -> int:
    marker = MARKER.encode("utf-8")
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=None,
        preexec_fn=_pdeathsig if sys.platform.startswith("linux") else None)

    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass

    out = sys.stdout.buffer
    seen = False
    tail = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read1(_READ_SIZE) if hasattr(proc.stdout, "read1") \
            else proc.stdout.read(_READ_SIZE)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        if not seen:
            # The marker can straddle a read boundary; keep just enough tail.
            window = _ANSI.sub(b"", tail + chunk)
            seen = marker in window
            tail = window[-len(marker):]
    proc.stdout.close()
    code = proc.wait()
    if code == 0 and not seen:
        return _SETUP_FAILED_EXIT
    if code < 0:
        _die_by_signal(-code)
    return code


# ── installation (called by register(), never by the script path) ────────────

#: ``$HERMES_BIN`` the dispatcher reads, and where the wrapper stashes the real
#: target it chains to.
GUARD_ENV = "HERMES_BIN"
REAL_ENV = "HERMES_REAL_BIN"


def guard_path() -> str:
    """An executable copy of this wrapper. The shipped file normally is one
    (mode 0755 in git); a checkout or bind mount that dropped the bit gets a
    0755 copy under the plugin's own data dir instead."""
    here = os.path.abspath(__file__)
    if os.access(here, os.X_OK):
        return here
    from . import storage
    dest_dir = storage.registry_dir() / "bin"
    dest = dest_dir / os.path.basename(here)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = open(here, "rb").read()
    if not dest.is_file() or dest.read_bytes() != src:
        dest.write_bytes(src)
    dest.chmod(0o755)
    return str(dest)


def install(mode: str = "wrapper") -> str:
    """Point ``$HERMES_BIN`` at this wrapper for dispatcher spawns. Returns the
    installed path, or "" when the guard is off or unavailable.

    Idempotent: a worker that loads the plugin itself sees ``$HERMES_BIN``
    already pointing here and leaves both variables alone, so wrappers never
    chain. Best-effort — a failure here must not cost the plugin its load.
    """
    if str(mode or "").strip().lower() != "wrapper":
        return ""
    try:
        path = guard_path()
        current = (os.environ.get(GUARD_ENV) or "").strip()
        if current and os.path.realpath(current) == os.path.realpath(path):
            return path
        if current:
            os.environ[REAL_ENV] = current
        elif not (os.environ.get(REAL_ENV) or "").strip():
            found = shutil.which("hermes")
            if found:
                os.environ[REAL_ENV] = found
        os.environ[GUARD_ENV] = path
        return path
    except Exception:                              # noqa: BLE001 — guard is optional
        return ""


def main(argv: list) -> int:
    real = _real_argv()
    args = argv[1:]
    if not os.environ.get("HERMES_KANBAN_TASK") or "-Q" in args:
        cmd = real + args
        os.execv(cmd[0], cmd)                      # no return
    return _guard(real + args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
