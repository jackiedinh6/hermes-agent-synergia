"""S12 — policy.py: the §6.2 table, exhaustively.

The plan calls the table-driven matrix the single most important test in the
suite: every row of §6.2 × {off, advise, enforce}, asserted exactly (patch
dict equality / block-message content), so a re-ordered row or a silent
semantic drift fails here first. Plus:

* §6.3 resolution order — each of the five steps wins where it should, and
  every ambiguity path returns ``None``;
* the four NEVER_GUARDED tools return ``None`` under every mode and every
  team state (the §6.2 hard rule, asserted not commented);
* every emitted directive merges shallow — whole top-level keys only;
* purity: the AST of policy.py imports nothing but ``__future__``/``typing``
  (the §5.2 layer rule S24 lints, pre-linted here).

Pure unit tests: no hermes_root fixture, no substrate, no I/O.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import model
from hermes_agent_synergia import policy

MODES = ("off", "advise", "enforce")


def team(**over) -> model.Team:
    d = {
        "slug": "project-x",
        "board": "project-x",
        "tenant": "project-x",
        "orchestrator": "orch-1",
        "members": [
            {"profile": "orch-1", "role": "orchestrator", "primary": True},
            {"profile": "dev-1", "role": "implementer"},
        ],
    }
    d.update(over)
    return model.Team.from_dict(d)


def reg(*teams: model.Team) -> model.Registry:
    return model.Registry(teams={t.slug: t for t in teams})


# ── §6.2 matrix ──────────────────────────────────────────────────────────────
# One case per table row (row 5 counted once per tool); each case's args are
# neutralised on every other row so the assertion pins ONE row. ``modes``
# maps mode -> None | {"modify": exact-patch} | {..., "note": True} |
# {"block": [required substrings]}.

def modify(patch, note=False):
    return {"modify": patch, "note": note}


def block(needles):
    return {"block": needles}


MATRIX = [
    # Row 1: kanban_create, board absent or ≠ team.board → modify{board}.
    {
        "id": "create-board-wrong",
        "tool": "kanban_create",
        "args": {"title": "t", "board": "default",
                 "tenant": "project-x", "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"board": "project-x"}),
           "enforce": modify({"board": "project-x"})},
    },
    {
        "id": "create-board-absent",
        "tool": "kanban_create",
        "args": {"title": "t", "tenant": "project-x", "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"board": "project-x"}),
           "enforce": modify({"board": "project-x"})},
    },
    # Row 2: tenant absent and stamping on → modify{tenant}.
    {
        "id": "create-tenant-absent",
        "tool": "kanban_create",
        "args": {"title": "t", "board": "project-x", "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"tenant": "project-x"}),
           "enforce": modify({"tenant": "project-x"})},
    },
    # Row 2 complement: tenant_stamping: never ⇒ the tenant row does not fire.
    {
        "id": "create-tenant-never-stamped",
        "tool": "kanban_create",
        "team_over": {"tenant_stamping": "never"},
        "args": {"title": "t", "board": "project-x", "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Row 3: assignee set and ∉ members — advise rewrites + note, enforce
    # refuses (the message carries team, violation, legal values, remedy).
    {
        "id": "create-assignee-outsider",
        "tool": "kanban_create",
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "some-outsider"},
        "modes": {"off": None,
                  "advise": modify({"assignee": "orch-1"}, note=True),
                  "enforce": block(["project-x", "some-outsider",
                                    "not a member",
                                    "dev-1, orch-1",
                                    "team add-member"])},
    },
    # Row 4: assignee absent → modify{assignee: orchestrator}, both modes.
    {
        "id": "create-assignee-absent",
        "tool": "kanban_create",
        "args": {"title": "t", "board": "project-x", "tenant": "project-x"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"assignee": "orch-1"}),
           "enforce": modify({"assignee": "orch-1"})},
    },
    # Row 4b: no workspace args on a worktree team → modify{workspace}.
    {
        "id": "create-workspace-worktree",
        "tool": "kanban_create",
        "team_over": {"workspace_policy": "worktree", "workdir": "/w/px"},
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"workspace_kind": "worktree",
                             "workspace_path": "/w/px"}),
           "enforce": modify({"workspace_kind": "worktree",
                              "workspace_path": "/w/px"})},
    },
    {
        "id": "create-workspace-scratch",
        "tool": "kanban_create",
        "team_over": {"workspace_policy": "scratch", "workdir": "/w/px"},
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Row 4b: a relative workdir is never sent as a path (the substrate
    # rejects it); the kind alone falls back to the board default_workdir.
    {
        "id": "create-workspace-relative-workdir",
        "tool": "kanban_create",
        "team_over": {"workspace_policy": "worktree", "workdir": "w/px"},
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "orch-1"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"workspace_kind": "worktree"}),
           "enforce": modify({"workspace_kind": "worktree"})},
    },
    # Row 4b complements: an explicit workspace or project always wins.
    {
        "id": "create-workspace-explicit",
        "tool": "kanban_create",
        "team_over": {"workspace_policy": "worktree", "workdir": "/w/px"},
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "orch-1", "workspace_kind": "scratch"},
        "modes": dict.fromkeys(MODES, None),
    },
    {
        "id": "create-workspace-project",
        "tool": "kanban_create",
        "team_over": {"workspace_policy": "worktree", "workdir": "/w/px"},
        "args": {"title": "t", "board": "project-x", "tenant": "project-x",
                 "assignee": "orch-1", "project": "px"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Row 5: kanban_link / kanban_unblock foreign board — advise rewrites,
    # enforce refuses (no correct rewrite under enforce).
    {
        "id": "link-board-wrong",
        "tool": "kanban_link",
        "args": {"task": "t-1", "parent": "t-2", "board": "default"},
        "modes": {"off": None,
                  "advise": modify({"board": "project-x"}),
                  "enforce": block(["project-x", "default", "board"])},
    },
    {
        "id": "unblock-board-wrong",
        "tool": "kanban_unblock",
        "args": {"task": "t-1", "board": "default"},
        "modes": {"off": None,
                  "advise": modify({"board": "project-x"}),
                  "enforce": block(["project-x", "default", "board"])},
    },
    # Row 5 complement: right board ⇒ the row cannot fire at all.
    {
        "id": "link-board-right",
        "tool": "kanban_link",
        "args": {"task": "t-1", "parent": "t-2", "board": "project-x"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Row 6: kanban_list foreign board → modify{board} in BOTH modes
    # (a read is never worth a retry loop).
    {
        "id": "list-board-wrong",
        "tool": "kanban_list",
        "args": {"board": "default"},
        "modes": dict.fromkeys(MODES, None)
        | {"advise": modify({"board": "project-x"}),
           "enforce": modify({"board": "project-x"})},
    },
    # Row 7: team_* are never guarded — they validate internally. A foreign
    # board in their args must not trigger a rewrite.
    {
        "id": "team-tool-unguarded",
        "tool": "team_create_task",
        "args": {"board": "default", "title": "t"},
        "modes": dict.fromkeys(MODES, None),
    },
    {
        "id": "team-catalog-unguarded",
        "tool": "team_catalog",
        "args": {"board": "default"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Row 8: everything else → None.
    {
        "id": "other-tool-unguarded",
        "tool": "web_search",
        "args": {"query": "q", "board": "default"},
        "modes": dict.fromkeys(MODES, None),
    },
    # Table-order probe (documented semantics): under enforce, an outsider
    # assignee refuses even when a modify row also matched — refuse where no
    # correct rewrite exists; under advise the rewrites merge into ONE
    # directive, exactly like the §5.6 board+tenant example.
    {
        "id": "create-board-and-outsider",
        "tool": "kanban_create",
        "args": {"title": "t", "board": "default", "assignee": "rando"},
        "modes": {"off": None,
                  "advise": modify({"board": "project-x",
                                    "tenant": "project-x",
                                    "assignee": "orch-1"}, note=True),
                  "enforce": block(["rando", "not a member"])},
    },
]


def _flat_params():
    out = []
    for case in MATRIX:
        for mode in MODES:
            out.append(pytest.param(case, mode,
                                     id=f"{case['id']}::{mode}"))
    return out


@pytest.mark.parametrize("case,mode", _flat_params())
def test_decision_matrix(case, mode):
    """§6.2, every row × {off, advise, enforce} — the spec's 'single most
    important test in the suite'."""
    t = team(policy={"mode": mode}, **(case.get("team_over") or {}))
    directive = policy.decide(t, "orch-1", case["tool"], dict(case["args"]))
    want = case["modes"][mode]

    if want is None:
        assert directive is None
        return
    assert directive is not None, f"expected a directive for {mode}"

    if "block" in want:
        assert set(directive) == {"block"}, directive
        message = directive["block"]
        assert isinstance(message, str) and message
        for needle in want["block"]:
            assert needle in message, f"block message lacks {needle!r}: {message}"
        return

    assert directive["modify"] == want["modify"], directive
    expected_keys = {"modify"} | ({"note"} if want.get("note") else set())
    assert set(directive) == expected_keys, directive
    if want.get("note"):
        assert isinstance(directive["note"], str) and directive["note"]
        assert "team" in directive["note"]  # §5.6: the note names the team too


# ── §6.3 resolution order ────────────────────────────────────────────────────

ALPHA = team(slug="alpha", board="alpha", tenant="tenant-a")
BETA = team(slug="beta", board="beta", tenant="tenant-b")


def test_step1_tenant_wins_over_board_team_and_primary():
    # env points steps 2-3 at alpha and alpha holds the ONLY primary,
    # yet the tenant (step 1) is authoritative: beta wins.
    beta_plain = BETA.from_dict({**BETA.to_dict(),
                                 "members": [{"profile": "orch-1"}]})
    r = reg(ALPHA, beta_plain)
    env = {"HERMES_TENANT": "tenant-b",
           "HERMES_KANBAN_BOARD": "alpha",
           "HERMES_TEAM": "alpha"}
    assert policy.resolve_team("orch-1", env, r) == "beta"


def test_step1_empty_tenant_is_unset():
    assert policy.resolve_team("orch-1", {"HERMES_TENANT": ""},
                               _reg_beta_primary()) == "beta"


def _reg_beta_primary():
    """alpha and beta both contain orch-1; ONLY beta has it primary."""
    alpha_plain = ALPHA.from_dict({**ALPHA.to_dict(),
                                   "members": [{"profile": "orch-1"}]})
    beta = BETA.from_dict({**BETA.to_dict(),
                           "members": [
                               {"profile": "orch-1",
                                "role": "orchestrator", "primary": True},
                               {"profile": "dev-1"}]})
    return reg(alpha_plain, beta)


def test_step2_board_owner_when_no_tenant():
    r = reg(ALPHA, BETA)
    env = {"HERMES_KANBAN_BOARD": "beta", "HERMES_TEAM": "alpha"}
    assert policy.resolve_team("orch-1", env, r) == "beta"


def test_step3_team_env_when_no_tenant_or_board():
    r = reg(ALPHA, BETA)
    assert policy.resolve_team("nobody", {"HERMES_TEAM": "beta"}, r) == "beta"


def test_step4_primary_team_when_no_env():
    beta_primary = BETA.from_dict({
        **BETA.to_dict(),
        "members": [{"profile": "shared-1", "primary": True}]})
    alpha_plain = ALPHA.from_dict({
        **ALPHA.to_dict(),
        "members": [{"profile": "shared-1"}]})
    assert policy.resolve_team("shared-1", {},
                               reg(alpha_plain, beta_primary)) == "beta"


def test_step5_ambiguous_no_env_no_primary():
    # The spec's named case: two teams, no env vars, no primary ⇒ None.
    alpha_plain = ALPHA.from_dict({**ALPHA.to_dict(),
                                   "members": [{"profile": "orch-1"}]})
    beta_plain = BETA.from_dict({**BETA.to_dict(),
                                 "members": [{"profile": "orch-1"}]})
    assert policy.resolve_team("orch-1", {}, reg(alpha_plain, beta_plain)) is None


def test_step5_member_without_primary_is_none():
    one = ALPHA.from_dict({**ALPHA.to_dict(), "members": [{"profile": "solo"}]})
    assert policy.resolve_team("solo", {}, reg(one)) is None


@pytest.mark.parametrize("env", [
    {"HERMES_KANBAN_BOARD": "shared"},   # two shared_board teams own it
    {"HERMES_TENANT": "same"},           # two teams claim one tenant
    {"HERMES_TENANT": "ghost"},          # names no team, and nothing below
])
def test_ambiguous_or_unknown_env_is_none(env):
    t1 = team(slug="s1", board="shared", tenant="same",
              members=[{"profile": "p1", "primary": False}])
    t2 = team(slug="s2", board="shared", tenant="same",
              members=[{"profile": "p1", "primary": False}])
    assert policy.resolve_team("p1", env, reg(t1, t2)) is None


def test_tenant_env_naming_nothing_falls_through_to_primary():
    alpha_plain = ALPHA.from_dict({**ALPHA.to_dict(),
                                   "members": [{"profile": "orch-1"}]})
    beta_primary = BETA.from_dict({
        **BETA.to_dict(),
        "members": [{"profile": "orch-1", "primary": True}]})
    assert policy.resolve_team("orch-1", {"HERMES_TENANT": "no-such"},
                               reg(alpha_plain, beta_primary)) == "beta"


def test_resolve_accepts_plain_mapping_registry():
    # reg is duck-typed: a bare slug→Team mapping works as well as a Registry.
    assert policy.resolve_team("orch-1", {"HERMES_TEAM": "beta"},
                               {"beta": BETA, "alpha": ALPHA}) == "beta"


def test_resolve_no_profile_no_env_is_none():
    assert policy.resolve_team(None, {}, reg(ALPHA, BETA)) is None


# ── §6.2 hard rule 1: never-guarded tools ────────────────────────────────────

@pytest.mark.parametrize("tool", sorted(policy.NEVER_GUARDED))
@pytest.mark.parametrize("mode", MODES)
def test_never_guarded_tools_return_none(tool, mode):
    """terminal / write_file / patch / execute_code: ``None`` under every
    mode, with maximally hostile args (foreign board, outsider assignee,
    absent tenant) so the assertion proves the gate, not the data."""
    t = team(policy={"mode": mode})
    hostile = {"command": "sqlite3 /root/.hermes/kanban.db",
               "board": "default", "tenant": "", "assignee": "some-outsider",
               "path": "/etc/shadow", "patch": "--- a\n+++ b"}
    assert policy.decide(t, "orch-1", tool, dict(hostile)) is None


def test_never_guarded_under_off_team_and_unresolved_session():
    off = team(policy={"mode": "off"})
    for tool in sorted(policy.NEVER_GUARDED):
        assert policy.decide(off, "orch-1", tool, {}) is None
        assert policy.decide(None, "orch-1", tool, {"board": "default"}) is None


def test_guarded_tools_set_is_exact():
    assert isinstance(policy.GUARDED_TOOLS, frozenset)
    assert policy.GUARDED_TOOLS == frozenset(
        {"kanban_create", "kanban_link", "kanban_unblock", "kanban_list"})
    assert not (policy.GUARDED_TOOLS & policy.NEVER_GUARDED)


# ── §6.2 hard rule 2: shallow args merging ───────────────────────────────────

@pytest.mark.parametrize("case", MATRIX, ids=lambda c: c["id"])
def test_directives_are_shallow_top_level_only(case):
    """A directive dict is exactly {modify|block[, note]}, and a modify
    carries only WHOLE top-level arg keys — never a dotted path, never a
    nested container a shallow host-side merge would half-overwrite."""
    t = team(**(case.get("team_over") or {}))
    for mode in MODES:
        d = policy.decide(t, "orch-1", case["tool"], dict(case["args"]))
        assert d is None or set(d) <= {"modify", "block", "note"}, d
        if d and "modify" in d:
            for key, value in d["modify"].items():
                assert "." not in key, f"dotted key {key!r} would fight shallow merge"
                assert not isinstance(value, (dict, list)), \
                    f"{key} must carry a whole scalar, got {value!r}"


# ── §5.2 purity ──────────────────────────────────────────────────────────────

_ALLOWED_IMPORT_ROOTS = {"__future__", "typing"}


def test_policy_module_imports_only_typing():
    """AST-level §5.2 purity: no hermes_cli, no os/os.path, no pathlib, no
    subprocess, no socket — stdlib typing is the only import."""
    tree = ast.parse(inspect.getsource(policy))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in _ALLOWED_IMPORT_ROOTS, \
                    f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in _ALLOWED_IMPORT_ROOTS, \
                f"forbidden from-import: {node.module}"
