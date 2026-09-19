"""S19 — dashboard/plugin_api.py (§10, guards g1–g5, tests per §14.7).

The dashboard IS live on this box (localhost:9119, auth_required), but it
was started before these routes existed, and the mount happens at startup
— so this suite runs the implement.md fallback: the router is mounted
**in-process** under the substrate's exact recipe
(``spec_from_file_location`` under the name ``web_server_dashboard.py:758``
uses, prefix ``/api/plugins/hermes-agent-synergia``), driven by
``fastapi.testclient.TestClient`` (an httpx client over an ASGITransport —
the same transport shape the live probe in implement.md's curl block will
exercise once the gateway restarts).

The session token is the dashboard's own credential: the guard RE-CHECKS
it through a vendored ``_has_valid_session_token`` (S21b), so the tests
monkeypatch ``web_server._SESSION_TOKEN`` and send the header — the same
gate the live stack enforces in middleware.

Coverage (mandated list, §14.7 dashboard section):
* every GET route answers 200 with the envelope;
* POST /teams stale ``expected_revision`` → 409 naming BOTH revisions,
  teams.yaml byte-unchanged (g1);
* POST /teams with ``name`` + ``roster`` → slug derived, orchestrator = the
  ``main`` member, the job result reports the team ``ready``; duplicate
  names → 409;
* POST /teams/{slug}/tasks → 201; the row lands with ``tenant`` set to the
  team slug (and foreign tenants never appear in GET /status);
* DELETE without ``confirm`` → 4xx (and no deletion);
* ``?dry_run=true`` → plan + zero writes (create, member-add, sync);
* ``dashboard_writes: false`` → 405 on every non-GET EXCEPT the dry-run
  sync (§11, one code path);
* GET /teams/{slug}/status does not mint a missing board DB (§5.2.1);
* POST → 202 {job_id}; GET /jobs/{id} walks pending→done and the result
  carries ``restart_notice`` as a FIELD (g4);
* skills?profile= shape (picker);
* non-member assignee → §7.3-shaped envelope;
* adopted member + ``delete_profile`` → profile left on disk, reported;
  managed member + ``delete_profile`` → profile gone;
* g3: non-JSON content type → 415, no/​bad token → 401, cross-origin → 403.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys
import time

import pytest
import yaml

import cli
import model
import storage
import hermes_cli.web_server as web_server
from test_catalog import _make_pack_root

P = "/api/plugins/hermes-agent-synergia"
TOKEN = "s19-test-session-token"
_API_FILE = (pathlib.Path(cli.__file__).resolve().parent
             / "dashboard" / "plugin_api.py")


# ── world fixtures ───────────────────────────────────────────────────────────

def _install_plugin(hermes_root, catalog_paths):
    d = hermes_root / "plugins" / "hermes-agent-synergia"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text("name: hermes-agent-synergia\n", encoding="utf-8")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["hermes-agent-synergia"],
                     "entries": {"hermes-agent-synergia": {
                         "settings": {"catalog_paths":
                                          [str(p) for p in catalog_paths]}}}}},
        sort_keys=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh(hermes_root):
    import catalog
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()
    yield
    storage._reset_caches_for_tests()
    catalog._reset_caches_for_tests()


_TMP_ROOT = [""]          # set by _fresh; helpers use it for workdirs


@pytest.fixture(autouse=True)
def _fresh2(hermes_root):
    _TMP_ROOT[0] = str(hermes_root.parent)
    yield


@pytest.fixture(autouse=True)
def _session_token(monkeypatch):
    """The guard re-checks the dashboard's own token; pin it so the tests
    can present it (mirrors web_server's env-provided token in the live
    stack)."""
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", TOKEN, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", None,
                        raising=False)


@pytest.fixture
def world(hermes_root, tmp_path):
    _install_plugin(hermes_root, [_make_pack_root(tmp_path / "packs")])
    return hermes_root


@pytest.fixture
def W(hermes_root):
    def _w(name):
        return str(hermes_root.parent / ("work-" + name))
    return _w


def _mount_plugin_api():
    """The substrate's own recipe (web_server_dashboard.
    _mount_plugin_api_routes): exec by FILE PATH under the plugin name,
    registered in sys.modules BEFORE exec; take the ``router`` attr."""
    name = "hermes_dashboard_plugin_hermes_agent_synergia"
    spec = importlib.util.spec_from_file_location(name, _API_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(_mount_plugin_api().router, prefix=P)
    return TestClient(app, base_url="http://testserver",
                      headers={"X-Hermes-Session-Token": TOKEN})


@pytest.fixture
def anon(world):
    """Same mount, no session token — the g2 probe."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(_mount_plugin_api().router, prefix=P)
    return TestClient(app, base_url="http://testserver")


# ── helpers ──────────────────────────────────────────────────────────────────

def _teams_yaml_bytes(world):
    p = world / "plugin-data" / "hermes-agent-synergia" / "teams.yaml"
    return p.read_bytes() if p.is_file() else None


def _job_done(client, job_id, timeout=30.0, *, state="done"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"{P}/jobs/{job_id}").json()["data"]["job"]
        if snap["state"] in ("done", "error"):
            assert snap["state"] == state, snap.get("error")
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {snap}")


def _create_team(client, slug="px", members=("qa:orchestrator",), *,
                 board=True, extra=None):
    body = {"slug": slug, "member": list(members),
            "workdir": _TMP_ROOT[0] + "/work-" + slug,
            "create_workdir": True, "no_sync": True}
    if board:
        body.update(board=slug, create_board=True)
    body.update(extra or {})
    r = client.post(f"{P}/teams", json=body)
    assert r.status_code == 202, r.text
    return r.json()["data"]


def _task_row(world, board, task_id):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        row = conn.execute(
            "select tenant, assignee, title, status from tasks where id = ?",
            (task_id,)).fetchone()
    return row


# ── GET surface: every row of §10 answers 200 ────────────────────────────────

def test_every_get_route_200(client, world, profile):
    profile("qa")
    data = _create_team(client)
    _job_done(client, data["job_id"])
    r = client.get(f"{P}/catalog")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"]["count"] >= 3
    assert r.json()["data"]["default_profile_naming"] == ""
    assert client.get(f"{P}/catalog/agency-backend-engineer"
                      ).status_code == 200
    assert client.get(f"{P}/catalog/agency-nobody").status_code == 404
    assert client.get(f"{P}/recipes").json()["data"]["count"] == 2
    assert client.get(f"{P}/templates").status_code == 404
    teams = client.get(f"{P}/teams").json()["data"]
    row = next(t for t in teams["teams"] if t["slug"] == "px")
    assert row["members"] == 1
    assert set(row["counts"]) == {"todo", "ready", "running", "review",
                                  "done", "blocked"}
    show = client.get(f"{P}/teams/px").json()["data"]["team"]
    assert show["_resolved"]["counts"]["todo"] == 0
    assert show["slug"] == "px"
    drift = client.get(f"{P}/teams/px/drift").json()["data"]
    assert isinstance(drift["drift"], list)
    assert set(drift["ready"]) == {"ready", "blocking"}
    assert client.get(f"{P}/doctor").json()["ok"] is True
    assert client.get(f"{P}/jobs/does-not-exist").status_code == 404


def test_catalog_reports_default_profile_naming(client, world):
    cfg = yaml.safe_load((world / "config.yaml").read_text())
    cfg["plugins"]["entries"]["hermes-agent-synergia"]["settings"][
        "default_profile_naming"] = "{short}-{team}"
    (world / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False),
                                       encoding="utf-8")
    body = client.get(f"{P}/catalog").json()["data"]
    assert body["default_profile_naming"] == "{short}-{team}"


def test_root_profiles_then_adopt(client, world, profile):
    """The profiles root is reachable only through these two routes; it is
    never folded into GET /catalog."""
    profile("qa")
    rows = client.get(f"{P}/root-profiles").json()["data"]
    assert [r["id"] for r in rows["profiles"]] == ["qa"]
    assert rows["count"] == 1
    assert "qa" not in {e["id"] for e in client.get(f"{P}/catalog"
                                                    ).json()["data"]["entries"]}

    r = client.post(f"{P}/catalog/adopt", json={"profile": "qa", "name": "qa-blue"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["adopted"] == "qa-blue"
    assert client.get(f"{P}/catalog/qa-blue").status_code == 200
    assert client.get(f"{P}/root-profiles"
                      ).json()["data"]["profiles"][0]["adopted_as"] == "qa-blue"


def test_post_catalog_adopt_refuses_bad_body_and_unknown_profile(client, world):
    r = client.post(f"{P}/catalog/adopt", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"
    r = client.post(f"{P}/catalog/adopt", json={"profile": "no-such"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_TEAM_NO_BLUEPRINT"


def test_get_status_mints_nothing(client, world, profile):
    """§5.2.1: connect() creates-and-migrates, so the activity panel's
    polling GET must gate on the DB FILE, never open it speculatively."""
    from hermes_cli import kanban_db
    profile("qa")
    data = _create_team(client, extra={"workspace_policy": "scratch"})
    _job_done(client, data["job_id"])
    db = kanban_db.kanban_db_path("px")
    if db.exists():
        db.unlink()                            # a board dir WITHOUT a db
    assert not db.exists()
    r = client.get(f"{P}/teams/px/status")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["board_db_path"] is None and d["tasks"] == []
    assert d["counts"] == {"todo": 0, "ready": 0, "running": 0,
                           "review": 0, "done": 0, "blocked": 0}
    assert not db.exists(), "GET must not mint the board DB"


def test_skills_profile_shape(client, world, profile):
    home = profile("eng")
    (home / "skills" / "one").mkdir(parents=True)
    (home / "skills" / "one" / "SKILL.md").write_text(
        "---\nname: one\ndescription: fixture skill\n---\nbody\n",
        encoding="utf-8")
    (home / "config.yaml").write_text(yaml.safe_dump(
        {"skills": {"disabled": ["one"]}}), encoding="utf-8")
    d = client.get(f"{P}/skills?profile=eng").json()["data"]
    assert d["profile"] == "eng"
    assert d["skills"] == [{"name": "one", "enabled": False}]
    assert client.get(f"{P}/skills?profile=ghost").status_code == 404
    assert client.get(f"{P}/skills").status_code == 400


# ── g1: CAS on every write; 409 names both revisions ─────────────────────────

def test_post_teams_stale_revision_409_names_both_revisions(client, world,
                                                            profile):
    profile("qa")
    before = _teams_yaml_bytes(world)
    r = client.post(f"{P}/teams", json={
        "slug": "px", "member": ["qa:orchestrator"],
        "workdir": _TMP_ROOT[0] + "/work-px", "expected_revision": 99})
    assert r.status_code == 409, r.text
    env = r.json()
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_CONFLICT"
    assert "99" in env["error"]["message"] and "found 0" in \
        env["error"]["message"]
    assert _teams_yaml_bytes(world) == before   # byte-unchanged


def test_post_teams_dry_run_is_a_plan_and_writes_nothing(client, world,
                                                         tmp_path):
    before = _teams_yaml_bytes(world)
    r = client.post(f"{P}/teams?dry_run=true", json={
        "name": "Project X", "workdir": str(tmp_path),
        "roster": [{"blueprint": "agency-backend-engineer", "main": True}]})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["dry_run"] is True
    assert d["team"]["slug"] == "project-x"
    assert d["team"]["display_name"] == "Project X"
    assert d["plans"], "dry-run must carry the instantiation plans"
    assert _teams_yaml_bytes(world) == before
    assert "project-x" not in storage.load().teams


def _root_model(world):
    cfg = yaml.safe_load((world / "config.yaml").read_text())
    cfg["model"] = {"default": "test-model", "provider": "custom",
                    "base_url": "http://127.0.0.1:9/v1"}
    (world / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False),
                                       encoding="utf-8")


_ROSTER = [{"blueprint": "agency-backend-engineer", "main": True},
           {"blueprint": "agency-code-reviewer", "role": "reviewer"}]


def test_post_teams_roster_creates_a_ready_team(client, world):
    _root_model(world)
    r = client.post(f"{P}/teams", json={
        "name": "Alpha Team", "workdir": _TMP_ROOT[0] + "/work-alpha",
        "roster": _ROSTER, "create_board": False, "no_sync": True})
    assert r.status_code == 202, r.text
    res = _job_done(client, r.json()["data"]["job_id"])["result"]
    team = res["team"]
    assert team["slug"] == "alpha-team"
    assert team["display_name"] == "Alpha Team"
    assert team["orchestrator"] == "alpha-team-backend-engineer"
    reviewer = next(m for m in team["members"]
                    if m["profile"] == "alpha-team-code-reviewer")
    assert reviewer["role"] == "reviewer" and reviewer["managed"] is True
    assert reviewer["model"] == "test-model"     # root model stamped
    # create_board/no_sync are overridden: the board exists, sync ran
    assert res["reconciled"] is not None
    assert res["ready"] == {"ready": True, "blocking": []}, res["ready"]
    drift = client.get(f"{P}/teams/alpha-team/drift").json()["data"]
    assert drift["ready"]["ready"] is True


def test_post_teams_duplicate_name_409(client, world):
    _root_model(world)
    r = client.post(f"{P}/teams", json={
        "name": "Alpha Team", "workdir": _TMP_ROOT[0] + "/work-alpha",
        "roster": _ROSTER})
    _job_done(client, r.json()["data"]["job_id"])
    before = _teams_yaml_bytes(world)
    for body in ({"name": "alpha  TEAM"},                  # same slug
                 {"slug": "other", "name": "ALPHA team"},  # same name
                 {"name": "alpha-team"}):                  # name = a slug
        body.update(workdir=_TMP_ROOT[0] + "/work-dup",
                    roster=[{"blueprint": "agency-code-reviewer",
                             "main": True}])
        r = client.post(f"{P}/teams?dry_run=true", json=body)
        assert r.status_code == 409, (body, r.text)
        assert r.json()["error"]["code"] == "E_TEAM_EXISTS"
    assert _teams_yaml_bytes(world) == before


@pytest.mark.parametrize("roster, status", [
    ([{"blueprint": "agency-backend-engineer"}], 400),               # no main
    ([{"blueprint": "agency-backend-engineer", "main": True},
      {"blueprint": "agency-code-reviewer", "main": True}], 400),    # two
    ([{"blueprint": "agency-nobody", "main": True}], 404),
    ("agency-backend-engineer", 400),
    ([{"main": True}], 400),
])
def test_post_teams_bad_roster_refused(client, world, roster, status):
    r = client.post(f"{P}/teams?dry_run=true", json={
        "name": "Bad", "workdir": _TMP_ROOT[0] + "/work-bad",
        "roster": roster})
    assert r.status_code == status, r.text
    assert r.json()["ok"] is False


def test_post_teams_unusable_name_400(client, world):
    r = client.post(f"{P}/teams?dry_run=true", json={
        "name": "!!!", "workdir": _TMP_ROOT[0] + "/work-bad",
        "roster": _ROSTER})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"


def test_delete_team_requires_confirm_and_reports_skips(
        client, world, profile, W):
    profile("qa")                                   # adopted member
    data = _create_team(client, members=["qa:orchestrator"])
    _job_done(client, data["job_id"])
    r = client.request("DELETE", f"{P}/teams/px",
                       json={"confirm": "wrong"})
    assert 400 <= r.status_code < 500
    assert "px" in storage.load().teams
    r = client.request("DELETE", f"{P}/teams/px", json={"confirm": "px"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["destroyed"] == "px"
    # The default purge acted only on managed members; the adopted one
    # is reported
    assert d.get("skipped_purge") == ["qa"]
    assert (world / "profiles" / "qa").is_dir()
    assert "px" not in storage.load().teams


# ── g4: 202 {job_id} → GET /jobs/{id}, restart_notice as a FIELD ────────────

def test_job_202_to_done_with_restart_notice_field(client, world, profile):
    profile("qa")
    data = _create_team(client)
    assert set(data) >= {"job_id", "job"}
    assert data["job"]["state"] in ("pending", "running")
    snap = _job_done(client, data["job_id"])
    assert snap["result"] is not None
    # the §9.4 restart notice travels as a FIELD, never prose to scrape
    assert "restart_notice" in snap["result"]
    assert "px" in storage.load().teams
    assert (world / "kanban" / "boards" / "px").is_dir()


def test_job_error_is_an_envelope(client, world, profile):
    """A doomed create (adopted profile missing) completes as state=error
    with a §7.3-shaped envelope, and the registry stays clean."""
    r = client.post(f"{P}/teams", json={
        "slug": "gone", "member": ["nosuchprofile:orchestrator"],
        "workdir": _TMP_ROOT[0] + "/work-gone", "no_sync": True})
    assert r.status_code == 202
    snap = _job_done(client, r.json()["data"]["job_id"],
                  state="error")
    assert snap["state"] == "error"
    assert snap["error"]["error"]["code"] == "E_TEAM_NO_PROFILE"
    assert "gone" not in storage.load().teams


# ── tasks: §7.4 semantics, tenant set, assignee enforced ────────────────────

def test_post_task_201_row_lands_with_tenant(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/tasks",
                    json={"title": "wire the board", "body": "details"})
    assert r.status_code == 201, r.text
    task_id = r.json()["data"]["task_id"]
    row = _task_row(world, "px", task_id)
    assert row is not None
    assert row[0] == "px"                    # tenant = team slug (§7.4)
    assert row[1] == "qa"                    # default assignee: orchestrator
    assert row[3] in ("ready", "running")  # kanban default, parentless
    st = client.get(f"{P}/teams/px/status").json()["data"]
    assert st["tasks"] and st["tasks"][0]["id"] == task_id
    assert sum(st["counts"].values()) == 1


def test_post_task_tenant_isolation(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px")) as conn:
        kanban_db.create_task(conn, title="foreign", assignee="qa",
                              tenant="other-team", board="px")
    st = client.get(f"{P}/teams/px/status").json()["data"]
    assert all(t["tenant"] == "px" for t in st["tasks"])
    assert sum(st["counts"].values()) == 0    # the foreign row is invisible


def test_post_task_non_member_assignee_refused(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/tasks",
                    json={"title": "t", "assignee": "stranger"})
    assert r.status_code == 400, r.text
    env = r.json()
    assert env["ok"] is False
    assert env["error"]["code"] == "E_TEAM_NOT_MEMBER"
    assert "qa" in env["error"]["hint"]           # roster in the hint
    assert "add-member" in env["error"]["remedy"]


def _make_worktree_without_repo():
    # a team whose record says worktree but whose workdir never got a repo
    # (created before create-time init, or switched behind its back)
    t = storage.load().teams["px"]
    storage.upsert(model.Team.from_dict(
        {**t.to_dict(), "workspace_policy": "worktree"}))
    return pathlib.Path(_TMP_ROOT[0]) / "work-px"


def test_post_task_worktree_inits_missing_repo(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(
        client, extra={"workspace_policy": "scratch"})["job_id"])
    wd = _make_worktree_without_repo()
    assert not (wd / ".git").exists()
    r = client.post(f"{P}/teams/px/tasks", json={"title": "t"})
    assert r.status_code == 201, r.text
    assert (wd / ".git").is_dir()


def test_post_task_worktree_missing_workdir_refused(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(
        client, extra={"workspace_policy": "scratch"})["job_id"])
    _make_worktree_without_repo().rmdir()
    r = client.post(f"{P}/teams/px/tasks", json={"title": "t"})
    assert r.status_code == 409, r.text
    env = r.json()
    assert env["error"]["code"] == "E_TEAM_DRIFT_UNFIXED"
    assert "git" in env["error"]["remedy"]


def test_post_task_worktree_team_create_inits_git(client, world, profile):
    profile("qa")
    wd = pathlib.Path(_TMP_ROOT[0]) / "work-px"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "app.py").write_text("print(1)\n")
    _job_done(client, _create_team(
        client, extra={"workspace_policy": "worktree"})["job_id"])
    assert (wd / ".git").is_dir()
    r = client.post(f"{P}/teams/px/tasks", json={"title": "t"})
    assert r.status_code == 201, r.text


# ── POST /teams/{slug}/workdir: move the root, profiles follow ───────────────

def test_post_workdir_moves_team_and_profiles(client, world, profile):
    from hermes_cli import kanban_db, kanban_db_connect
    from test_reconcile_apply import _SEED     # a doc the config writer can edit
    profile("qa", _SEED)
    _job_done(client, _create_team(client)["job_id"])
    new = _TMP_ROOT[0] + "/work-px/app1"
    r = client.post(f"{P}/teams/px/workdir", json={"workdir": new})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["changed"] is True and d["dry_run"] is False
    assert storage.load().teams["px"].workdir == new
    cfg = yaml.safe_load((world / "profiles" / "qa" / "config.yaml").read_text())
    assert cfg["terminal"]["cwd"] == new
    assert kanban_db.read_board_metadata("px")["default_workdir"] == new
    assert (pathlib.Path(new) / ".git").is_dir()   # worktree: its own repo
    t = client.post(f"{P}/teams/px/tasks", json={"title": "t"})
    assert t.status_code == 201, t.text
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path("px")) as conn:
        row = conn.execute("select workspace_path from tasks where id = ?",
                           (t.json()["data"]["task_id"],)).fetchone()
    assert row[0] == new


def test_post_workdir_dry_run_writes_nothing(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    before = _teams_yaml_bytes(world)
    new = _TMP_ROOT[0] + "/work-px/app2"
    r = client.post(f"{P}/teams/px/workdir?dry_run=true", json={"workdir": new})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["dry_run"] is True and d["changed"] is True
    assert any(i["check"] == "workdir.exists" for i in d["drift"])
    assert _teams_yaml_bytes(world) == before
    assert not pathlib.Path(new).exists()


def test_post_workdir_refuses_bad_paths(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    for bad in ("relative/dir", "/x/../y", "", str(world / "inside")):
        r = client.post(f"{P}/teams/px/workdir", json={"workdir": bad})
        assert r.status_code == 400, (bad, r.text)
        assert r.json()["error"]["code"] == "E_TEAM_INVALID"
    assert storage.load().teams["px"].workdir == _TMP_ROOT[0] + "/work-px"


def test_post_workdir_stale_revision_409(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    storage._reset_caches_for_tests()
    rev = storage.load().revision
    r = client.post(f"{P}/teams/px/workdir",
                    json={"workdir": _TMP_ROOT[0] + "/work-other",
                          "expected_revision": rev - 1})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "E_TEAM_CONFLICT"


def test_post_team_defaults_to_worktree(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    show = client.get(f"{P}/teams/px").json()["data"]["team"]
    assert show["workspace_policy"] == "worktree"
    assert (pathlib.Path(_TMP_ROOT[0]) / "work-px" / ".git").is_dir()


def test_post_team_worktree_failure_falls_back_to_scratch(client, world, profile,
                                                          monkeypatch):
    import resolve
    from errors import TeamError

    def refuse(workdir):
        raise TeamError("E_TEAM_INVALID", "git missing", hint="h", remedy="r")

    monkeypatch.setattr(resolve, "ensure_worktree_repo", refuse)
    profile("qa")
    snap = _job_done(client, _create_team(client)["job_id"])
    assert "git missing" in snap["result"]["warning"]
    show = client.get(f"{P}/teams/px").json()["data"]["team"]
    assert show["workspace_policy"] == "scratch"


# ── members: adopt, instantiate, dry-run, removal ───────────────────────────

def test_add_member_adopt_and_dry_run(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    profile("dev")
    before = _teams_yaml_bytes(world)
    r = client.post(f"{P}/teams/px/members?dry_run=true",
                    json={"profile": "dev", "expected_revision": 1})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["dry_run"] is True
    assert _teams_yaml_bytes(world) == before     # nothing written
    r = client.post(f"{P}/teams/px/members",
                    json={"profile": "dev", "role": "implementer",
                          "expected_revision": 1})
    assert r.status_code == 202, r.text
    snap = _job_done(client, r.json()["data"]["job_id"])
    assert "restart_notice" in snap["result"]
    t = storage.load().teams["px"]
    dev = next(m for m in t.members if m.profile == "dev")
    assert dev.role == "implementer" and dev.managed is False


def test_add_member_stale_revision_writes_nothing(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    profile("dev")
    before = _teams_yaml_bytes(world)
    r = client.post(f"{P}/teams/px/members",
                    json={"profile": "dev", "expected_revision": 41})
    assert r.status_code == 409
    assert "expected revision 41" in r.text and "found 1" in r.text
    assert _teams_yaml_bytes(world) == before


def test_add_member_blueprint_instantiates_managed(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/members",
                    json={"blueprint": "agency-code-reviewer",
                          "expected_revision": 1})
    assert r.status_code == 202, r.text
    snap = _job_done(client, r.json()["data"]["job_id"])
    assert "restart_notice" in snap["result"]
    created = snap["result"]["created"]
    assert created and created[0] == "px-code-reviewer"
    t = storage.load().teams["px"]
    m = next(mm for mm in t.members if mm.profile == created[0])
    assert m.managed is True and m.blueprint == "agency-code-reviewer"
    assert (world / "profiles" / created[0]).is_dir()


def test_add_member_blueprint_carries_picker_skills(client, world, profile):
    """S20 blocker 1: the catalog picker's skill multi-select must reach
    ``Member.skills`` — G3's DoD is "add a member from the catalog picker
    WITH A CHOSEN SKILL SET". Round-trip the picker's exact body and assert
    the stored record carries it."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/members",
                    json={"blueprint": "agency-code-reviewer",
                          "name": "px-code-reviewer",
                          "role": "reviewer",
                          "capacity": 1,
                          "skills": ["arxiv", "grounded-citations"],
                          "expected_revision": 1})
    assert r.status_code == 202, r.text
    snap = _job_done(client, r.json()["data"]["job_id"])
    inst = snap["result"]["created"][0]
    m = next(mm for mm in storage.load().teams["px"].members
             if mm.profile == inst)
    # verbatim, order preserved, no dedupe/sort — same as the template path
    assert m.skills == ["arxiv", "grounded-citations"]
    assert m.to_dict()["skills"] == ["arxiv", "grounded-citations"]
    # and the record the job returned agrees with the one on disk
    assert next(mm for mm in snap["result"]["team"]["members"]
                if mm["profile"] == inst)["skills"] == [
                    "arxiv", "grounded-citations"]


@pytest.mark.parametrize("skills_value", [[], None, "not-a-list"])
def test_add_member_blueprint_empty_skills_is_none_not_empty_list(
        client, world, profile, skills_value):
    """The ``None`` branch, pinned. ``skills: []`` and ``skills: None`` are
    NOT interchangeable downstream: reconcile leaves ``None`` alone ("not
    managed by the team", reconcile.py:972) but projects ``[]`` through
    ``resolve.disabled_skills_for`` into "disable EVERY installed skill".
    An empty or absent pick must therefore land as None and be omitted from
    the record, exactly as ``templates.member_drafts`` omits it."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    body = {"blueprint": "agency-code-reviewer", "expected_revision": 1}
    if skills_value is not None:
        body["skills"] = skills_value
    r = client.post(f"{P}/teams/px/members", json=body)
    assert r.status_code == 202, r.text
    snap = _job_done(client, r.json()["data"]["job_id"])
    inst = snap["result"]["created"][0]
    m = next(mm for mm in storage.load().teams["px"].members
             if mm.profile == inst)
    assert m.skills is None
    assert "skills" not in m.to_dict()          # never a stored empty list


def test_adopt_path_ignores_skills(client, world, profile):
    """The adopt branch builds an argparse Namespace with no ``skills``
    (plugin_api.py:713-720) and ``_add_member_impl`` stamps only
    profile/role/primary/capacity (cli.py:826-829) — so a ``skills`` key on
    an adopt body is inert, and deliberately so: an adopted profile's skills
    are not the team's to manage (it did not create it). Pinned, because
    the managed branch above now DOES read the key."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    profile("dev")
    r = client.post(f"{P}/teams/px/members",
                    json={"profile": "dev", "skills": ["arxiv"],
                          "expected_revision": 1})
    assert r.status_code == 202, r.text
    _job_done(client, r.json()["data"]["job_id"])
    dev = next(m for m in storage.load().teams["px"].members
               if m.profile == "dev")
    assert dev.managed is False
    assert dev.skills is None                   # never team-managed skills
    assert "skills" not in dev.to_dict()


@pytest.mark.parametrize("skills_value", [["arxiv", "grounded-citations"],
                                          [], None])
def test_picker_and_roster_stamp_identical_skills(client, world, profile,
                                                  skills_value):
    """The two paths that can put skills on a member — the add-member picker
    and the create roster (``cli._plan_roster``) — must produce the SAME
    record for the same input, or a team created from a roster and a member
    added by hand would drift for no reason."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    body = {"blueprint": "agency-code-reviewer", "expected_revision": 1}
    if skills_value is not None:
        body["skills"] = skills_value
    r = client.post(f"{P}/teams/px/members", json=body)
    snap = _job_done(client, r.json()["data"]["job_id"])
    inst = snap["result"]["created"][0]
    picker = next(mm for mm in storage.load().teams["px"].members
                  if mm.profile == inst).to_dict()

    row = {"blueprint": "agency-code-reviewer", "role": "reviewer",
           "main": True}
    if skills_value is not None:
        row["skills"] = skills_value
    stamped = cli._plan_roster([row], "px", model.DEFAULT_PROFILE_NAMING,
                               dry_run=False)[0][0].to_dict()
    assert picker.get("skills") == stamped.get("skills")
    assert ("skills" in picker) == ("skills" in stamped)


def test_remove_adopted_member_delete_profile_keeps_it_on_disk(
        client, world, profile):
    profile("qa")
    profile("dev")
    _job_done(client, _create_team(client,
                                    members=["qa:orchestrator",
                                             "dev:implementer"])["job_id"])
    r = client.request("DELETE", f"{P}/teams/px/members/dev",
                       json={"confirm": "dev",
                             "delete_profile": True})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    # Adopted ⇒ never deleted, and the payload SAYS SO
    assert d["deleted_profile"] is None
    assert d["skipped_delete_profile"] == "dev"
    assert (world / "profiles" / "dev").is_dir()
    assert "dev" not in {m.profile
                         for m in storage.load().teams["px"].members}


def test_remove_managed_member_deletes_profile_and_reassigns(
        client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/members",
                    json={"blueprint": "agency-code-reviewer",
                          "expected_revision": 1})
    snap = _job_done(client, r.json()["data"]["job_id"])
    inst = snap["result"]["created"][0]
    # give the instance an open card first
    client.post(f"{P}/teams/px/tasks",
                json={"title": "review", "assignee": inst})
    r = client.request("DELETE", f"{P}/teams/px/members/{inst}",
                       json={"confirm": inst, "delete_profile": True,
                             "reassign_open_to": "qa"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["deleted_profile"] == inst          # managed ⇒ really gone
    assert d["reassigned"] == 1
    assert not (world / "profiles" / inst).exists()
    # and the card moved
    r = client.get(f"{P}/teams/px/status")
    rows = [t for t in r.json()["data"]["tasks"]]
    assert len(rows) == 1 and rows[0]["assignee"] == "qa"


def test_delete_member_requires_confirm(client, world, profile):
    """g5: member removal is destructive — a missing or wrong ``confirm``
    is refused 4xx and the roster is untouched (teams.yaml byte-unchanged,
    the member still in storage)."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    before = _teams_yaml_bytes(world)

    def members():
        return {m.profile for m in storage.load().teams["px"].members}

    assert "qa" in members()
    r = client.request("DELETE", f"{P}/teams/px/members/qa",
                       json={})                     # no confirm at all
    assert 400 <= r.status_code < 500, r.text
    assert "qa" in members()
    r = client.request("DELETE", f"{P}/teams/px/members/qa",
                       json={"confirm": "wrong"})   # wrong confirm token
    assert 400 <= r.status_code < 500, r.text
    assert "qa" in members()
    assert _teams_yaml_bytes(world) == before       # byte-unchanged


# ── sync (dry default; the one non-GET allowed read-only) ───────────────────

def test_sync_defaults_to_dry_run_then_applies(client, world, profile):
    """§9.3: instance repair is never implicit — the dry run reports the
    drift, and the apply fixes it only under the CLI's own flags."""
    import shutil
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/members",
                    json={"blueprint": "agency-code-reviewer",
                          "expected_revision": 1})
    inst = _job_done(client, r.json()["data"]["job_id"])["result"][
        "created"][0]
    shutil.rmtree(world / "profiles" / inst)         # manufactured drift
    r = client.post(f"{P}/teams/px/sync", json={})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["dry_run"] is True and d["drift"] and not d["applied"]
    assert any(i["check"] == "member.instance_missing" for i in d["drift"])
    assert not (world / "profiles" / inst).exists()  # dry wrote nothing
    r = client.post(f"{P}/teams/px/sync?dry_run=false",
                    json={"reinstantiate": True})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["dry_run"] is False and d["applied"]
    assert "restart_notice" in d
    assert (world / "profiles" / inst).is_dir()      # apply restored it


# ── g5 + g7 + g3: the guards, one code path ─────────────────────────────────

def _writes_off(world):
    cfg = yaml.safe_load((world / "config.yaml").read_text())
    cfg["plugins"]["entries"]["hermes-agent-synergia"]["settings"]["dashboard_writes"] \
        = False
    (world / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False),
                                       encoding="utf-8")


def test_dashboard_writes_false_is_405_everywhere_except_dry_sync(
        client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    _writes_off(world)
    r = client.post(f"{P}/teams", json={"slug": "yy",
                                        "member": ["qa:orchestrator"],
                                        "workdir": _TMP_ROOT[0] + "/work-yy"})
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "DASHBOARD_READONLY"
    assert client.post(f"{P}/teams/px/tasks",
                       json={"title": "t"}).status_code == 405
    assert client.request("DELETE", f"{P}/teams/px",
                          json={"confirm": "px"}).status_code == 405
    assert client.post(f"{P}/catalog/adopt",
                       json={"profile": "qa"}).status_code == 405
    # the single exception: dry-run sync writes nothing and answers
    r = client.post(f"{P}/teams/px/sync?dry_run=true", json={})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["dry_run"] is True
    # ... and the apply twin is still a 405
    assert client.request(
        "POST", f"{P}/teams/px/sync?dry_run=false",
        json={}).status_code == 405
    # reads all still 200
    assert client.get(f"{P}/teams").status_code == 200


def test_g3_json_content_type(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/tasks", content=b'{"title": "x"}',
                    headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "CONTENT_TYPE_JSON_REQUIRED"
    r = client.post(f"{P}/teams/px/tasks", content=b'{"title": "x"}',
                    headers={"content-type":
                             "application/json; charset=utf-8"})
    assert r.status_code == 201                    # params tolerated


def test_g2_session_token_required(client, anon, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = anon.post(f"{P}/teams/px/tasks", json={"title": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "DASHBOARD_SESSION_REQUIRED"
    # the Bearer twin works too (substrate's legacy path)
    r = anon.post(f"{P}/teams/px/tasks", json={"title": "x"},
                  headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 201


def test_g3_same_origin(client, world, profile, monkeypatch):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1",
                        raising=False)
    # right Host, foreign Origin → refused (the copied guard's Origin rule)
    r = client.post(f"{P}/teams/px/tasks", json={"title": "x"},
                    headers={"host": "127.0.0.1",
                             "origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ORIGIN_MISMATCH"
    # foreign Host → refused before anything else (DNS-rebinding twin)
    r = client.post(f"{P}/teams/px/tasks", json={"title": "x"},
                    headers={"host": "evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "HOST_MISMATCH"
    # same-origin Host + matching Origin → through
    r = client.post(f"{P}/teams/px/tasks", json={"title": "x"},
                    headers={"host": "127.0.0.1",
                             "origin": "http://127.0.0.1"})
    assert r.status_code == 201


def test_unknown_body_key_is_400(client, world):
    r = client.post(f"{P}/teams", json={"slug": "px", "colour": "blue"})
    assert r.status_code == 400
    assert "colour" in r.json()["error"]["message"]


# ── S19b: the four findings ─────────────────────────────────────────────────

def _gated_client(session=False, token_attest=False):
    """Mount the plugin behind a faithful fake of the gated stack:
    ``app.state.auth_required`` plus a middleware that leaves exactly the
    state ``gated_auth_middleware``/``auth_middleware`` leave on an
    authenticated request — ``request.state.session`` for a verified
    cookie/bearer (dashboard_auth/middleware.py:168/:198), or
    ``request.state.token_authenticated`` for a valid token
    (web_server.py auth_middleware). NO token header is sent: in gated
    mode a logged-in browser never carries one."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.state.auth_required = True            # the substrate's gate flag

    @app.middleware("http")
    async def _gate(request, call_next):
        if session:
            request.state.session = {"handle": "s19b-cookie", "user": "dev"}
        if token_attest:
            request.state.token_authenticated = True
        return await call_next(request)

    app.include_router(_mount_plugin_api().router, prefix=P)
    return TestClient(app, base_url="http://testserver")   # no token header


def test_f1_gated_mode_mutation_needs_no_token_header(world, profile, anon):
    """S19b F1: in gated mode the credential is the middleware-attested
    cookie session, not the loopback token — a mutation must pass on the
    attestation and must NOT be demanded the header the mode never sends."""
    profile("qa")
    tc = _gated_client(session=True)
    _job_done(tc, _create_team(tc)["job_id"])            # 202 before S19b: 401
    tc2 = _gated_client(token_attest=True)               # seam attestation
    _job_done(tc2, _create_team(tc2, slug="py")["job_id"])
    # fail closed: gated mount, but the middleware authenticated nobody
    tc3 = _gated_client()
    r = tc3.post(f"{P}/teams", json={"slug": "zz"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "DASHBOARD_SESSION_REQUIRED"
    # and the ungated mount still refuses the no-credential request (g2)
    r = anon.post(f"{P}/teams", json={"slug": "zz"})
    assert r.status_code == 401


def test_f2_adopt_job_cas_survives_the_queue_window(client, world, profile):
    """S19b F2: the adopt job re-CASes against the CLIENT's revision, not
    its own fresh snapshot — a registry move during the RUN_LOCK queue
    window lands as E_TEAM_CONFLICT in the job result (g1, no lost update)."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    profile("dev")
    jobs = sys.modules["hermes_agent_synergia_dashboard_jobs"]     # the route's own jobs
    with jobs.RUN_LOCK:                                 # hold the queue shut
        r = client.post(f"{P}/teams/px/members",
                        json={"profile": "dev", "expected_revision": 1})
        assert r.status_code == 202, r.text              # enqueue passes g1
        job_id = r.json()["data"]["job_id"]
        # a registry move the client never saw (flock-scoped, not RUN_LOCK)
        storage.upsert(storage.load().teams["px"], expected_revision=1)
    snap = _job_done(client, job_id, state="error")
    assert snap["error"]["error"]["code"] == "E_TEAM_CONFLICT"
    assert "found 2" in snap["error"]["error"]["message"]
    assert "dev" not in {m.profile
                         for m in storage.load().teams["px"].members}


def test_f3_body_dry_run_honoured_and_conflict_400(client, world, tmp_path):
    """S19b F3: body ``"dry_run"`` is honored (it is in _CREATE_BODY_KEYS —
    accepting-then-ignoring it was the bug); query and body must AGREE,
    and an explicit conflict is an explicit 400."""
    before = _teams_yaml_bytes(world)
    body = {"slug": "px", "member": ["qa:orchestrator"],
            "workdir": str(tmp_path), "create_workdir": True,
            "dry_run": True}
    r = client.post(f"{P}/teams", json=body)
    assert r.status_code == 200, r.text                  # dry: plan, no job
    d = r.json()["data"]
    assert d["dry_run"] is True and d["team"]["slug"] == "px"
    assert _teams_yaml_bytes(world) == before            # nothing written
    r = client.post(f"{P}/teams?dry_run=true", json=body)  # agreement: dry
    assert r.status_code == 200, r.text
    # disagreement → explicit 400 naming both sides, never a silent pick
    r = client.post(f"{P}/teams?dry_run=true",
                    json={**body, "dry_run": False})
    assert r.status_code == 400
    env = r.json()["error"]
    assert env["code"] == "E_TEAM_INVALID" and "conflict" in env["message"]
    assert "query" in env["message"] and "body" in env["message"]
    # a non-boolean body value is refused, not ignored
    r = client.post(f"{P}/teams", json={**body, "dry_run": "maybe"})
    assert r.status_code == 400
    assert "boolean" in r.json()["error"]["message"]
    assert _teams_yaml_bytes(world) == before


def test_f4_sync_dry_run_honours_only(client, world, profile):
    """S19b F4: the dry preview carries exactly the items an apply with
    the same body would act on — the ``only`` filter is applied BEFORE
    reporting, like the CLI's _dry_report (cli.py:1247)."""
    import shutil
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    r = client.post(f"{P}/teams/px/members",
                    json={"blueprint": "agency-code-reviewer",
                          "expected_revision": 1})
    inst = _job_done(client, r.json()["data"]["job_id"])["result"][
        "created"][0]
    shutil.rmtree(world / "profiles" / inst)             # member.instance_missing
    shutil.rmtree(_TMP_ROOT[0] + "/work-px")             # workdir.exists
    r = client.post(f"{P}/teams/px/sync", json={})       # unfiltered dry
    d = r.json()["data"]
    checks = {i["check"] for i in d["drift"]}
    assert {"member.instance_missing", "workdir.exists"} <= checks
    # filtered dry: list form
    r = client.post(f"{P}/teams/px/sync",
                    json={"only": ["member.instance_missing"]})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["drift"], "the filtered check must still be reported"
    assert all(i["check"] == "member.instance_missing" for i in d["drift"])
    # comma-string form is equivalent (the CLI's --only spelling)
    r2 = client.post(f"{P}/teams/px/sync",
                     json={"only": "member.instance_missing"})
    assert r2.json()["data"]["drift"] == d["drift"]
    # dry wrote nothing
    assert not (world / "profiles" / inst).exists()


# ── busy guard: E_TEAM_BUSY → 409, on_busy "stop" goes through ─────────────

def _running_card(board, assignee):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        tid = kanban_db.create_task(conn, title="busy", assignee=assignee,
                                    initial_status="running")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()
    return tid


def test_busy_team_refuses_409_and_stop_goes_through(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    tid = _running_card("px", "qa")
    r = client.request("DELETE", f"{P}/teams/px", json={"confirm": "px"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "E_TEAM_BUSY"
    r = client.post(f"{P}/teams/px/workdir",
                    json={"workdir": _TMP_ROOT[0] + "/work-moved",
                          "expected_revision": storage.load().revision})
    assert r.status_code == 409, r.text
    assert "px" in storage.load().teams
    # waiting is the client's job: a request thread never blocks on a card
    r = client.request("DELETE", f"{P}/teams/px",
                       json={"confirm": "px", "on_busy": "wait"})
    assert r.status_code == 400
    r = client.request("DELETE", f"{P}/teams/px",
                       json={"confirm": "px", "on_busy": "stop"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["stopped"] == [tid]
    assert "px" not in storage.load().teams


def test_remove_member_stop_reassigns_and_resumes_the_card(
        client, world, profile):
    profile("qa")
    profile("dev")
    _job_done(client, _create_team(client,
                                    members=["qa:orchestrator",
                                             "dev:implementer"])["job_id"])
    tid = _running_card("px", "dev")
    body = {"confirm": "dev", "reassign_open_to": "qa"}
    r = client.request("DELETE", f"{P}/teams/px/members/dev", json=body)
    assert r.status_code == 409, r.text
    r = client.request("DELETE", f"{P}/teams/px/members/dev",
                       json=dict(body, on_busy="stop"))
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["stopped"] == [tid]
    assert d["reassigned"] == 1
    assert d["resumed"] == [tid]
    _tenant, assignee, _title, status = _task_row(world, "px", tid)
    assert (assignee, status) == ("qa", "ready")


def test_sync_apply_busy_409_while_dry_run_answers(client, world, profile,
                                                   monkeypatch):
    import reconcile
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    _running_card("px", "qa")
    monkeypatch.setattr(reconcile, "would_write", lambda *a, **k: True)
    r = client.post(f"{P}/teams/px/sync?dry_run=false", json={})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "E_TEAM_BUSY"
    r = client.post(f"{P}/teams/px/sync", json={})
    assert r.status_code == 200, r.text


# ── task runs: the activity panel's detail view ─────────────────────────────

def _card(board, title, *, tenant="px", parents=(), creator=None):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return kanban_db.create_task(conn, title=title, assignee="qa",
                                     tenant=tenant, board=board,
                                     parents=tuple(parents),
                                     creator_task_id=creator)


def _set_started(board, started):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        for tid, ts in started.items():
            conn.execute("update tasks set started_at = ? where id = ?",
                         (ts, tid))
        conn.commit()


def test_task_run_tree_in_execution_order(client, world, profile):
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    root = client.post(f"{P}/teams/px/tasks",
                       json={"title": "ship it"}).json()["data"]["task_id"]
    child = _card("px", "design", parents=[root])
    grand = _card("px", "build", parents=[child])
    orphan = _card("px", "review", creator=child)   # no link: creator adopts it
    _card("px", "foreign", tenant="other-team", parents=[root])
    other_root = _card("px", "unrelated")
    _set_started("px", {root: 100, child: 200, orphan: 300, grand: 400})

    r = client.get(f"{P}/teams/px/tasks/{root}")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["root_id"] == root
    assert [t["id"] for t in d["tasks"]] == [root, child, orphan, grand]
    assert d["tasks"][1]["parents"] == [root]
    assert d["tasks"][2]["parents"] == [child]
    assert client.get(f"{P}/teams/px/tasks/{grand}"
                      ).json()["data"]["root_id"] == root

    st = client.get(f"{P}/teams/px/status").json()["data"]
    assert {t["id"]: t["root_id"] for t in st["tasks"]}[grand] == root
    roots = {r["id"]: r for r in st["roots"]}
    assert set(roots) == {root, other_root}
    assert (roots[root]["total"], roots[root]["done"]) == (4, 0)


def test_task_log_reads_the_worker_log(client, world, profile):
    from hermes_cli import kanban_db
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    tid = client.post(f"{P}/teams/px/tasks",
                      json={"title": "t"}).json()["data"]["task_id"]
    r = client.get(f"{P}/teams/px/tasks/{tid}/log")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["exists"] is False
    path = kanban_db.worker_log_path(tid, board="px")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("line one\nline two\n", encoding="utf-8")
    d = client.get(f"{P}/teams/px/tasks/{tid}/log").json()["data"]
    assert (d["exists"], d["truncated"]) == (True, False)
    assert d["content"] == "line one\nline two\n"
    d = client.get(f"{P}/teams/px/tasks/{tid}/log?tail=12").json()["data"]
    assert d["truncated"] is True and d["content"] == "line two\n"


def test_task_run_404s_and_never_mints(client, world, profile):
    from hermes_cli import kanban_db
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    foreign = _card("px", "foreign", tenant="other-team")
    for path in (f"/teams/px/tasks/{foreign}", f"/teams/px/tasks/{foreign}/log",
                 "/teams/px/tasks/t_nope", "/teams/px/tasks/t_nope/log",
                 "/teams/ghost/tasks/t_nope", "/teams/ghost/tasks/t_nope/log"):
        r = client.get(P + path)
        assert r.status_code == 404, (path, r.text)
        assert r.json()["error"]["code"] == "E_TEAM_NOT_FOUND"
    db = kanban_db.kanban_db_path("px")
    db.unlink()
    for path in ("/teams/px/tasks/t_nope", "/teams/px/tasks/t_nope/log"):
        assert client.get(P + path).status_code == 404
    assert not db.exists(), "GET must not mint the board DB"


def test_task_forest_missing_board_is_empty(world):
    import resolve
    assert resolve.task_forest("no-such-board", "px") == {}


# ── approvals: the queue, its artifacts, and the two decisions ───────────────

def _park_card(board, tenant, *, title="needs a decision", reason="A or B?",
               comment=None, artifact=None, assignee="qa"):
    """A card genuinely parked on a human, built through the substrate's own
    primitives so the blocked event and its reason are the real ones."""
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        tid = kanban_db.create_task(
            conn, title=title, body="the proposal", assignee=assignee,
            tenant=tenant, initial_status="running", board=board)
        if comment:
            kanban_db.add_comment(conn, tid, f"{tenant}-lead", comment)
        if artifact is not None:
            kanban_db.add_attachment(
                conn, tid, filename=artifact.name, stored_path=str(artifact),
                content_type="text/markdown", size=artifact.stat().st_size)
        assert kanban_db.block_task(conn, tid, reason=reason, kind="needs_input")
    return tid


def _attachment_ids(board, task_id):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        return [a.id for a in kanban_db.list_attachments(conn, task_id)]


@pytest.fixture
def waiting(client, world, profile, tmp_path):
    """Team ``px`` with one card waiting on a human, artifact attached."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    art = tmp_path / "design.md"
    art.write_text("# Design\n\nOption A or Option B.\n", encoding="utf-8")
    tid = _park_card("px", "px", comment="Owner: approve A or B to unblock.",
                     artifact=art)
    return {"task_id": tid, "artifact": art,
            "attachment_id": _attachment_ids("px", tid)[0]}


def test_get_approvals_is_registry_wide(client, waiting):
    data = client.get(f"{P}/approvals").json()["data"]
    assert data["count"] == 1
    row = data["approvals"][0]
    assert row["id"] == waiting["task_id"]
    assert row["team"] == "px"
    assert row["headline"] == "Owner: approve A or B to unblock."
    assert row["block_reason"] == "A or B?"
    assert [a["filename"] for a in row["attachments"]] == ["design.md"]


def test_get_team_approvals_and_detail(client, waiting):
    data = client.get(f"{P}/teams/px/approvals").json()["data"]
    assert [r["id"] for r in data["approvals"]] == [waiting["task_id"]]
    d = client.get(
        f"{P}/teams/px/tasks/{waiting['task_id']}/approval").json()["data"]
    assert d["body"] == "the proposal"
    assert d["awaiting_approval"] is True
    assert [c["body"] for c in d["comments"]] == [
        "Owner: approve A or B to unblock."]


def test_get_attachment_serves_text_and_honours_tail(client, waiting):
    att = waiting["attachment_id"]
    body = client.get(f"{P}/teams/px/attachments/{att}").json()["data"]
    assert body["text"].startswith("# Design")
    assert body["truncated"] is False
    short = client.get(f"{P}/teams/px/attachments/{att}?tail=6").json()["data"]
    assert short["truncated"] is True and len(short["text"]) == 6


def test_get_attachment_of_another_tenant_is_404(client, waiting, tmp_path):
    """The attachment id is global to the board — the tenant check is the
    whole point of this route."""
    secret = tmp_path / "secret.md"
    secret.write_text("theirs", encoding="utf-8")
    foreign = _park_card("px", "other-tenant", artifact=secret)
    att = _attachment_ids("px", foreign)[0]
    r = client.get(f"{P}/teams/px/attachments/{att}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_TEAM_NOT_FOUND"


def test_approve_requires_the_confirm_token(client, waiting):
    tid = waiting["task_id"]
    r = client.post(f"{P}/teams/px/tasks/{tid}/approve", json={"note": "A"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"
    # and the card is untouched
    assert client.get(f"{P}/approvals").json()["data"]["count"] == 1


def test_approve_comments_unblocks_and_clears_the_queue(client, waiting):
    tid = waiting["task_id"]
    r = client.post(f"{P}/teams/px/tasks/{tid}/approve",
                    json={"note": "A: OAuth app-only", "confirm": tid})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["unblocked"] is True and data["status"] != "done"
    assert _task_row(None, "px", tid)["status"] != "blocked"
    assert client.get(f"{P}/approvals").json()["data"]["count"] == 0


def test_reject_park_keeps_the_card_in_the_queue(client, waiting):
    tid = waiting["task_id"]
    r = client.post(f"{P}/teams/px/tasks/{tid}/reject",
                    json={"reason": "cite the rate limits", "confirm": tid,
                          "park": True})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["unblocked"] is False
    assert client.get(f"{P}/approvals").json()["data"]["count"] == 1


def test_decision_on_a_card_that_is_not_waiting_is_400(client, waiting):
    tid = waiting["task_id"]
    assert client.post(f"{P}/teams/px/tasks/{tid}/approve",
                       json={"note": "A", "confirm": tid}).status_code == 200
    r = client.post(f"{P}/teams/px/tasks/{tid}/approve",
                    json={"note": "again", "confirm": tid})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"


def test_approvals_writes_are_405_when_the_dashboard_is_read_only(
        client, world, waiting):
    tid = waiting["task_id"]
    _writes_off(world)
    assert client.post(f"{P}/teams/px/tasks/{tid}/approve",
                       json={"note": "A", "confirm": tid}).status_code == 405
    assert client.post(f"{P}/teams/px/tasks/{tid}/reject",
                       json={"reason": "no", "confirm": tid}).status_code == 405
    # reading the queue is unaffected — the operator still SEES the card
    assert client.get(f"{P}/approvals").json()["data"]["count"] == 1


# ── the stuck lane on the dashboard ──────────────────────────────────────────

def _giveup_card(board, tenant, *, title="verify the engine",
                 error="protocol violation", failures=2):
    """A card parked by the dispatcher's circuit breaker: a raw UPDATE to
    blocked with no ``block_kind``, plus the ``gave_up`` event that carries the
    failure text under ``error``."""
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        tid = kanban_db.create_task(
            conn, title=title, assignee="qa", tenant=tenant,
            initial_status="running", board=board)
        conn.execute("UPDATE tasks SET status='blocked', consecutive_failures=?, "
                     "last_failure_error=? WHERE id=?", (failures, error, tid))
        kanban_db._append_event(conn, tid, "gave_up",
                                {"error": error, "failures": failures})
        conn.commit()
    return tid


def test_approvals_carry_the_lane_tag_and_the_counts(client, waiting):
    stuck = _giveup_card("px", "px")
    data = client.get(f"{P}/approvals").json()["data"]
    assert data["count"] == 2
    assert data["counts"] == {"needs_input": 1, "stuck": 1}
    kinds = {r["id"]: r["reason_kind"] for r in data["approvals"]}
    assert kinds == {waiting["task_id"]: "needs_input", stuck: "stuck"}
    row = next(r for r in data["approvals"] if r["id"] == stuck)
    assert row["failure_error"] == "protocol violation"
    assert row["consecutive_failures"] == 2


def test_team_approvals_filter_by_kind(client, waiting):
    stuck = _giveup_card("px", "px")
    only = client.get(f"{P}/teams/px/approvals?kind=stuck").json()["data"]
    assert [r["id"] for r in only["approvals"]] == [stuck]
    only = client.get(f"{P}/teams/px/approvals?kind=needs_input").json()["data"]
    assert [r["id"] for r in only["approvals"]] == [waiting["task_id"]]
    # a nonsense filter narrows nothing rather than failing the read
    both = client.get(f"{P}/teams/px/approvals?kind=nope").json()["data"]
    assert both["count"] == 2


def test_release_requires_the_confirm_token(client, waiting):
    tid = _giveup_card("px", "px")
    r = client.post(f"{P}/teams/px/tasks/{tid}/release", json={"note": "retry"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"
    assert _task_row(None, "px", tid)["status"] == "blocked"


def test_release_unblocks_the_stuck_card_and_clears_the_lane(client, waiting):
    tid = _giveup_card("px", "px")
    r = client.post(f"{P}/teams/px/tasks/{tid}/release",
                    json={"note": "run it under the wrapper", "confirm": tid})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["action"] == "release" and data["unblocked"] is True
    assert data["status"] != "done"
    assert _task_row(None, "px", tid)["status"] != "blocked"
    after = client.get(f"{P}/approvals").json()["data"]
    assert after["counts"] == {"needs_input": 1, "stuck": 0}


def test_release_on_a_card_waiting_for_a_human_is_400(client, waiting):
    tid = waiting["task_id"]
    r = client.post(f"{P}/teams/px/tasks/{tid}/release",
                    json={"note": "retry", "confirm": tid})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_INVALID"
    assert "hermes team approve" in r.json()["error"]["remedy"]
    assert _task_row(None, "px", tid)["status"] == "blocked"


def test_release_is_405_when_the_dashboard_is_read_only(client, world, waiting):
    tid = _giveup_card("px", "px")
    _writes_off(world)
    assert client.post(f"{P}/teams/px/tasks/{tid}/release",
                       json={"note": "retry", "confirm": tid}).status_code == 405
    # the operator still SEES the stuck card
    assert client.get(f"{P}/approvals").json()["data"]["counts"]["stuck"] == 1


def test_approval_detail_marks_a_stuck_card(client, waiting):
    tid = _giveup_card("px", "px")
    d = client.get(f"{P}/teams/px/tasks/{tid}/approval").json()["data"]
    assert d["stuck"] is True
    assert d["awaiting_approval"] is False
    assert d["block_reason"] == "protocol violation"


# ── steering a running card: POST …/note and the thread's comments ──────────

def _force_status(board, task_id, status):
    from hermes_cli import kanban_db, kanban_db_connect
    with kanban_db_connect.connect_closing(
            db_path=kanban_db.kanban_db_path(board)) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?",
                     (status, task_id))
        conn.commit()


@pytest.fixture
def steerable(client, world, profile):
    """Team ``px`` with one card the dispatcher is already running."""
    profile("qa")
    _job_done(client, _create_team(client)["job_id"])
    tid = client.post(f"{P}/teams/px/tasks",
                      json={"title": "ship it"}).json()["data"]["task_id"]
    _force_status("px", tid, "running")
    return tid


def test_note_comments_a_running_card_without_a_confirm_token(client, steerable):
    """A note only appends a comment — the same shape as creating a card, which
    takes no token either. ``delivery`` tells the composer whether the worker
    hears it now (the substrate's ≥6s comment bridge) or at its next run."""
    r = client.post(f"{P}/teams/px/tasks/{steerable}/note",
                    json={"text": "prefer the streaming parser"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["action"] == "note" and d["delivery"] == "mid-run"
    assert d["unblocked"] is False and d["comment_id"]
    assert _task_row(None, "px", steerable)["status"] == "running"


def test_note_without_text_is_400(client, steerable):
    for body in ({}, {"text": "   "}, {"text": 7}):
        r = client.post(f"{P}/teams/px/tasks/{steerable}/note", json=body)
        assert r.status_code == 400, (body, r.text)
        assert r.json()["error"]["code"] == "E_TEAM_INVALID"
        assert "--text" in r.json()["error"]["remedy"]


def test_note_on_another_tenants_card_is_404(client, steerable):
    foreign = _card("px", "not ours", tenant="other-team")
    r = client.post(f"{P}/teams/px/tasks/{foreign}/note", json={"text": "go"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_TEAM_NOT_FOUND"


def test_note_is_405_when_the_dashboard_is_read_only(client, world, steerable):
    _writes_off(world)
    assert client.post(f"{P}/teams/px/tasks/{steerable}/note",
                       json={"text": "go"}).status_code == 405


def test_task_tree_carries_comments_only_when_asked(client, steerable):
    client.post(f"{P}/teams/px/tasks/{steerable}/note", json={"text": "go wide"})
    plain = client.get(f"{P}/teams/px/tasks/{steerable}").json()["data"]
    assert "comments" not in plain["tasks"][0]
    with_comments = client.get(
        f"{P}/teams/px/tasks/{steerable}?comments=true").json()["data"]
    row = with_comments["tasks"][0]
    assert [c["body"] for c in row["comments"]] == ["NOTE: go wide"]
    assert row["comments"][0]["author"].startswith("operator")


def test_task_tree_carries_events_only_when_asked(client, steerable):
    """The thread draws one entry per transition, at the transition's own
    time — so the tree must be able to ship them, and a status poll must not
    pay for them."""
    plain = client.get(f"{P}/teams/px/tasks/{steerable}").json()["data"]
    assert "events" not in plain["tasks"][0]
    with_events = client.get(
        f"{P}/teams/px/tasks/{steerable}?events=true").json()["data"]
    rows = with_events["tasks"][0]["events"]
    assert [e["kind"] for e in rows][0] == "created"
    assert [e["created_at"] for e in rows] == sorted(e["created_at"] for e in rows)
    assert set(rows[0]) == {"id", "kind", "created_at", "payload"}


def test_task_tree_events_and_comments_are_independent(client, steerable):
    client.post(f"{P}/teams/px/tasks/{steerable}/note", json={"text": "go wide"})
    row = client.get(
        f"{P}/teams/px/tasks/{steerable}?comments=true&events=true"
    ).json()["data"]["tasks"][0]
    assert row["comments"] and "events" in row
    # The comment is not also an event: one message, one entry in the thread.
    assert "commented" not in [e["kind"] for e in row["events"]]
