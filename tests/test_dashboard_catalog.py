"""Dashboard routes over ``catalog_store``: GET flags + detail,
POST /catalog (author), POST /catalog/import (dry run, then a 202 job),
DELETE /catalog/{id} (confirm), POST /catalog/{id}/visibility — each behind
the same g1–g7 guards as every other write. Mount and fixtures are
test_dashboard_api's, so the world carries an external pack root with
agency-backend-engineer / agency-code-reviewer / agency-qa-tester.
"""

from __future__ import annotations

import yaml

import catalog_store
from test_catalog_store import _pack_repo
from test_dashboard_api import (  # noqa: F401 — fixtures, incl. the autouse ones
    P, _fresh, _fresh2, _job_done, _session_token, anon, client, world)


def _ids(client, query=""):
    return {e["id"] for e in client.get(P + "/catalog" + query).json()["data"]["entries"]}


def test_get_catalog_carries_revision_and_flags(client):
    data = client.get(P + "/catalog").json()["data"]
    assert data["revision"] == 0
    assert data["entries"]
    assert all(e["local"] is False and e["hidden"] is False for e in data["entries"])


def test_author_then_detail(client):
    r = client.post(P + "/catalog", json={
        "id": "ui-one", "display_name": "UI One", "category": "qa",
        "soul": "# Soul",
        "skills": [{"name": "alpha", "description": "A", "body": "do it"}]})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["revision"] == 1
    d = client.get(P + "/catalog/ui-one").json()["data"]
    assert d["entry"]["local"] is True and d["entry"]["display_name"] == "UI One"
    assert d["soul"] == "# Soul\n"
    assert [(s["name"], s["description"], s["body"]) for s in d["skills"]] == [
        ("alpha", "A", "do it\n")]
    assert d["soul_truncated"] is False
    assert d["referenced_by"] == {"teams": [], "recipes": []}


def test_edit_then_detail(client):
    client.post(P + "/catalog", json={
        "id": "ed-ui", "display_name": "Ed", "description": "One.", "soul": "# Soul",
        "skills": [{"name": "alpha", "description": "A", "body": "a"}]})
    rev = client.get(P + "/catalog").json()["data"]["revision"]
    r = client.patch(P + "/catalog/ed-ui", json={
        "description": "Two.", "display_name": None,
        "skills": [{"name": "beta", "description": "B", "body": "b"}],
        "expected_revision": rev})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["changed"] == ["description", "skills"]
    d = client.get(P + "/catalog/ed-ui").json()["data"]
    assert (d["entry"]["display_name"], d["entry"]["description"]) == ("Ed", "Two.")
    assert d["soul"] == "# Soul\n"
    assert [s["dir"] for s in d["skills"]] == ["alpha", "beta"]


def test_edit_refusals_write_nothing(client, anon):
    client.post(P + "/catalog", json={"id": "ed-no", "soul": "x"})
    rev = catalog_store.load_index(use_cache=False)["revision"]
    for body in ({"skills": "nope"}, {"soul": 5}, {"remove_skills": [1]}, {}):
        assert client.patch(P + "/catalog/ed-no", json=body).status_code == 400, body
    assert client.patch(P + "/catalog/ghost-one", json={"soul": "y"}).status_code == 404
    assert client.patch(P + "/catalog/agency-qa-tester",
                        json={"soul": "y"}).status_code == 400
    assert anon.patch(P + "/catalog/ed-no", json={"soul": "y"}).status_code == 401
    assert client.patch(P + "/catalog/ed-no", content="soul=y",
                        headers={"content-type": "text/plain"}).status_code == 415
    r = client.patch(P + "/catalog/ed-no", json={"soul": "y", "expected_revision": 9})
    assert r.status_code == 409
    assert catalog_store.load_index(use_cache=False)["revision"] == rev
    assert (catalog_store.profiles_dir() / "ed-no" / "SOUL.md").read_text() == "x\n"


def test_author_refusals_write_nothing(client, anon):
    assert client.post(P + "/catalog", json={}).status_code == 400
    assert client.post(P + "/catalog",
                       json={"id": "x-one", "skills": "nope"}).status_code == 400
    assert anon.post(P + "/catalog", json={"id": "x-one"}).status_code == 401
    assert client.post(P + "/catalog", content="id=x",
                       headers={"content-type": "text/plain"}).status_code == 415
    r = client.post(P + "/catalog", json={"id": "x-one", "expected_revision": 9})
    assert r.status_code == 409 and "9" in r.json()["error"]["message"]
    r = client.post(P + "/catalog", json={"id": "agency-qa-tester"})
    assert r.status_code == 409          # would shadow the external pack root
    assert not catalog_store.index_path().exists()


def test_import_dry_run_then_job(client, tmp_path):
    repo = _pack_repo(tmp_path / "import-src")
    r = client.post(P + "/catalog/import?dry_run=true",
                    json={"source": str(repo), "only": ["agency-a"]})
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()["data"]["plan"]["entries"]] == ["agency-a"]
    assert not catalog_store.index_path().exists()

    r = client.post(P + "/catalog/import", json={"source": str(repo)})
    assert r.status_code == 202, r.text
    job = _job_done(client, r.json()["data"]["job_id"])
    assert job["state"] == "done", job
    assert job["result"]["count"] == 3 and job["result"]["restart_notice"] is None
    assert {"agency-a", "agency-b", "agency-c"} <= _ids(client)


def test_import_bad_body_and_bad_source(client):
    assert client.post(P + "/catalog/import", json={}).status_code == 400
    r = client.post(P + "/catalog/import?dry_run=true",
                    json={"source": "/definitely/not/here"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_TEAM_CATALOG_UNAVAILABLE"


def test_delete_requires_confirm(client):
    client.post(P + "/catalog", json={"id": "del-one", "soul": "x"})
    assert client.request("DELETE", P + "/catalog/del-one", json={}).status_code == 400
    assert (catalog_store.profiles_dir() / "del-one").is_dir()
    r = client.request("DELETE", P + "/catalog/del-one", json={"confirm": "del-one"})
    assert r.status_code == 200 and r.json()["data"]["removed"] == "del-one"
    assert "del-one" not in _ids(client, "?include_hidden=true")


def test_delete_external_entry_is_refused(client):
    r = client.request("DELETE", P + "/catalog/agency-qa-tester",
                       json={"confirm": "agency-qa-tester"})
    assert r.status_code == 400 and "hide" in r.json()["error"]["remedy"]


def test_visibility_toggles_the_picker_view(client):
    r = client.post(P + "/catalog/agency-qa-tester/visibility", json={"hidden": True})
    assert r.status_code == 200 and r.json()["data"]["hidden"] is True
    assert "agency-qa-tester" not in _ids(client)
    assert "agency-qa-tester" in _ids(client, "?include_hidden=true")
    entry = client.get(P + "/catalog/agency-qa-tester").json()["data"]["entry"]
    assert entry["hidden"] is True
    assert client.post(P + "/catalog/agency-qa-tester/visibility",
                       json={"hidden": "yes"}).status_code == 400
    client.post(P + "/catalog/agency-qa-tester/visibility", json={"hidden": False})
    assert "agency-qa-tester" in _ids(client)


def test_readonly_dashboard_refuses_catalog_writes(client, world):
    cfg_path = world / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["plugins"]["entries"]["hermes-agent-synergia"]["settings"]["dashboard_writes"] = False
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert client.post(P + "/catalog", json={"id": "ro-one"}).status_code == 405
    assert client.post(P + "/catalog/import?dry_run=true",
                       json={"source": "/x"}).status_code == 405
    assert client.request("DELETE", P + "/catalog/x",
                          json={"confirm": "x"}).status_code == 405
    assert client.patch(P + "/catalog/x", json={"soul": "y"}).status_code == 405
    assert client.get(P + "/catalog").status_code == 200
