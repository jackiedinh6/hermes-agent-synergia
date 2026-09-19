"""S1 — pytest twin of ``scripts/verify_substrate.py``.

The same probe, so a substrate bump breaks CI and not just the manual loop.
The script owns the logic (it must stay runnable standalone pre-pytest);
this module loads it by path, runs ``run_checks()`` once at import, and
surfaces every check as its own named test so a failure points straight at
the broken contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_substrate.py"

_spec = importlib.util.spec_from_file_location("_verify_substrate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_vmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vmod)

_RESULTS = _vmod.run_checks()
_ASSERTED = [r for r in _RESULTS if r.asserted]
_NOTICES = [r for r in _RESULTS if not r.asserted]


def test_probe_surface_intact():
    """S1 pins the probe at exactly 50 asserted checks — it must not drift
    silently in either direction (drop coverage, or pad the count). 43 through
    the approvals inbox, plus five attachment-substrate rows and the
    worker-exit guard's two."""
    assert len(_ASSERTED) == 50, (
        f"probe surface drifted: {len(_ASSERTED)} asserted, S1 pins 50")


def _boom():
    raise AssertionError("deliberately broken")


def test_check_harness_records_failures_loudly():
    """A check that reports a failure condition must land as ok=False.

    Failure is signalled by raising, or by returning no pass-detail — under
    the fixed harness neither can ever print as ``ok``.
    """
    results: list = []
    _vmod.check(results, "raising check", _boom)
    _vmod.check(results, "falsy check", lambda: "")
    assert [r.ok for r in results] == [False, False]
    assert "deliberately broken" in results[0].detail
    assert results[0].line().startswith("FAIL")
    assert results[1].line().startswith("FAIL")


@pytest.mark.parametrize("label", [r.label for r in _ASSERTED], ids=[r.label for r in _ASSERTED])
def test_substrate_contract(label):
    result = next(r for r in _ASSERTED if r.label == label)
    assert result.ok, result.detail


def test_notices_render(capsys):
    """Do.8/Do.13 are operator notices: present, printed, never asserted."""
    assert len(_NOTICES) >= 2, "compat-window and catalog notices missing"
    for r in _NOTICES:
        print(r.line())


# ── S4.6 fallback: model.py inlines profile-name normalization ──────────────

@pytest.mark.parametrize("raw", [
    "Default", " DEFAULT ", "Agency-QA", "  spaced-name ", " déjà-vu ".strip(),
])
def test_model_normalize_matches_substrate(raw):
    from hermes_cli import profiles
    import model
    assert model._normalize_profile_name(raw) == profiles.normalize_profile_name(raw)


# ── §5.2.1 "private symbols": provision copies the dist-owned walk ──────────

def test_provision_owned_paths_matches_substrate(hermes_root, tmp_path):
    """``provision._owned_paths`` must agree with ``_owned_entries``.

    The plan preview used to CALL the private symbol. It now reimplements it
    from public inputs, so this is the guard that a substrate change to the
    ownership rules fails in CI instead of silently emptying ``--dry-run``.
    """
    import tempfile
    from pathlib import Path, PurePosixPath
    from hermes_cli import profile_distribution as pdist
    import provision

    source = _vmod.pack_root() / "hermes-agency" / "profiles" / "agency-backend-engineer"
    if not source.is_dir():
        pytest.skip(f"pack root unavailable: {source}")
    with tempfile.TemporaryDirectory(prefix="owned-eq-") as tmp:
        plan = pdist.plan_install(str(source), Path(tmp), override_name="probe-owned")
        theirs = sorted(str(PurePosixPath(*rel))
                        for _, rel in pdist._owned_entries(plan.staged_dir, plan.manifest))
        ours = provision._owned_paths(plan.staged_dir, plan.manifest)
    assert ours == theirs
    assert ours, "the agency pack owns at least SOUL.md + skills"
