"""S18 — ``hermes team adopt``: an existing hand-made board becomes a team
without losing its history (implement.md S18; spec §8.1 adopt row,
§12 "team status empty though cards exist").

The board here is genuinely LEGACY: created by the substrate's own
``create_board`` (never ``team create``), cards seeded with direct SQLite
INSERTs — ``tenant`` NULL exactly like a hand-made board's rows. On top of
that world the tests walk the whole S18 contract:

* ``--members-from-board`` → roster = the DISTINCT non-null assignees, none
  primary, ``revision`` 1 (spec-silent roles: everyone ``member`` except the
  first assignee in sorted order, who is the orchestrator — documented in
  cli.py's S18 section);
* ``--backfill-tenant`` without ``--yes`` → refuses, count printed BEFORE the
  refusal, rows and registry untouched (explicit, CONFIRMED, never
  implicit);
* ``--backfill-tenant --yes`` → all N rows stamped, the undo UPDATE printed
  verbatim, and the tenant-scoped counts equal the pre-adopt row count (the
  Verify block's line-4 semantics: ``team status`` reads its numbers through
  ``resolve.task_counts(board, tenant=…)``, so the counts are compared
  there — the CLI ``status`` subcommand joined the surface in S18b and is
  covered by test_cli_status.py, so this module keeps comparing at the seam);
* running the printed revert UPDATE → every row back to NULL, the tenant
  counts back to the zero-drift state (reversible);
* a second adopt onto the same board → ``E_TEAM_BOARD_TAKEN`` (I1);
* without ``--backfill-tenant`` → a warning naming the flag, rows untouched
  (the §12 row: status MAY legitimately read zero);
* missing ``--board`` → ``E_TEAM_INVALID`` with a usage-shaped remedy.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest

import cli
import resolve
import storage


# ── world fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_world(hermes_root, monkeypatch):
    """Temp HERMES_HOME (conftest) + drop every board/profile identity the
    host shell or a previous test could leak into the substrate's resolution.
    Same hygiene as test_integration_policy."""
    for var in (
        "HERMES_PROFILE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_KEY",
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
    """Legal workdir paths — outside the Hermes root (§4.2 guard)."""
    def _w(name):
        return str(hermes_root.parent / ("work-" + name))
    return _w


# ── legacy-world seeding (direct substrate calls, no plugin writes) ──────────

def _make_board(board: str, *, default_workdir: str | None = None):
    """A HAND-MADE board: substrate ``create_board`` (dir + board.json + DB),
    never ``team create``."""
    from hermes_cli import kanban_db as kb
    return kb.create_board(board, name=f"legacy board {board}",
                           default_workdir=default_workdir)


def _seed_cards(board: str, rows: list) -> None:
    """Direct INSERTs on the board DB — the pattern from
    test_integration_policy's raw-query witnesses. Each row: (id, assignee);
    ``tenant`` deliberately left NULL (a pre-stamp legacy card)."""
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(board)
    now = int(time.time())
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        conn.executemany(
            "insert into tasks (id, title, status, created_at, assignee)"
            " values (?, ?, 'todo', ?, ?)",
            [(tid, f"legacy card {tid}", now, assignee)
             for tid, assignee in rows],
        )
        conn.commit()


def _tenant_groups(board: str) -> dict:
    """tenant → count, straight from the board DB (the Verify block's
    ``select tenant,count(*) from tasks group by 1``)."""
    from hermes_cli import kanban_db, kanban_db_connect
    db_path = kanban_db.kanban_db_path(board)
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        rows = conn.execute(
            "select tenant, count(*) from tasks group by 1").fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _run(parser, *argv, capsys):
    """Parse + dispatch; returns (exit_code, stdout_envelope_or_None, stderr)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)
    return code, envelope, out.err


def _seed_legacy_board(board: str, W: str) -> dict:
    """Board + workdir + 5 untenant'd cards over three assignees.
    Returns the seeded world facts."""
    import os
    os.makedirs(W, exist_ok=True)
    _make_board(board, default_workdir=W)
    rows = [("t1", "dev"), ("t2", "lead"), ("t3", "dev"),
            ("t4", "qa"), ("t5", "qa")]
    _seed_cards(board, rows)
    return {"rows": len(rows), "assignees": ["dev", "lead", "qa"]}


# ── roster derivation ────────────────────────────────────────────────────────

def test_adopt_members_from_board_yields_exact_roster(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)

    code, env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                         "--members-from-board", "--json", capsys=capsys)
    assert code == 0, err
    assert env["ok"] is True, env
    team = env["data"]["team"]
    assert team["board"] == "legacy"
    assert team["tenant"] == "legacy"
    assert team["revision"] == 1                      # fresh record
    members = {m["profile"]: m for m in team["members"]}
    # EXACTLY the distinct assignees — five cards, three people.
    assert sorted(members) == ["dev", "lead", "qa"]
    # none primary (an adopt claims no plane-1 owner)
    assert [m for m in team["members"] if m.get("primary")] == []
    # every adopted member is managed: false (adopt created nothing, I7)
    assert all(m.get("managed") is False or "managed" not in m
               for m in team["members"])
    # spec-silent decisions, deterministic: first assignee in SORTED order
    # is the orchestrator (I3 needs one); everyone else role "member".
    assert team["orchestrator"] == "dev"
    assert members["dev"]["role"] == "orchestrator"
    assert members["lead"]["role"] == "member"
    assert members["qa"]["role"] == "member"


def test_adopt_missing_board_flag_refused_with_usage(
        hermes_root, profile, parser, W, capsys):
    _seed_legacy_board("legacy", W("legacy"))
    code, env, err = _run(parser, "adopt", "legacy", "--members-from-board",
                          "--json", capsys=capsys)
    assert code == 1                                  # TeamError, not argparse
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_INVALID"
    assert "--board SLUG" in env["error"]["message"]
    assert "--board" in env["error"]["remedy"]        # usage-shaped remedy
    storage._reset_caches_for_tests()
    assert "legacy" not in storage.load().teams       # nothing written


def test_adopt_nonexistent_board_refused(
        hermes_root, profile, parser, W, capsys):
    code, env, err = _run(parser, "adopt", "legacy", "--board", "no-such",
                          "--members-from-board", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NO_BOARD"  # adopt NEVER creates one


# ── backfill: explicit, CONFIRMED, reversible, never implicit ────────────────

def test_backfill_without_yes_refuses_and_writes_nothing(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)

    code, env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                          "--members-from-board", "--backfill-tenant",
                          "--json", capsys=capsys)
    assert code == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_INVALID"
    # the confirmation contract: the affected-row count is printed BEFORE
    # any refusal (human line on stderr, and named in the envelope message).
    assert f"{world['rows']} task row(s)" in err
    assert str(world["rows"]) in env["error"]["message"]
    # rows unchanged…
    assert _tenant_groups("legacy") == {None: world["rows"]}
    # …and no team record: a refused backfill must not half-adopt.
    storage._reset_caches_for_tests()
    assert "legacy" not in storage.load().teams


def test_backfill_with_yes_stamps_every_row_and_status_counts_match(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)
    pre = _tenant_groups("legacy")
    assert pre == {None: world["rows"]}

    code, env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                          "--members-from-board", "--backfill-tenant",
                          "--yes", "--json", capsys=capsys)
    assert code == 0, err
    assert env["ok"] is True, env
    data = env["data"]
    assert data["unstamped_rows"] == world["rows"]
    assert data["backfilled"]["rows_stamped"] == world["rows"]

    # the Verify block's line-4 semantics: the tenant-scoped counts equal the
    # pre-adopt row count. (``team status`` reads through
    # resolve.task_counts(board, tenant=…); the CLI status subcommand is not
    # in this build's surface, so the counts are read at that same seam.)
    counts = resolve.task_counts("legacy", tenant="legacy")
    assert sum(counts.values()) == world["rows"]      # == pre-adopt row count

    post = _tenant_groups("legacy")
    assert post == {"legacy": world["rows"]}

    # REVERSIBLE: the undo UPDATE is printed verbatim…
    undo = data["backfilled"]["undo_sql"]
    assert undo == "UPDATE tasks SET tenant=NULL WHERE tenant='legacy'"
    assert undo in err                                # printed, not just data


def test_undo_command_reverts_the_backfill(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)

    code, env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                          "--members-from-board", "--backfill-tenant",
                          "--yes", "--json", capsys=capsys)
    assert code == 0, err
    assert _tenant_groups("legacy") == {"legacy": world["rows"]}

    # run the printed revert UPDATE verbatim (the reversible half).
    from hermes_cli import kanban_db, kanban_db_connect
    undo = env["data"]["backfilled"]["undo_sql"]
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("legacy")) as conn:
        cur = conn.execute(undo)
        conn.commit()
        assert int(cur.rowcount) == world["rows"]

    assert _tenant_groups("legacy") == {None: world["rows"]}
    # back to the §12 zero-drift state: status reads zero though cards exist.
    assert sum(resolve.task_counts("legacy", tenant="legacy").values()) == 0
    assert sum(resolve.task_counts("legacy", None).values()) == world["rows"]


# ── I1: the board is taken once it's a team's ────────────────────────────────

def test_second_adopt_onto_same_board_is_board_taken(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)
    code, _env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                           "--members-from-board", "--backfill-tenant",
                           "--yes", "--json", capsys=capsys)
    assert code == 0, err

    # a SECOND team cannot land on the adopted board (I1).
    code, env, err = _run(parser, "adopt", "other", "--board", "legacy",
                          "--members-from-board", "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_BOARD_TAKEN"
    assert "legacy" in env["error"]["message"]        # names the holder…
    assert "other" not in str(storage.load().teams)   # …and nothing wrote.


# ── §12 row: no --backfill-tenant ⇒ warn, never write ────────────────────────

def test_without_backfill_warns_names_the_flag_and_rows_untouched(
        hermes_root, profile, parser, W, capsys):
    world = _seed_legacy_board("legacy", W("legacy"))
    for p in world["assignees"]:
        profile(p)

    code, env, err = _run(parser, "adopt", "legacy", "--board", "legacy",
                          "--members-from-board", "--json", capsys=capsys)
    assert code == 0, err
    assert env["ok"] is True
    # the warning names the flag (and the §12 symptom it cures).
    assert "--backfill-tenant" in err
    assert "--backfill-tenant" in env["data"]["warning"]
    assert env["data"]["unstamped_rows"] == world["rows"]
    assert env["data"]["backfilled"] is None
    # rows untouched — the stamping stays explicitly declined.
    assert _tenant_groups("legacy") == {None: world["rows"]}


# ── S18b: mixed-tenant board — the backfill's WHERE scoping, unit-proven ───────

def test_backfill_on_mixed_tenant_board_stamps_only_the_null_rows(
        hermes_root, profile, parser, W, capsys):
    """``--backfill-tenant`` touches ``WHERE tenant IS NULL`` and ONLY
    those rows. A board carrying NULL-, foreign- and own-tenant rows must
    come out of the backfill with the foreign rows byte-intact, the NULLs
    stamped, and the tenant-scoped counts (the seam ``team status`` reads
    through) seeing own + backfilled work only — never the foreign rows."""
    import os
    wd = W("mixed")
    os.makedirs(wd, exist_ok=True)
    _make_board("mixed", default_workdir=wd)
    now = int(time.time())
    rows = [  # (id, assignee, tenant) — the three kinds the backfill distinguishes
        ("n1", "dev", None), ("n2", "qa", None),
        ("f1", "x-dev", "foreign"), ("f2", "x-qa", "foreign"),
        ("o1", "dev", "mixed"),
    ]
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("mixed")) as conn:
        conn.executemany(
            "insert into tasks (id, title, status, created_at, assignee, tenant)"
            " values (?, ?, 'todo', ?, ?, ?)",
            [(tid, f"mixed card {tid}", now, a, ten)
             for tid, a, ten in rows])
        conn.commit()
    for p in ("dev", "qa", "x-dev", "x-qa"):
        profile(p)

    code, env, err = _run(parser, "adopt", "mixed", "--board", "mixed",
                          "--members-from-board", "--backfill-tenant",
                          "--yes", "--json", capsys=capsys)
    assert code == 0, err
    data = env["data"]
    # only the NULL rows counted as unstamped…
    assert data["unstamped_rows"] == 2
    # …and only the NULL rows got stamped (foreign and own rows untouched).
    assert data["backfilled"]["rows_stamped"] == 2

    # group-by-1 witness: foreign rows byte-intact, own stamp preserved,
    # NULLs gone (own = pre-stamped o1 + backfilled n1/n2).
    assert _tenant_groups("mixed") == {"foreign": 2, "mixed": 3}

    # the seam ``team status`` reads through: counts see exactly the team's
    # work — own + backfilled, zero foreign rows.
    counts = resolve.task_counts("mixed", tenant="mixed")
    assert sum(counts.values()) == 3
    assert sum(resolve.task_counts("mixed", tenant="foreign").values()) == 2
