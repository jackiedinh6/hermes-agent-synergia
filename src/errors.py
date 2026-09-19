"""One error type carrying the code/hint/remedy envelope every surface returns.

Spec §7.3 / Appendix C. The rule that shapes this module: *an error the model
cannot act on is a wasted turn* — so ``hint`` (the legal values) and ``remedy``
(a runnable command) are **mandatory** on every error, enforced by the
constructor, not by convention.

Exit codes per Appendix C / §8.2: ``1`` failure, ``3`` locked/CAS (retryable),
``4`` unfixable drift. ``0`` and ``2`` never come from an error object —
``0`` is success and ``2`` is argparse's usage error.
"""

from __future__ import annotations

# code -> exit code, exactly the Appendix C table.
_EXIT = {
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

# Module constants so callers reference codes, never retype them.
E_TEAM_NOT_FOUND = "E_TEAM_NOT_FOUND"
E_TEAM_EXISTS = "E_TEAM_EXISTS"
E_TEAM_INVALID = "E_TEAM_INVALID"
E_TEAM_NO_PROFILE = "E_TEAM_NO_PROFILE"
E_TEAM_NO_BOARD = "E_TEAM_NO_BOARD"
E_TEAM_BOARD_TAKEN = "E_TEAM_BOARD_TAKEN"
E_TEAM_PRIMARY_CONFLICT = "E_TEAM_PRIMARY_CONFLICT"
E_TEAM_CAPACITY_CONFLICT = "E_TEAM_CAPACITY_CONFLICT"
E_TEAM_NOT_MEMBER = "E_TEAM_NOT_MEMBER"
E_TEAM_LOCKED = "E_TEAM_LOCKED"
E_TEAM_CONFLICT = "E_TEAM_CONFLICT"
E_TEAM_MANAGED = "E_TEAM_MANAGED"
E_TEAM_NO_BLUEPRINT = "E_TEAM_NO_BLUEPRINT"
E_TEAM_CATALOG_UNAVAILABLE = "E_TEAM_CATALOG_UNAVAILABLE"
E_TEAM_PROFILE_EXISTS = "E_TEAM_PROFILE_EXISTS"
E_TEAM_PROVISION_FAILED = "E_TEAM_PROVISION_FAILED"
E_TEAM_MANAGED_CONFLICT = "E_TEAM_MANAGED_CONFLICT"
E_TEAM_DRIFT_UNFIXED = "E_TEAM_DRIFT_UNFIXED"
E_TEAM_BUSY = "E_TEAM_BUSY"

ALL_CODES = tuple(sorted(_EXIT))


def exit_code_for(code: str) -> int:
    """Exit code of a known error code; unknown codes exit 1 (generic failure)."""
    return _EXIT.get(code, 1)


class TeamError(Exception):
    """A team fault with everything a caller (CLI, tool, dashboard) needs.

    ``hint`` lists the legal values; ``remedy`` is a runnable command. Both are
    mandatory — the constructor raises on an empty one (spec §7.3).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str,
        remedy: str,
        exit_code: int | None = None,
    ) -> None:
        if code not in _EXIT:
            raise ValueError(f"unknown team error code: {code!r}")
        if not (hint or "").strip():
            raise ValueError("TeamError requires a non-empty hint (spec §7.3)")
        if not (remedy or "").strip():
            raise ValueError("TeamError requires a non-empty remedy (spec §7.3)")
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.remedy = remedy
        self.exit_code = exit_code if exit_code is not None else exit_code_for(code)

    def to_envelope(self, team: str | None = None) -> dict:
        """The §7.3 failure envelope, verbatim in key names and nesting."""
        envelope: dict = {"ok": False}
        if team is not None:
            envelope["team"] = team
        envelope["error"] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "remedy": self.remedy,
        }
        return envelope

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class TeamRegistryError(TeamError):
    """Storage fault in the registry file itself (parse/IO, not a domain rule).

    ``storage.load()`` raises this on an unparseable ``teams.yaml``; callers on
    the hot path must fail *open* (spec §5.4), callers on a write path report
    it verbatim.
    """

    def __init__(self, message: str, *, hint: str, remedy: str,
                 code: str = E_TEAM_INVALID, exit_code: int | None = None) -> None:
        super().__init__(code, message, hint=hint, remedy=remedy, exit_code=exit_code)
