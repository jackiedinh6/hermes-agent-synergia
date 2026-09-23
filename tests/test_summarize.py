"""The approval briefing: ``summarize.py``.

Every test here drives a duck-typed fake in place of the host's
``PluginLlm``, because the point under test is not the model's prose — it is
the contract around the call: the card revision that keys the cache, the clamp
applied to whatever comes back, and the promise that a failed briefing costs
the operator nothing but the briefing.
"""

from __future__ import annotations

import pytest

import storage
from hermes_agent_synergia import approvals, summarize
from model import Team


@pytest.fixture(autouse=True)
def _clean_world(hermes_root, monkeypatch):
    for var in ("HERMES_PROFILE", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    storage._reset_caches_for_tests()
    summarize._MEMO.clear()
    yield
    summarize._MEMO.clear()
    storage._reset_caches_for_tests()


ANSWER = {
    "summary": "The architect proposes OAuth app-only auth for the tracker.",
    "question": "Approve app-only auth, or require delegated auth?",
    "options": [
        {"label": "Approve app-only", "detail": "approve design: app-only is fine.",
         "verb": "approve", "recommended": True},
        {"label": "Require delegated", "detail": "request changes: use delegated auth.",
         "verb": "reject"},
    ],
    "risks": ["no per-user audit trail"],
}


class _Result:
    def __init__(self, parsed, model="claude-test"):
        self.parsed = parsed
        self.model = model


class FakeLlm:
    """Records every call; answers with ``ANSWER`` unless told otherwise."""

    def __init__(self, answer=None, raises=None):
        self.answer = ANSWER if answer is None else answer
        self.raises = raises
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises
        return _Result(self.answer)


def _conn(board):
    from hermes_cli import kanban_db, kanban_db_connect
    return kanban_db_connect.connect_closing(
        db_path=kanban_db.kanban_db_path(board))


@pytest.fixture
def alpha(tmp_path):
    """Team ``alpha``: one design gate parked on a human, one stuck card."""
    from hermes_cli import kanban_db
    kanban_db.create_board("alpha", name="alpha board")
    art = tmp_path / "design.md"
    art.write_text("# Design\n\nOAuth app-only, no delegated flow.\n", encoding="utf-8")
    with _conn("alpha") as conn:
        gate = kanban_db.create_task(
            conn, title="Design the tracker", body="the design body",
            assignee="architect", tenant="alpha", initial_status="running",
            board="alpha")
        kanban_db.add_comment(conn, gate, "alpha-architect",
                              "## Design Ready for Human Review")
        kanban_db.add_attachment(conn, gate, filename="design.md",
                                 stored_path=str(art),
                                 content_type="text/markdown",
                                 size=art.stat().st_size)
        assert kanban_db.block_task(conn, gate, reason="design approval",
                                    kind="needs_input")
        stuck = kanban_db.create_task(
            conn, title="flaky step", assignee="qa", tenant="alpha",
            initial_status="running", board="alpha")
        assert kanban_db.block_task(conn, stuck, reason="no network",
                                    kind="capability")
    storage.upsert(Team.from_dict(
        {"slug": "alpha", "board": "alpha", "workdir": "/w/summarize",
         "tenant": "alpha", "orchestrator": "lead",
         "members": [{"profile": "lead", "role": "orchestrator", "primary": True}]}))
    storage._reset_caches_for_tests()
    return {"gate": gate, "stuck": stuck, "artifact": art}


def _team():
    return storage.load().teams["alpha"]


def _comment(board, task_id, author, body):
    from hermes_cli import kanban_db
    with _conn(board) as conn:
        kanban_db.add_comment(conn, task_id, author, body)


# ── what the model is shown, and what comes back ─────────────────────────────

def test_summary_is_the_clamped_payload_plus_its_card(alpha):
    llm = FakeLlm()
    out = summarize.summary(_team(), alpha["gate"], llm=llm)
    assert out["state"] == "ready"
    assert out["summary"] == ANSWER["summary"]
    assert out["question"] == ANSWER["question"]
    assert [o["label"] for o in out["options"]] == ["Approve app-only", "Require delegated"]
    assert [o["verb"] for o in out["options"]] == ["approve", "reject"]
    assert out["risks"] == ["no per-user audit trail"]
    assert out["model"] == "claude-test"
    assert out["task_id"] == alpha["gate"]
    assert out["team"] == "alpha"
    assert out["lane"] == approvals.NEEDS_INPUT
    assert out["cached"] is False
    assert out["key"] == summarize.key_of(approvals.summary_inputs(_team(), alpha["gate"]))


def test_the_card_body_comments_and_artifact_all_reach_the_model(alpha):
    llm = FakeLlm()
    summarize.summary(_team(), alpha["gate"], llm=llm)
    assert len(llm.calls) == 1
    kw = llm.calls[0]
    assert kw["json_schema"] is summarize.SCHEMA
    assert kw["purpose"] == "approval-summary"
    assert "task" not in kw          # an unregistered routing key is a trust error
    text = "\n".join(b["text"] for b in kw["input"])
    assert alpha["gate"] in text
    assert "the design body" in text
    assert "design approval" in text
    assert "## Design Ready for Human Review" in text
    assert "OAuth app-only, no delegated flow." in text


# ── the cache, keyed on the card revision ────────────────────────────────────

def test_an_unchanged_card_is_summarized_once(alpha):
    llm = FakeLlm()
    first = summarize.summary(_team(), alpha["gate"], llm=llm)
    second = summarize.summary(_team(), alpha["gate"], llm=llm)
    assert len(llm.calls) == 1
    assert second["cached"] is True
    assert second["summary"] == first["summary"]
    assert second["key"] == first["key"]


def test_a_new_comment_is_a_new_card_revision(alpha):
    llm = FakeLlm()
    first = summarize.summary(_team(), alpha["gate"], llm=llm)
    _comment("alpha", alpha["gate"], "alpha-lead", "Security wants delegated auth.")
    second = summarize.summary(_team(), alpha["gate"], llm=llm)
    assert len(llm.calls) == 2
    assert second["key"] != first["key"]
    assert second["cached"] is False


def test_refresh_pays_the_call_again_on_the_same_revision(alpha):
    llm = FakeLlm()
    summarize.summary(_team(), alpha["gate"], llm=llm)
    out = summarize.summary(_team(), alpha["gate"], llm=llm, refresh=True)
    assert len(llm.calls) == 2
    assert out["cached"] is False


def test_the_cache_survives_the_process_memo(alpha):
    """The disk mirror, not the dict: a fresh process serves the same card."""
    llm = FakeLlm()
    summarize.summary(_team(), alpha["gate"], llm=llm)
    assert summarize.cache_path().is_file()
    summarize._MEMO.clear()
    out = summarize.summary(_team(), alpha["gate"], llm=llm)
    assert len(llm.calls) == 1
    assert out["cached"] is True


def test_a_failed_briefing_is_not_cached(alpha):
    llm = FakeLlm(raises=RuntimeError("model down"))
    summarize.summary(_team(), alpha["gate"], llm=llm)
    summarize.summary(_team(), alpha["gate"], llm=llm)
    assert len(llm.calls) == 2


# ── failure never costs the operator the card ────────────────────────────────

def test_an_llm_failure_is_a_payload_not_an_exception(alpha):
    out = summarize.summary(_team(), alpha["gate"],
                            llm=FakeLlm(raises=RuntimeError("model down")))
    assert out["state"] == "error"
    assert "model down" in out["error"]
    assert out["options"] == []


def test_an_unusable_answer_is_an_error_not_half_a_briefing(alpha):
    out = summarize.summary(_team(), alpha["gate"],
                            llm=FakeLlm(answer={"question": "?", "options": []}))
    assert out["state"] == "error"
    assert out["summary"] == ""


# ── lanes and the off switch ─────────────────────────────────────────────────

def test_a_lane_that_owes_no_decision_is_skipped(alpha):
    llm = FakeLlm()
    out = summarize.summary(_team(), alpha["stuck"], llm=llm)
    assert out["state"] == "skipped"
    assert out["lane"] == approvals.STUCK
    assert llm.calls == []


def test_the_summary_has_an_off_switch(alpha, hermes_root):
    (hermes_root / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes-agent-synergia:\n"
        "      settings:\n        approval_summary: false\n", encoding="utf-8")
    storage._reset_caches_for_tests()
    llm = FakeLlm()
    out = summarize.summary(_team(), alpha["gate"], llm=llm)
    assert out["state"] == "off"
    assert llm.calls == []


# ── the clamp ────────────────────────────────────────────────────────────────

def test_only_one_option_may_be_recommended():
    out = summarize.normalize({
        "summary": "s",
        "options": [{"label": "a", "detail": "d", "verb": "approve", "recommended": True},
                    {"label": "b", "detail": "e", "verb": "reject", "recommended": True}]})
    assert [o["recommended"] for o in out["options"]] == [True, False]


def test_an_unknown_verb_falls_back_to_approve_and_junk_options_are_dropped():
    out = summarize.normalize({
        "summary": "s",
        "options": [{"label": "a", "detail": "d", "verb": "merge"},
                    {"label": "", "detail": "d", "verb": "approve"},
                    "not an option"]})
    assert [(o["label"], o["verb"]) for o in out["options"]] == [("a", "approve")]


def test_long_text_is_truncated_rather_than_sent_whole():
    out = summarize.normalize({"summary": "x" * 5000, "question": "q",
                               "options": []})
    assert len(out["summary"]) <= summarize.SUMMARY_MAX_CHARS + 20
    assert out["summary"].endswith("(truncated)")


def test_a_non_object_answer_is_refused():
    with pytest.raises(ValueError):
        summarize.normalize(["nope"])


def test_the_prompt_version_is_part_of_the_key():
    args = ("t1", "blocked", [1, 2], ["a"])
    before = summarize.fingerprint(*args)
    saved = summarize.PROMPT_VERSION
    try:
        summarize.PROMPT_VERSION = saved + 1
        assert summarize.fingerprint(*args) != before
    finally:
        summarize.PROMPT_VERSION = saved
