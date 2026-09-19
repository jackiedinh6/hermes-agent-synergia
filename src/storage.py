"""storage.py — the ONLY writer of ``teams.yaml`` (spec §5.4, §4.4).

``save_yaml_file`` (S11a) is the flock + re-read-under-lock + ``.bak`` +
atomic-write body ``save`` (registry) goes through — one write discipline
for any YAML store under ``registry_dir()``.

The registry is host-global, so this module resolves the
root through ``hermes_constants.get_default_hermes_root()`` and **never**
through ``plugin_data_dir()`` — the plugin-data dir follows ``HERMES_HOME``,
which flips to a profile home under ``hermes -p <profile>`` and would split
the registry per profile. ``registry_dir()`` below is the only resolution
point; do not work around it.

Layering (spec §5.2, plan S21): imports ``yaml``/stdlib plus the two
substrate leaf modules (``hermes_constants``, ``utils``). No ``hermes_cli``
import — that whole weight belongs to ``resolve.py``.
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import yaml

import hermes_constants
import utils

from .errors import (
    TeamError,
    TeamRegistryError,
    E_TEAM_CONFLICT,
    E_TEAM_LOCKED,
    E_TEAM_NOT_FOUND,
)
from .model import Registry, Team

log = logging.getLogger("hermes_plugins.hermes_agent_synergia.storage")

REGISTRY_FILENAME = "teams.yaml"
_LOCK_RETRIES = 5
_LOCK_RETRY_MS = 200

# Empty today: schema_version 1 is the first shipped format. The machinery
# lands with S5 so the first real migration (if one ever ships) is a one-liner
# and the .bak-before-migrate discipline is already tested (§4.4).
MIGRATIONS: dict = {}

# ── module caches ────────────────────────────────────────────────────────────
_PARSE_MEMO: dict = {}        # (path, mtime_ns, size) -> Registry
_LAST_GOOD: Optional[Registry] = None
_LOGGED_FIRST_FAILURE = False
_PROFILE_INDEX: tuple = (None, None)   # (memo_key, index)


def _reset_caches_for_tests() -> None:
    global _LAST_GOOD, _LOGGED_FIRST_FAILURE, _PROFILE_INDEX
    _PARSE_MEMO.clear()
    _LAST_GOOD = None
    _LOGGED_FIRST_FAILURE = False
    _PROFILE_INDEX = (None, None)


# ── resolution ───────────────────────────────────────────────────────────────

def registry_dir() -> Path:
    """Resolution order, exactly: env → config setting → host-global default."""
    env = os.environ.get("HERMES_TEAM_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(env))

    setting = _setting_registry_dir()
    if setting:
        return Path(os.path.expanduser(setting))

    return hermes_constants.get_default_hermes_root() / "plugin-data" / "hermes-agent-synergia"


def registry_path() -> Path:
    return registry_dir() / REGISTRY_FILENAME


def _setting_registry_dir() -> str:
    """``plugins.entries.hermes-agent-synergia.settings.registry_dir`` from the ROOT config.

    Read raw (uncached, exact) from ``<root>/config.yaml`` — never from the
    profile-scoped config; the registry is host-global. Cheap and
    fail-open: an unreadable root config just means "use the default".
    """
    try:
        root = hermes_constants.get_default_hermes_root()
        cfg_file = root / "config.yaml"
        if not cfg_file.is_file():
            return ""
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        entry = ((data.get("plugins") or {}).get("entries") or {}).get("hermes-agent-synergia") or {}
        settings = entry.get("settings") or {}
        value = settings.get("registry_dir")
        return str(value).strip() if value else ""
    except Exception:
        return ""


# ── read paths ───────────────────────────────────────────────────────────────

def load(*, use_cache: bool = True) -> Registry:
    """Parse the registry. Raises ``TeamRegistryError`` on a bad file.

    Absent file → empty registry (a fresh host has none). Parse failure →
    raise; this is the *correctness* path (``team list``/``show``/CLI writes).
    """
    global _LAST_GOOD
    path = registry_path()
    if not path.is_file():
        reg = Registry()
        _LAST_GOOD = reg
        return reg
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if use_cache and _PARSE_MEMO.get("key") == key:
            return _PARSE_MEMO["registry"]
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise TeamRegistryError(
                f"{path} is not a mapping",
                hint="teams.yaml must be a YAML mapping with schema_version/teams.",
                remedy=f"restore {path} from {path}.bak or delete it to start clean",
            )
        reg = _migrate(Registry.from_dict(raw), path)
    except TeamError:
        raise
    except Exception as exc:
        raise TeamRegistryError(
            f"cannot parse {path}: {exc}",
            hint="The registry file is corrupt or hand-broken.",
            remedy=f"restore {path} from {path}.bak, or run: hermes team doctor",
        ) from exc
    _PARSE_MEMO["key"] = key
    _PARSE_MEMO["registry"] = reg
    _LAST_GOOD = reg
    return reg


def load_or_empty() -> Registry:
    """Hot path (spec §5.4): **fail open.** Never raises.

    On failure return the last good parse; on a first-ever failure return an
    empty registry and log once — a YAML typo must not block every tool call
    on the host.
    """
    global _LAST_GOOD, _LOGGED_FIRST_FAILURE
    try:
        return load()
    except Exception as exc:
        if _LAST_GOOD is not None:
            return _LAST_GOOD
        if not _LOGGED_FIRST_FAILURE:
            log.warning("hermes-agent-synergia: registry unreadable (%s); running with no teams", exc)
            _LOGGED_FIRST_FAILURE = True
        return Registry()


# ── write path: flock + CAS + .bak + atomic ──────────────────────────────────

def _acquire_lock(lock_path: Path):
    """Non-blocking flock with bounded retry; returns the open lock file or raises."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")  # noqa: SIM115 — returned; caller closes
    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRIES):
        try:
            _flock_ex_nb(fh)
            return fh
        except BlockingIOError as exc:  # pragma: no cover — platform-dependent
            last_exc = exc
        except OSError as exc:
            last_exc = exc
            code = getattr(exc, "errno", None)
            import errno as _errno
            if code not in (_errno.EAGAIN, _errno.EACCES, getattr(_errno, "EWOULDBLOCK", None)):
                fh.close()
                raise TeamRegistryError(
                    f"cannot lock {lock_path}: {exc}",
                    hint="The registry lock file is unusable.",
                    remedy="check permissions on the registry directory",
                ) from exc
        time.sleep(_LOCK_RETRY_MS / 1000)
    fh.close()
    raise TeamError(
        E_TEAM_LOCKED,
        f"registry lock held by another writer (retried {_LOCK_RETRIES}x "
        f"{_LOCK_RETRY_MS}ms)",
        hint="E_TEAM_LOCKED is retryable — another hermes team write is in flight.",
        remedy="wait a moment and re-run the command",
    ) from last_exc


def _flock_ex_nb(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except ImportError:                          # pragma: no cover — POSIX host
        import msvcrt                            # shape per hermes_cli/auth.py:542
        import msvcrt as _ms
        _ms.locking(fh.fileno(), _ms.LK_NBLK, 0x7FFFFFFF)


def _flock_un(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:                          # pragma: no cover
        pass


def save_yaml_file(path, *, read_current, build_payload,
                   expected_revision: Optional[int] = None,
                   label: str = "file",
                   conflict_hint: str = "E_TEAM_CONFLICT is retryable — fetch "
                                       "the current revision, reapply your "
                                       "change, retry.",
                   conflict_remedy: str = "hermes team list --json   # then "
                                         "retry with the fresh revision") -> None:
    """The shared write path: flock → re-read-under-lock → CAS → .bak →
    atomic write. One body for every YAML store — S11a: *two hand-rolled
    writers is how the registry loses data*.

    Callers supply the read/build seams instead of a concrete record type:

    * ``read_current() -> (revision, base_payload)`` — MUST re-read fresh
      (uncached) because it runs under the lock; ``base_payload`` is the raw
      current mapping (``{}`` when absent) and may be ``None`` if the caller
      re-reads in ``build_payload`` itself;
    * ``build_payload(base_payload) -> dict`` — the full new mapping WITHOUT
      the envelope fields; the helper stamps ``revision = current + 1`` and
      ``schema_version`` (default 1), exactly as ``save`` always has.

    The ``.bak`` lands beside ``path`` (same ``<name>.bak`` discipline as the
    registry), and CAS failure raises ``E_TEAM_CONFLICT`` naming both
    revisions — retryable, never lost-writes.
    """
    lock_path = path.with_name(path.name + ".lock")
    fh = _acquire_lock(lock_path)
    try:
        current_revision, base = read_current()
        if expected_revision is not None and current_revision != expected_revision:
            raise TeamError(
                E_TEAM_CONFLICT,
                f"{label} changed under us: expected revision {expected_revision}, "
                f"found {current_revision}",
                hint=conflict_hint,
                remedy=conflict_remedy,
            )
        payload = build_payload(base)
        payload["revision"] = current_revision + 1
        payload["schema_version"] = payload.get("schema_version", 1)
        if path.is_file():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        path.parent.mkdir(parents=True, exist_ok=True)
        utils.atomic_yaml_write(path, payload)
    finally:
        _flock_un(fh)
        fh.close()


def save(reg: Registry, *, expected_revision: Optional[int] = None) -> Registry:
    """The only writer of ``teams.yaml``. Delegates the write discipline to
    :func:`save_yaml_file` (S11a); this
    function keeps the registry-specific read/memo/reload semantics verbatim.

    ``expected_revision`` (from a prior ``load()``) enables compare-and-swap:
    the registry re-read under the lock must carry that revision, or the write
    is refused with ``E_TEAM_CONFLICT`` naming both revisions.
    """
    path = registry_path()

    def _read_current():
        current = load(use_cache=False)
        return current.revision, current.to_dict()

    save_yaml_file(
        path,
        read_current=_read_current,
        build_payload=lambda _base: reg.to_dict(),
        expected_revision=expected_revision,
        label="registry",
        conflict_hint="E_TEAM_CONFLICT is retryable — re-run hermes team show to fetch "
                      "the current revision, reapply your change, retry.",
        conflict_remedy="hermes team list --json   # then retry with the fresh revision",
    )
    # Invalidate so the next load sees the new file (and refresh last-good).
    _PARSE_MEMO.clear()
    result = load(use_cache=False)
    # Return with the revision the caller should pass as expected_revision next.
    return result


def upsert(team, *, expected_revision: Optional[int] = None):
    """Insert-or-replace one team record through the single CAS writer.

    An existing record merges (old wins on nothing — the new record replaces it
    wholesale, but unknown keys from the old record survive via to_dict/from_dict
    reparse). The per-team ``revision`` bumps on every upsert (§4.1).
    """
    reg = load()
    teams = dict(reg.teams)
    old = teams.get(team.slug)
    if old is not None:
        merged = Team.from_dict({**old.to_dict(), **team.to_dict()})
        d = merged.to_dict()
        d["revision"] = int(old.revision or 0) + 1
        stored = Team.from_dict(d)
    else:
        stored = team
    teams[team.slug] = stored
    new_reg = Registry(
        schema_version=reg.schema_version,
        revision=reg.revision,
        teams=teams,
        _extra=dict(reg._extra),
    )
    return save(new_reg, expected_revision=expected_revision).teams[team.slug]


def delete(slug: str, *, expected_revision: Optional[int] = None) -> Registry:
    reg = load()
    if slug not in reg.teams:
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"no team {slug!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    teams = {k: v for k, v in reg.teams.items() if k != slug}
    return save(Registry(schema_version=reg.schema_version, teams=teams,
                         _extra=dict(reg._extra)),
                expected_revision=expected_revision)


def index_by_profile() -> dict:
    """profile -> [team slugs declaring it]. Cache-shared with the parse memo."""
    global _PROFILE_INDEX
    reg = load_or_empty()
    key = _PARSE_MEMO.get("key")
    if _PROFILE_INDEX[0] == key:
        return _PROFILE_INDEX[1]
    index: dict = {}
    for slug, t in reg.teams.items():
        for m in t.members:
            index.setdefault(m.profile, []).append(slug)
    _PROFILE_INDEX = (key, index)
    return index


# ── migrations (§4.4) ────────────────────────────────────────────────────────

def _migrate(reg: Registry, path: Path) -> Registry:
    """Run MIGRATIONS[from][to] in sequence, writing back once after a .bak."""
    version = reg.schema_version
    if version not in MIGRATIONS:
        return reg
    before = copy.deepcopy(reg.to_dict())
    while version in MIGRATIONS:
        steps = MIGRATIONS[version]
        nxt = min(steps)
        reg = steps[nxt](reg)
        reg = Registry.from_dict({**reg.to_dict(), "schema_version": nxt})
        version = nxt
    if reg.to_dict() != before:
        if path.is_file():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        utils.atomic_yaml_write(path, reg.to_dict())
    return reg
