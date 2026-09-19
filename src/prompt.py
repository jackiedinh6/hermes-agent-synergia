"""prompt.py — the team-contract section, pure (spec §5.2 layering).

Layer: ``prompt.py ─► (nothing)``. Like ``policy.py``, this module performs
**no I/O** and imports nothing but stdlib ``typing`` — no ``hermes_cli``, no
``os``/``os.path``, no ``pathlib``, not even ``model``: team records are
consumed duck-typed (attributes or Mapping keys, read never constructed).
It is consumed by the hook that passes it to
``ctx.register_system_prompt_section``: rendered once per session at
session start from the live registry, and persisted verbatim by core.

Public API:

* :func:`render` — ``(team, profile) -> str``, the contract block below.
* :func:`render_attachments` — the attachments block (static text).
* :data:`MAX_CHARS` — the hard cap (§13), enforced on the FINAL string.
* :data:`KNOWN_ROLES` / :data:`ROLE_CLOSINGS` — the six well-known roles
  and their closing lines; anything else renders :data:`GENERIC_CLOSING`.

The block:

    ## Team: <display name> (<slug>)
    Description: <sanitized, one line — droppable under the cap>
    You are `<profile>`, role: <role>.
    Board: <board>   Work directory: <workdir>   Tenant: <tenant>
    Members: <profile> (<role>[; primary][; you]), ...
    Rules: create and assign cards only on board `<board>`, only to the
    members above.
    Use team_create_task / team_assign. Card `tenant` must be `<tenant>`.
    <role-specific closing line>

Security (§13, "registry as an injection vector"): ``teams.yaml`` content
lands in the system prompt. Every interpolated field is run through
:func:`clean` first — all C0/C1 control characters (NUL, ESC/ANSI, BEL, CR/LF
included) and format characters (zero-width, bidi) are removed and whitespace
runs collapse to single spaces, so a description can never open a new line or
forge a section header. The result is hard-capped at ``MAX_CHARS``: the
description line is dropped first, then the tail truncated — the team line,
"You are" line, roster, rules and closing line are the payload that survives.

Tenant: an empty ``tenant`` field means the slug.
"""

from __future__ import annotations

from typing import Any, Mapping

# §13 hard cap. Enforced on the FINAL string, after sanitisation.
MAX_CHARS = 1000

# The six well-known roles the renderer understands; unknown roles
# render generically (never raise — a bad role string must not cost a session
# its team context).
KNOWN_ROLES = ("orchestrator", "implementer", "reviewer",
               "researcher", "verifier", "writer")

# Role-specific closings (the last line of the block). Each states what
# this role does and where its boundary is.
ROLE_CLOSINGS = {
    "orchestrator": (
        "Your role is to decompose, assign and accept work — delegate to the "
        "members above; do not implement it yourself."
    ),
    "implementer": (
        "Your role implements the cards assigned to you on this team's "
        "board, in the team work directory above."
    ),
    "reviewer": (
        "Your role does not implement changes; it reviews them."
    ),
    "researcher": (
        "Your role gathers evidence and reports findings; it does not merge, "
        "deploy or rewrite team files."
    ),
    "verifier": (
        "Your role verifies completed cards against their acceptance criteria "
        "before they ship."
    ),
    "writer": (
        "Your role drafts and edits documentation and user-facing text, not "
        "product code."
    ),
}

# Fallback for roles outside the six.
GENERIC_CLOSING = "Follow the team rules above in everything you do."

# The rules line (§13: boundary restated — board + roster are the limits).
_RULES_TEMPLATE = (
    "Rules: create and assign cards only on board `{board}`, "
    "only to the members above."
)
_STAMP_TEMPLATE = (
    "Use team_create_task / team_assign. Card `tenant` must be `{tenant}`."
)

# Role shown when the queried profile has no roster entry.
_UNLISTED_ROLE = "unlisted"


# ── §13 sanitisation ─────────────────────────────────────────────────────────

def _is_control(ch: str) -> bool:
    """True for characters that could forge a prompt boundary: C0 (NUL …
    US, i.e. everything < 0x20, plus DEL 0x7F), C1 (0x80–0x9F, ANSI-adjacent
    escapes included) and the Cf class — zero-width, word-joiner and bidi
    controls that can hide text or invert it. Written as ord ranges because
    §5.2 purity allows no ``unicodedata`` import."""
    o = ord(ch)
    return (o < 0x20 or 0x7F <= o <= 0x9F
            or 0x200B <= o <= 0x200F       # zero-width + bidi marks/overrides
            or 0x202A <= o <= 0x202E       # bidi embedding controls
            or 0x2060 <= o <= 0x2064       # word joiner, invisible ops
            or o == 0xFEFF                 # byte-order mark
            or 0xFFF9 <= o <= 0xFFFB)      # interlinear annotation controls


def clean(value: Any) -> str:
    """Sanitize one registry-sourced field for interpolation (§13).

    Control and format characters become a space (CR/LF cannot survive, so a
    description cannot open a new line or forge a ``##`` header; substitution
    rather than deletion so glued tokens like ``"end\rc"`` never merge into
    one word), then whitespace runs collapse to single spaces. ``None`` reads
    as ``""``; non-strings are stringified defensively.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if not text:
        return ""
    stripped = "".join(" " if _is_control(ch) else ch for ch in text)
    return " ".join(stripped.split())


# ── duck-typed record readers (prompt.py imports no model, §5.2) ─────────────

def _get(obj: Any, name: str, default: Any = None) -> Any:
    """One field of a record: ``model`` dataclasses answer by attribute,
    plain mappings (the ``to_dict``/YAML shape) by key."""
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _roster_entry(member: Any, self_profile: str) -> str:
    profile = clean(_get(member, "profile"))
    role = clean(_get(member, "role")) or "member"
    flags = []
    if _get(member, "primary", False):
        flags.append("primary")
    if profile and profile == self_profile:
        flags.append("you")
    tail = ("; " + "; ".join(flags)) if flags else ""
    return f"{profile} ({role}{tail})"


def _role_of(team: Any, self_profile: str) -> str:
    """The queried profile's role, or ``unlisted`` when it is not in the
    roster (rendered generically — never raises)."""
    for m in (_get(team, "members") or ()):
        if clean(_get(m, "profile")) == self_profile:
            return clean(_get(m, "role")) or "member"
    return _UNLISTED_ROLE


# ── rendering ────────────────────────────────────────────────────────────────

def closing_for(role: Any) -> str:
    """The role-specific closing line: one of the six known, else
    :data:`GENERIC_CLOSING`. Case- and whitespace-tolerant."""
    name = clean(role).lower()
    return ROLE_CLOSINGS.get(name, GENERIC_CLOSING)


def _truncate(text: str, max_chars: int) -> str:
    """Last resort under the cap: keep the head, cut at the last word
    boundary, mark the cut. The leading structural lines are what survive."""
    if max_chars <= 1:
        return ""
    cut = text[: max_chars - 1]
    head, sep, _ = cut.rpartition(" ")
    if sep and head:
        cut = head
    return cut.rstrip() + "…"


def render(team: Any, profile: Any, max_chars: int = MAX_CHARS) -> str:
    """The contract block for *profile* acting on *team* — see the module
    docstring for the shape. *team* is a ``model.Team`` (or its dict shape);
    *profile* the session's profile name. Everything interpolated from the
    team record passes :func:`clean`; the result passes :data:`MAX_CHARS`
    unconditionally: over the cap the description line is dropped first, then
    the tail truncated, so the structural payload is always intact and a
    hand-edited description can never smuggle a large prompt payload (§13).

    Returns a single string for ``register_system_prompt_section``.
    Deterministic: the same inputs always render the same bytes.
    """
    slug = clean(_get(team, "slug"))
    display = clean(_get(team, "display_name")) or slug
    board = clean(_get(team, "board")) or slug
    workdir = clean(_get(team, "workdir"))
    tenant = clean(_get(team, "tenant")) or slug  # Empty tenant = the slug
    description = clean(_get(team, "description"))
    self_profile = clean(profile)
    role = _role_of(team, self_profile)

    lines: list = [f"## Team: {display} ({slug})"]
    if description:
        lines.append(f"Description: {description}")
    lines.append(f"You are `{self_profile}`, role: {role}.")
    lines.append(f"Board: {board}   Work directory: {workdir}   Tenant: {tenant}")
    lines.append(
        "Members: " + ", ".join(
            _roster_entry(m, self_profile) for m in (_get(team, "members") or ())
        )
    )
    lines.append(_RULES_TEMPLATE.format(board=board))
    lines.append(_STAMP_TEMPLATE.format(tenant=tenant))
    lines.append(closing_for(role))

    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    # §13: over the cap, the description is the payload to drop — it is the
    # one line a hand-editor controls and the one nothing downstream parses.
    lines = [ln for ln in lines if not ln.startswith("Description: ")]
    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    return _truncate(block, max_chars)

# ── the attachments section ──────────────────────────────────────────────────

# Registered as a second system-prompt section, so the guidance ships with the
# tool instead of being patched into the substrate's own KANBAN_GUIDANCE.
ATTACHMENTS_MAX_CHARS = 600

_ATTACHMENTS_BLOCK = (
    "## Attachments (team)\n"
    "Attach real downloadable artifacts; never paste a link in a comment instead.\n"
    "- `team_attach_path` — a file that already exists on disk. Hermes reads the "
    "bytes itself; this is the normal case.\n"
    "- `kanban_attach` — only for bytes you are already holding in the conversation.\n"
    "- `kanban_attach_url` — only for a link you were given.\n"
    "Never hand-encode base64, and never publish an internal file somewhere "
    "fetchable just to get a URL. 25 MB cap; `kanban_attachments` lists a card's "
    "files. Workers may attach only to their own card."
)


def render_attachments(max_chars: int = ATTACHMENTS_MAX_CHARS) -> str:
    """The attachments block — static text, capped like :func:`render`."""
    if len(_ATTACHMENTS_BLOCK) <= max_chars:
        return _ATTACHMENTS_BLOCK
    return _truncate(_ATTACHMENTS_BLOCK, max_chars)
