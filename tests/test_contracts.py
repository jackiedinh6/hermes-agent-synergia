"""S21 — contract tests (spec §14.5, implement.md S21).

Four guarantees, so the plugin cannot silently break against a substrate
bump or a careless edit:

1. **Schema freeze** — every tool schema of ``schemas.ALL`` hashed (SHA-256
   of ``json.dumps(schema, sort_keys=True)``, the deterministic canonical
   form; descriptions ride along and so do ``parameters``/``required``) and
   compared against the checked-in fixture ``tests/data/schema_hashes.json``.
   A change must be *deliberate*: regenerate with
   ``python team_plugin/scripts/lint_imports.py --update-schema-hashes``,
   review the model-facing diff, commit the fixture. (The two §7.4 spec
   dicts keep their stricter verbatim guard in test_tools.py; S14.)
2. **Schema budget** — §7.5 capped the nine schemas at 1 000 pseudo-tokens
   (``len(json.dumps) // 4``, the G2 formula from test_tools.py);
   ``team_attach_path`` raises the cap to 1 200, asserted *alongside the
   freeze* so an eleventh tool cannot land unnoticed.
   Tool-count resolution (S21): ``tools.TOOL_TABLE`` is derived from
   ``schemas.ALL`` and holds exactly ten tools (whoami, list, show, status,
   catalog, create_task, assign, handoff, sync, attach_path) — matching
   plugin.yaml's ``provides_tools`` and spec §7.4. ``team_dispatch`` / ``team_spawn`` /
   ``team_kanban`` do not exist in this repo's registry or design doc.
3. **``hermes plugins doctor hermes-agent-synergia`` exits 0** — the container gate.
   Tests run on the host (F4: no pytest in the container), so the doctor
   runs via ``docker exec``; without docker or without the running
   ``hermes-agent`` container the test **skips** (host CI has no container;
   the live gate is the same command the Verifier runs by hand).
4. **``hermes plugins compat`` does not list hermes-agent-synergia** — same
   subprocess pattern. Green as of S21b (the vendor fix): S19's
   ``dashboard/plugin_api.py`` imported the ``web_deps`` plugin-compat
   alias for the dashboard's session-token check (a ``restored-def`` with
   no replacement), which is both a compat hit and §5.2.1's banned
   "import a name through a PLUGIN-COMPAT block" pattern; S21b vendored
   the predicate into plugin_api.py. This gate stays because the
   consequence is total — one hit after COMPAT_REMOVAL_DATE and
   PluginManager skips the plugin entirely. The standalone lint
   (``team_plugin/scripts/lint_imports.py``) fails on the same class of
   hit with rc 1.

The lint itself lives in ``scripts/lint_imports.py`` (runnable standalone,
style of ``verify_substrate.py``); this module loads it by path — the
``test_substrate_contract.py`` pattern — so the hash helper has exactly one
implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_agent_synergia import schemas

_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parents[2]
_LINT_SCRIPT = _TESTS.parent / "scripts" / "lint_imports.py"
_FIXTURE = _TESTS / "data" / "schema_hashes.json"
_REGEN = "thirdparty/hermes-agent-synergia/scripts/lint_imports.py --update-schema-hashes"

# Container gate (3./4.): overridable for dev shells with a different name.
_CONTAINER = os.environ.get("HERMES_TEST_CONTAINER", "hermes-agent")


_spec = importlib.util.spec_from_file_location("_lint_imports", _LINT_SCRIPT)
assert _spec is not None and _spec.loader is not None
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)


# ── 1. schema freeze ─────────────────────────────────────────────────────────

def test_schema_freeze_fixture_matches_schemas():
    """Every schema's canonical-JSON SHA-256 must match the checked-in
    fixture — a change to model-facing text must be a fixture diff."""
    assert _FIXTURE.is_file(), (
        f"fixture missing: {_FIXTURE.relative_to(_REPO)} — regenerate with "
        f"`python {_REGEN}`")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    got = _lint.schema_hashes()

    assert len(got) == 10 == len(set(got)), (
        f"schema count drifted: {len(got)} hashed — §7.2's nine plus "
        "team_attach_path")
    assert set(got) == {s["name"] for s in schemas.ALL}

    problems = []
    for name in sorted(set(expected) | set(got)):
        if name not in expected:
            problems.append(f"{name}: NEW tool without a fixture entry")
        elif name not in got:
            problems.append(f"{name}: tool removed but fixture kept it")
        elif got[name] != expected[name]:
            problems.append(f"{name}: schema text changed")
    if problems:
        pytest.fail(
            "schema freeze broken (model-facing text changed):\n  "
            + "\n  ".join(problems)
            + f"\n  if intentional: `python {_REGEN}`, review the diff, "
              "commit the fixture")


# ── 2. budget (§7.5, G2 formula) ────────────────────────────────────────────

def test_budget_under_1200_tokens():
    """Ten schemas, total json-dumps//4 < 1200 — same formula as G2 in
    test_tools.py, asserted beside the freeze so an eleventh tool that slips
    past the count guard still trips here first."""
    n = sum(len(json.dumps(s)) for s in schemas.ALL) // 4
    assert n < 1200, n


# ── container gates (3, 4) ───────────────────────────────────────────────────

def _docker_running() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", _CONTAINER],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


_in_container = pytest.mark.skipif(
    not _docker_running(),
    reason=f"container gate: docker or running {_CONTAINER!r} unavailable "
           f"on this host (live gate: docker exec {_CONTAINER} hermes …)")


def _docker_hermes(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", _CONTAINER, "hermes", *args],
        capture_output=True, text=True, timeout=120)


@_in_container
def test_plugins_doctor_exits_zero():
    r = _docker_hermes("plugins", "doctor", "hermes-agent-synergia")
    assert r.returncode == 0, f"doctor output:\n{r.stdout}{r.stderr}"


@_in_container
def test_plugins_compat_does_not_list_hermes_agent_synergia():
    r = _docker_hermes("plugins", "compat")
    out = r.stdout + r.stderr
    assert "hermes-agent-synergia" not in out, f"compat hit reported:\n{out}"
