"""S18b — ``hermes team status <team> [--status S] [--assignee P]
[--limit N] [--json]``: the read side of adopt, in the CLI.

The subcommand mirrors the ``team_status`` TOOL through the same seams —
``resolve.task_counts(board, tenant=…)`` for the six §4.3 buckets,
``tools._list_team_tasks`` for the tenant-scoped rows, the tool's own
``_OPEN_STATUSES`` / limit rules / ``_task_view`` row shape — so the
agreement between tool, dashboard route and CLI is structural, not a
re-implementation. The world below therefore compares the CLI output to
``resolve.task_counts`` directly (the seam both read through), and the
mixed-tenant backfill regression lives with its command in
test_adopt.py.

The CLI runs AS THE OPERATOR: unknown team ⇒ E_TEAM_NOT_FOUND (exit 1,
remedy names ``hermes team list``), but NO membership gate — create/adopt/
show have none, and neither does status. A tenant-less team reads honestly
EMPTY (the conventional ``tenant = <slug>`` scope) plus the §12 note naming
``--backfill-tenant``.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest

import cli
import resolve
import storage
from model import Team

COUNT_KEYS = ["todo", "ready", "running", "review", "done", "blocked"]


# ── world fixtures (same hygiene as test_adopt) ──────────────────────────────

@pytest.fixture(autouse=True)
def _clean_world(hermes_root, monkeypatch):
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


def _run(parser, *argv, capsys):
    """Parse + dispatch; returns (exit_code, stdout_envelope_or_None, stderr)."""
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)
    return code, envelope, out.err


def _board(board: str) -> None:
    from hermes_cli import kanban_db as kb
    kb.create_board(board, name=f"status board {board}")


def _seed(board: str, rows: list) -> None:
    """Direct INSERTs — (id, assignee, status, priority, age_seconds, tenant)."""
    from hermes_cli import kanban_db, kanban_db_connect
    now = int(time.time())
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        conn.executemany(
            "insert into tasks (id, title, status, created_at, assignee,"
            " tenant, priority) values (?, ?, ?, ?, ?, ?, ?)",
            [(tid, f"card {tid}", st, now - age, a, ten, pr)
             for tid, a, st, pr, age, ten in rows],
        )
        conn.commit()


def _team(slug: str, board: str, *, tenant=None, workdir: str = "/w/status"):
    d = {"slug": slug, "board": board, "workdir": workdir,
         "orchestrator": "lead",
         "members": [{"profile": "lead", "role": "orchestrator",
                      "primary": True},
                     {"profile": "dev", "role": "member"}]}
    if tenant is not None:
        d["tenant"] = tenant
    storage.upsert(Team.from_dict(d))
    storage._reset_caches_for_tests()


@pytest.fixture
def alpha():
    """Team ``alpha`` on board ``alpha``: five STAMPED cards over four open
    statuses plus one done; priorities 3/2/1/0 pin the display order
    (t3 running, t1 todo, t2 ready, t4 blocked; t5 done is not open)."""
    _board("alpha")
    _seed("alpha", [
        ("t3", "dev", "running", 3, 60, "alpha"),
        ("t1", "dev", "todo", 2, 7200, "alpha"),
        ("t2", "qa", "ready", 1, 3600, "alpha"),
        ("t4", "lead", "blocked", 0, 3 * 86400, "alpha"),
        ("t5", "dev", "done", 0, 86400, "alpha"),
    ])
    _team("alpha", "alpha", tenant="alpha")


# ── happy path: counts + open cards, scoped to the team's tenant ─────────────

def test_status_counts_and_open_cards(alpha, parser, capsys):
    code, env, err = _run(parser, "status", "alpha", "--json", capsys=capsys)
    assert code == 0, err
    assert env["ok"] is True, env
    data = env["data"]
    assert data["team"] == "alpha"
    assert data["board"] == "alpha"
    assert data["counts"] == {"todo": 1, "ready": 1, "running": 1,
                              "review": 0, "done": 1, "blocked": 1}
    # the tool/API/CLI agreement, checked at the shared seam:
    assert data["counts"] == resolve.task_counts("alpha", tenant="alpha")
    # open = the four non-done cards, priority DESC as seeded.
    assert data["total_matching"] == 4
    assert [c["id"] for c in data["open_cards"]] == ["t3", "t1", "t2", "t4"]
    card = data["open_cards"][0]
    # the tools._task_view row shape (the dashboard/API row shape minus body).
    assert {"id", "title", "status", "assignee", "priority",
            "created_at"} <= set(card)
    assert card["status"] == "running"
    assert card["assignee"] == "dev"


def test_status_scopes_to_tenant_not_the_whole_board(alpha, parser, capsys):
    # A foreign tenant's cards exist on the same board and MUST NOT count.
    _seed("alpha", [("x1", "someone", "todo", 9, 60, "foreign")])
    code, env, err = _run(parser, "status", "alpha", "--json", capsys=capsys)
    assert code == 0, err
    assert env["data"]["counts"]["todo"] == 1        # still only alpha's
    assert env["data"]["total_matching"] == 4
    assert all(c["id"] != "x1" for c in env["data"]["open_cards"])


# ── filters and the limit (mirror tools._h_status) ───────────────────────────

def test_status_filter_by_status(alpha, parser, capsys):
    code, env, err = _run(parser, "status", "alpha", "--status", "done",
                          "--json", capsys=capsys)
    assert code == 0, err
    assert env["data"]["status"] == "done"
    assert [c["id"] for c in env["data"]["open_cards"]] == ["t5"]
    assert env["data"]["total_matching"] == 1
    # counts stay the full six-bucket picture (§4.3), filter or not.
    assert sorted(env["data"]["counts"]) == sorted(COUNT_KEYS)


def test_status_filter_by_assignee(alpha, parser, capsys):
    code, env, err = _run(parser, "status", "alpha", "--assignee", "dev",
                          "--json", capsys=capsys)
    assert code == 0, err
    assert [c["id"] for c in env["data"]["open_cards"]] == ["t3", "t1"]
    assert env["data"]["total_matching"] == 2


def test_status_limit_caps_cards_not_counts_or_total(alpha, parser, capsys):
    code, env, err = _run(parser, "status", "alpha", "--limit", "2",
                          "--json", capsys=capsys)
    assert code == 0, err
    assert len(env["data"]["open_cards"]) == 2
    assert [c["id"] for c in env["data"]["open_cards"]] == ["t3", "t1"]
    assert env["data"]["total_matching"] == 4        # truth kept, view capped
    assert env["data"]["limit"] == 2


def test_status_limit_out_of_range_is_invalid(alpha, parser, capsys):
    for bad in ("0", "101"):
        code, env, err = _run(parser, "status", "alpha", "--limit", bad,
                              "--json", capsys=capsys)
        assert code == 1, err                        # errors.exit_code_for ⇒ 1
        assert env["ok"] is False
        assert env["error"]["code"] == "E_TEAM_INVALID"
        assert "1..100" in env["error"]["hint"]      # hint names legal values


def test_status_unknown_status_is_invalid_with_remedy(alpha, parser, capsys):
    code, env, err = _run(parser, "status", "alpha", "--status", "urgent",
                          "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_INVALID"
    assert "urgent" in env["error"]["message"]
    assert "hermes team status alpha" in env["error"]["remedy"]


# ── errors: unknown team ⇒ E_TEAM_NOT_FOUND with a runnable remedy ───────────

def test_status_unknown_team_names_the_code_and_a_remedy(hermes_root, parser,
                                                        capsys):
    code, env, err = _run(parser, "status", "ghost", "--json", capsys=capsys)
    assert code == 1
    assert env["ok"] is False
    assert env["team"] == "ghost"                    # §7.3 failure envelope
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"
    assert "ghost" in env["error"]["message"]
    assert env["error"]["remedy"] == "hermes team list"
    for key in ("code", "message", "hint", "remedy"):
        assert env["error"][key]


# ── envelope discipline (§7.3 / §8.2) ────────────────────────────────────────

def test_status_json_is_exactly_one_envelope_object(alpha, parser, capsys):
    # _run json.loads()s the WHOLE stdout: a second JSON object or any stray
    # print would make it (and this test) fail.
    code, envelope, err = _run(parser, "status", "alpha", "--json",
                               capsys=capsys)
    assert code == 0
    assert set(envelope) == {"ok", "data"}
    assert "_human" not in envelope["data"]          # display-only, stripped
    assert set(envelope["data"]) >= {
        "team", "board", "tenant", "counts", "open_cards", "total_matching",
        "status", "assignee", "limit"}


def test_status_human_mode_writes_only_to_stderr(alpha, parser, capsys):
    args = parser.parse_args(["status", "alpha"])    # no --json
    code = cli.handle(args)
    out = capsys.readouterr()
    assert code == 0
    assert out.out == ""                             # --json contract inverse
    err = out.err
    assert "team alpha" in err
    assert "todo=1" in err and "done=1" in err
    assert "t3" in err and "running" in err
    assert "2h" in err                               # age column (t1, 7200s)
    assert "3d" in err                               # age column (t4)
    assert "t5" not in err                           # done is not an open card


# ── §12: the tenant-less team reads honestly EMPTY + names the remedy ────────

def test_status_tenantless_team_reads_empty_with_backfill_note(hermes_root,
                                                              parser, capsys):
    _board("beta")
    _seed("beta", [("b1", "dev", "todo", 0, 60, None),
                   ("b2", "dev", "ready", 0, 60, None)])
    _team("beta", "beta")                            # NO tenant key: ""

    code, env, err = _run(parser, "status", "beta", "--json", capsys=capsys)
    assert code == 0, err
    data = env["data"]
    # honest empty: scoped by the conventional label, nothing is stamped to it
    assert all(v == 0 for v in data["counts"].values())
    assert data["open_cards"] == []
    assert data["total_matching"] == 0
    assert data["tenant"] == "beta"                  # the conventional scope
    # the §12 warning: symptom named, remedy runnable (mirrors adopt's warning)
    assert "--backfill-tenant" in data["warning"]
    assert "hermes team adopt beta --board beta --backfill-tenant" in \
        data["warning"]
    assert "WARNING" in err


def test_status_missing_board_db_reads_zero_never_mints(hermes_root, parser,
                                                       capsys):
    """§5.2.1: a reader must never mint a board directory — the counts seam
    and the row seam both gate on the DB FILE existing."""
    from hermes_cli import kanban_db as kb
    _team("gamma", "gamma-board", tenant="gamma")
    assert not kb.kanban_db_path("gamma-board").is_file()

    code, env, err = _run(parser, "status", "gamma", "--json", capsys=capsys)
    assert code == 0, err
    assert all(v == 0 for v in env["data"]["counts"].values())
    assert env["data"]["open_cards"] == []
    assert not kb.kanban_db_path("gamma-board").is_file()   # still absent
