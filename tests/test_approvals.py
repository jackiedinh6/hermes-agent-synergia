"""The human-approval inbox: ``approvals.py`` and its three CLI verbs.

The predicate under test is deliberately two-part: ``status == 'blocked'`` AND
``block_kind == 'needs_input'``. ``unblock_task`` keeps ``block_kind`` across an
unblock, so a one-part predicate on the kind alone would keep re-serving every
card the operator has already answered — the regression this module pins first.

The rest is the operator's round trip: find the card without knowing its team,
read its artifact, approve or reject it, and see the queue empty. Approve is
comment + unblock and nothing else: the plugin never completes a card on the
operator's behalf, because completion is what promotes the children and only
the assignee knows whether the work is done.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest

import cli
import storage
from hermes_agent_synergia import approvals
from model import Team


@pytest.fixture(autouse=True)
def _clean_world(hermes_root, monkeypatch):
    for var in ("HERMES_PROFILE", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_KANBAN_TASK"):
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
    args = parser.parse_args(list(argv))
    code = cli.handle(args)
    out = capsys.readouterr()
    envelope = None
    if getattr(args, "json", False) and out.out.strip():
        envelope = json.loads(out.out)
    return code, envelope, out


def _team(slug: str, board: str, *, tenant=None):
    d = {"slug": slug, "board": board, "workdir": "/w/approvals",
         "orchestrator": "lead",
         "members": [{"profile": "lead", "role": "orchestrator", "primary": True}]}
    if tenant is not None:
        d["tenant"] = tenant
    storage.upsert(Team.from_dict(d))
    storage._reset_caches_for_tests()


def _conn(board):
    from hermes_cli import kanban_db, kanban_db_connect
    return kanban_db_connect.connect_closing(
        db_path=kanban_db.kanban_db_path(board))


def _card(board, *, tenant, title="a card", assignee="lead", body=None,
          block=None, reason=None, comments=(), attachments=()):
    """A real card built through the substrate's own primitives, so the
    blocked event, its reason and the comment/attachment rows are the ones
    production writes."""
    from hermes_cli import kanban_db
    with _conn(board) as conn:
        tid = kanban_db.create_task(
            conn, title=title, body=body, assignee=assignee, tenant=tenant,
            initial_status="running", board=board)
        for author, text in comments:
            kanban_db.add_comment(conn, tid, author, text)
        for filename, path, ctype in attachments:
            kanban_db.add_attachment(
                conn, tid, filename=filename, stored_path=str(path),
                content_type=ctype, size=path.stat().st_size if hasattr(path, "stat") else 0)
        if block:
            assert kanban_db.block_task(conn, tid, reason=reason, kind=block)
    return tid


def _status_of(board, tid):
    from hermes_cli import kanban_db
    with _conn(board) as conn:
        t = kanban_db.get_task(conn, tid)
    return (t.status, t.block_kind)


def _comments_of(board, tid):
    from hermes_cli import kanban_db
    with _conn(board) as conn:
        return [(c.author, c.body) for c in kanban_db.list_comments(conn, tid)]


@pytest.fixture
def alpha(tmp_path):
    """Team ``alpha`` with one card genuinely waiting on a human, one blocked
    for a different reason, and one plain open card."""
    from hermes_cli import kanban_db
    kanban_db.create_board("alpha", name="alpha board")
    art = tmp_path / "design.md"
    art.write_text("# Design\n\nOption A or Option B.\n", encoding="utf-8")
    waiting = _card(
        "alpha", tenant="alpha", title="Design the tracker",
        body="the design body",
        comments=[("alpha-architect", "Key decisions:\n- OAuth app-only"),
                  ("alpha-lead", "Owner: approve A or B to unblock.")],
        attachments=[("design.md", art, "text/markdown")],
        block="needs_input", reason="needs a human decision on A vs B")
    other = _card("alpha", tenant="alpha", title="flaky step",
                  block="capability", reason="no network")
    open_card = _card("alpha", tenant="alpha", title="ordinary work")
    _team("alpha", "alpha", tenant="alpha")
    return {"waiting": waiting, "other": other, "open": open_card, "artifact": art}


# ── the predicate ────────────────────────────────────────────────────────────

def test_queue_is_blocked_and_needs_input_only(alpha):
    t = storage.load().teams["alpha"]
    assert [r["id"] for r in approvals.pending(t)] == [alpha["waiting"]]


def test_block_kind_survives_unblock_so_status_must_be_checked_too(alpha):
    """The regression this predicate exists for: after an approve the card
    still carries ``block_kind='needs_input'`` — only its status changed."""
    t = storage.load().teams["alpha"]
    approvals.approve(t, alpha["waiting"], "A: OAuth app-only")
    status, kind = _status_of("alpha", alpha["waiting"])
    assert status != "blocked"
    assert kind == "needs_input"          # the substrate keeps it on purpose
    assert approvals.pending(t) == []


def test_queue_is_tenant_scoped(alpha):
    """Another tenant's waiting card on the SAME board is not this team's."""
    _card("alpha", tenant="foreign", title="not ours",
          block="needs_input", reason="theirs")
    t = storage.load().teams["alpha"]
    assert [r["id"] for r in approvals.pending(t)] == [alpha["waiting"]]


def test_row_carries_what_the_operator_needs(alpha):
    t = storage.load().teams["alpha"]
    row = approvals.pending(t)[0]
    assert row["team"] == "alpha"
    assert row["assignee"] == "lead"
    assert row["block_reason"] == "needs a human decision on A vs B"
    # headline = first non-empty line of the NEWEST comment
    assert row["headline"] == "Owner: approve A or B to unblock."
    assert [a["filename"] for a in row["attachments"]] == ["design.md"]
    assert row["attachments"][0]["previewable"] is True
    assert row["age_seconds"] >= 0


def test_pending_all_spans_teams(alpha):
    from hermes_cli import kanban_db
    kanban_db.create_board("beta", name="beta board")
    b = _card("beta", tenant="beta", title="beta decision",
              block="needs_input", reason="pick one")
    _team("beta", "beta", tenant="beta")
    ids = {r["id"] for r in approvals.pending_all()}
    assert ids == {alpha["waiting"], b}


def test_pending_is_empty_for_a_team_whose_board_has_no_db(tmp_path):
    _team("ghost", "ghost-board", tenant="ghost")
    t = storage.load().teams["ghost"]
    assert approvals.pending(t) == []       # a reader never mints a board


# ── decisions ────────────────────────────────────────────────────────────────

def test_approve_comments_and_unblocks_but_never_completes(alpha):
    t = storage.load().teams["alpha"]
    res = approvals.approve(t, alpha["waiting"], "A: OAuth app-only")
    assert res["unblocked"] is True
    assert res["status"] in ("ready", "todo", "review")
    assert res["status"] != "done"
    author, body = _comments_of("alpha", alpha["waiting"])[-1]
    assert author.startswith("operator")
    assert body == "APPROVED: A: OAuth app-only"


def test_reject_unblocks_so_the_agent_reworks(alpha):
    t = storage.load().teams["alpha"]
    res = approvals.reject(t, alpha["waiting"], "cite the rate limits")
    assert res["unblocked"] is True
    assert _status_of("alpha", alpha["waiting"])[0] != "blocked"
    assert _comments_of("alpha", alpha["waiting"])[-1][1] == \
        "CHANGES REQUESTED: cite the rate limits"


def test_reject_park_records_the_comment_and_leaves_it_blocked(alpha):
    t = storage.load().teams["alpha"]
    res = approvals.reject(t, alpha["waiting"], "still thinking", park=True)
    assert res["unblocked"] is False
    assert _status_of("alpha", alpha["waiting"]) == ("blocked", "needs_input")
    assert [r["id"] for r in approvals.pending(t)] == [alpha["waiting"]]


def test_decision_refused_when_the_card_is_not_waiting_on_a_human(alpha):
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.approve(t, alpha["other"], "go ahead")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert "capability" in err.message      # the refusal names what it IS
    assert err.exit_code == 1


def test_decision_refused_on_an_empty_note(alpha):
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.approve(t, alpha["waiting"], "   ")
    assert exc.value.code == "E_TEAM_INVALID"


def test_cross_tenant_card_is_not_found_rather_than_readable(alpha):
    foreign = _card("alpha", tenant="foreign", title="not ours",
                    block="needs_input", reason="theirs")
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.detail(t, foreign)
    assert exc.value.code == "E_TEAM_NOT_FOUND"


# ── artifacts ────────────────────────────────────────────────────────────────

def test_artifact_returns_text_and_respects_the_cap(alpha):
    t = storage.load().teams["alpha"]
    att = approvals.pending(t)[0]["attachments"][0]
    blob = approvals.artifact(t, att["id"])
    assert blob["text"].startswith("# Design")
    assert blob["truncated"] is False
    small = approvals.artifact(t, att["id"], max_bytes=6)
    assert small["truncated"] is True
    assert len(small["text"]) == 6


def test_artifact_of_another_tenants_card_is_refused(alpha, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("theirs", encoding="utf-8")
    foreign = _card("alpha", tenant="foreign", title="not ours",
                    attachments=[("secret.md", secret, "text/markdown")],
                    block="needs_input", reason="theirs")
    from hermes_cli import kanban_db
    with _conn("alpha") as conn:
        att_id = kanban_db.list_attachments(conn, foreign)[0].id
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.artifact(t, att_id)
    assert exc.value.code == "E_TEAM_NOT_FOUND"


def test_binary_artifact_is_described_not_decoded(alpha, tmp_path):
    blob_path = tmp_path / "shot.png"
    blob_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    tid = _card("alpha", tenant="alpha", title="with image",
                attachments=[("shot.png", blob_path, "image/png")],
                block="needs_input", reason="look at this")
    t = storage.load().teams["alpha"]
    from hermes_cli import kanban_db
    with _conn("alpha") as conn:
        att_id = kanban_db.list_attachments(conn, tid)[0].id
    out = approvals.artifact(t, att_id)
    assert out["previewable"] is False
    assert out["text"] is None
    assert "not previewable" in out["note"]


def test_task_attachments_lists_each_cards_files(alpha, tmp_path):
    """The thread's bulk read: the same view the approval row carries, keyed by
    card, so a deliverable is readable where the work is read."""
    other = tmp_path / "notes.txt"
    other.write_text("plain", encoding="utf-8")
    plain = _card("alpha", tenant="alpha", title="second card",
                  attachments=[("notes.txt", other, "text/plain")])
    t = storage.load().teams["alpha"]
    out = approvals.task_attachments(t, [alpha["waiting"], plain, alpha["open"]])
    assert [a["filename"] for a in out[alpha["waiting"]]] == ["design.md"]
    assert out[alpha["waiting"]][0]["previewable"] is True
    assert [a["filename"] for a in out[plain]] == ["notes.txt"]
    assert out[alpha["open"]] == []


def test_task_attachments_skips_another_tenants_card(alpha, tmp_path):
    """The ids arrive from a URL, so the tenant is re-checked here — a foreign
    card is absent, not empty, and never leaks a filename."""
    secret = tmp_path / "secret.md"
    secret.write_text("theirs", encoding="utf-8")
    foreign = _card("alpha", tenant="foreign", title="not ours",
                    attachments=[("secret.md", secret, "text/markdown")])
    t = storage.load().teams["alpha"]
    out = approvals.task_attachments(t, [alpha["waiting"], foreign])
    assert foreign not in out
    assert list(out) == [alpha["waiting"]]


def test_task_attachments_without_a_board_db_is_empty_and_creates_nothing():
    from hermes_cli import kanban_db
    _team("gamma", "gamma", tenant="gamma")
    t = storage.load().teams["gamma"]
    assert approvals.task_attachments(t, ["t_nope"]) == {}
    assert not kanban_db.kanban_db_path("gamma").exists()


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_approvals_lists_both_lanes_and_filters_by_kind(alpha, parser,
                                                            capsys):
    code, env, _out = _run(parser, "approvals", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["count"] == 2
    assert env["data"]["counts"] == {"needs_input": 1, "stuck": 1}
    assert {r["id"] for r in env["data"]["approvals"]} == \
        {alpha["waiting"], alpha["other"]}

    code, env, _out = _run(parser, "approvals", "--kind", "needs_input",
                           "--json", capsys=capsys)
    assert code == 0
    assert [r["id"] for r in env["data"]["approvals"]] == [alpha["waiting"]]

    code, env, _out = _run(parser, "approvals", "--kind", "stuck", "--json",
                           capsys=capsys)
    assert code == 0
    assert [r["id"] for r in env["data"]["approvals"]] == [alpha["other"]]


def test_cli_approvals_human_names_the_next_command(alpha, parser, capsys):
    code, _env, out = _run(parser, "approvals", capsys=capsys)
    assert code == 0
    assert "awaiting your approval" in out.err
    assert f"hermes team approve {alpha['waiting']}" in out.err
    assert "design.md" in out.err


def test_cli_approvals_empty_state_says_so(parser, capsys):
    _team("quiet", "quiet-board", tenant="quiet")
    code, _env, out = _run(parser, "approvals", capsys=capsys)
    assert code == 0
    assert "nothing is waiting on you" in out.err


def test_cli_show_renders_body_comments_and_artifacts(alpha, parser, capsys):
    code, _env, out = _run(parser, "approvals", "--show", alpha["waiting"],
                           capsys=capsys)
    assert code == 0
    assert "the design body" in out.err
    assert "Key decisions:" in out.err
    assert "design.md" in out.err


def test_cli_artifact_goes_to_stdout_so_it_can_be_piped(alpha, parser, capsys):
    code, _env, out = _run(parser, "approvals", "--show", alpha["waiting"],
                           "--artifact", "design.md", capsys=capsys)
    assert code == 0
    assert out.out.startswith("# Design")     # data on stdout
    assert "design.md" in out.err             # its label on stderr


def test_cli_unknown_artifact_lists_the_real_ones(alpha, parser, capsys):
    code, _env, out = _run(parser, "approvals", "--show", alpha["waiting"],
                           "--artifact", "nope.md", capsys=capsys)
    assert code == 1
    assert "design.md" in out.err


def test_cli_approve_finds_the_card_without_being_told_the_team(alpha, parser,
                                                                capsys):
    code, env, _out = _run(parser, "approve", alpha["waiting"],
                           "--note", "A: OAuth app-only", "--json",
                           capsys=capsys)
    assert code == 0
    assert env["data"]["team"] == "alpha"
    assert env["data"]["unblocked"] is True
    assert _status_of("alpha", alpha["waiting"])[0] != "blocked"


def test_cli_reject_park_keeps_the_card_blocked(alpha, parser, capsys):
    code, env, _out = _run(parser, "reject", alpha["waiting"],
                           "--reason", "cite limits", "--park", "--json",
                           capsys=capsys)
    assert code == 0
    assert env["data"]["unblocked"] is False
    assert _status_of("alpha", alpha["waiting"]) == ("blocked", "needs_input")


def test_cli_approve_on_a_wrongly_blocked_card_fails_with_the_status(alpha,
                                                                     parser,
                                                                     capsys):
    code, env, _out = _run(parser, "approve", alpha["other"], "--note", "go",
                           "--json", capsys=capsys)
    assert code == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_INVALID"
    assert "capability" in env["error"]["message"]


def test_cli_approve_unknown_card_names_the_teams(alpha, parser, capsys):
    code, env, _out = _run(parser, "approve", "t_nope", "--note", "go",
                           "--json", capsys=capsys)
    assert code == 1
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"
    assert "alpha" in env["error"]["hint"]


# ── the other surfaces the queue feeds ───────────────────────────────────────

def test_status_reports_the_awaiting_count(alpha, parser, capsys):
    code, env, out = _run(parser, "status", "alpha", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["awaiting_approval"] == 1
    assert env["data"]["awaiting_approval_ids"] == [alpha["waiting"]]
    code, _env, out = _run(parser, "status", "alpha", capsys=capsys)
    assert "awaiting your approval: 1" in out.err


def test_status_reports_the_stuck_count_separately(alpha, parser, capsys):
    """Status must not fold the two lanes together: one number is a decision
    the operator owes, the other is work nobody is waiting on."""
    tid = _gave_up("alpha", tenant="alpha")
    code, env, out = _run(parser, "status", "alpha", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["awaiting_approval"] == 1
    assert env["data"]["stuck"] == 2
    assert tid in env["data"]["stuck_ids"]
    assert alpha["waiting"] not in env["data"]["stuck_ids"]
    code, _env, out = _run(parser, "status", "alpha", capsys=capsys)
    assert "stuck: 2" in out.err
    assert "hermes team release" in out.err


def test_task_view_carries_block_kind_for_every_surface(alpha):
    from hermes_agent_synergia import tools
    t = storage.load().teams["alpha"]
    rows = {r.id: r for r in tools._list_team_tasks(t)}
    assert tools._task_view(rows[alpha["waiting"]])["block_kind"] == "needs_input"


def test_doctor_warns_about_a_stalled_team(alpha, parser, capsys):
    code, env, _out = _run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["approvals_waiting"] == 1
    warns = [f for f in env["data"]["findings"]
             if f["check"] == "approvals_waiting"]
    assert warns and warns[0]["severity"] == "warn"
    assert "alpha" in warns[0]["message"]


def test_doctor_warns_about_stuck_cards_too(alpha, parser, capsys):
    _gave_up("alpha", tenant="alpha")
    code, env, _out = _run(parser, "doctor", "--json", capsys=capsys)
    assert code == 0
    assert env["data"]["stuck_cards"] == 2
    warns = [f for f in env["data"]["findings"] if f["check"] == "stuck_cards"]
    assert warns and warns[0]["severity"] == "warn"
    assert "alpha" in warns[0]["message"]
    assert "--kind stuck" in warns[0]["remedy"]


def test_doctor_says_plainly_that_no_messenger_is_configured(alpha, parser,
                                                             capsys):
    code, env, _out = _run(parser, "doctor", "--json", capsys=capsys)
    infos = [f for f in env["data"]["findings"]
             if f["check"] == "approvals_notify"]
    assert infos, "doctor must not let the operator assume pings are on"
    assert "home channel" in infos[0]["message"]


# ── the stuck lane ───────────────────────────────────────────────────────────

_GAVE_UP_ERROR = ("worker exited cleanly (rc=0) without calling kanban_complete "
                  "or kanban_block — protocol violation.\nsee the run log")


def _gave_up(board, *, tenant, title="verify the engine", failures=2,
             error=_GAVE_UP_ERROR):
    """A card parked the way the dispatcher's circuit breaker parks one: a raw
    ``UPDATE`` to blocked that never touches ``block_kind``, plus a ``gave_up``
    event whose payload names the failure under ``error``, not ``reason``
    (``kanban_db_dispatch._record_failure``). Nothing here calls
    ``block_task``, because production does not either."""
    from hermes_cli import kanban_db
    tid = _card(board, tenant=tenant, title=title)
    with _conn(board) as conn:
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=?, "
            "last_failure_error=? WHERE id=?", (failures, error, tid))
        kanban_db._append_event(conn, tid, "gave_up",
                                {"error": error, "failures": failures})
        conn.commit()
    return tid


def _task(board, tid):
    from hermes_cli import kanban_db
    with _conn(board) as conn:
        return kanban_db.get_task(conn, tid)


def _failure_state(board, tid):
    t = _task(board, tid)
    return (t.consecutive_failures, t.last_failure_error)


def test_a_dispatcher_giveup_is_stuck_and_never_pending(alpha):
    """The card that motivated the lane: blocked with block_kind NULL, so the
    needs_input predicate must keep refusing it and the stuck one must claim it."""
    tid = _gave_up("alpha", tenant="alpha")
    task = _task("alpha", tid)
    assert task.status == "blocked" and task.block_kind is None
    assert approvals.is_stuck(task) is True
    assert approvals.is_pending(task) is False
    assert approvals.kind_of(task) == "stuck"


def test_the_two_lanes_are_disjoint(alpha):
    _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    pending_ids = {r["id"] for r in approvals.pending(t)}
    stuck_ids = {r["id"] for r in approvals.stuck(t)}
    assert pending_ids and stuck_ids
    assert pending_ids.isdisjoint(stuck_ids)
    assert {r["id"] for r in approvals.queue(t)} == pending_ids | stuck_ids


def test_pending_still_returns_only_the_needs_input_card(alpha):
    """The needs_input contract is unchanged by the second lane."""
    _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    assert [r["id"] for r in approvals.pending(t)] == [alpha["waiting"]]


def test_stuck_row_carries_the_giveup_reason_and_the_failure_count(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    row = next(r for r in approvals.stuck(t) if r["id"] == tid)
    assert row["reason_kind"] == "stuck"
    # the gave_up payload says `error`, not `reason`
    assert row["block_reason"] == _GAVE_UP_ERROR
    assert row["failure_error"] == _GAVE_UP_ERROR
    assert row["consecutive_failures"] == 2
    assert row["blocked_at"] is not None


def test_needs_input_rows_keep_the_d23_payload(alpha):
    """A waiting row gains the lane tag and nothing else — no failure fields."""
    t = storage.load().teams["alpha"]
    row = approvals.pending(t)[0]
    assert row["reason_kind"] == "needs_input"
    assert "failure_error" not in row
    assert "consecutive_failures" not in row


def test_queue_kinds_filter_each_lane(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    assert {r["id"] for r in approvals.queue(t, kinds=("stuck",))} == \
        {tid, alpha["other"]}
    assert [r["id"] for r in approvals.queue(t, kinds=("needs_input",))] == \
        [alpha["waiting"]]


def test_queue_all_spans_teams_in_both_lanes(alpha):
    from hermes_cli import kanban_db
    kanban_db.create_board("beta", name="beta board")
    _team("beta", "beta", tenant="beta")
    b = _gave_up("beta", tenant="beta", title="beta gave up")
    a = _gave_up("alpha", tenant="alpha")
    assert {r["id"] for r in approvals.queue_all()} == \
        {alpha["waiting"], alpha["other"], a, b}
    assert {r["id"] for r in approvals.pending_all()} == {alpha["waiting"]}


def test_counts_split_by_lane(alpha):
    _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    assert approvals.count_pending(t) == 1
    assert approvals.count_stuck(t) == 2       # the give-up + the capability block


def test_release_comments_unblocks_and_resets_the_retry_budget(alpha):
    """The unblock is the point: it clears ``consecutive_failures`` and
    ``last_failure_error``, so the card gets its full retry budget back instead
    of one run before the circuit breaker trips again."""
    tid = _gave_up("alpha", tenant="alpha")
    assert _failure_state("alpha", tid) == (2, _GAVE_UP_ERROR)
    t = storage.load().teams["alpha"]
    res = approvals.release(t, tid, "run it under the wrapper this time")
    assert res["action"] == "release"
    assert res["unblocked"] is True
    assert res["status"] != "done"
    assert _status_of("alpha", tid)[0] != "blocked"
    assert _comments_of("alpha", tid)[-1][1] == \
        "RELEASED: run it under the wrapper this time"
    assert _failure_state("alpha", tid) == (0, None)
    assert [r["id"] for r in approvals.stuck(t)] == [alpha["other"]]


def test_release_never_completes_the_card(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    res = approvals.release(t, tid, "retry")
    assert res["status"] in ("ready", "todo", "review")


def test_release_refuses_a_card_waiting_on_a_human_and_names_approve(alpha):
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.release(t, alpha["waiting"], "retry")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert "hermes team approve" in err.remedy
    assert _status_of("alpha", alpha["waiting"]) == ("blocked", "needs_input")


def test_release_refuses_a_card_that_is_not_blocked(alpha):
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.release(t, alpha["open"], "retry")
    assert exc.value.code == "E_TEAM_INVALID"
    assert "--kind stuck" in exc.value.remedy


def test_release_refuses_an_empty_note(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.release(t, tid, "   ")
    assert exc.value.code == "E_TEAM_INVALID"
    assert _status_of("alpha", tid)[0] == "blocked"   # nothing was written


def test_approve_still_refuses_a_stuck_card_and_names_release(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.approve(t, tid, "go ahead")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert "the dispatcher gave up" in err.message
    assert f"hermes team release {tid}" in err.remedy


def test_a_stuck_card_in_another_tenant_is_not_found(alpha):
    foreign = _gave_up("alpha", tenant="foreign", title="not ours")
    t = storage.load().teams["alpha"]
    assert foreign not in {r["id"] for r in approvals.queue(t)}
    with pytest.raises(Exception) as exc:
        approvals.release(t, foreign, "retry")
    assert exc.value.code == "E_TEAM_NOT_FOUND"


def test_detail_marks_a_stuck_card(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    d = approvals.detail(t, tid)
    assert d["stuck"] is True
    assert d["awaiting_approval"] is False
    assert d["failure_error"] == _GAVE_UP_ERROR


# ── the stuck lane on the CLI ────────────────────────────────────────────────

def test_cli_release_finds_the_card_without_a_team(alpha, parser, capsys):
    tid = _gave_up("alpha", tenant="alpha")
    code, env, _out = _run(parser, "release", tid, "--note", "retry it",
                           "--json", capsys=capsys)
    assert code == 0
    assert env["ok"] is True
    assert env["data"]["action"] == "release"
    assert _status_of("alpha", tid)[0] != "blocked"


def test_cli_release_on_a_waiting_card_fails_with_the_approve_remedy(
        alpha, parser, capsys):
    code, env, _out = _run(parser, "release", alpha["waiting"], "--note", "go",
                           "--json", capsys=capsys)
    assert code == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_INVALID"
    assert "hermes team approve" in env["error"]["remedy"]


def test_cli_queue_prints_the_release_command_for_a_stuck_row(alpha, parser,
                                                              capsys):
    tid = _gave_up("alpha", tenant="alpha")
    code, _env, out = _run(parser, "approvals", capsys=capsys)
    assert code == 0
    assert "STUCK" in out.err
    assert f"hermes team release {tid} --note" in out.err
    assert "protocol violation." in out.err            # first line of the failure
    assert "2 consecutive failure(s)" in out.err
    # the waiting card keeps its own pair of commands
    assert f"hermes team approve {alpha['waiting']}" in out.err


def test_cli_show_explains_a_stuck_card_instead_of_dismissing_it(alpha, parser,
                                                                 capsys):
    tid = _gave_up("alpha", tenant="alpha")
    code, _env, out = _run(parser, "approvals", "--show", tid, capsys=capsys)
    assert code == 0
    assert "STUCK" in out.err
    assert "not waiting on a human right now" not in out.err
    assert f"hermes team release {tid}" in out.err


# ── steering a card that is already moving ───────────────────────────────────

def _force_status(board, tid, status):
    with _conn(board) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()


def test_note_comments_without_unblocking_or_touching_status(alpha):
    """A note is a comment and nothing else. The delivery is the substrate's
    live bridge (``inject_new_comments_from_env`` steers new comments into the
    running agent), so the card must keep running for it to be heard."""
    _force_status("alpha", alpha["open"], "running")
    t = storage.load().teams["alpha"]
    res = approvals.note(t, alpha["open"], "prefer the streaming parser")
    assert res["action"] == "note"
    assert res["unblocked"] is False
    assert res["delivery"] == "mid-run"
    assert _status_of("alpha", alpha["open"]) == ("running", None)
    author, body = _comments_of("alpha", alpha["open"])[-1]
    assert author.startswith("operator")
    assert body == "NOTE: prefer the streaming parser"


def test_note_on_a_card_that_is_not_running_waits_for_its_next_run(alpha):
    """A queued card has no worker polling it, so the note is context its next
    run reads — the operator is told which of the two happened."""
    assert _status_of("alpha", alpha["open"])[0] == "ready"
    t = storage.load().teams["alpha"]
    res = approvals.note(t, alpha["open"], "start from the fixture")
    assert res["delivery"] == "next_run"
    assert _status_of("alpha", alpha["open"])[0] == "ready"
    assert _comments_of("alpha", alpha["open"])[-1][1] == \
        "NOTE: start from the fixture"


def test_note_refuses_a_card_waiting_on_a_human_and_names_approve(alpha):
    """The refusal exists because the comment WOULD land: the operator would
    read the thread as answered while the card stayed blocked forever."""
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.note(t, alpha["waiting"], "use option A")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert f"hermes team approve {alpha['waiting']}" in err.remedy
    assert _comments_of("alpha", alpha["waiting"])[-1][1] != "NOTE: use option A"
    assert _status_of("alpha", alpha["waiting"]) == ("blocked", "needs_input")


def test_note_refuses_a_stuck_card_and_names_release(alpha):
    tid = _gave_up("alpha", tenant="alpha")
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.note(t, tid, "try the wrapper")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert f"hermes team release {tid}" in err.remedy


def test_note_refuses_a_done_card(alpha):
    _force_status("alpha", alpha["open"], "done")
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.note(t, alpha["open"], "one more thing")
    assert exc.value.code == "E_TEAM_INVALID"
    assert _comments_of("alpha", alpha["open"]) == []


def test_note_refuses_empty_text_and_names_the_text_flag(alpha):
    t = storage.load().teams["alpha"]
    with pytest.raises(Exception) as exc:
        approvals.note(t, alpha["open"], "   ")
    err = exc.value
    assert err.code == "E_TEAM_INVALID"
    assert "--text" in err.remedy
    assert _comments_of("alpha", alpha["open"]) == []


def test_cli_note_finds_the_card_without_a_team(alpha, parser, capsys):
    _force_status("alpha", alpha["open"], "running")
    code, env, _out = _run(parser, "note", alpha["open"],
                           "--text", "prefer the streaming parser",
                           "--json", capsys=capsys)
    assert code == 0
    assert env["ok"] is True
    assert env["data"]["action"] == "note"
    assert env["data"]["delivery"] == "mid-run"
    assert env["data"]["comment_id"]
    assert _status_of("alpha", alpha["open"]) == ("running", None)


def test_cli_note_says_when_the_worker_hears_it(alpha, parser, capsys):
    _force_status("alpha", alpha["open"], "running")
    code, _env, out = _run(parser, "note", alpha["open"], "--text", "go wide",
                           capsys=capsys)
    assert code == 0
    assert "~6s" in out.err
    assert "hermes kanban unblock" not in out.err      # a note never unblocks
