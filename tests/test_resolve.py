"""S7 — resolve.py read half.

A temp root with two profiles and one board (per the step's Verify):
assert the §4.3 ``_resolved`` shape field for field; a deleted profile
yields ``exists: false`` rather than an exception; ``terminal.cwd: "."``
is reported as unset, not as the literal ``"."`` (hazard table).
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import types

import pytest

import resolve
from errors import TeamError
from model import Member, Project, Team

# §4.3's _resolved keys, verbatim and in order.
RESOLVED_KEYS = [
    "board_exists", "board_db_path", "board_default_workdir", "board_project_id",
    "workdir_exists", "workdir_is_git_repo", "members", "counts",
    "managed_profile_count", "drift",
]
# §4.3's member-block keys (full block — the spec shows it on the existing member).
MEMBER_KEYS = {
    "exists", "managed", "blueprint", "blueprint_in_catalog",
    "distribution_source", "distribution_installed_at",
    "terminal_cwd", "terminal_cwd_matches", "toolsets",
    "description", "description_empty", "env_is_placeholder",
    "max_in_progress_per_profile",
}
COUNT_KEYS = ["todo", "ready", "running", "review", "done", "blocked"]


@pytest.fixture
def world(hermes_root, profile):
    """Two profiles + one board: proj-be (managed instance, fully stamped)
    and qa (adopted member)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles as hc_profiles

    workdir = hermes_root.parent / "work-proj"
    workdir.mkdir()
    kb.create_board("proj-board", name="Proj", default_workdir=str(workdir))

    be = profile("proj-be", {
        "terminal": {"cwd": str(workdir)},
        "toolsets": ["hermes-cli", "kanban", "team"],
        "kanban": {"max_in_progress_per_profile": 2},
    })
    (be / "distribution.yaml").write_text(
        "source: /opt/profile-hermes/hermes-agency/profiles/agency-backend-engineer\n"
        "installed_at: '2026-09-07T12:03:11+00:00'\n", encoding="utf-8")
    hc_profiles.write_profile_meta(
        be, description="Builds server-side application logic, APIs, services…")
    profile("qa", {})

    team = Team(
        slug="proj", display_name="Proj", board="proj-board",
        workdir=str(workdir), tenant="proj", orchestrator="proj-be",
        members=(
            Member(profile="proj-be", role="engineer",
                   blueprint="agency-backend-engineer",
                   blueprint_source="pack:hermes-agency", managed=True, primary=True),
            Member(profile="qa", role="verifier"),
        ),
    )
    return types.SimpleNamespace(team=team, root=hermes_root, workdir=workdir, be=be)


# ── §4.3 shape ──────────────────────────────────────────────────────────────

def test_resolved_shape_matches_4_3_field_for_field(world):
    o = resolve.observe(world.team).to_dict()
    assert list(o.keys()) == RESOLVED_KEYS
    assert list(o["counts"].keys()) == COUNT_KEYS
    assert set(o["members"]) == {"proj-be", "qa"}
    assert set(o["members"]["proj-be"].keys()) == MEMBER_KEYS
    # Sprawl is measured, not hidden.
    assert o["managed_profile_count"] == 1
    # observe() is read-only: drift is reconcile's job (S9).
    assert o["drift"] == []


def test_board_fields(world):
    from hermes_cli import kanban_db as kb
    o = resolve.observe(world.team).to_dict()
    assert o["board_exists"] is True
    assert o["board_db_path"] == str(kb.kanban_db_path("proj-board"))
    # Hazard "default_workdir is kind-scoped": observed and recorded — never
    # used by resolve to infer task placement.
    assert o["board_default_workdir"] == str(world.workdir)
    assert o["board_project_id"] is None
    # A removed board degrades, it does not raise.
    shutil.rmtree(kb.board_dir("proj-board"))
    o = resolve.observe(world.team).to_dict()
    assert o["board_exists"] is False
    assert o["board_db_path"] is None


def test_workdir_fields(world):
    o = resolve.observe(world.team).to_dict()
    assert o["workdir_exists"] is True
    assert o["workdir_is_git_repo"] is False
    (world.workdir / ".git").mkdir()
    assert resolve.observe(world.team).to_dict()["workdir_is_git_repo"] is True


def _head_files(wd) -> list:
    return subprocess.run(
        ["git", "-C", str(wd), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()


def test_ensure_worktree_repo_snapshots_existing_files(world):
    wd = str(world.workdir)
    (world.workdir / "app.py").write_text("print(1)\n")
    assert resolve.workdir_has_commit(wd) is False
    assert resolve.ensure_worktree_repo(wd) is True
    assert resolve.workdir_has_commit(wd) is True
    assert _head_files(wd) == ["app.py"]
    assert resolve.ensure_worktree_repo(wd) is False       # idempotent


def test_ensure_worktree_repo_fallback_identity(world, monkeypatch):
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    wd = str(world.workdir)
    subprocess.run(["git", "-C", wd, "init", "-q"], check=True)   # no HEAD yet
    assert resolve.ensure_worktree_repo(wd) is True
    log = subprocess.run(["git", "-C", wd, "log", "-1", "--format=%ae|%s"],
                         capture_output=True, text=True, check=True)
    assert log.stdout.strip() == \
        "hermes-agent-synergia@localhost|hermes-agent-synergia: initial snapshot"


def test_nested_workdir_gets_its_own_repo(world):
    outer = str(world.workdir)
    assert resolve.ensure_worktree_repo(outer) is True
    (world.workdir / "app1").mkdir()
    (world.workdir / "app1" / "main.py").write_text("print(1)\n")
    sub = str(world.workdir / "app1")
    # the outer HEAD must not count: tasks would worktree the whole outer repo
    assert resolve.workdir_has_commit(sub) is False
    assert resolve.ensure_worktree_repo(sub) is True
    assert resolve.workdir_has_commit(sub) is True
    assert _head_files(sub) == ["main.py"]
    exclude = world.workdir / ".git" / "info" / "exclude"
    assert "/app1/" in exclude.read_text().splitlines()
    status = subprocess.run(["git", "-C", outer, "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert "app1" not in status


def test_ensure_worktree_repo_failure_is_team_error(tmp_path):
    with pytest.raises(TeamError) as ei:
        resolve.ensure_worktree_repo(str(tmp_path / "missing"))
    assert ei.value.code == "E_TEAM_INVALID"
    assert "git" in ei.value.remedy


# ── member block ────────────────────────────────────────────────────────────

def test_existing_member_full_block(world, monkeypatch):
    # The catalog depends on ambient pack roots; the hazard we test is that
    # the LOOKUP is consulted and degrades to a bool, not its host truth.
    monkeypatch.setattr(resolve, "_blueprint_in_catalog",
                       lambda bp: bp == "agency-backend-engineer")
    m = resolve.observe(world.team).to_dict()["members"]["proj-be"]
    assert m["exists"] is True
    assert m["managed"] is True                       # from the record
    assert m["blueprint"] == "agency-backend-engineer"
    assert m["blueprint_in_catalog"] is True
    assert m["distribution_source"] == (
        "/opt/profile-hermes/hermes-agency/profiles/agency-backend-engineer")
    assert m["distribution_installed_at"] == "2026-09-07T12:03:11+00:00"
    assert m["terminal_cwd"] == str(world.workdir)
    assert m["terminal_cwd_matches"] is True
    assert m["toolsets"] == ["hermes-cli", "kanban", "team"]
    assert m["description"].startswith("Builds server-side")
    assert m["description_empty"] is False
    assert m["env_is_placeholder"] is False
    assert m["max_in_progress_per_profile"] == 2


def test_adopted_member_defaults(world):
    m = resolve.observe(world.team).to_dict()["members"]["qa"]
    assert m["exists"] is True and m["managed"] is False
    assert m["blueprint"] is None and m["blueprint_in_catalog"] is False
    assert m["terminal_cwd"] is None and m["terminal_cwd_matches"] is False
    assert m["description_empty"] is True


def test_blueprint_in_catalog_degrades_to_false(world):
    # §4.3 footnote: false ⇒ the pack moved or was removed — info, not drift,
    # and a missing catalog must never crash the read.
    assert resolve._blueprint_in_catalog("no-such-blueprint-anywhere") is False
    assert resolve._blueprint_in_catalog(None) is False


@pytest.mark.parametrize("sentinel", sorted(resolve.model.TERMINAL_CWD_SENTINELS))
def test_sentinel_terminal_cwd_reported_unset(hermes_root, profile, sentinel):
    """Hazard "terminal.cwd sentinels": every sentinel reads as unset — None,
    never the literal — and therefore as a mismatch, never as a real path."""
    profile("sentry", {"terminal": {"cwd": sentinel}})
    team = Team(slug="t", board="", workdir=str(hermes_root.parent / "w"),
                orchestrator="sentry",
                members=(Member(profile="sentry"),))
    m = resolve.observe(team).to_dict()["members"]["sentry"]
    assert m["exists"] is True
    assert m["terminal_cwd"] is None
    assert m["terminal_cwd_matches"] is False


def test_env_is_placeholder_only_for_real_env_file(hermes_root, profile):
    """A regular seeded .env is the placeholder; a symlink to the
    root .env (the deployment convention) is not."""
    home = profile("inst", {})
    (home / ".env").write_text("KEY=seeded\n", encoding="utf-8")
    assert resolve._env_is_placeholder(home) is True
    root_env = hermes_root / ".env"
    root_env.write_text("KEY=root\n", encoding="utf-8")
    (home / ".env").unlink()
    (home / ".env").symlink_to(root_env)
    assert resolve._env_is_placeholder(home) is False
    (home / ".env").unlink()
    assert resolve._env_is_placeholder(home) is False


# ── dangling references never crash a read ─────────────────────────────────

def test_deleted_profile_yields_exists_false_not_exception(world):
    shutil.rmtree(resolve.profile_home("qa"))
    o = resolve.observe(world.team).to_dict()
    # §4.3 pins the dangling block to exactly this two-key form.
    assert o["members"]["qa"] == {"exists": False, "managed": False}
    # The rest of the snapshot still computes (observe never half-crashes).
    assert o["members"]["proj-be"]["exists"] is True
    assert o["managed_profile_count"] == 1


def test_malformed_config_degrades_to_empty(world, profile):
    profile("proj-be", "{{{{ not yaml")   # overwrite with unreadable content
    m = resolve.observe(world.team).to_dict()["members"]["proj-be"]
    assert m["exists"] is True           # profile on disk, no exception
    assert m["toolsets"] == []
    assert m["terminal_cwd"] is None


def test_observe_invalid_board_slug_degrades(world):
    team = dataclasses.replace(world.team, board="Bad Slug")
    o = resolve.observe(team).to_dict()
    assert o["board_exists"] is False
    assert all(v == 0 for v in o["counts"].values())


# ── the small readers ───────────────────────────────────────────────────────

def test_profile_config_path_and_read(world):
    p = resolve.profile_config_path("proj-be")
    assert p == world.be / "config.yaml"
    cfg = resolve.read_profile_config("proj-be")
    assert cfg["terminal"]["cwd"] == str(world.workdir)
    assert cfg["kanban"]["max_in_progress_per_profile"] == 2
    # unknown profile ⇒ {} (the raw reader swallows FileNotFoundError)
    assert resolve.read_profile_config("no-such-profile") == {}


def test_read_profile_config_is_uncached(world):
    # The whole point of read_user_config_raw (hazard "load_config() cache"):
    # two reads see a between-reads edit — no process cache in the way.
    assert resolve.read_profile_config("qa") == {}
    (resolve.profile_config_path("qa")).write_text("toolsets: [kanban]\n",
                                                   encoding="utf-8")
    assert resolve.read_profile_config("qa") == {"toolsets": ["kanban"]}


def test_board_meta_validates_our_copied_slug_re():
    with pytest.raises(TeamError) as exc:
        resolve.board_meta("Bad Slug")
    assert exc.value.code == "E_TEAM_INVALID"


# ── task_counts (public list_tasks over a gated connection) ────────────────

def _seed(board, rows):
    from hermes_cli import kanban_db as kb
    with resolve._kanban_conn().connect_closing(board=board) as conn:
        for status, tenant in rows:
            tid = kb.create_task(conn, title=f"t-{status}-{tenant}",
                                 tenant=tenant, initial_status="running")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            conn.commit()


def test_task_counts_bucketed_and_tenant_scoped(world):
    _seed("proj-board", [
        (s, "proj") for s in COUNT_KEYS
    ] + [("todo", "other-team"), ("archived", "proj")])
    scoped = resolve.task_counts("proj-board", "proj")
    assert scoped == {s: 1 for s in COUNT_KEYS}   # archived row excluded
    everything = resolve.task_counts("proj-board", None)
    assert everything["todo"] == 2                # both tenants
    # The same numbers reach observe() through team.tenant.
    o = resolve.observe(world.team).to_dict()
    assert o["counts"] == scoped


def test_task_counts_missing_board_is_zeros_and_creates_nothing(hermes_root):
    from hermes_cli import kanban_db as kb
    assert resolve.task_counts("ghost-board", "t") == {s: 0 for s in COUNT_KEYS}
    # A reader must never mint a board: connect() would create it, the gate must stop it.
    assert not kb.board_dir("ghost-board").exists()
    assert not kb.kanban_db_path("ghost-board").exists()


# ── task_comments (the chat thread's history) ───────────────────────────────

def _carded(board, title, comments=()):
    from hermes_cli import kanban_db as kb
    with resolve._kanban_conn().connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title=title, tenant="proj",
                             initial_status="running")
        for author, body in comments:
            kb.add_comment(conn, tid, author, body)
    return tid


def test_task_comments_group_per_card_oldest_first(world):
    a = _carded("proj-board", "a", [("operator:default", "NOTE: go wide"),
                                    ("proj-be", "on it")])
    b = _carded("proj-board", "b")
    got = resolve.task_comments("proj-board", [a, b, ""])
    assert set(got) == {a, b}
    assert got[b] == []
    assert [c["body"] for c in got[a]] == ["NOTE: go wide", "on it"]
    assert set(got[a][0]) == {"id", "author", "body", "created_at"}
    assert got[a][0]["author"] == "operator:default"


def test_task_comments_keep_the_newest_within_the_cap(world):
    tid = _carded("proj-board", "chatty",
                  [("proj-be", f"c{i}") for i in range(6)])
    assert [c["body"] for c in resolve.task_comments(
        "proj-board", [tid], limit=2)[tid]] == ["c4", "c5"]
    assert len(resolve.task_comments("proj-board", [tid])[tid]) == 6


def test_task_comments_missing_board_is_empty_and_creates_nothing(hermes_root):
    from hermes_cli import kanban_db as kb
    assert resolve.task_comments("ghost-board", ["t_1"]) == {}
    assert resolve.task_comments("ghost-board", []) == {}
    assert not kb.board_dir("ghost-board").exists()


# ── task_events (the chat thread's status timeline) ─────────────────────────

def test_task_events_are_status_kinds_only(world):
    from hermes_cli import kanban_db as kb
    tid = _carded("proj-board", "evented", [("proj-be", "on it")])
    with resolve._kanban_conn().connect_closing(board="proj-board") as conn:
        kb.block_task(conn, tid, reason="needs a key", kind="needs_input")
    rows = resolve.task_events("proj-board", [tid])[tid]
    kinds = [e["kind"] for e in rows]
    # `commented` ships through task_comments; a second copy here would render
    # every message twice in the thread.
    assert "commented" not in kinds
    assert kinds[0] == "created" and "blocked" in kinds
    assert set(rows[0]) == {"id", "kind", "created_at", "payload"}


def test_task_events_payload_is_trimmed_to_the_whitelist(world):
    tid = _carded("proj-board", "trimmed")
    created = resolve.task_events("proj-board", [tid])[tid][0]
    assert created["kind"] == "created"
    assert set(created["payload"]) <= set(resolve._EVENT_KEYS)
    # The card's body, skills and workspace never reach a 1s poll.
    assert "workspace_path" not in created["payload"]


def test_task_events_say_when_a_run_is_a_review(world):
    """The dashboard offers Pause per running card, and approvals.pause skips
    a reviewer run — the thread payload has to tell the two apart."""
    from hermes_cli import kanban_db as kb
    tid = _carded("proj-board", "reviewed")
    with resolve._kanban_conn().connect_closing(board="proj-board") as conn:
        assert kb.request_review(conn, tid, summary="ready", force=True)
        assert kb.claim_review_task(conn, tid)
    claimed = [e for e in resolve.task_events("proj-board", [tid])[tid]
               if e["kind"] == "claimed"]
    assert claimed[-1]["payload"]["source_status"] == "review"


def test_task_events_keep_the_newest_within_the_cap(world):
    from hermes_cli import kanban_db as kb
    tid = _carded("proj-board", "chatty")
    with resolve._kanban_conn().connect_closing(board="proj-board") as conn:
        for i in range(3):
            kb.block_task(conn, tid, reason=f"r{i}", kind="needs_input")
            kb.unblock_task(conn, tid)
    rows = resolve.task_events("proj-board", [tid], limit=2)[tid]
    assert [e["kind"] for e in rows] == ["blocked", "unblocked"]


def test_task_events_carry_the_attach_moment_and_its_filename(world, tmp_path):
    """A deliverable landing is part of the thread's story, and the filename is
    how the entry finds the file in the card's attachment list."""
    from hermes_cli import kanban_db as kb
    art = tmp_path / "DESIGN.md"
    art.write_text("# Design\n", encoding="utf-8")
    tid = _carded("proj-board", "with a file")
    with resolve._kanban_conn().connect_closing(board="proj-board") as conn:
        kb.add_attachment(conn, tid, filename="DESIGN.md", stored_path=str(art),
                          content_type="text/markdown", size=art.stat().st_size)
    rows = resolve.task_events("proj-board", [tid])[tid]
    attached = [e for e in rows if e["kind"] == "attached"]
    assert len(attached) == 1
    assert attached[0]["payload"]["filename"] == "DESIGN.md"


def test_task_events_missing_board_is_empty_and_creates_nothing(hermes_root):
    from hermes_cli import kanban_db as kb
    assert resolve.task_events("ghost-board", ["t_1"]) == {}
    assert resolve.task_events("ghost-board", []) == {}
    assert not kb.board_dir("ghost-board").exists()
    assert not kb.kanban_db_path("ghost-board").exists()


# ── project_for ─────────────────────────────────────────────────────────────

def test_project_for_unlinked_is_none(world):
    assert resolve.project_for(world.team) is None


def test_project_for_linked_and_dangling(world):
    from hermes_cli import projects_db as pdb
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="Proj X",
                                 folders=[str(world.workdir)])
    linked = dataclasses.replace(
        world.team, project=Project(mode="linked", id=pid))
    found = resolve.project_for(linked)
    assert found is not None and found["id"] == pid
    assert found["name"] == "Proj X"
    # The row goes away but the link stays → None, not an exception:
    # a link to an id that no longer resolves is just a dangling reference.
    dangling = dataclasses.replace(
        world.team, project=Project(mode="linked", id="p_deadbeef"))
    assert resolve.project_for(dangling) is None
    # mode "none" is never a lookup request, even with a stale id.
    assert resolve.project_for(
        dataclasses.replace(linked, project=Project(mode="none", id=pid))) is None
