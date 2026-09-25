"""dashboard/plugin_api.py — the hermes-agent-synergia HTTP surface (§10).

Mounted at ``/api/plugins/hermes-agent-synergia/*`` by the dashboard's
``_mount_plugin_api_routes()`` (``hermes_cli/web_server_dashboard.py:758``):
this file is exec'd by *file path* as ``hermes_dashboard_plugin_hermes_agent_synergia``
and must expose a module-level ``router`` (FastAPI ``APIRouter``). It is NOT
imported as a package submodule, so relative imports would fail — the
plugin's own modules are reached through ``_pkg()``/``_mod()`` below, by the
name the PluginManager loaded them under.

House style, copied from the closest analogue in the tree
(``$HA/plugins/kanban/dashboard/plugin_api.py``): thin wrappers over the
owning Python module, so the CLI and the HTTP surface cannot drift. Every
route here delegates to the same ``storage`` / ``catalog`` / ``provision`` /
``reconcile`` / ``cli`` functions the CLI calls — there is no second
implementation of any of them.

Guard g2 (auth): NONE of our own. The dashboard's ephemeral session token
is the gate (``auth_middleware`` in ``web_server.py``), and the whole
surface is gated twice more by the substrate: a ``user``-source plugin's
Python is imported only when ``hermes-agent-synergia`` is in ``plugins.enabled``
(GHSA-mcfc-hp25-cjv7), and its api file is refused for ``project``
plugins entirely. **Never ship this plugin as a project plugin**
(``./.hermes/plugins/``) — see §13, GHSA-5qr3-c538-wm9j. The re-check
mirrors the substrate's own plugin-API auth predicate
(``web_server._require_token`` :410-417): in GATED mode
(``app.state.auth_required``) the credential is the OAuth cookie session
— ``gated_auth_middleware`` (dashboard_auth/middleware.py:148) stamps
the verified session on ``request.state.session`` and a logged-in
browser deliberately carries NO token header; on the loopback/ungated
mount it is the ephemeral session token, re-checked through the vendored
copy of the substrate's own token predicate (``_has_valid_session_token``
below — a verbatim copy of the ``web_server`` owner's body, not an import
through a PLUGIN-COMPAT re-export block, per §5.2.1; S21b).
``request.state.token_authenticated`` (set by ``auth_middleware`` for a
valid token, gated or not) also attests. This is a defense-in-depth
RE-CHECK — never a second credential system, never a demand for the
credential the running mode does not send (S19b F1); it fails closed
when nothing is attested.

Guard g3 (CSRF): mutations require a JSON content type and the dashboard's
own Host/Origin same-origin check. Recorded provenance, do-not-invent: no
mutating route under ``hermes_cli/web_routers/`` carries an inline
guard — ``cron.py``, ``actions.py`` (``POST /api/gateway/drain``),
``files.py`` (``POST .../upload``, ``.../mkdir``, ``.../delete``) and
``ops.py`` rely on the middleware stack (custom session-token header →
CORS preflight; ``host_header_middleware`` for DNS rebinding,
GHSA-ppp5-vxwm-4cf7) plus Pydantic JSON bodies. The only *inline*
same-origin guard the dashboard ships is the one its mutating WebSocket
routes run — ``_ws_host_origin_reason``
(``hermes_cli/web_server_chat.py:165``, reached from
``web_routers/chat_ws.py:123`` via ``_ws_gate``, close code 4403), which
exists precisely because HTTP middleware does not run for WebSockets.
``_same_origin_reason`` below copies that guard verbatim (Host via the
substrate's own ``_is_accepted_host``; an http(s) ``Origin`` header, when
present, must target the bound host; missing/non-http Origin passes, as
the copy source does), and the JSON content-type requirement is the one
implement.md mandates on top of it.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import pathlib
import sys
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_HERE = pathlib.Path(__file__).resolve().parent
jobs: Any = None  # bound below (dashboard/jobs.py, same trust level)

# ── module plumbing (file-path exec ⇒ no relative imports) ──────────────────

_JOBS_NAME = "hermes_agent_synergia_dashboard_jobs"
if _JOBS_NAME in sys.modules:
    jobs = sys.modules[_JOBS_NAME]
else:
    _jspec = importlib.util.spec_from_file_location(_JOBS_NAME, _HERE / "jobs.py")
    jobs = importlib.util.module_from_spec(_jspec)
    sys.modules[_JOBS_NAME] = jobs
    _jspec.loader.exec_module(jobs)


_PKG_NAMES = ("hermes_plugins.hermes_agent_synergia", "hermes_agent_synergia")


def _pkg():
    """Resolve the hermes-agent-synergia package by whatever name it was loaded under
    (``hermes_plugins.hermes_agent_synergia`` under the PluginManager, ``hermes_agent_synergia``
    in the test harness, a ``__home_<digest>`` scope variant for a second
    install scope; load-from-file only as a last resort)."""
    for name in _PKG_NAMES:
        mod = sys.modules.get(name)
        if mod is not None:
            return mod
    for name in sorted(sys.modules):
        if name.startswith("hermes_plugins.hermes_agent_synergia"):
            return sys.modules[name]
    init = _HERE.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_agent_synergia", init, submodule_search_locations=[str(_HERE.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_agent_synergia"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mod(name: str):
    """One of the plugin's own modules (storage/model/catalog/...), by the
    owning-module convention: the route bodies never re-implement them."""
    return importlib.import_module(f"{_pkg().__name__}.{name}")


# ── envelopes (spec §7.3/§8.2 — the same shapes the CLI emits) ───────────────

def _env(code: str, message: str, *, hint: str, remedy: str) -> dict:
    return {"ok": False,
            "error": {"code": code, "message": message,
                      "hint": hint, "remedy": remedy}}


def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def _ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": True, "data": data})


def _team_err(exc) -> JSONResponse:
    """TeamError → HTTP. CAS/locked/exists → 409-423; not-found shapes →
    404; every other refusal is a client-correctable 400."""
    code = getattr(exc, "code", "") or ""
    status = {
        "E_TEAM_CONFLICT": 409, "E_TEAM_LOCKED": 423, "E_TEAM_EXISTS": 409,
        "E_TEAM_NOT_FOUND": 404, "E_TEAM_NO_BLUEPRINT": 404,
        "E_TEAM_NO_PROFILE": 404, "E_TEAM_NOT_MEMBER": 400,
        "E_TEAM_DRIFT_UNFIXED": 409, "E_TEAM_BUSY": 409,
    }.get(code, 400)
    env = exc.to_envelope() if hasattr(exc, "to_envelope") else _env(
        code or "E_TEAM_INVALID", str(exc),
        hint="See hermes team --help.", remedy="hermes team --help")
    return _json(status, env)


# ── guard g3 (+ g7 dashboard_writes gate) — ONE code path ───────────────────

def _writes_enabled() -> bool:
    """``plugins.entries.hermes-agent-synergia.settings.dashboard_writes`` (§11,
    default true). false restores the read-only dashboard: writes go through
    the CLI only."""
    settings = _mod("cli")._settings()
    value = settings.get("dashboard_writes")
    if value is None:
        return True
    return bool(value)


def _same_origin_reason(request: Request) -> Optional[str]:
    """COPIED from ``_ws_host_origin_reason``
    (``hermes_cli/web_server_chat.py:165``; the dashboard's own gate for
    its mutating WebSocket routes, ``web_routers/chat_ws.py:123``, close
    4403). Semantics preserved verbatim: no ``bound_host`` ⇒ skip; the
    Host header must be acceptable for the bound interface
    (``_is_accepted_host``, GHSA-ppp5-vxwm-4cf7); an http(s) ``Origin``,
    when present, must target the bound host; a missing or non-web
    Origin (file://, packaged Electron) is trusted, exactly as the copy
    source does — the credential check is the boundary there."""
    from hermes_cli.web_server import _is_accepted_host, app
    bound_host = getattr(app.state, "bound_host", None)
    if not bound_host:
        return None
    trusted_public_hosts = getattr(app.state, "trusted_public_hosts",
                                   frozenset())
    host_header = request.headers.get("host", "")
    if not _is_accepted_host(host_header, bound_host, trusted_public_hosts):
        return f"host_mismatch host={host_header or '?'} bound={bound_host}"
    origin = request.headers.get("origin", "")
    if not origin:
        return None
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return None
    if (not parsed.netloc
            or not _is_accepted_host(parsed.netloc, bound_host,
                                     trusted_public_hosts)):
        return f"origin_mismatch origin={origin} bound={bound_host}"
    return None


def _mutation_guard(request: Request) -> Optional[JSONResponse]:
    """Every non-GET on this router passes through here, exactly once.

    Order: setting gate (g7) → JSON content type → same-origin (g3,
    copied guard) → dashboard session token (g2, re-check of the
    dashboard's own credential — never our own auth).
    """
    path = request.url.path
    if not _writes_enabled():
        # §11: with dashboard_writes false, every non-GET EXCEPT the
        # dry-run sync returns 405 (dry-run sync writes nothing, so it is
        # the read path it stands for). sync's default is dry (§9).
        dry_sync = (path.endswith("/sync")
                    and (request.query_params.get("dry_run")
                         or "").strip().lower() != "false")
        if not dry_sync:
            return _json(405, _env(
                "DASHBOARD_READONLY",
                "dashboard_writes is false — this dashboard is read-only "
                "; writes stay on the CLI",
                hint="Set plugins.entries.hermes-agent-synergia.settings."
                     "dashboard_writes: true in the root config.yaml to "
                     "re-enable dashboard writes.",
                remedy="hermes team --help   # the CLI path is unaffected"))
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ctype != "application/json":
        return _json(415, _env(
            "CONTENT_TYPE_JSON_REQUIRED",
            f"content-type {ctype or '(missing)'!r} refused; mutations post "
            "application/json",
            hint="curl -H 'Content-Type: application/json' -d '{...}' "
                 "(the shape web_routers/ mutating routes accept)",
            remedy="set the Content-Type header to application/json"))
    reason = _same_origin_reason(request)
    if reason is not None:
        code = ("HOST_MISMATCH" if reason.startswith("host_")
                else "ORIGIN_MISMATCH")
        return _json(403, _env(
            code, reason,
            hint="Same-origin only — Host must match the bound interface "
                 "and an http(s) Origin, when sent, must target it "
                 "(DNS-rebinding guard, GHSA-ppp5-vxwm-4cf7).",
            remedy="drive this API from the dashboard, or from localhost "
                   "with an explicit Host header"))
    # g2: the dashboard's own credential, mirrored from the substrate's
    # plugin-API auth predicate (web_server.py ``_require_token`` :410-417,
    # static gate :588-492). In GATED mode (``app.state.auth_required``) a
    # logged-in browser carries the OAuth cookie session and DELIBERATELY
    # NO token header — auth_middleware skips the token path there and
    # gated_auth_middleware (dashboard_auth/middleware.py:148) stamps the
    # verified session on ``request.state.session``; ``token_authenticated``
    # is the seam's own token attestation. Accept any attestation the
    # middleware already won; else re-check the loopback/ungated token
    # through the vendored substrate predicate (``_has_valid_session_token``
    # below). Fail closed with nothing attested. (S19b F1: gated requests
    # must not 401 for lacking a credential the gate never sends.)
    if not _g2_ok(request):
        return _json(401, _env(
            "DASHBOARD_SESSION_REQUIRED",
            "unauthenticated dashboard request (no valid session token and "
            "no middleware-attested session)",
            hint="All /api/* routes — including this plugin's — require the "
                 "dashboard's credential: the ephemeral session token "
                 "(X-Hermes-Session-Token / Authorization: Bearer) on the "
                 "loopback mount, or a cookie session authenticated by the "
                 "gate when the dashboard runs in gated mode.",
            remedy="open the dashboard; the SPA injects the credential"))
    return None


def _g2_ok(request: Request) -> bool:
    """True iff this request carries a credential the substrate itself
    would accept. Order: middleware attestations first (gated mode), token
    seam second (loopback/ungated/in-process). Never demands the token in
    gated mode; never trusts the state when the middleware didn't run."""
    if getattr(request.state, "session", None) is not None:
        return True                       # gate/bearer session (gated mode)
    if getattr(request.state, "token_authenticated", False):
        return True                       # auth_middleware token attestation
    try:
        # Vendored copy of the substrate's own predicate for the dashboard's
        # OWN token (see _has_valid_session_token below). Fail closed if not
        # importable — without the substrate this file has no trust context
        # at all. (S21b: replaced the PLUGIN-COMPAT web_deps alias import;
        # that block is scheduled for deletion and §5.2.1 bans it.)
        return bool(_has_valid_session_token(request))
    except Exception:                                       # noqa: BLE001
        return False


# Vendored verbatim from the pinned substrate's REAL owner,
# thirdparty/hermes-agent/hermes_cli/web_server.py:379-390
# (``_has_valid_session_token``; the web_deps PLUGIN-COMPAT block was only a
# late-bound alias to it). We do NOT import the plugin-facing alias — it is a
# restored-def with COMPAT_REMOVAL_DATE 2026-09-14, after which PluginManager
# skips any plugin whose source references it, and §5.2.1 bans importing
# through PLUGIN-COMPAT re-export blocks outright. The live session token and
# header name are read late-bound off the web_server module object each call,
# exactly as the alias did, so ``_apply_ssh_session_token`` rotation
# (web_server.py :305-308) is honored; the token is snapshotted once per call so both compares
# use the same value (the original's two global reads cannot diverge in any
# observable way — a failed compare on either path still yields False).
def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated header avoids collisions with reverse proxies that already
    use ``Authorization`` (Caddy ``basic_auth``); the legacy Bearer path
    stays for older dashboard bundles.
    """
    import hmac
    from hermes_cli import web_server as _ws
    session_header = request.headers.get(_ws._SESSION_HEADER_NAME, "")
    token = _ws._SESSION_TOKEN
    if session_header and hmac.compare_digest(session_header.encode(),
                                               token.encode()):
        return True
    auth = request.headers.get("authorization", "")
    return hmac.compare_digest(auth.encode(), f"Bearer {token}".encode())


async def _body(request: Request):
    """The (already content-type-gated) JSON body. Returns a dict, or a
    JSONResponse 400 refusal the caller should return verbatim."""
    try:
        raw = await request.json()
    except Exception:                                       # noqa: BLE001
        return _json(400, _env(
            "E_TEAM_INVALID", "request body must be one JSON object",
            hint="Send { ... } with Content-Type: application/json.",
            remedy="see §10 for the body of each route"))
    if not isinstance(raw, dict):
        return _json(400, _env(
            "E_TEAM_INVALID", "request body must be a JSON object, not "
            "an array or scalar",
            hint="Send { ... } with Content-Type: application/json.",
            remedy="see §10 for the body of each route"))
    return raw


def _cas_gate(body: Mapping, reg) -> Optional[JSONResponse]:
    """g1 pre-flight: an explicit ``expected_revision`` must match the
    registry's current revision. The write's own CAS
    (``storage.upsert``/``save``) re-checks under the lock — this gate
    exists to answer 409 naming BOTH revisions before any side effect."""
    exp = body.get("expected_revision")
    if exp is None:
        return None
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return _json(400, _env(
            "E_TEAM_INVALID", "expected_revision must be an integer",
            hint="GET /teams first; send the registry revision you read.",
            remedy="hermes team list --json"))
    if exp_i != int(reg.revision):
        return _json(409, _env(
            "E_TEAM_CONFLICT",
            f"registry changed under us: expected revision {exp_i}, "
            f"found {reg.revision}",
            hint="E_TEAM_CONFLICT is retryable — fetch the current "
                 "revision, reapply your change, retry.",
            remedy="hermes team list --json   # then retry with the fresh "
                   "revision"))
    return None


# ── GET routes — thin wrappers over the owning modules ──────────────────────

@router.get("/catalog")
async def get_catalog(request: Request):
    """Available-profile entries — ``catalog.entries()``, the same
    call the CLI's ``team profiles list`` makes. Hidden entries stay out
    unless ``?include_hidden=true`` (the Profiles screen's toggle)."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_catalog_body, request)


def _truthy(value) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _catalog_body(request: Request):
    cat = _mod("catalog")
    qp = request.query_params
    rows = cat.entries(pack=qp.get("pack"), category=qp.get("category"),
                       role=qp.get("role"), q=qp.get("q"),
                       include_hidden=_truthy(qp.get("include_hidden")),
                       local_only=_truthy(qp.get("local")))
    if _truthy(qp.get("installed")):
        rows = [e for e in rows if e.get("installed_as")]
    cli = _mod("cli")
    return _ok({"entries": rows, "count": len(rows),
                "revision": cli._catalog_revision(),
                "default_profile_naming":
                    cli._settings().get("default_profile_naming") or ""})


@router.get("/catalog/{entry_id}")
def get_catalog_entry(entry_id: str):
    """One entry plus what the Profiles screen shows: SOUL.md, skills, and
    the team members whose blueprint it is."""
    store = _mod("catalog_store")
    try:
        entry = _mod("catalog").get(entry_id)
        detail = store.describe(entry.source_path)
        refs = store.references(entry.id)
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok({"entry": entry.to_dict(), "soul": detail["soul"],
                "soul_truncated": detail["soul_truncated"],
                "skills": detail["skills"], "referenced_by": refs})


# ── catalog store writes — the same g1-g7 guards as every write ─────────────

def _catalog_gate(body: Mapping) -> Optional[JSONResponse]:
    """g1 pre-flight against the catalog index's OWN revision (it has its
    own CAS counter, separate from teams.yaml's)."""
    exp = body.get("expected_revision")
    if exp is None:
        return None
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return _json(400, _env(
            "E_TEAM_INVALID", "expected_revision must be an integer",
            hint="GET /catalog first; send the catalog revision you read.",
            remedy="hermes team profiles list --json"))
    try:
        current = _mod("catalog_store").load_index(use_cache=False)["revision"]
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    if exp_i != current:
        return _json(409, _env(
            "E_TEAM_CONFLICT",
            f"catalog changed under us: expected revision {exp_i}, "
            f"found {current}",
            hint="E_TEAM_CONFLICT is retryable — fetch the current revision, "
                 "reapply your change, retry.",
            remedy="hermes team profiles list --json   # then retry with the "
                   "fresh revision"))
    return None


def _expected(body: Mapping) -> Optional[int]:
    exp = body.get("expected_revision")
    return None if exp is None else int(exp)


@router.post("/catalog")
async def post_catalog_entry(request: Request):
    """Author a new entry in the plugin's store (inert files — no job, 201).
    Server-side paths (the CLI's --skill-dir) are deliberately not
    accepted over HTTP; skills arrive inline."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    eid = body.get("id")
    if not eid or not isinstance(eid, str):
        return _json(400, _env(
            "E_TEAM_INVALID", 'body must carry the entry "id"',
            hint='{"id": "my-reviewer", "display_name": "…", "category": '
                 '"qa", "description": "…", "soul": "# Role …", "skills": '
                 '[{"name": "…", "description": "…", "body": "…"}]}',
            remedy="hermes team profiles add --help"))
    skills = body.get("skills") or []
    if not isinstance(skills, list):
        return _json(400, _env(
            "E_TEAM_INVALID", '"skills" must be a list of objects',
            hint='[{"name": "wcag-review", "description": "…", "body": "…"}]',
            remedy="hermes team profiles add --help"))
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    store = _mod("catalog_store")
    try:
        result = await _thread(lambda: store.author_entry(
            eid,
            display_name=str(body.get("display_name") or ""),
            category=str(body.get("category") or ""),
            description=str(body.get("description") or ""),
            soul=str(body.get("soul") or ""),
            skills=skills,
            version=str(body.get("version") or ""),
            author=str(body.get("author") or ""),
            license=str(body.get("license") or ""),
            replace=bool(body.get("replace", False)),
            expected_revision=_expected(body)))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(result, status=201)


_EDIT_TEXT_FIELDS = ("display_name", "category", "description", "soul",
                     "version", "author", "license")


@router.patch("/catalog/{entry_id}")
async def patch_catalog_entry(request: Request, entry_id: str):
    """Edit a local entry in place: absent (or null) keys are left alone,
    ``""`` clears a field, ``skills`` upserts inline skills and
    ``remove_skills`` deletes them. As on POST, server-side skill
    directories are not accepted over HTTP."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    fields = {k: body[k] for k in _EDIT_TEXT_FIELDS if body.get(k) is not None}
    bad = sorted(k for k, v in fields.items() if not isinstance(v, str))
    skills = body.get("skills") or []
    remove = body.get("remove_skills") or []
    if (bad or not isinstance(skills, list) or not isinstance(remove, list)
            or not all(isinstance(n, str) for n in remove)):
        return _json(400, _env(
            "E_TEAM_INVALID",
            'text fields must be strings, "skills" a list of objects and '
            '"remove_skills" a list of names'
            + (f" (bad: {', '.join(bad)})" if bad else ""),
            hint='{"soul": "# Role …", "skills": [{"name": "…", '
                 '"description": "…", "body": "…"}], "remove_skills": '
                 '["old-skill"]}',
            remedy="hermes team profiles edit --help"))
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    store = _mod("catalog_store")
    try:
        result = await _thread(lambda: store.update_entry(
            entry_id, skills=skills, remove_skills=remove,
            expected_revision=_expected(body), **fields))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(result)


@router.get("/root-profiles")
async def get_root_profiles():
    """Live profiles under the hermes root that ``POST /catalog/adopt`` can
    take. They are NOT catalog entries — the catalog never reads the profiles
    root (there is no tier C).

    Not mounted at ``/catalog/root``: ``GET /catalog/{entry_id}`` above would
    shadow it.
    """
    from starlette.concurrency import run_in_threadpool
    rows = await run_in_threadpool(_mod("catalog").root_profiles)
    return _ok({"profiles": rows, "count": len(rows)})


@router.post("/catalog/adopt")
async def post_catalog_adopt(request: Request):
    """``{"profile": "my-reviewer"}`` copies a live root profile into the
    plugin's store as a real entry (201). A local copy is fast, so no job."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    profile = body.get("profile")
    if not profile or not isinstance(profile, str):
        return _json(400, _env(
            "E_TEAM_INVALID", 'body must carry the "profile" to adopt',
            hint='{"profile": "my-reviewer", "name": "reviewer", '
                 '"replace": false}',
            remedy="hermes team profiles adopt --help"))
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    store = _mod("catalog_store")
    try:
        result = await _thread(lambda: store.adopt_profile(
            profile,
            name=(body.get("name") or None),
            replace=bool(body.get("replace", False)),
            expected_revision=_expected(body)))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(result, status=201)


@router.post("/catalog/import")
async def post_catalog_import(request: Request):
    """Import from a directory or git URL. ``?dry_run=true`` → 200 {plan}
    (staged in a temp dir, nothing written); otherwise 202 {job_id}, since
    a clone can take a while (g4)."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    source = body.get("source")
    only = body.get("only")
    name = body.get("name") or None
    if not source or not isinstance(source, str) or (
            only is not None and not isinstance(only, (list, str))):
        return _json(400, _env(
            "E_TEAM_INVALID",
            'body must carry "source" (string) and optionally "only" '
            '(list or comma string)',
            hint='{"source": "https://github.com/Dadmin88/hermes-profile-packs",'
                 ' "only": ["agency-qa-tester"]}',
            remedy="hermes team profiles import --help"))
    store = _mod("catalog_store")
    if (request.query_params.get("dry_run") or "").strip().lower() == "true":
        try:
            plan = await _thread(
                lambda: store.plan_import(source, only=only, name=name))
        except Exception as exc:                            # noqa: BLE001
            return _team_err(exc)
        return _ok({"plan": plan, "dry_run": True})
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    exp = _expected(body)
    replace = bool(body.get("replace", False))

    def _work(job_id):
        result = store.import_entries(
            source, only=only, name=name, replace=replace,
            expected_revision=exp,
            progress=lambda msg: jobs.set_progress(job_id, msg))
        return {**result, "restart_notice": None}

    job = jobs.start("catalog.import", _work, title=f"import {source}")
    return _ok({"job_id": job["id"], "job": job}, status=202)


@router.delete("/catalog/{entry_id}")
async def delete_catalog_entry(request: Request, entry_id: str):
    """Delete a local entry; profiles already instantiated from it are
    untouched."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    refusal = _confirm(body, entry_id, "catalog entry deletion")
    if refusal is not None:
        return refusal
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    store = _mod("catalog_store")
    try:
        result = await _thread(lambda: store.remove_entry(
            entry_id, expected_revision=_expected(body)))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(result)


@router.post("/catalog/{entry_id}/visibility")
async def post_catalog_visibility(request: Request, entry_id: str):
    """``{"hidden": bool}`` — keep an entry out of the pickers, or put it
    back. Works for external-root entries too; nothing under a pack root
    is written."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    hidden = body.get("hidden")
    if not isinstance(hidden, bool):
        return _json(400, _env(
            "E_TEAM_INVALID", 'body must carry "hidden": true|false',
            hint='{"hidden": true}',
            remedy=f"hermes team profiles hide {entry_id}"))
    gate = _catalog_gate(body)
    if gate is not None:
        return gate
    store = _mod("catalog_store")
    try:
        result = await _thread(lambda: store.set_hidden(
            entry_id, hidden, expected_revision=_expected(body)))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(result)


@router.get("/recipes")
def get_recipes(request: Request):
    rows = _mod("catalog").recipes()
    qp = request.query_params
    pack, q = qp.get("pack"), (qp.get("q") or "").lower()
    if pack:
        rows = [r for r in rows if r.get("pack") == pack]
    if q:
        rows = [r for r in rows
                if q in (str(r.get("id", "")) + " " + str(r.get("display_name", ""))
                         + " " + " ".join(r.get("keywords") or [])).lower()]
    return _ok({"recipes": rows, "count": len(rows)})


@router.get("/skills")
def get_skills(request: Request):
    """Installed skills + enabled state for one profile — the member
    picker's multi-select (1b). ``skills.disabled`` is the complement
    stored per deviation 8."""
    resolve = _mod("resolve")
    profile = (request.query_params.get("profile") or "").strip()
    if not profile:
        return _json(400, _env(
            "E_TEAM_INVALID", "GET /skills requires ?profile=",
            hint="?profile=<profile-name>",
            remedy="curl -H \"X-Hermes-Session-Token: ...\" "
                   "…/skills?profile=px-backend-engineer"))
    if not resolve.profile_exists(profile):
        return _json(404, _env(
            "E_TEAM_NO_PROFILE", f"profile {profile!r} does not exist",
            hint="Known profiles: hermes profile list",
            remedy=f"hermes profile create {profile} --clone"))
    installed = resolve.installed_skill_names(profile)
    cfg = resolve.read_profile_config(profile) or {}
    disabled = set((cfg.get("skills") or {}).get("disabled") or [])
    return _ok({"profile": profile,
                "skills": [{"name": n, "enabled": n not in disabled}
                           for n in installed]})


@router.get("/teams")
def get_teams():
    """List + per-team counts (§10). Counts are the six §4.3 buckets,
    tenant-scoped, degrading to zeros for a missing board."""
    storage = _mod("storage")
    resolve = _mod("resolve")
    reg = storage.load()
    rows = []
    for slug in sorted(reg.teams):
        t = reg.teams[slug]
        rows.append({"slug": t.slug, "display_name": t.display_name,
                     "board": t.board, "workdir": t.workdir,
                     "orchestrator": t.orchestrator,
                     "members": len(t.members), "revision": t.revision,
                     "counts": resolve.task_counts(t.board,
                                                   t.tenant or None)})
    return _ok({"teams": rows, "count": len(rows),
                "revision": reg.revision})


def _team_or_404(slug: str):
    storage = _mod("storage")
    reg = storage.load()
    t = reg.teams.get(slug)
    if t is None:
        return None, _json(404, _env(
            "E_TEAM_NOT_FOUND", f"no team {slug!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list")), reg, storage
    return t, None, reg, storage


@router.get("/teams/{slug}")
def get_team(slug: str):
    """Record + ``_resolved`` (§4.3) — the same computed block the CLI's
    ``show`` returns, via :func:`resolve.observe`."""
    resolve = _mod("resolve")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    d = t.to_dict()
    d["_resolved"] = resolve.observe(t).to_dict()
    return _ok({"team": d})


def _task_view(task) -> dict:
    """The activity panel's row shape (§10 status). Body is omitted —
    the panel polls; large bodies belong in the task detail view."""
    return {"id": task.id, "title": task.title, "status": task.status,
            "step_label": _mod("model").step_label(task.title, task.id),
            "assignee": task.assignee, "priority": task.priority,
            "tenant": task.tenant, "workspace_kind": task.workspace_kind,
            "workspace_path": task.workspace_path,
            "created_at": task.created_at, "started_at": task.started_at,
            "completed_at": task.completed_at, "skills": task.skills}


@router.get("/teams/{slug}/status")
def get_team_status(slug: str):
    """Tenant-scoped task rows — the activity panel's only source (1b).
    Each row names the started task it belongs to (``root_id``); ``roots``
    lists the started tasks, newest first, with their subtask progress.

    §5.2.1 hazard: ``kanban_db_connect`` creates and migrates on first
    open, so a GET must gate on the board's DB FILE existing — same
    discipline :func:`resolve.task_counts` already follows."""
    kb = _kb()
    resolve = _mod("resolve")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    db_path = kb.kanban_db_path(t.board)
    present = db_path.is_file()
    forest = resolve.task_forest(t.board, t.tenant or t.slug) if present else {}
    rows, roots = [], []
    for rid, group in forest.items():
        tasks = group["tasks"]
        roots.append({"id": rid, "total": len(tasks),
                      "done": sum(1 for x in tasks if x.status == "done"),
                      "_created": tasks[0].created_at})
        rows.extend(dict(_task_view(x), root_id=rid) for x in tasks)
    rows.sort(key=lambda r: (-(r["priority"] or 0), r["created_at"]))
    roots.sort(key=lambda r: -r.pop("_created"))
    return _ok({"team": t.slug, "board": t.board,
                "board_db_path": str(db_path) if present else None,
                "counts": resolve.task_counts(t.board, t.tenant or None),
                "tasks": rows, "roots": roots})


def _task_404(t, task_id: str) -> JSONResponse:
    return _json(404, _env(
        "E_TEAM_NOT_FOUND",
        f"no card {task_id!r} for team {t.slug} on board {t.board!r}",
        hint="Only cards in the team's tenant are visible here.",
        remedy=f"hermes team status {t.slug}"))


@router.get("/teams/{slug}/tasks/{task_id}")
def get_team_task(request: Request, slug: str, task_id: str):
    """One started task and all its subtasks, in execution order — the
    chat thread's detail view. A subtask id answers with the tree it
    belongs to. Read-only: ``resolve.task_forest`` never mints the DB.

    ``?comments=true`` adds each card's comments. Opt-in, because it costs a
    second pass over the board: the thread wants them (they ARE the
    conversation — operator notes, approve/reject decisions, agent replies),
    a status poll does not.

    ``?events=true`` adds each card's status transitions (``created``,
    ``claimed``, ``blocked``, ``completed``, …), the same way and for the same
    reason: the thread draws one entry per transition at the transition's own
    time, so a card never moves once it is on screen.

    ``?attachments=true`` adds each card's files, so a deliverable is readable
    in the thread instead of only in the approval inbox. Same opt-in, same
    extra pass; the views come from ``approvals`` so the chip the thread draws
    is the chip the approval card draws."""
    resolve = _mod("resolve")
    approvals = _mod("approvals")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    want_comments = _truthy(request.query_params.get("comments"))
    want_events = _truthy(request.query_params.get("events"))
    want_files = _truthy(request.query_params.get("attachments"))
    for rid, group in resolve.task_forest(t.board, t.tenant or t.slug).items():
        if any(x.id == task_id for x in group["tasks"]):
            ids = [x.id for x in group["tasks"]]
            comments = (resolve.task_comments(t.board, ids)
                        if want_comments else {})
            events = resolve.task_events(t.board, ids) if want_events else {}
            files = approvals.task_attachments(t, ids) if want_files else {}
            rows = []
            for x in group["tasks"]:
                row = dict(_task_view(x),
                           parents=group["parents"].get(x.id, []),
                           summary=group["summaries"].get(x.id))
                if want_comments:
                    row["comments"] = comments.get(x.id, [])
                if want_events:
                    row["events"] = events.get(x.id, [])
                if want_files:
                    row["attachments"] = files.get(x.id, [])
                rows.append(row)
            return _ok({"team": t.slug, "board": t.board, "root_id": rid,
                        "tasks": rows})
    return _task_404(t, task_id)


_LOG_TAIL_DEFAULT = 256 * 1024
_LOG_TAIL_MAX = 2_000_000


@router.get("/teams/{slug}/tasks/{task_id}/log")
def get_team_task_log(request: Request, slug: str, task_id: str):
    """Worker stdout/stderr of one team card — the payload of the kanban
    plugin's ``GET /tasks/{id}/log``, limited to the team's tenant.
    ``?tail=`` caps the bytes returned (default 256 KiB)."""
    kb = _kb()
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    db_path = kb.kanban_db_path(t.board)
    task = None
    if db_path.is_file():
        from hermes_cli import kanban_db_connect
        with kanban_db_connect.connect_closing(db_path=db_path) as conn:
            task = kb.get_task(conn, task_id)
    if task is None or task.tenant != (t.tenant or t.slug):
        return _task_404(t, task_id)
    try:
        tail = int(request.query_params.get("tail") or _LOG_TAIL_DEFAULT)
    except ValueError:
        tail = _LOG_TAIL_DEFAULT
    tail = min(max(tail, 1), _LOG_TAIL_MAX)
    content = kb.read_worker_log(task_id, tail_bytes=tail, board=t.board)
    path = kb.worker_log_path(task_id, board=t.board)
    size = path.stat().st_size if path.is_file() else 0
    return _ok({"task_id": task_id, "exists": content is not None,
                "size_bytes": size, "truncated": size > tail,
                "path": str(path), "content": content or ""})


# ── approvals: the operator inbox ────────────────────────────────────────────
#
# Thin wrappers over ``approvals.py`` — the CLI calls the same functions, so
# the predicates, the comment bodies and the refusals cannot drift between the
# two surfaces. Every lane comes back in one list — needs_input, stuck,
# review, triage, guarded — each row tagged with ``reason_kind`` and counted
# in ``counts``, which is what lets the UI show one panel and pick the verb
# per row. ``GET /approvals`` is registry-wide and is the ONLY source the
# header badge reads: the badge is what makes a parked card findable from any
# dashboard page.

def _kinds(request: Request):
    """The ``?kind=`` filter, mirroring the CLI's ``--kind``. Anything else is
    every lane — a bad query narrows nothing rather than 400-ing a read."""
    approvals = _mod("approvals")
    kind = (request.query_params.get("kind") or "all").strip()
    # ``None`` — not the whole tuple — so the unfiltered read honours the
    # ``parked_lanes`` setting. An explicit ``?kind=`` is the operator asking
    # for that lane by name and overrides it.
    return (kind,) if kind in approvals.QUEUE_KINDS else None


@router.get("/approvals")
def get_approvals(request: Request):
    """Every team's parked cards, every lane, newest wait first."""
    approvals = _mod("approvals")
    rows = approvals.queue_all(kinds=_kinds(request))
    return _ok({"count": len(rows), "counts": _lane_counts(rows),
                "approvals": rows})


@router.get("/teams/{slug}/approvals")
def get_team_approvals(request: Request, slug: str):
    approvals = _mod("approvals")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        rows = approvals.queue(t, kinds=_kinds(request))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok({"team": t.slug, "board": t.board, "count": len(rows),
                "counts": _lane_counts(rows), "approvals": rows})


def _lane_counts(rows) -> dict:
    """``{lane: n}`` over every lane — the panel subtitle and the badge.

    Zeros included: the UI indexes this without a guard, and a badge whose
    key set changes with the data flickers.
    """
    approvals = _mod("approvals")
    out = dict.fromkeys(approvals.QUEUE_KINDS, 0)
    for r in rows:
        kind = r.get("reason_kind")
        if kind in out:
            out[kind] += 1
    return out


@router.get("/teams/{slug}/tasks/{task_id}/approval")
def get_team_task_approval(slug: str, task_id: str):
    """One parked card in full: body, every comment, the artifact list."""
    approvals = _mod("approvals")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        return _ok(approvals.detail(t, task_id))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)


@router.get("/teams/{slug}/tasks/{task_id}/summary")
async def get_team_task_summary(request: Request, slug: str, task_id: str):
    """What this card is asking, and the choices that answer it.

    Deliberately its own route. The inbox polls every 5s and must never cost a
    model call, so the lists carry only ``summary_key`` (the card revision) and
    the briefing is fetched per card, once per revision, by whoever is actually
    looking at it. ``?refresh=1`` re-asks the model for the same revision.

    A read, so no ``_mutation_guard`` — but a blocking one (SQLite, then the
    model), so it runs on a worker thread like every other blocking call here.
    """
    summarize = _mod("summarize")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    refresh = (request.query_params.get("refresh") or "").strip() in ("1", "true", "yes")
    try:
        return _ok(await _thread(
            lambda: summarize.summary(t, task_id, refresh=refresh)))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)


@router.get("/teams/{slug}/attachments/{attachment_id}")
def get_team_attachment(request: Request, slug: str, attachment_id: int):
    """One artifact, inline. The attachment id is global to the board, so
    ``approvals.artifact`` resolves it to its task and refuses when that
    task's tenant is not this team's — one team's URL space must not read
    another's blob. Text types only; ``?tail=`` caps the bytes."""
    approvals = _mod("approvals")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        tail = int(request.query_params.get("tail")
                   or approvals.ARTIFACT_TAIL_DEFAULT)
    except ValueError:
        tail = approvals.ARTIFACT_TAIL_DEFAULT
    try:
        return _ok(approvals.artifact(t, attachment_id, max_bytes=tail))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)



# ── workdir browser: the Workdir tab's read-only file list ──────────────────
#
# ``?path=`` is relative to the team's workdir; ``resolve`` refuses ``..``,
# absolute paths, symlinks out of the folder and credential files.

@router.get("/teams/{slug}/files")
def get_team_files(request: Request, slug: str):
    resolve = _mod("resolve")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        return _ok(resolve.workdir_listing(
            t, request.query_params.get("path") or ""))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)


@router.get("/teams/{slug}/files/read")
def get_team_file(request: Request, slug: str):
    """The start of one file as text; ``?tail=`` caps the bytes."""
    resolve = _mod("resolve")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        tail = int(request.query_params.get("tail")
                   or resolve.WORKDIR_PREVIEW_DEFAULT)
    except ValueError:
        tail = resolve.WORKDIR_PREVIEW_DEFAULT
    try:
        return _ok(resolve.workdir_file(
            t, request.query_params.get("path") or "", max_bytes=tail))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)


@router.get("/teams/{slug}/files/download")
def get_team_file_download(request: Request, slug: str):
    """One file as a data URL — a plain link would not carry the session
    token header, so the page saves it itself, like the host Files tab."""
    resolve = _mod("resolve")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        return _ok(resolve.workdir_download(
            t, request.query_params.get("path") or ""))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)

async def _decision_route(request: Request, slug: str, task_id: str,
                          *, action: str):
    """Shared body of approve/reject/release: guard, confirm token, then the
    ``approvals`` call on a worker thread (it writes SQLite)."""
    approvals = _mod("approvals")
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    refusal = _confirm(body, task_id, f"{action} decision on {task_id}")
    if refusal is not None:
        return refusal
    text = body.get("reason") if action == "reject" else body.get("note")
    park = bool(body.get("park")) if action == "reject" else False
    try:
        if action == "approve":
            payload = await _thread(
                lambda: approvals.approve(t, task_id, text))
        elif action == "release":
            payload = await _thread(
                lambda: approvals.release(t, task_id, text))
        else:
            payload = await _thread(
                lambda: approvals.reject(t, task_id, text, park=park))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(payload)


@router.post("/teams/{slug}/tasks/{task_id}/approve")
async def post_team_task_approve(request: Request, slug: str, task_id: str):
    """``{note, confirm: "<task_id>"}`` — the lane picks the move.

    On a ``needs_input`` card: comment + back to the pool (unblocked, or
    specified out of ``triage``), and the assignee resumes, acts on the note
    and completes it. On a ``review`` card: comment +
    complete, because the operator has just made the only decision that card
    was waiting for and no agent run will make it again. Either way the
    children are promoted by the completion, not by this route.
    """
    return await _decision_route(request, slug, task_id, action="approve")


@router.post("/teams/{slug}/tasks/{task_id}/reject")
async def post_team_task_reject(request: Request, slug: str, task_id: str):
    """``{reason, confirm: "<task_id>", park?}`` — comment, then rework.

    ``needs_input`` returns to the pool; ``review`` reopens to the
    implementer named in the ``review_requested`` event. ``park: true``
    records the comment and moves nothing.
    """
    return await _decision_route(request, slug, task_id, action="reject")


@router.post("/teams/{slug}/tasks/{task_id}/release")
async def post_team_task_release(request: Request, slug: str, task_id: str):
    """``{note, confirm: "<task_id>"}`` — put a parked card back in the pool.

    One verb over the three lanes no human verdict is owed on: ``stuck``
    unblocks and resets the retry budget, ``triage`` specifies back to
    ``todo``, ``guarded`` clears the stale failure the dispatcher's respawn
    guard reads. Refused on a card waiting for a human decision
    (``needs_input``, ``review``): that one is approved or rejected, so the
    decision reaches the agent as a decision.
    """
    return await _decision_route(request, slug, task_id, action="release")


@router.post("/teams/{slug}/tasks/{task_id}/note")
async def post_team_task_note(request: Request, slug: str, task_id: str):
    """``{text}`` — steer a card that is already moving: comment, no unblock.

    No ``confirm`` token: g5 guards the destructive verbs, and this one only
    appends a comment, exactly like ``POST /teams/{slug}/tasks``. The worker
    picks it up through the substrate's live comment bridge, so ``delivery``
    in the payload says whether the agent hears it mid-run or at its next run.
    """
    approvals = _mod("approvals")
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _json(400, _env(
            "E_TEAM_INVALID", "text is required",
            hint='{"text": "…"} — what the agent should take into account.',
            remedy=f'hermes team note {task_id} --text "…"'))
    try:
        payload = await _thread(lambda: approvals.note(t, task_id, text))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(payload)


async def _thread_route(request: Request, slug: str, task_id: str,
                        *, action: str):
    approvals = _mod("approvals")
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    if action == "pause":
        refusal = _confirm(body, task_id, f"pause of the thread of {task_id}")
        if refusal is not None:
            return refusal
    verb = approvals.pause if action == "pause" else approvals.resume
    try:
        payload = await _thread(lambda: verb(t, task_id))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(payload)


@router.post("/teams/{slug}/tasks/{task_id}/pause")
async def post_team_task_pause(request: Request, slug: str, task_id: str):
    """``{confirm: "<task_id>"}`` — park every running, ready and todo card of
    the card's thread and stop the running workers.

    Confirmed because it kills a worker: the agent's in-process context is
    lost and resume starts a fresh run over the same workdir.
    """
    return await _thread_route(request, slug, task_id, action="pause")


@router.post("/teams/{slug}/tasks/{task_id}/resume")
async def post_team_task_resume(request: Request, slug: str, task_id: str):
    """``{}`` — re-queue every paused card of the card's thread. No
    ``confirm``: it only puts cards back in the pool."""
    return await _thread_route(request, slug, task_id, action="resume")


@router.get("/teams/{slug}/tasks/{task_id}/merge")
def get_team_task_merge(slug: str, task_id: str):
    """Preview of ``hermes team merge``: the result branch of the card's
    thread, the commits it holds over the workdir's branch, and what would
    stop a fast-forward. Writes nothing."""
    cli = _mod("cli")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        data = cli._merge_impl(t.slug, task_id)
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    data.pop("_human", None)
    return _ok(data)


@router.get("/teams/{slug}/tasks/{task_id}/report")
def get_team_task_report(slug: str, task_id: str):
    """What happened on the card's thread: per card, who worked and reviewed
    it, how long each attempt ran and the tokens it cost
    (:func:`resolve.thread_report`). Writes nothing."""
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    try:
        data = _mod("resolve").thread_report(t, task_id)
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(data)


@router.post("/teams/{slug}/tasks/{task_id}/merge")
async def post_team_task_merge(request: Request, slug: str, task_id: str):
    """``{}`` — ``hermes team merge --apply``: fast-forward the workdir's
    branch to the thread's result. No ``confirm``: only a fast-forward onto a
    clean checkout is made, so nothing in the workdir is lost."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    cli = _mod("cli")
    try:
        data = await _thread(lambda: cli._merge_impl(t.slug, task_id,
                                                     apply=True))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    data.pop("_human", None)
    return _ok(data)


@router.get("/teams/{slug}/drift")
def get_team_drift(slug: str):
    reconcile = _mod("reconcile")
    t, err, _reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    items = reconcile.diff(t)
    return _ok({"team": t.slug,
                "drift": [d.to_dict() for d in items],
                "ready": reconcile.readiness(t, items)})


@router.get("/doctor")
def get_doctor():
    """Registry-wide invariant report — the same payload the CLI's
    ``doctor --json`` wraps, via the factored ``cli._doctor_impl``."""
    cli = _mod("cli")
    payload = cli._doctor_impl(argparse.Namespace(json=True))
    payload = dict(payload)
    payload.pop("_human", None)
    return _ok(payload)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    snap = jobs.snapshot(job_id)
    if snap is None:
        return _json(404, _env(
            "E_TEAM_NOT_FOUND", f"no job {job_id!r} (jobs are process "
            "state — a gateway restart clears them; recover via "
            "`hermes team sync <team>`)",
            hint="Jobs are in-process by design (guard g4).",
            remedy="hermes team sync <team> --json"))
    return _ok({"job": snap})


def _parse_dry(value, where: str):
    """One ``dry_run`` value -> True/False/None, or a 400 refusal. Accepted:
    JSON booleans, and the query string's ``true``/``false`` (anything else
    the caller's contract treats as absent for the query, invalid for the
    body)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    if where == "query":
        return None                       # unknown flag value: absent, as
    return _json(400, _env(              #   argparse would, then default
        "E_TEAM_INVALID",
        f"dry_run must be a boolean, got {value!r} in the {where}",
        hint='"dry_run": true or false (JSON boolean).',
        remedy="see §10 for the body of each route"))


# ── POST routes (mutations) ─────────────────────────────────────────────────

_CREATE_BODY_KEYS = {
    "slug", "team", "name", "roster", "member", "tier", "display_name",
    "description", "board", "allow_shared_board", "workdir",
    "workspace_policy", "profile_naming", "orchestrator", "policy",
    "create_board", "create_workdir", "create_missing", "clone_from",
    "clone_secrets", "link_env", "no_sync", "expected_revision",
    "dry_run",
}


def _create_ns(body: Mapping, slug: str, dry_run: bool) -> argparse.Namespace:
    """POST /teams body → the exact Namespace argparse would build for
    ``hermes team create`` (defaults mirrored from the parser), so the
    dashboard runs THE SAME code path (no fork). The board, the workdir and
    the embedded sync are forced on: a created team must be ready for work,
    so ``create_board``/``create_workdir``/``no_sync`` are accepted and
    overridden."""
    return argparse.Namespace(
        team=slug, member=list(body.get("member") or []),
        roster=body.get("roster"),
        from_specs=None, from_recipe=None,
        tier=body.get("tier") or "recommended",
        orchestrator=body.get("orchestrator"),
        board=body.get("board"),
        create_board=True,
        allow_shared_board=bool(body.get("allow_shared_board", False)),
        workdir=body.get("workdir"),
        create_workdir=True,
        workspace_policy=body.get("workspace_policy") or "worktree",
        policy=body.get("policy"),
        display_name=body.get("display_name"),
        description=body.get("description"),
        profile_naming=body.get("profile_naming"),
        create_missing=bool(body.get("create_missing", False)),
        clone_from=body.get("clone_from"),
        clone_secrets=bool(body.get("clone_secrets", False)),
        link_env=bool(body.get("link_env", False)),
        dry_run=bool(dry_run),
        no_sync=False,
        expected_revision=body.get("expected_revision"),
        json=True,
    )


async def _thread(fn):
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(fn)


@router.post("/teams")
async def post_team(request: Request):
    """Create a team. Body is ``{name, workdir, roster: [{blueprint, role,
    main, skills, model, provider}], …}`` — the roster is catalog profiles,
    ``main`` marks the orchestrator — or a team spec with ``slug``/``member``.
    ``name`` derives the slug and is the display name. Everything runs
    inside ``cli._create_impl`` — reused, not forked. ``?dry_run=true`` →
    200 {plan}; otherwise 202 {job_id} (g4)."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    unknown = set(body) - _CREATE_BODY_KEYS
    if unknown:
        return _json(400, _env(
            "E_TEAM_INVALID",
            f"unknown body key(s): {', '.join(sorted(unknown))}",
            hint="Accepted: " + ", ".join(sorted(_CREATE_BODY_KEYS)),
            remedy="§10: body is {name, workdir, roster, …}"))
    roster = body.get("roster")
    if roster is not None and (not isinstance(roster, list) or not all(
            isinstance(row, dict) for row in roster)):
        return _json(400, _env(
            "E_TEAM_INVALID", '"roster" must be a list of objects',
            hint='[{"blueprint": "agency-backend-engineer", "main": true}, '
                 '{"blueprint": "agency-code-reviewer"}]',
            remedy="GET /catalog lists the blueprints"))
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _json(400, _env(
            "E_TEAM_INVALID", '"name" must be a string',
            hint='{"name": "Project X", …}', remedy="hermes team create --help"))
    name = (name or "").strip()
    slug = body.get("slug") or body.get("team")
    if not slug and name:
        try:
            slug = _mod("model").slugify(name)
        except Exception as exc:                            # noqa: BLE001
            return _team_err(exc)
    if name and not body.get("display_name"):
        body["display_name"] = name
    if not slug or not isinstance(slug, str):
        return _json(400, _env(
            "E_TEAM_INVALID", "body must carry the team name as \"name\"",
            hint='e.g. {"name": "Project X", "workdir": "~/repos/x", '
                 '"roster": [{"blueprint": "…", "main": true}]}',
            remedy="hermes team create --help"))
    # S19b F3: ?dry_run= and body "dry_run": both are honored (curl
    # ergonomics), and when both speak they must agree — an explicit
    # conflict is a 400, never a silent pick.
    dry_q = _parse_dry(request.query_params.get("dry_run"), "query")
    if isinstance(dry_q, JSONResponse):
        return dry_q
    dry_b = _parse_dry(body.get("dry_run"), "body")
    if isinstance(dry_b, JSONResponse):
        return dry_b
    if dry_q is not None and dry_b is not None and dry_q != dry_b:
        return _json(400, _env(
            "E_TEAM_INVALID",
            f"dry_run conflict: query says {dry_q!r}, body says {dry_b!r}",
            hint="Send the flag in ONE place: ?dry_run=true or "
                 '"dry_run": true in the body.',
            remedy="§10: POST /teams takes ?dry_run= or body dry_run"))
    dry = dry_q if dry_q is not None else bool(dry_b)
    cli = _mod("cli")
    reg = _mod("storage").load()
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    ns = _create_ns(body, str(slug), dry)
    if ns.dry_run:
        try:
            payload = await _thread(lambda: cli._create_impl(ns))
        except Exception as exc:                            # noqa: BLE001
            return _team_err(exc)
        payload = dict(payload)
        payload.pop("_human", None)
        return _ok(payload)
    job = jobs.start("team.create", lambda _jid: _create_job_work(cli, ns),
                     title=f"create {slug}")
    return _ok({"job_id": job["id"], "job": job}, status=202)


def _create_job_work(cli, ns) -> dict:
    """Job body for POST /teams: run the CLI's own create and lift the
    §9.4 restart notice into ``result.restart_notice`` (g4 — a field,
    never prose)."""
    payload = cli._create_impl(ns)
    result = {k: v for k, v in payload.items() if k != "_human"}
    result["restart_notice"] = (result.get("reconciled") or {}).get(
        "restart_notice")
    return result


@router.post("/teams/{slug}/members")
async def post_team_member(request: Request, slug: str):
    """Add one member: ``{"profile": …}`` adopts an existing profile;
    ``{"blueprint": …}`` instantiates a managed instance through the same
    (draft, entry, plan) provisioning path the CLI uses. dry-run → 200
    {plan}; otherwise 202 {job_id} (g4)."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, reg, storage = _team_or_404(slug)
    if err is not None:
        return err
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    profile = body.get("profile")
    blueprint = body.get("blueprint")
    if bool(profile) == bool(blueprint):
        return _json(400, _env(
            "E_TEAM_INVALID",
            "body must carry exactly one of \"profile\" (adopt an existing "
            "profile) or \"blueprint\" (instantiate a catalog entry)",
            hint='{"profile": "qa"} or {"blueprint": "agency-qa-tester", '
                 '"role": "verifier", "expected_revision": <r>}',
            remedy="hermes team add-member --help"))
    dry = (request.query_params.get("dry_run") or "").strip().lower() == "true"
    model = _mod("model")
    if profile:
        ns = argparse.Namespace(
            team=t.slug, profile=str(profile),
            role=str(body.get("role") or "member"),
            capacity=body.get("capacity"),
            primary=bool(body.get("primary", False)),
            dry_run=dry, expected_revision=body.get("expected_revision"),
            json=True,
        )
        cli = _mod("cli")
        if dry:
            try:
                payload = await _thread(lambda: cli._add_member_impl(ns))
            except Exception as exc:                        # noqa: BLE001
                return _team_err(exc)
            payload = dict(payload)
            payload.pop("_human", None)
            payload.setdefault("plan", None)
            payload["dry_run"] = True
            return _ok(payload)
        job = jobs.start("team.member_add",
                         lambda _jid: _adopt_job_work(cli, ns),
                         title=f"add {profile} to {t.slug}")
        return _ok({"job_id": job["id"], "job": job}, status=202)
    # ── managed (blueprint) path ───────────────────────────────────────────
    catalog = _mod("catalog")
    provision = _mod("provision")
    try:
        entry = catalog.get(str(blueprint))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    cli = _mod("cli")
    naming = (t.profile_naming
              or cli._settings().get("default_profile_naming")
              or model.DEFAULT_PROFILE_NAMING)
    name = (model._normalize_profile_name(str(body["name"]))
            if body.get("name")
            else provision.render_name(t.slug, entry, naming))
    draft = model.Member.from_dict({
        "profile": name,
        "role": str(body.get("role") or entry.suggested_role or "member"),
        "capacity": int(body.get("capacity") or 1),
        "blueprint": entry.id,
        "blueprint_source": f"pack:{entry.pack}",
        "managed": True,
        "primary": True,          # managed members are always primary
        # S20 picker: the chosen skill set rides onto the member exactly as
        # the create roster stamps it (``cli._plan_roster``) — a
        # TRUTHINESS gate, not an isinstance one, so the two paths produce
        # identical records for identical input. ``[]`` and ``None`` are not
        # interchangeable here: reconcile skips ``skills is None`` ("not
        # managed by the team", reconcile.py:972) but projects ``[]`` through
        # ``resolve.disabled_skills_for`` into "disable every installed
        # skill". An empty pick must therefore land as None (``_dump_known``
        # then omits the key), never as an empty list. Elements are copied
        # verbatim, as the roster path does; ``disabled_skills_for`` is
        # where str/strip normalisation belongs.
        "skills": (list(body["skills"])
                   if isinstance(body.get("skills"), list) and body["skills"]
                   else None),
    })
    candidate = model.Team.from_dict({
        **t.to_dict(),
        "members": [m.to_dict() for m in t.members] + [draft.to_dict()],
    })
    try:
        errs = model.check_invariants(reg, candidate)
        if errs:
            raise errs[0]
        if dry:
            plan = provision.plan(entry, name)
            return _ok({"team": candidate.to_dict(), "plan": plan,
                        "dry_run": True})
        # S19b F2 (g1): the job's upsert CASes against the CLIENT's
        # revision when it sent one — otherwise the snapshot this route
        # just read (never None: a queue-window registry move must be
        # able to 409 the job, not pass silently).
        exp = body.get("expected_revision")
        exp = int(reg.revision if exp is None else exp)
        job = jobs.start(
            "team.member_add",
            lambda _jid: _managed_job_work(entry, draft, candidate,
                                           storage, exp),
            title=f"instantiate {name}")
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok({"job_id": job["id"], "job": job}, status=202)


def _adopt_job_work(cli, ns) -> dict:
    payload = cli._add_member_impl(ns)
    result = {k: v for k, v in payload.items() if k != "_human"}
    result.setdefault("restart_notice", None)
    return result


def _managed_job_work(entry, draft, candidate, storage, expected_revision) -> dict:
    provision = _mod("provision")
    reconcile = _mod("reconcile")
    created = []
    provision.instantiate(entry, draft.profile, role=draft.role)
    created.append(draft.profile)
    stored = storage.upsert(candidate, expected_revision=expected_revision)
    report = reconcile.apply(stored)
    return {"team": stored.to_dict(), "created": created,
            "reconciled": report.to_dict(),
            "restart_notice": report.restart_notice}


@router.post("/teams/{slug}/tasks")
async def post_team_task(request: Request, slug: str):
    """Start a task on a team (§10, §7.4). Board, tenant, workspace and
    the default assignee come from the RECORD — exactly as
    ``team_create_task`` does; a non-member assignee is refused with the
    §7.3 envelope."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    kb = _kb()
    model = _mod("model")
    t, err, reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        return _json(400, _env(
            "E_TEAM_INVALID", "title is required",
            hint='{"title": "...", "body": "...", "assignee": "<member>"} '
                 "— assignee must be a team member.",
            remedy=f"hermes team show {t.slug}   # the roster"))
    members = {m.profile: m for m in t.members}
    assignee = body.get("assignee")
    role = body.get("role")
    if assignee:
        assignee = model._normalize_profile_name(str(assignee))
        if assignee not in members:
            return _json(400, _env(
                "E_TEAM_NOT_MEMBER",
                f"{assignee} is not a member of team {t.slug}",
                hint="Members: " + ", ".join(m.profile for m in t.members),
                remedy=f"hermes team add-member {t.slug} {assignee}"))
    elif role:
        match = next((m for m in t.members if m.role == str(role)), None)
        if match is None:
            return _json(400, _env(
                "E_TEAM_NOT_MEMBER",
                f"no team member holds role {str(role)!r}",
                hint="Team roles: " + (", ".join(sorted(
                    {m.role for m in t.members})) or "(none)"),
                remedy=f"hermes team add-member {t.slug} <profile> --role "
                       f"{role}"))
        assignee = match.profile
    else:
        assignee = t.orchestrator or None
    workspace_kind = _mod("policy").WORKSPACE_KIND.get(
        t.workspace_policy or "scratch", "scratch")
    import os
    workdir = str(os.path.expanduser(t.workdir or ""))
    workspace_path = workdir if workspace_kind in ("worktree", "dir") else None
    resolve = _mod("resolve")

    def _write():
        from hermes_cli import kanban_db_connect
        resolve.prepare_worktree_workdir(t)
        # Board follows the record; create it like ``create --create-board``
        # (mkdir -p semantics, §5.5) — this endpoint is a mutation, so the
        # board DB being minted here is in scope (unlike GET /status).
        if not resolve.board_exists(t.board):
            resolve.ensure_board(t.board, name=model.board_label(t),
                                 default_workdir=t.workdir or None)
        with kanban_db_connect.connect_closing(
                db_path=kb.kanban_db_path(t.board)) as conn:
            task_id = kb.create_task(
                conn, created_by="dashboard", board=t.board,
                title=title.strip(), body=body.get("body"),
                assignee=assignee, tenant=(t.tenant or t.slug),
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                priority=int(body.get("priority") or 0),
                # initial_status omitted: the CLI leaves it at the
                # kanban default (``running``); VALID_INITIAL_STATUSES
                # does not even allow ``todo``.
                parents=tuple(body.get("parents") or ()),
                skills=(list(body["skills"])
                        if isinstance(body.get("skills"), list) else None))
        return {"task_id": task_id, "board": t.board,
                "tenant": (t.tenant or t.slug), "assignee": assignee}

    try:
        data = await _thread(_write)
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok(data, status=201)


@router.post("/teams/{slug}/sync")
async def post_team_sync(request: Request, slug: str):
    """``?dry_run=true`` (the default) → DriftItem[] preview, nothing
    written — the one non-GET allowed with dashboard_writes false (§11).
    ``?dry_run=false`` applies."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    dry_raw = (request.query_params.get("dry_run") or "true").strip().lower()
    dry = dry_raw != "false"
    body: dict = {}
    if (request.headers.get("content-type") or "").strip():
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
    t, err, reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    reconcile = _mod("reconcile")

    # the CLI's sync flags ride the body (same names, same defaults);
    # ``--reinstantiate`` etc. stay opt-in exactly as on the CLI (§9.3).
    # create_missing is the CLI's both-gates alias (S11's _cmd_sync).
    cm = bool(body.get("create_missing"))
    cb = cm or bool(body.get("create_board"))
    cw = cm or bool(body.get("create_workdir"))
    only_raw = body.get("only")
    only: Optional[frozenset] = None
    if isinstance(only_raw, str):
        only = frozenset(c.strip() for c in only_raw.split(",") if c.strip())
    elif isinstance(only_raw, list):
        only = frozenset(str(c).strip() for c in only_raw if str(c).strip())
    elif only_raw is not None:
        return _json(400, _env(
            "E_TEAM_INVALID", "only must be a comma-separated string or "
            "a list of check names",
            hint='e.g. "member.instance_missing,workdir.exists" or '
                 '["member.instance_missing"]',
            remedy="hermes team sync <team> --dry-run --only <checks>"))
    if only is not None and not only:
        only = None
    on_busy, refusal = _on_busy(body)
    if refusal is not None:
        return refusal
    cli = _mod("cli")

    def _run():
        if dry:
            # S19b F4: the dry preview carries exactly the items an apply
            # with the same body would act on/report — filter FIRST, like
            # the CLI's _dry_report (cli.py:1247), not after.
            items = reconcile.diff(t)
            if only is not None:
                items = [d for d in items if d.check in only]
            return reconcile.ApplyReport(
                slug=t.slug, applied=[], drift=items,
                profiles_updated=0, restart_notice=None, exit_code=0), {}
        report, fields, _lines = cli._sync_one_apply(
            t,
            reinstantiate=bool(body.get("reinstantiate")),
            link_env=bool(body.get("link_env")),
            create_board=cb,
            create_workdir=cw,
            only=only,
            on_busy=on_busy)
        return report, fields

    try:
        report, fields = await _thread(_run)
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    return _ok({"team": t.slug, "dry_run": dry,
                "drift": [d.to_dict() for d in report.drift],
                "applied": [a.to_dict() for a in report.applied],
                "profiles_updated": report.profiles_updated,
                "restart_notice": report.restart_notice,
                "exit_code": report.exit_code, **fields})


@router.post("/teams/{slug}/workdir")
async def post_team_workdir(request: Request, slug: str):
    """Move the team's root folder — ``hermes team set-workdir --apply``:
    record, board default_workdir, git (worktree), every primary member's
    terminal.cwd. ``?dry_run=true`` → nothing written, candidate drift."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t, err, reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    workdir = body.get("workdir")
    if not isinstance(workdir, str) or not workdir.strip():
        return _json(400, _env(
            "E_TEAM_INVALID", "workdir is required",
            hint='{"workdir": "/abs/path", "expected_revision": <r>}',
            remedy=f"hermes team set-workdir {t.slug} <dir> --apply"))
    dry = (request.query_params.get("dry_run") or "").strip().lower() == "true"
    on_busy, refusal = _on_busy(body)
    if refusal is not None:
        return refusal
    cli = _mod("cli")
    try:
        data = await _thread(lambda: cli._set_workdir_impl(
            t.slug, workdir.strip(), apply=not dry, preview=dry,
            expected_revision=body.get("expected_revision"),
            on_busy=on_busy))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    data.pop("_human", None)
    return _ok(data)


# ── DELETE routes (mutations; confirm required — g5) ────────────────────────

def _confirm(body: Mapping, want: str, what: str):
    got = body.get("confirm")
    if got == want:
        return None
    return _json(400, _env(
        "E_TEAM_INVALID",
        f"destructive delete requires confirm: {want!r} (got {got!r})",
        hint=f'The body must carry {{"confirm": "{want}"}} to acknowledge '
             f"the {what}.",
        remedy='curl ... -d \'{"confirm": "' + want + '"}\''))


def _on_busy(body: Mapping):
    """``(mode, refusal)`` for a mutation body's ``on_busy``: ``refuse``
    (default) or ``stop``. The dashboard waits by retrying ``refuse`` —
    a request thread never blocks on a running card."""
    mode = body.get("on_busy") or "refuse"
    if mode in ("refuse", "stop"):
        return mode, None
    return None, _json(400, _env(
        "E_TEAM_INVALID", f"on_busy must be refuse or stop (got {mode!r})",
        hint='To wait, retry with "refuse" until the team is idle (the '
             "dashboard does); the CLI blocks with --on-busy wait.",
        remedy="hermes team <command> ... --on-busy wait"))


@router.delete("/teams/{slug}")
async def delete_team(request: Request, slug: str):
    """Destroy the record and its managed profiles (``purge_profiles``
    defaults to true; ``false`` keeps them). Profile purge acts ONLY on
    managed members and reports the skipped ones."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    _t, err, reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    refusal = _confirm(body, slug, "team destruction")
    if refusal is not None:
        return refusal
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    on_busy, refusal = _on_busy(body)
    if refusal is not None:
        return refusal
    ns = argparse.Namespace(
        team=slug, yes=True,
        purge_board=bool(body.get("purge_board")),
        purge_profiles=bool(body.get("purge_profiles", True)),
        on_busy=on_busy, json=True)
    cli = _mod("cli")
    try:
        payload = await _thread(lambda: cli._destroy_impl(ns))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    payload = dict(payload)
    payload.pop("_human", None)
    return _ok(payload)


@router.delete("/teams/{slug}/members/{profile}")
async def delete_team_member(request: Request, slug: str, profile: str):
    """Remove one member. ``delete_profile`` acts on managed members only
    (the payload reports a skipped one). ``reassign_open_to`` moves the
    removed member's open cards to another member."""
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    model = _mod("model")
    t, err, reg, _storage = _team_or_404(slug)
    if err is not None:
        return err
    canon = model._normalize_profile_name(profile)
    refusal = _confirm(body, canon, "member removal")
    if refusal is not None:
        return refusal
    cas = _cas_gate(body, reg)
    if cas is not None:
        return cas
    on_busy, refusal = _on_busy(body)
    if refusal is not None:
        return refusal
    cli = _mod("cli")
    ns = argparse.Namespace(
        team=t.slug, profile=canon,
        delete_profile=bool(body.get("delete_profile")),
        on_busy=on_busy, json=True)
    try:
        payload = await _thread(lambda: cli._remove_member_impl(ns))
    except Exception as exc:                                # noqa: BLE001
        return _team_err(exc)
    payload = dict(payload)
    payload.pop("_human", None)
    reassign_to = body.get("reassign_open_to")
    if reassign_to is not None:
        reassign_to = model._normalize_profile_name(str(reassign_to))
        stored = model.Team.from_dict(payload["team"])
        if reassign_to not in {m.profile for m in stored.members}:
            payload["reassign_skipped"] = {
                "to": reassign_to,
                "reason": f"{reassign_to} is not a member of team {stored.slug}",
            }
            payload["reassigned"] = 0
        else:
            moved = await _thread(
                lambda: _reassign_open(stored, canon, reassign_to))
            payload["reassigned"] = len(moved)
            # Cards the busy guard stopped continue under their new owner.
            stopped = set(payload.get("stopped") or ())
            to_resume = [tid for tid in moved if tid in stopped]
            if to_resume:
                resolve = _mod("resolve")
                payload["resumed"] = await _thread(
                    lambda: resolve.resume_cards(stored.board, to_resume))
    return _ok(payload)


def _reassign_open(team, old_profile: str, new_profile: str) -> list:
    """Move *team*'s open cards from ``old_profile`` to ``new_profile``
    and return their ids (public ``kanban_db.list_tasks``/``assign_task``;
    the board's own DB — never teams.yaml)."""
    kb = _kb()
    from hermes_cli import kanban_db_connect
    db_path = kb.kanban_db_path(team.board)
    if not db_path.is_file():
        return []
    moved = []
    with kanban_db_connect.connect_closing(db_path=db_path) as conn:
        for task in kb.list_tasks(conn, assignee=old_profile):
            if task.status in ("done", "archived"):
                continue
            try:
                if kb.assign_task(conn, task.id, new_profile):
                    moved.append(task.id)
            except RuntimeError:      # running under a claim — leave it
                continue
    return moved


# ── substrate accessors (lazy, matching resolve.py's discipline) ────────────

def _kb():
    from hermes_cli import kanban_db
    return kanban_db
