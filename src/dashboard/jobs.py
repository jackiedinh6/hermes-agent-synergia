"""In-process job registry backing the dashboard's ``202 {job_id}`` responses.

Guard g4 (spec §10/§5.8): ``POST /teams`` and ``POST /teams/{slug}/members``
run provisioning in the background under ONE in-process lock (jobs
serialize — two concurrent team creates could otherwise interleave through
the profile machinery), and the client polls ``GET /jobs/{id}``.

State is process-local by design: a gateway restart drops running jobs.
That is safe, not data loss — the registry write is LAST in the create
sequence (§5.5), so a dead job either never stamped the team (re-run the
POST) or stamped it with the interrupted-create adoption path (re-run the
same POST; it adopts created instances and creates only the rest). A
``hermes team sync`` reads the residual drift either way.

Result contract (g4): a finished job's ``result`` carries the §9.4
gateway-restart notice as a FIELD named ``restart_notice`` (``null`` when
no served config changed) — never prose the UI would have to scrape.
"""

from __future__ import annotations

import threading
import uuid

_STORE_LOCK = threading.Lock()   # guards _JOBS only; never held during work
RUN_LOCK = threading.Lock()      # THE single in-process provisioning lock (g4)

_JOBS: dict = {}


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def snapshot(job_id: str):
    """A deep-ish copy of one job record (callers may not see live dicts)."""
    with _STORE_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in job.items()}


def set_progress(job_id: str, progress: str) -> None:
    with _STORE_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["progress"] = progress
            job["updated_at"] = _now()


def _finish(job_id: str, *, state: str, result=None, error=None) -> None:
    with _STORE_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["state"] = state
        job["result"] = result
        job["error"] = error
        job["progress"] = state
        job["updated_at"] = _now()
        job["finished_at"] = _now()


def start(kind: str, work, *, title: str = "") -> dict:
    """Create a job and run ``work(job_id)`` in a daemon thread.

    ``work`` returns the result dict (which must carry ``restart_notice``
    as a field when the operation touched served config — g4) or raises;
    a raise lands as ``state: "error"`` with a §7.3-shaped ``error``
    envelope in the job record. Returns the initial job snapshot
    immediately so the route can answer ``202 {job_id}``.
    """
    job_id = uuid.uuid4().hex
    with _STORE_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "kind": kind, "title": title,
            "state": "pending", "progress": "queued",
            "result": None, "error": None,
            "created_at": _now(), "updated_at": _now(), "finished_at": None,
        }

    def _run():
        # g4: ONE lock for all background provisioning.
        with RUN_LOCK:
            with _STORE_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    return
                job["state"] = "running"
                job["progress"] = "running"
                job["updated_at"] = _now()
            try:
                result = work(job_id)
                _finish(job_id, state="done", result=result)
            except Exception as exc:                       # noqa: BLE001
                _finish(job_id, state="error", error=_error_envelope(exc))

    thread = threading.Thread(target=_run, name=f"hermes-agent-synergia-job-{job_id[:8]}",
                              daemon=True)
    thread.start()
    return snapshot(job_id)


def _error_envelope(exc) -> dict:
    """A job failure reads like the §7.3 envelope the route would have
    returned synchronously."""
    code = getattr(exc, "code", None)
    if code is not None:                                   # TeamError-shaped
        return {"ok": False, "error": {
            "code": code,
            "message": str(exc),
            "hint": getattr(exc, "hint", "") or "The job failed; nothing "
                  "further was written after the failure point.",
            "remedy": getattr(exc, "remedy", "") or "hermes team sync <team>",
        }}
    return {"ok": False, "error": {
        "code": "E_TEAM_PROVISION_FAILED",
        "message": f"job failed: {type(exc).__name__}: {exc}",
        "hint": "Already-created instances are KEPT; re-running the POST "
                "adopts them (resumable create, §5.5).",
        "remedy": "hermes team doctor",
    }}
