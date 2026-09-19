"""S14 verification — schemas + tools (spec §7, plan S14).

Covers the four things the step says must be true:

1. The nine schemas exist and match ``TOOL_TABLE``; the two §7.4 dicts match
   the spec **literally** — the test re-parses
   ``docs/hermes-agent-synergia-plugin-design.md`` and ``ast.literal_eval``s the §7.4
   code block, so the byte-freeze is enforced by the suite, not by a comment.
2. Visibility (§7.1): a fabricated delegated-child context registers zero
   team tools; a non-team profile sees none; a worker (team member, not
   orchestrator) sees exactly four; the orchestrator sees all nine.
3. Envelopes (§7.3): every failure path carries a non-empty code, message,
   hint AND remedy.
4. The read-only promises: ``team_catalog`` and ``team_sync`` write nothing
   (byte snapshots around the call), and ``team_sync`` never reaches
   ``reconcile.apply`` even if a caller tries to make it.

Plus §7.5 budget and the no-``team_clone`` rule (§7.2).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

import errors
import model
import reconcile
import storage
from hermes_agent_synergia import schemas, tools
from model import Member, Team

# ── spec paths (the byte-freeze source of truth) ─────────────────────────────

_SPEC = (pathlib.Path(__file__).resolve().parents[1]
         / "docs" / "hermes-agent-synergia-plugin-design.md")


# The design doc is the byte-freeze source. It is not vendored into this
# checkout yet (carve-out dropped docs/); the gate arms itself the moment the
# file lands, rather than failing every run until then.
_spec_required = pytest.mark.skipif(
    not _SPEC.is_file(),
    reason=f"byte-freeze source absent: {_SPEC.name} — the §7.4 gate is off "
           "until the design doc is back in docs/")


def _spec_schema(name: str) -> dict:
    """The literal assigned to *name* inside the §7.4 code block."""
    text = _SPEC.read_text(encoding="utf-8")
    head = text.index("### 7.4 Key schemas")
    block = text[head:text.index("```", text.index("```", head) + 3) + 0]
    # first fenced python block after the heading
    start = text.index("```", head)
    stop = text.index("\n```", start + 3)
    block_src = text[start + 3:stop]
    tree = ast.parse(block_src.replace("\r\n", "\n"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in §7.4 code block")


def _tree_fingerprint(root):
    """path -> (size, sha256) for every file under *root*. mtimes are not
    trusted (a rewrite could preserve them); content and existence are."""
    fp = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = pathlib.Path(dirpath) / name
            try:
                data = p.read_bytes()
            except OSError:
                data = b"<unreadable>"
            fp[str(p.relative_to(root))] = (len(data), hashlib.sha256(data).hexdigest())
    return fp


# ── world fixtures (same shape as test_dashboard_api's world/profile) ────────

@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    storage._reset_caches_for_tests()
    import catalog
    catalog._reset_caches_for_tests()
    yield
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()


@pytest.fixture
def wd(hermes_root):
    """A legal team workdir (outside the Hermes root — model.validate_team)."""
    d = hermes_root.parent / "team-work"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _make_team(slug, members, orchestrator, board, wd, tenant=None):
    import hermes_constants
    t = Team.from_dict({
        "slug": slug,
        "display_name": slug + " team",
        "created_at": model.now_iso(),
        "updated_at": model.now_iso(),
        "board": board,
        "workdir": wd,
        "tenant": tenant or slug,
        "orchestrator": orchestrator,
        "members": [{"profile": p, "role": ("orchestrator" if p == orchestrator
                                             else "implementer")}
                    for p in members],
    })
    model.validate_team(t, hermes_constants.get_default_hermes_root())
    return t


@pytest.fixture
def world(hermes_root, profile, wd, monkeypatch):
    """Registry with team ``px`` (orchestrator qa, member qa-2; outsider is
    on nothing), an on-disk board, and the two member profiles."""
    profile("qa")
    profile("qa-2")
    profile("outsider")
    from hermes_cli import kanban_db as _kb
    reg = storage.Registry(teams={"px": _make_team(
        "px", ["qa", "qa-2"], "qa", "px", wd)})
    storage.save(reg, expected_revision=reg.revision)
    return hermes_root


def _call(tool_name: str, args: dict) -> dict:
    """Invoke a registered handler exactly as the substrate would."""
    for name, _schema, handler, _emoji, _check in tools.TOOL_TABLE:
        if name == tool_name:
            return json.loads(handler(dict(args)))
    raise AssertionError(f"{tool_name} not registered")


# ── 1. schemas ↔ TOOL_TABLE, verbatim §7.4, budget ───────────────────────────

def test_ten_schemas_exposed_in_order():
    assert len(schemas.ALL) == 10
    names = [s["name"] for s in schemas.ALL]
    assert len(set(names)) == 10
    for s in schemas.ALL:
        assert s["parameters"]["type"] == "object"
        assert isinstance(s["description"], str) and s["description"]


def test_tool_table_matches_schemas():
    assert len(tools.TOOL_TABLE) == 10
    for row in tools.TOOL_TABLE:
        assert len(row) == 5
        name, schema, handler, emoji, check_fn = row
        assert callable(handler) and callable(check_fn) and emoji
        assert schema is schemas._BY_NAME[name] if hasattr(schemas, "_BY_NAME") \
            else schema["name"] == name


@_spec_required
def test_team_create_task_verbatim_spec():
    assert schemas.TEAM_CREATE_TASK_SCHEMA == _spec_schema("TEAM_CREATE_TASK_SCHEMA")


@_spec_required
def test_team_status_verbatim_spec():
    assert schemas.TEAM_STATUS_SCHEMA == _spec_schema("TEAM_STATUS_SCHEMA")


def test_budget_under_1200_tokens():
    # §7.5 capped the nine at 1 000; team_attach_path raises it to 1 200.
    n = sum(len(json.dumps(s)) for s in schemas.ALL) // 4
    assert n < 1200, n


def test_no_team_clone_anywhere():
    names = {s["name"] for s in schemas.ALL} | {r[0] for r in tools.TOOL_TABLE}
    assert "team_clone" not in names
    assert not any("clone" in n for n in names)


def test_schemas_module_imports_only_typing():
    src = pathlib.Path(tools.__file__).with_name("schemas.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in ("__future__", "typing"), \
                    f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in ("__future__", "typing"), \
                f"forbidden from-import: {node.module}"


# ── 2. visibility (§7.1) ─────────────────────────────────────────────────────

def _visible_names() -> set:
    return {name for name, _s, _h, _e, check in tools.TOOL_TABLE if check()}


def test_delegated_child_sees_zero(world, monkeypatch):
    from agent.delegation_context import delegated_child_context
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    with delegated_child_context("s-test"):
        assert _visible_names() == set()


def test_non_team_profile_sees_none(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "outsider")
    assert _visible_names() == set()


def test_no_profile_at_all_sees_none(world, monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert _visible_names() == set()


def test_worker_sees_exactly_the_read_tools_plus_attach(world, monkeypatch):
    # A worker must be able to attach its own artifacts, so
    # team_attach_path joins the four read tools.
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    assert _visible_names() == {"team_whoami", "team_list", "team_show",
                                "team_status", "team_attach_path"}


def test_orchestrator_sees_all_ten(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    assert len(_visible_names()) == 10
    assert _visible_names() == {s["name"] for s in schemas.ALL}


def test_handoff_is_orchestrator_only_deviation_commented():
    # implement.md S14.3 beats the §7.2 table for team_handoff (see the
    # deviation note in the tools.py module docstring).
    assert "team_handoff" in tools._ORCHESTRATOR_ONLY


# ── 3. envelopes (§7.3): every error carries code/message/hint/remedy ────────

def _assert_envelope(env: dict):
    assert env["ok"] is False
    err = env["error"]
    for key in ("code", "message", "hint", "remedy"):
        assert err.get(key), f"{key} missing/empty: {err}"
    assert err["code"] in errors.ALL_CODES


def test_outsider_assignee_envelope(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call("team_create_task", {"title": "t", "assignee": "outsider"})
    _assert_envelope(env)
    assert env["error"]["code"] == "E_TEAM_NOT_MEMBER"
    assert "qa" in env["error"]["hint"]          # hint lists the roster
    assert "add-member" in env["error"]["remedy"]  # remedy is runnable


def test_create_task_missing_title(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    _assert_envelope(_call("team_create_task", {}))


def test_show_unknown_team(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call("team_show", {"team": "nope"})
    _assert_envelope(env)
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"


def test_status_bad_limit(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    _assert_envelope(_call("team_status", {"limit": 1000}))


def test_assign_unknown_card(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    _assert_envelope(_call("team_assign", {"task_id": "nope", "assignee": "qa"}))


# ── 4. the read tools ────────────────────────────────────────────────────────

def test_whoami(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    env = _call("team_whoami", {})
    assert env["ok"] is True and env["team"] == "px"
    data = env["data"]
    assert data["profile"] == "qa-2"
    assert data["role"] == "implementer"
    assert data["board"] == "px"
    assert data["workdir"].endswith("team-work")
    assert data["teammates"] == ["qa"]


def test_list_only_own_teams(world, monkeypatch):
    # add a second team qa is NOT on
    reg = storage.load()
    reg2 = storage.Registry(
        schema_version=reg.schema_version,
        teams={**reg.teams, "other": _make_team(
            "other", ["outsider"], "outsider", "other",
            reg.teams["px"].workdir)})
    storage.save(reg2, expected_revision=reg.revision)
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call("team_list", {})
    data = env["data"]
    assert [t["slug"] for t in data["teams"]] == ["px"]


def test_show_has_resolved(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call("team_show", {})
    assert env["ok"] is True
    team = env["data"]["team"]
    assert team["slug"] == "px"
    assert "_resolved" in team


def test_status_counts_and_open_cards(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "do it", "assignee": "qa-2"})["data"]["task_id"]
    env = _call("team_status", {})
    data = env["data"]
    assert set(data["counts"]) == {"todo", "ready", "running", "review",
                                   "done", "blocked"}
    created = _task_row("px", tid)
    assert sum(data["counts"].values()) == 1
    assert data["counts"][created.status] == 1
    assert data["open_cards"][0]["id"] == tid
    assert data["open_cards"][0]["assignee"] == "qa-2"
    # tenant-scoping: a foreign card on the SAME board is invisible
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing(db_path=kb.kanban_db_path("px")) as conn:
        kb.create_task(conn, title="foreign", assignee="someone-else",
                       tenant="not-px", board="px")
    env2 = _call("team_status", {})
    assert sum(env2["data"]["counts"].values()) == 1  # foreign tenant excluded
    # status filter: finish it, then it drops out of the open set
    _call("team_handoff", {"task_id": tid, "summary": "done", "assignee": "qa"})
    env3 = _call("team_status", {"status": "done"})
    assert any(c["id"] == tid for c in env3["data"]["open_cards"])


# ── 5. team_catalog: read-only, remedy-bearing ───────────────────────────────

PACK_ENTRIES = [
    {"id": "agency-backend-engineer", "pack": "agency", "category": "engineering",
     "description": "Builds APIs."},
    {"id": "agency-security-reviewer", "pack": "agency", "category": "security",
     "description": "Threat models and reviews."},
]


def _make_pack_root(dest, entries):
    """Minimal pack root: packs.json + manifest + a profile dir per entry."""
    pack = dest / "agency"
    pack.mkdir(parents=True)
    (dest / "packs.json").write_text(json.dumps(
        {"packs": [{"name": "agency", "manifest": "manifest.json",
                    "path": "agency"}]}), encoding="utf-8")
    (dest / "manifest.json").write_text(json.dumps(
        {"profiles": [{"name": e["id"], "display_name": e["id"],
                       "category": e["category"]} for e in entries],
         "distribution": {"profile_root": "profiles"}}), encoding="utf-8")
    for e in entries:
        d = pack / "profiles" / e["id"]
        d.mkdir(parents=True)
        (d / "distribution.yaml").write_text(
            yaml.safe_dump({"description": e["description"]}), encoding="utf-8")
    return dest


@pytest.fixture
def catalog_world(world, tmp_path, monkeypatch):
    root = _make_pack_root(tmp_path / "packs", PACK_ENTRIES)
    cfg = world / "config.yaml"
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    raw.setdefault("plugins", {}).setdefault("entries", {}).setdefault(
        "hermes-agent-synergia", {}).setdefault("settings", {})["catalog_paths"] = [str(root)]
    cfg.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return root


def test_catalog_filters_read_only(catalog_world, world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    before = _tree_fingerprint(world)
    env = _call("team_catalog", {"category": "engineering"})
    assert env["ok"] is True
    ids = [e["id"] for e in env["data"]["entries"]]
    assert ids == ["agency-backend-engineer"]
    for e in env["data"]["entries"]:
        assert set(e) >= {"id", "display_name", "category",
                          "description", "suggested_role"}
    # search filter narrows, and nothing on disk moved
    env2 = _call("team_catalog", {"search": "security"})
    assert [e["id"] for e in env2["data"]["entries"]] == ["agency-security-reviewer"]
    assert _tree_fingerprint(world) == before


def test_catalog_description_offers_the_remedy():
    d = schemas.TEAM_CATALOG_SCHEMA["description"]
    assert "nothing" in d.lower()          # "creates nothing", in words
    assert "hermes team add-member" in d   # the runnable proposal


# ── 6. team_sync: dry-run only, never writes, never reaches apply ────────────

def test_sync_dry_run_never_writes(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")

    def _boom(*a, **k):
        raise AssertionError("team_sync must never call reconcile.apply")

    monkeypatch.setattr(reconcile, "apply", _boom)
    before = _tree_fingerprint(world)
    env = _call("team_sync", {})
    assert env["ok"] is True
    data = env["data"]
    assert data["dry_run"] is True and data["nothing_written"] is True
    assert isinstance(data["drift"], list)
    assert _tree_fingerprint(world) == before


def test_sync_description_says_dry_run():
    d = schemas.TEAM_SYNC_SCHEMA["description"]
    assert "dry-run" in d.lower() or "dry run" in d.lower()
    assert "never" in d.lower()


# ── 7. writes that are allowed: create / assign / handoff ────────────────────

def _task_row(board, task_id):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return kanban_db.get_task(conn, task_id)


def test_create_task_happy_path(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    env = _call("team_create_task", {"title": "Wire S14", "body": "spec",
                                     "assignee": "qa-2"})
    assert env["ok"] is True, env
    data = env["data"]
    assert data["board"] == "px" and data["tenant"] == "px"
    assert data["assignee"] == "qa-2"
    row = _task_row("px", data["task_id"])
    assert row.tenant == "px" and row.assignee == "qa-2" and row.title == "Wire S14"
    # never accepts a board param: the record wins
    env2 = _call("team_create_task", {"title": "t2", "assignee": "qa-2",
                                      "board": "someone-elses-board"})
    assert env2["ok"] is True
    assert _task_row("px", env2["data"]["task_id"]) is not None


def test_create_task_worktree_inits_missing_repo(world, wd, monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    t = storage.load().teams["px"]
    storage.upsert(Team.from_dict({**t.to_dict(), "workspace_policy": "worktree"}))

    # no workdir at all: nothing to init, refused before any card exists
    os.rename(wd, wd + ".moved")
    env = _call("team_create_task", {"title": "t", "assignee": "qa-2"})
    assert env["ok"] is False, env
    assert env["error"]["code"] == "E_TEAM_DRIFT_UNFIXED"
    assert "git" in env["error"]["remedy"]
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px")) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 0

    # workdir without a repo (team predates create-time init): inited here
    os.rename(wd + ".moved", wd)
    assert not os.path.exists(os.path.join(wd, ".git"))
    env = _call("team_create_task", {"title": "t", "assignee": "qa-2"})
    assert env["ok"] is True, env
    assert os.path.isdir(os.path.join(wd, ".git"))
    assert _task_row("px", env["data"]["task_id"]).workspace_kind == "worktree"


def test_create_task_shared_uses_the_team_checkout(world, wd, monkeypatch):
    """`shared` teams run every card in ONE checkout (kind `dir`), which the
    substrate never removes on completion — unlike `worktree`, whose branch is
    deleted with the tree."""
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    t = storage.load().teams["px"]
    storage.upsert(Team.from_dict({**t.to_dict(), "workspace_policy": "shared"}))

    env = _call("team_create_task", {"title": "t", "assignee": "qa-2"})
    assert env["ok"] is True, env
    row = _task_row("px", env["data"]["task_id"])
    assert row.workspace_kind == "dir"
    assert row.workspace_path == wd
    assert os.path.isdir(os.path.join(wd, ".git"))


def test_assign_reassigns_member_card(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "t", "assignee": "qa"})["data"]["task_id"]
    env = _call("team_assign", {"task_id": tid, "assignee": "qa-2"})
    assert env["ok"] is True
    assert _task_row("px", tid).assignee == "qa-2"


def test_assign_outsider_refused(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "t", "assignee": "qa"})["data"]["task_id"]
    _assert_envelope(_call("team_assign", {"task_id": tid, "assignee": "outsider"}))


def test_handoff_completes_and_links_successor(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "review", "assignee": "qa"})["data"]["task_id"]
    env = _call("team_handoff", {"task_id": tid, "summary": "findings ...",
                                 "assignee": "qa-2"})
    assert env["ok"] is True, env
    data = env["data"]
    assert data["completed"] == tid
    assert data["parents"] == [tid]
    assert _task_row("px", tid).status == "done"
    child = _task_row("px", data["created"])
    assert child.assignee == "qa-2"
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px")) as conn:
        parents = [r["parent_id"] for r in conn.execute(
            "select parent_id from task_links where child_id = ?",
            (data["created"],))]
    assert parents == [tid]


def test_handoff_outsider_next_assignee_refused(world, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "t", "assignee": "qa"})["data"]["task_id"]
    _assert_envelope(_call("team_handoff", {"task_id": tid, "summary": "s",
                                           "assignee": "outsider"}))


# ── 8. team_attach_path ──────────────────────────────────────────────────────

def _call_kw(tool_name: str, args: dict, **kw) -> dict:
    """Invoke a handler with the substrate's dispatch kwargs — ``task_id`` here
    is the terminal/workspace session key, not a kanban card id."""
    for name, _schema, handler, _emoji, _check in tools.TOOL_TABLE:
        if name == tool_name:
            return json.loads(handler(dict(args), **kw))
    raise AssertionError(f"{tool_name} not registered")


@pytest.fixture
def attach_world(world, tmp_path, monkeypatch):
    """Team ``px`` with one card assigned to ``qa-2``, plus a recorded task
    workspace holding ``hello.txt``. Yields ``(card_id, workspace, session_key)``.

    The workspace is recorded the way a real session records it (after a
    command, or on workspace-override registration), so the confinement check
    exercises the substrate's own anchor rather than a stub.
    """
    from tools import terminal_tool
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    tid = _call("team_create_task", {"title": "attach me",
                                     "assignee": "qa-2"})["data"]["task_id"]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_bytes(b"hi\n")
    session_key = "sess-attach"
    terminal_tool.record_session_cwd(session_key, str(ws))
    try:
        yield tid, ws, session_key
    finally:
        terminal_tool.clear_session_cwd(session_key)


def _attachments(board, task_id):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return kanban_db.list_attachments(conn, task_id)


def test_attach_path_worker_happy_path(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "hello.txt"}, task_id=key)
    assert env["ok"] is True, env
    data = env["data"]
    assert data["task_id"] == tid and data["board"] == "px"
    assert data["filename"] == "hello.txt" and data["size"] == 3
    assert data["content_type"] == "text/plain"
    rows = _attachments("px", tid)
    assert [r.filename for r in rows] == ["hello.txt"]
    assert pathlib.Path(rows[0].stored_path).read_bytes() == b"hi\n"


def test_attach_path_filename_and_content_type_overrides(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path",
                   {"path": "hello.txt", "filename": "greeting.log",
                    "content_type": "application/x-custom"}, task_id=key)
    assert env["ok"] is True, env
    assert env["data"]["filename"] == "greeting.log"
    assert env["data"]["content_type"] == "application/x-custom"


def test_attach_path_rejects_traversal(attach_world, monkeypatch):
    tid, ws, key = attach_world
    (ws.parent / "outside.txt").write_bytes(b"secret")
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "../outside.txt"}, task_id=key)
    _assert_envelope(env)
    assert "'..'" in env["error"]["message"]
    assert not _attachments("px", tid)


def test_attach_path_rejects_outside_workspace(attach_world, monkeypatch):
    tid, ws, key = attach_world
    outside = ws.parent / "outside.txt"
    outside.write_bytes(b"secret")
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": str(outside)}, task_id=key)
    _assert_envelope(env)
    assert "outside the task workspace" in env["error"]["message"]
    assert not _attachments("px", tid)


def test_attach_path_rejects_oversize_before_reading(attach_world, monkeypatch):
    tid, ws, key = attach_world
    from hermes_cli import kanban_db
    (ws / "big.bin").write_bytes(b"x" * 64)
    monkeypatch.setattr(kanban_db, "KANBAN_ATTACHMENT_MAX_BYTES", 16)
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "big.bin"}, task_id=key)
    _assert_envelope(env)
    assert "64 bytes" in env["error"]["message"]
    assert not _attachments("px", tid)


def test_attach_path_rejects_missing_file(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "nope.txt"}, task_id=key)
    _assert_envelope(env)
    assert "no file at" in env["error"]["message"]


def test_attach_path_rejects_directory(attach_world, monkeypatch):
    tid, ws, key = attach_world
    (ws / "sub").mkdir()
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "sub"}, task_id=key)
    _assert_envelope(env)
    assert "not a regular file" in env["error"]["message"]


def test_attach_path_worker_cannot_touch_a_sibling_card(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    other = _call("team_create_task", {"title": "sibling",
                                       "assignee": "qa-2"})["data"]["task_id"]
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {"path": "hello.txt", "task_id": other},
                   task_id=key)
    _assert_envelope(env)
    assert "scoped to card" in env["error"]["message"]
    assert "kanban_comment" in env["error"]["remedy"]
    assert not _attachments("px", other)


def test_attach_path_orchestrator_confined_to_own_tenant(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing(db_path=kb.kanban_db_path("px")) as conn:
        foreign = kb.create_task(conn, title="foreign", assignee="someone-else",
                                 tenant="not-px", board="px")
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    env = _call_kw("team_attach_path", {"path": "hello.txt", "task_id": str(foreign)},
                   task_id=key)
    _assert_envelope(env)
    assert env["error"]["code"] == "E_TEAM_NOT_FOUND"
    # its own card still works, so the guard is the tenant and not the caller
    env2 = _call_kw("team_attach_path", {"path": "hello.txt", "task_id": tid},
                    task_id=key)
    assert env2["ok"] is True, env2


def test_attach_path_needs_a_card(attach_world, monkeypatch):
    _tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    env = _call_kw("team_attach_path", {"path": "hello.txt"}, task_id=key)
    _assert_envelope(env)
    assert "task_id is required" in env["error"]["message"]


def test_attach_path_needs_a_path(attach_world, monkeypatch):
    tid, _ws, key = attach_world
    monkeypatch.setenv("HERMES_PROFILE", "qa-2")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = _call_kw("team_attach_path", {}, task_id=key)
    _assert_envelope(env)
    assert "path is required" in env["error"]["message"]
