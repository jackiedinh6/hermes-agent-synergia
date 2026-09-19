"""S3 — errors.py: Appendix C completeness, exit mapping, mandatory hint/remedy."""

from __future__ import annotations

import pytest

from errors import ALL_CODES, TeamError, TeamRegistryError

# The Appendix C table, transcribed (code -> exit). Spec v1.3 lists 18 codes;
# implement.md's "13" is superseded by the spec.
APPENDIX_C = {
    "E_TEAM_NOT_FOUND": 1,
    "E_TEAM_EXISTS": 1,
    "E_TEAM_INVALID": 1,
    "E_TEAM_NO_PROFILE": 1,
    "E_TEAM_NO_BOARD": 1,
    "E_TEAM_BOARD_TAKEN": 1,
    "E_TEAM_PRIMARY_CONFLICT": 1,
    "E_TEAM_CAPACITY_CONFLICT": 1,
    "E_TEAM_NOT_MEMBER": 1,
    "E_TEAM_LOCKED": 3,
    "E_TEAM_CONFLICT": 3,
    "E_TEAM_MANAGED": 1,
    "E_TEAM_NO_BLUEPRINT": 1,
    "E_TEAM_CATALOG_UNAVAILABLE": 1,
    "E_TEAM_PROFILE_EXISTS": 1,
    "E_TEAM_PROVISION_FAILED": 1,
    "E_TEAM_MANAGED_CONFLICT": 1,
    "E_TEAM_DRIFT_UNFIXED": 4,
    "E_TEAM_BUSY": 3,
}


def test_all_appendix_c_codes_present():
    assert set(ALL_CODES) == set(APPENDIX_C)


@pytest.mark.parametrize(("code", "exit_code"), sorted(APPENDIX_C.items()))
def test_exit_code_mapping(code, exit_code):
    err = TeamError(code, "m", hint="h", remedy="r")
    assert err.exit_code == exit_code


def test_envelope_shape_matches_spec():
    err = TeamError(
        "E_TEAM_NOT_MEMBER",
        "qa-2 is not a member of team project-x",
        hint="Members: agency-orchestrator, qa.",
        remedy="hermes team add-member project-x qa-2",
    )
    env = err.to_envelope(team="project-x")
    assert env == {
        "ok": False,
        "team": "project-x",
        "error": {
            "code": "E_TEAM_NOT_MEMBER",
            "message": "qa-2 is not a member of team project-x",
            "hint": "Members: agency-orchestrator, qa.",
            "remedy": "hermes team add-member project-x qa-2",
        },
    }


def test_envelope_omits_team_when_absent():
    err = TeamError("E_TEAM_NOT_FOUND", "no team alpha", hint="try team list", remedy="hermes team list")
    assert "team" not in err.to_envelope()


@pytest.mark.parametrize("field", ["hint", "remedy"])
def test_missing_hint_or_remedy_raises(field):
    kwargs = {"hint": "h", "remedy": "r"}
    kwargs[field] = ""
    with pytest.raises(ValueError, match=field):
        TeamError("E_TEAM_INVALID", "m", **kwargs)


def test_unknown_code_rejected():
    with pytest.raises(ValueError, match="unknown team error code"):
        TeamError("E_TEAM_MADE_UP", "m", hint="h", remedy="r")


def test_registry_error_is_team_error_with_storage_defaults():
    err = TeamRegistryError("teams.yaml is not valid YAML", hint="h", remedy="r")
    assert isinstance(err, TeamError)
    assert err.code == "E_TEAM_INVALID"
