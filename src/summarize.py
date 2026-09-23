"""What a parked card is actually asking, and the choices that answer it.

The inbox's only "what is this about" signal was ``approvals._headline`` — the
first line of the newest comment. On the gate this feature exists for, an
architect's design approval, that line reads ``## Design Ready for Human
Review``, which tells the operator nothing they did not already know. Deciding
meant opening the card, reading the thread and reading the attached document.

So the card is summarized at read time by the host's own model: title, body,
park reason, the recent comments, the newest run summary and the text of the
first few attachments go in; a summary, the one question the card is really
asking, and two to four concrete options come back. An option carries the verb
that applies it (``approve``/``reject``) and the text to post with it, so the
operator picks instead of composing — and always edits before sending, because
nothing here decides anything.

Only the two decision lanes are summarized (``approvals.DECISION_LANES``);
``stuck``/``triage``/``guarded`` owe no decision and are unchanged.

Every summary is cached under its card's :func:`fingerprint`, in process and on
disk, so a dashboard polling the inbox every 5s pays one model call per card
revision — and a card nobody opens pays none, because the call is made by the
summary route, never by the list.

Layering (§5.2): no ``hermes_cli`` import. The card comes in already read by
``approvals.summary_inputs``; the only substrate this module touches is the
host's ``agent.plugin_llm`` facade, which runs on the user's active model with
no trust flag to configure.
"""

from __future__ import annotations

import hashlib
import logging
import time

from . import storage

log = logging.getLogger(__name__)

PLUGIN_ID = "hermes-agent-synergia"

# Bumped whenever the prompt or the schema changes: it is part of every cache
# key, so a new prompt invalidates yesterday's summaries instead of serving
# them.
PROMPT_VERSION = 1

CACHE_FILENAME = "approval-summaries.yaml"
CACHE_MAX_CARDS = 200

MAX_COMMENTS = 12
COMMENT_MAX_CHARS = 1500
BODY_MAX_CHARS = 4000
ARTIFACT_MAX_CHARS = 6000
RUN_SUMMARY_MAX_CHARS = 2000

MAX_TOKENS = 900
TIMEOUT_SECONDS = 60.0

SUMMARY_MAX_CHARS = 900
QUESTION_MAX_CHARS = 240
LABEL_MAX_CHARS = 80
DETAIL_MAX_CHARS = 600
MAX_OPTIONS = 4
MAX_RISKS = 4

_VERBS = ("approve", "reject")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "question", "options"],
    "properties": {
        "summary": {"type": "string"},
        "question": {"type": "string"},
        "options": {
            "type": "array",
            "minItems": 2,
            "maxItems": MAX_OPTIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "detail", "verb"],
                "properties": {
                    "label": {"type": "string"},
                    "detail": {"type": "string"},
                    "verb": {"type": "string", "enum": list(_VERBS)},
                    "recommended": {"type": "boolean"},
                },
            },
        },
        "risks": {"type": "array", "maxItems": MAX_RISKS,
                  "items": {"type": "string"}},
    },
}

INSTRUCTIONS = """\
You are briefing a human operator on one card in a Hermes approvals inbox. The
card is parked: an agent stopped working and is waiting for this person's
decision. Read everything given and answer for them.

Produce:
- summary: 2-4 sentences. What was done or proposed, and what is now being
  decided. Concrete and specific — name the actual design, file, command or
  trade-off. Never restate the card's heading.
- question: one sentence. The single decision this card is waiting on.
- options: 2-4 real choices that answer that question, best first. Each has
  - label: a few words naming the choice;
  - detail: the message to post to the agent if the operator picks it, written
    to the agent in the imperative, self-contained, no greeting;
  - verb: "approve" if the choice lets the agent carry on with what it
    proposed, "reject" if the agent must change course first;
  - recommended: true on at most one option, only when the material clearly
    supports it.
  If the card names its own choices, use those. Never invent facts that are
  not in the material; when it is thin, say so in the summary and keep the
  options generic (approve as proposed / request a specific change).
- risks: up to 4 short phrases, only what the material actually supports.

The operator edits any text before it is sent. Write nothing that pretends a
decision has been made.
"""


# ── the cache key ────────────────────────────────────────────────────────────

def fingerprint(task_id, status, comment_ids, attachment_ids) -> str:
    """One card revision. Any new comment, any new attachment and any status
    change is a different card to summarize; a new prompt is too."""
    h = hashlib.sha256()
    h.update(f"{PROMPT_VERSION}|{task_id}|{status or ''}|".encode())
    h.update(",".join(str(c) for c in sorted(comment_ids or [])).encode())
    h.update(b"|")
    h.update(",".join(str(a) for a in sorted(attachment_ids or [])).encode())
    return h.hexdigest()[:16]


def key_of(card) -> str:
    """:func:`fingerprint` of a card read by ``approvals.summary_inputs``."""
    return fingerprint(card.get("id"), card.get("status"),
                       [c.get("id") for c in card.get("comments") or []],
                       [a.get("id") for a in card.get("attachments") or []])


# ── settings ─────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """``plugins.entries.hermes-agent-synergia.settings.approval_summary``
    (true by default): false makes every surface render as it did before."""
    from . import cli
    value = cli._settings().get("approval_summary", True)
    if isinstance(value, str):
        value = value.strip().lower() not in ("false", "off", "0", "no", "")
    return bool(value)


# ── cache ────────────────────────────────────────────────────────────────────

_MEMO: dict = {}


def cache_path():
    return storage.registry_dir() / CACHE_FILENAME


def _load_cache() -> dict:
    import yaml
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                              # noqa: BLE001 — a cache
        return {}
    cards = raw.get("cards") if isinstance(raw, dict) else None
    return cards if isinstance(cards, dict) else {}


def _store(task_id: str, key: str, payload: dict) -> None:
    """Remember one summary, in process and on disk.

    A cache, so a write that fails is logged and dropped: the next read simply
    pays the model call again.
    """
    _MEMO[task_id] = (key, payload)
    row = {"key": key, "written_at": int(time.time()), "payload": payload}

    def _read_current():
        import yaml
        path = cache_path()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        raw = raw if isinstance(raw, dict) else {}
        return int(raw.get("revision") or 0), raw

    def _build(base):
        cards = dict((base or {}).get("cards") or {})
        cards[task_id] = row
        if len(cards) > CACHE_MAX_CARDS:
            oldest = sorted(cards.items(),
                            key=lambda kv: int((kv[1] or {}).get("written_at") or 0))
            for tid, _ in oldest[:len(cards) - CACHE_MAX_CARDS]:
                cards.pop(tid, None)
        return {"schema_version": 1, "cards": cards}

    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        storage.save_yaml_file(path, read_current=_read_current,
                               build_payload=_build, label="approval summaries")
    except Exception as exc:                       # noqa: BLE001 — a cache
        log.debug("hermes-agent-synergia: summary cache write failed: %s", exc)


def cached(task_id: str, key: str):
    """The stored summary for this card revision, or None."""
    hit = _MEMO.get(task_id)
    if hit and hit[0] == key:
        return hit[1]
    row = _load_cache().get(task_id) or {}
    if row.get("key") != key or not isinstance(row.get("payload"), dict):
        return None
    _MEMO[task_id] = (key, row["payload"])
    return row["payload"]


def forget(task_id: str) -> None:
    _MEMO.pop(task_id, None)


# ── the model call ───────────────────────────────────────────────────────────

def _llm():
    """The host's plugin facade. ``ctx.llm`` is this object constructed lazily
    (``hermes_cli/plugins.py``), and the dashboard's API module is exec'd by
    path without a ``register`` call, so it is built here instead of captured.
    """
    from agent.plugin_llm import PluginLlm
    return PluginLlm(plugin_id=PLUGIN_ID)


def _clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def card_blocks(card) -> list:
    """The card as input blocks: what it is, what parked it, what was said,
    what was built, what was attached."""
    lane = card.get("reason_kind") or ""
    head = [f"card: {card.get('id')} — {card.get('title') or '(untitled)'}",
            f"lane: {lane}  (approve/reject are the verbs that move it)",
            f"assignee: {card.get('assignee') or '(unassigned)'}",
            f"status: {card.get('status')}"]
    if card.get("block_reason"):
        head.append(f"parked because: {card['block_reason']}")
    blocks = [{"type": "text", "text": "\n".join(head)}]
    if card.get("body"):
        blocks.append({"type": "text",
                       "text": "card body:\n" + _clip(card["body"], BODY_MAX_CHARS)})
    if card.get("run_summary"):
        blocks.append({"type": "text",
                       "text": "newest run summary (the agent's own handoff):\n"
                               + _clip(card["run_summary"], RUN_SUMMARY_MAX_CHARS)})
    comments = list(card.get("comments") or [])[-MAX_COMMENTS:]
    if comments:
        lines = [f"[{c.get('author') or '?'}] {_clip(c.get('body'), COMMENT_MAX_CHARS)}"
                 for c in comments]
        blocks.append({"type": "text",
                       "text": "comments, oldest first:\n" + "\n\n".join(lines)})
    for att in card.get("artifact_texts") or []:
        blocks.append({"type": "text",
                       "text": f"attachment {att['filename']}:\n"
                               + _clip(att.get("text"), ARTIFACT_MAX_CHARS)})
    return blocks


def _option(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    label = _clip(raw.get("label"), LABEL_MAX_CHARS)
    detail = _clip(raw.get("detail"), DETAIL_MAX_CHARS)
    if not label or not detail:
        return {}
    verb = str(raw.get("verb") or "").strip().lower()
    return {"label": label, "detail": detail,
            "verb": verb if verb in _VERBS else "approve",
            "recommended": bool(raw.get("recommended"))}


def normalize(parsed) -> dict:
    """The model's answer, clamped to the shape the UI renders. Raises
    ``ValueError`` when there is nothing usable — the card then renders the way
    it always did rather than showing half a briefing."""
    if not isinstance(parsed, dict):
        raise ValueError("model returned no JSON object")
    summary = _clip(parsed.get("summary"), SUMMARY_MAX_CHARS)
    if not summary:
        raise ValueError("model returned no summary")
    options = [o for o in (_option(x) for x in parsed.get("options") or []) if o]
    seen = False
    for opt in options:
        # At most one recommendation, or the operator is being pushed twice.
        if opt["recommended"] and seen:
            opt["recommended"] = False
        seen = seen or opt["recommended"]
    risks = [_clip(r, LABEL_MAX_CHARS) for r in (parsed.get("risks") or [])]
    return {"summary": summary,
            "question": _clip(parsed.get("question"), QUESTION_MAX_CHARS),
            "options": options[:MAX_OPTIONS],
            "risks": [r for r in risks if r][:MAX_RISKS]}


def summarize(card, *, llm=None) -> dict:
    """One model call over one card. Never raises: a failure is a payload the
    surfaces render as "no summary", because a briefing that did not arrive
    must not cost the operator the card."""
    try:
        client = llm if llm is not None else _llm()
        result = client.complete_structured(
            instructions=INSTRUCTIONS,
            input=card_blocks(card),
            json_schema=SCHEMA,
            schema_name="approval_briefing",
            max_tokens=MAX_TOKENS,
            timeout=TIMEOUT_SECONDS,
            purpose="approval-summary",
        )
        payload = normalize(getattr(result, "parsed", None))
        payload["state"] = "ready"
        payload["model"] = getattr(result, "model", None)
        return payload
    except Exception as exc:                       # noqa: BLE001 — a briefing
        log.debug("hermes-agent-synergia: approval summary failed: %s", exc)
        return {"state": "error", "error": f"{type(exc).__name__}: {exc}"[:200],
                "summary": "", "question": "", "options": [], "risks": []}


# ── the one entry point ──────────────────────────────────────────────────────

def summary(t, task_id: str, *, refresh: bool = False, llm=None) -> dict:
    """The card's briefing: cached, or computed once and cached.

    ``state`` is what a surface switches on: ``off`` (setting disabled),
    ``skipped`` (a lane that owes no decision), ``ready`` or ``error``.
    """
    from . import approvals
    if not enabled():
        return {"state": "off", "task_id": task_id}
    card = approvals.summary_inputs(t, task_id)
    if card.get("reason_kind") not in approvals.DECISION_LANES:
        return {"state": "skipped", "task_id": task_id,
                "lane": card.get("reason_kind")}
    key = key_of(card)
    payload = None if refresh else cached(task_id, key)
    cache_hit = payload is not None
    if payload is None:
        payload = summarize(card, llm=llm)
        if payload.get("state") == "ready":
            _store(task_id, key, payload)
    out = dict(payload)
    out.update({"task_id": task_id, "team": t.slug, "key": key,
                "lane": card.get("reason_kind"), "cached": cache_hit})
    return out
