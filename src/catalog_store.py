"""catalog_store.py — the plugin's own writable catalog.

The catalog used to be only a read-only view over pack roots
(``catalog_paths``). This module adds the store the plugin owns,
``<registry_dir>/catalog/``:

* ``profiles/<id>/`` — one distribution per entry (``distribution.yaml``,
  ``SOUL.md``, ``skills/…``), the payload shape a pack ships, so
  ``provision.instantiate`` installs from it unchanged;
* ``catalog.yaml`` — the index: ``entries`` (pack, display_name, category,
  origin, added_at per id), ``hidden`` (ids kept out of the pickers,
  external-root ids included) and ``recipes`` merged in by pack imports.

``catalog.yaml`` is written only through ``storage.save_yaml_file`` (flock +
CAS + ``.bak`` + atomic), mutating the base it re-reads under the lock.
Directories are written before the index row that names them and removed
after it, so a crash leaves at worst an orphan directory (``orphans()``),
never a row pointing at nothing.

Layering: no ``hermes_cli`` import (the S21 importer set is frozen) — git
staging copies the substrate's rule instead of calling it (§5.2.1).
``catalog`` imports this module at load time, so ``catalog`` is imported
lazily here.
"""

from __future__ import annotations

import difflib
import itertools
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import model, storage
from .errors import (
    TeamError,
    TeamRegistryError,
    E_TEAM_CATALOG_UNAVAILABLE,
    E_TEAM_EXISTS,
    E_TEAM_INVALID,
    E_TEAM_NO_BLUEPRINT,
)

INDEX_FILENAME = "catalog.yaml"
LOCAL_PACK = "local"
GIT_CLONE_TIMEOUT_S = 300
SOUL_PREVIEW_MAX = 64 * 1024

# Copied from hermes_cli.profile_distribution.USER_OWNED_EXCLUDE (§5.2.1);
# tests/test_catalog_store.py asserts parity with the pinned substrate.
USER_OWNED_EXCLUDE = frozenset({
    "auth.json", ".env",
    "state.db", "state.db-shm", "state.db-wal",
    "hermes_state.db", "response_store.db",
    "response_store.db-shm", "response_store.db-wal",
    "gateway.pid", "gateway_state.json", "processes.json",
    "auth.lock", "active_profile", ".update_check",
    "errors.log", ".hermes_history",
    "memories", "sessions", "logs", "plans", "workspace", "home",
    "image_cache", "audio_cache", "document_cache",
    "browser_screenshots", "checkpoints", "sandboxes",
    "backups", "cache",
    "hermes-agent", ".worktrees", "profiles", "bin", "node_modules",
    "local",
})

_SKIP_NAMES = (".git", "__pycache__")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Copied from profile_distribution._looks_like_git_url (§5.2.1).
_GITHUB_SHORTHAND_RE = re.compile(r"^github\.com/[\w.-]+/[\w.-]+/?$")
_SOURCE_HINT = ("Import sources: a git URL (github.com/user/repo, https://…, "
                "git@…) or a directory holding distribution.yaml (one entry) "
                "or packs.json (a pack repo).")

_PARSE_MEMO: dict = {}


def _reset_caches_for_tests() -> None:
    _PARSE_MEMO.clear()


# ── paths (anchor: beside teams.yaml) ────────────────────────────────────────

def local_root() -> Path:
    return storage.registry_dir() / "catalog"


def index_path() -> Path:
    return local_root() / INDEX_FILENAME


def profiles_dir() -> Path:
    return local_root() / "profiles"


# ── index ────────────────────────────────────────────────────────────────────

def _normalize(raw: dict) -> dict:
    entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    hidden = raw.get("hidden") if isinstance(raw.get("hidden"), list) else []
    recipes = raw.get("recipes") if isinstance(raw.get("recipes"), list) else []
    return {
        "revision": int(raw.get("revision") or 0),
        "entries": {str(k): dict(v) if isinstance(v, dict) else {}
                    for k, v in entries.items()},
        "hidden": sorted({str(h) for h in hidden}),
        "recipes": [dict(r) for r in recipes if isinstance(r, dict) and r.get("id")],
    }


def load_index(*, use_cache: bool = True) -> dict:
    """``{revision, entries, hidden, recipes}``; absent file → empty store.
    The returned mapping is shared with the memo — treat it as read-only."""
    path = index_path()
    if not path.is_file():
        return _normalize({})
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if use_cache and _PARSE_MEMO.get("key") == key:
            return _PARSE_MEMO["index"]
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("not a mapping")
        index = _normalize(raw)
    except Exception as exc:
        raise TeamRegistryError(
            f"cannot parse {path}: {exc}",
            hint="The catalog index is corrupt or hand-broken.",
            remedy=f"restore {path} from {path}.bak",
        ) from exc
    _PARSE_MEMO["key"] = key
    _PARSE_MEMO["index"] = index
    return index


def _write_index(mutate: Callable[[dict], None], *,
                 expected_revision: Optional[int] = None) -> dict:
    """Apply ``mutate`` to the index re-read under the lock, then write."""
    path = index_path()

    def _read_current():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        raw = raw if isinstance(raw, dict) else {}
        return int(raw.get("revision") or 0), raw

    def _build(base):
        index = _normalize(base or {})
        mutate(index)
        return {"schema_version": 1, "entries": index["entries"],
                "hidden": sorted(set(index["hidden"])), "recipes": index["recipes"]}

    storage.save_yaml_file(
        path,
        read_current=_read_current,
        build_payload=_build,
        expected_revision=expected_revision,
        label="catalog",
        conflict_hint="E_TEAM_CONFLICT is retryable — re-read the catalog "
                      "revision, reapply your change, retry.",
        conflict_remedy="hermes team profiles list --json   # then retry with "
                        "the fresh revision",
    )
    _PARSE_MEMO.clear()
    return load_index(use_cache=False)


# ── validation ───────────────────────────────────────────────────────────────

def _few(ids: list) -> str:
    head = ", ".join(ids[:10])
    return head + (f" (+{len(ids) - 10} more)" if len(ids) > 10 else "")


def validate_id(entry_id) -> str:
    """Catalog ids become profile names on instantiation, so they obey the
    profile-name rule."""
    eid = str(entry_id or "").strip().lower()
    if not model.PROFILE_NAME_RE.match(eid) or eid in model.RESERVED_NAMES:
        raise TeamError(
            E_TEAM_INVALID,
            f"catalog id {entry_id!r} is not a valid profile name",
            hint=f"Ids must match {model.PROFILE_NAME_RE.pattern} and not be "
                 f"one of {sorted(model.RESERVED_NAMES)}.",
            remedy="hermes team profiles add my-reviewer --soul-file SOUL.md",
        )
    return eid


def _external_ids() -> set:
    from . import catalog
    return {e.id for e in catalog.load(use_cache=False).entries.values()
            if not e.local}


def _exists_error(ids: list) -> TeamError:
    return TeamError(
        E_TEAM_EXISTS,
        f"already in the local catalog: {_few(ids)}",
        hint="Pass --replace (dashboard: \"replace\": true) to overwrite the "
             "stored copy.",
        remedy="hermes team profiles import SOURCE --replace",
    )


def _refuse_clashes(ids: list, *, replace: bool) -> None:
    ext = _external_ids()
    shadow = sorted(i for i in ids if i in ext)
    if shadow:
        raise TeamError(
            E_TEAM_EXISTS,
            f"provided by an external pack root: {_few(shadow)}",
            hint="A local entry may not shadow an external one — one id, one "
                 "source.",
            remedy=f"hermes team profiles show {shadow[0]}",
        )
    if replace:
        return
    entries = load_index(use_cache=False)["entries"]
    taken = sorted(i for i in ids if i in entries or (profiles_dir() / i).exists())
    if taken:
        raise _exists_error(taken)


def _reject_symlinks(src: Path, *, payload_only: bool = False) -> None:
    """Refuse symlinks anywhere under *src*.

    ``payload_only`` narrows the walk to what ``_copy_payload`` would take —
    a live profile home carries ``sessions``/``cache``/``workspace`` trees the
    store never copies, and those are both huge and legitimately full of
    links.
    """
    if payload_only:
        roots = _payload_children(src)
        walk = itertools.chain(roots, *(r.rglob("*") for r in roots
                                        if r.is_dir() and not r.is_symlink()))
    else:
        walk = src.rglob("*")
    for p in walk:
        if p.is_symlink():
            raise TeamError(
                E_TEAM_INVALID,
                f"{src.name}: payload contains a symlink ({p.relative_to(src)})",
                hint="Profile payloads cannot contain symlinks — the substrate "
                     "refuses them at install, so the catalog refuses them at "
                     "import.",
                remedy="replace the symlink with a real file, then re-run",
            )


# ── payload files ────────────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _payload_children(src: Path) -> list:
    return sorted(c for c in src.iterdir()
                  if c.name not in USER_OWNED_EXCLUDE and c.name not in _SKIP_NAMES)


def _payload_files(src: Path) -> list:
    out = []
    for child in _payload_children(src):
        if child.is_dir():
            out.extend(str(p.relative_to(src)) for p in sorted(child.rglob("*"))
                       if p.is_file() and "__pycache__" not in p.parts)
        else:
            out.append(child.name)
    return out


def _copy_payload(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns(*_SKIP_NAMES)
    for child in _payload_children(src):
        if child.is_dir():
            shutil.copytree(child, dest / child.name, ignore=ignore)
        else:
            shutil.copy2(child, dest / child.name)


def _warnings(entry_id: str, src: Path) -> list:
    if (src / "SOUL.md").is_file():
        return []
    return [f"{entry_id}: no SOUL.md — instances run with the default persona"]


def _row(pack: str, display_name: str, category: str, origin: str) -> dict:
    row = {"pack": pack or LOCAL_PACK, "display_name": display_name,
           "category": category, "origin": origin}
    return {k: v for k, v in row.items() if v}


# ── commit: directories first, one index write last ─────────────────────────

def _place(entry_id: str, fill: Callable[[Path], None], *, replace: bool) -> tuple:
    """Build ``profiles/<id>`` in a staging dir, then swap it in. Returns
    ``(target, trash)``; ``trash`` keeps a replaced copy until the index write
    commits, so a failed commit can put it back."""
    root = local_root()
    profiles_dir().mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{uuid.uuid4().hex[:12]}"
    target = profiles_dir() / entry_id
    trash = None
    try:
        fill(staging)
        if target.exists():
            if not replace:
                raise _exists_error([entry_id])
            trash = root / f".trash-{uuid.uuid4().hex[:12]}"
            os.replace(target, trash)
        os.replace(staging, target)
    except BaseException:
        if trash is not None and not target.exists():
            os.replace(trash, target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target, trash


def _commit(items: list, *, replace: bool, expected_revision: Optional[int],
            recipes: tuple = (), progress: Optional[Callable[[str], None]] = None) -> dict:
    """``items`` is ``[(id, fill, row)]``; a callable ``row`` is given
    ``(existing row or None, stamp)`` under the index lock. Any failure
    rolls the placed directories back, restoring replaced copies."""
    placed = []
    try:
        for n, (eid, fill, _row) in enumerate(items, 1):
            if progress:
                progress(f"writing {eid} ({n}/{len(items)})")
            placed.append(_place(eid, fill, replace=replace))
        stamp = model.now_iso()

        def mutate(index):
            for eid, _fill, row in items:
                if eid in index["entries"] and not replace:
                    raise _exists_error([eid])
                index["entries"][eid] = (
                    row(index["entries"].get(eid), stamp) if callable(row)
                    else {**row, "added_at": stamp})
            if recipes:
                by_id = {r["id"]: r for r in index["recipes"]}
                by_id.update({r["id"]: r for r in recipes})
                index["recipes"] = [by_id[k] for k in sorted(by_id)]

        index = _write_index(mutate, expected_revision=expected_revision)
    except BaseException:
        for target, trash in reversed(placed):
            shutil.rmtree(target, ignore_errors=True)
            if trash is not None:
                os.replace(trash, target)
        raise
    for _target, trash in placed:
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)
    return index


# ── author ───────────────────────────────────────────────────────────────────

def _validate_skill(skill) -> dict:
    if not isinstance(skill, dict):
        raise TeamError(
            E_TEAM_INVALID, f"skill {skill!r} must be an object",
            hint='{"name": "wcag-review", "description": "…", "body": "…"}',
            remedy="hermes team profiles add --help")
    name = str(skill.get("name") or "").strip().lower()
    if not _SKILL_NAME_RE.match(name):
        raise TeamError(
            E_TEAM_INVALID, f"skill name {skill.get('name')!r} is invalid",
            hint=f"Skill names must match {_SKILL_NAME_RE.pattern}.",
            remedy="hermes team profiles add --help")
    return {"name": name,
            "description": str(skill.get("description") or "").strip(),
            "body": str(skill.get("body") or "")}


def _skill_md(name: str, description: str, body: str,
              extra: Optional[dict] = None) -> str:
    front = yaml.safe_dump({**(extra or {}), "name": name,
                            "description": description or name},
                           sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def _skill_sources(skills, skill_dirs) -> tuple:
    """``(inline skill rows, skill directories)``, validated, names unique
    across both."""
    skill_rows = [_validate_skill(s) for s in skills or ()]
    dirs = [Path(d).expanduser() for d in skill_dirs or ()]
    for d in dirs:
        if not (d / "SKILL.md").is_file():
            raise TeamError(
                E_TEAM_INVALID, f"skill dir {d} has no SKILL.md",
                hint="A skill directory must hold SKILL.md at its root.",
                remedy="hermes team profiles add ID --skill-dir path/to/skill")
        _reject_symlinks(d)
    names = [s["name"] for s in skill_rows] + [d.name for d in dirs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise TeamError(
            E_TEAM_INVALID, f"duplicate skill names: {', '.join(dupes)}",
            hint="Each skill lands in skills/<name>/ — names must be unique.",
            remedy="rename or drop the duplicate skill")
    return skill_rows, dirs


def author_entry(entry_id, *, display_name: str = "", category: str = "",
                 description: str = "", soul: str = "", skills=(), skill_dirs=(),
                 version: str = "", author: str = "", license: str = "",
                 replace: bool = False,
                 expected_revision: Optional[int] = None) -> dict:
    """Write a new entry from form fields (the dashboard's New profile
    dialog and ``team profiles add``)."""
    eid = validate_id(entry_id)
    soul = str(soul or "")
    skill_rows, dirs = _skill_sources(skills, skill_dirs)
    _refuse_clashes([eid], replace=replace)

    def fill(dest: Path) -> None:
        dest.mkdir(parents=True)
        dist = {"name": eid, "version": version or "0.1.0"}
        dist.update((k, v) for k, v in (("description", description),
                                        ("author", author),
                                        ("license", license)) if v)
        (dest / "distribution.yaml").write_text(
            yaml.safe_dump(dist, sort_keys=False, allow_unicode=True), encoding="utf-8")
        if soul.strip():
            (dest / "SOUL.md").write_text(soul.rstrip() + "\n", encoding="utf-8")
        for s in skill_rows:
            sdir = dest / "skills" / s["name"]
            sdir.mkdir(parents=True)
            (sdir / "SKILL.md").write_text(_skill_md(**s), encoding="utf-8")
        for d in dirs:
            shutil.copytree(d, dest / "skills" / d.name,
                            ignore=shutil.ignore_patterns(*_SKIP_NAMES))

    index = _commit([(eid, fill, _row(LOCAL_PACK, display_name or eid, category,
                                      "authored"))],
                    replace=replace, expected_revision=expected_revision)
    target = profiles_dir() / eid
    return {"id": eid, "path": str(target), "revision": index["revision"],
            "warnings": _warnings(eid, target)}


# ── import ───────────────────────────────────────────────────────────────────

def _looks_like_git_url(s: str) -> bool:
    return (s.endswith(".git")
            or s.startswith(("git@", "ssh://", "git://", "http://", "https://"))
            or bool(_GITHUB_SHORTHAND_RE.match(s)))


def _unavailable(message: str, remedy: str = "hermes team profiles import --help") -> TeamError:
    return TeamError(E_TEAM_CATALOG_UNAVAILABLE, message, hint=_SOURCE_HINT,
                     remedy=remedy)


def _git_clone(url: str, dest: Path) -> None:
    if _GITHUB_SHORTHAND_RE.match(url):
        url = f"https://{url.rstrip('/')}"
    # Never prompt (dashboard jobs have no TTY); no ext::/file:: transports.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_ALLOW_PROTOCOL="https:http:ssh:git")
    remedy = f"git ls-remote {url}   # check the URL and access"
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL,
                       env=env, timeout=GIT_CLONE_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise _unavailable("git is not installed", remedy) from exc
    except subprocess.TimeoutExpired as exc:
        raise _unavailable(f"git clone {url} timed out after "
                           f"{GIT_CLONE_TIMEOUT_S}s", remedy) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise _unavailable(f"git clone {url} failed: {stderr[-500:]}", remedy) from exc
    shutil.rmtree(dest / ".git", ignore_errors=True)


def _stage(source: str, workdir: Path) -> tuple:
    """``(root, provenance)``: a git URL is shallow-cloned into ``workdir``;
    a local directory is read in place."""
    src = str(source or "").strip()
    if not src:
        raise _unavailable("import source is empty")
    if _looks_like_git_url(src):
        dest = workdir / "clone"
        _git_clone(src, dest)
        return dest, src
    path = Path(src).expanduser()
    if path.is_dir():
        return path.resolve(), str(path.resolve())
    raise _unavailable(f"cannot resolve import source {source!r}")


def _only_set(only) -> set:
    if not only:
        return set()
    items = only.split(",") if isinstance(only, str) else list(only)
    return {str(i).strip().lower() for i in items if str(i).strip()}


def _discover(root: Path, provenance: str, *, only, name) -> tuple:
    """``(kind, candidates, recipes)`` for a staged source."""
    wanted = _only_set(only)
    if (root / "distribution.yaml").is_file():
        if wanted:
            raise TeamError(
                E_TEAM_INVALID,
                "--only applies to pack repos; this source is one distribution",
                hint="Use --name to import it under another id.",
                remedy=f"hermes team profiles import {provenance} --name <id>")
        dist = _read_yaml(root / "distribution.yaml")
        eid = validate_id(name or dist.get("name") or root.name)
        return "profile", [{
            "id": eid, "src": root, "pack": LOCAL_PACK,
            "display_name": str(dist.get("display_name") or eid),
            "category": str(dist.get("category") or ""),
            "rename": eid != str(dist.get("name") or ""),
        }], []
    if (root / "packs.json").is_file():
        if name:
            raise TeamError(
                E_TEAM_INVALID,
                "--name applies to a single distribution; a pack keeps its ids",
                hint="Use --only to pick entries from the pack.",
                remedy=f"hermes team profiles import {provenance} --only ID[,ID…]")
        from . import catalog
        _sources, entries, recipes, infos = catalog._build([str(root)])
        if not entries:
            detail = "; ".join(msg for _code, msg in infos[:3]) or "no entries"
            raise _unavailable(f"no loadable profiles in {provenance}: {detail}")
        if wanted:
            missing = sorted(wanted - set(entries))
            if missing:
                near = difflib.get_close_matches(missing[0], list(entries), n=3,
                                                 cutoff=0.4)
                raise TeamError(
                    E_TEAM_NO_BLUEPRINT,
                    f"not in this pack repo: {_few(missing)}",
                    hint=(f"Near matches: {', '.join(near)}" if near
                          else "The dry run lists every id the source holds."),
                    remedy=f"hermes team profiles import {provenance} --dry-run")
            entries = {k: v for k, v in entries.items() if k in wanted}
        cands = [{"id": validate_id(e.id), "src": Path(e.source_path),
                  "pack": e.pack, "display_name": e.display_name,
                  "category": e.category, "rename": False}
                 for e in entries.values()]
        return ("pack", sorted(cands, key=lambda c: c["id"]),
                [r.to_dict() for r in recipes])
    raise _unavailable(f"no distribution.yaml or packs.json at the root of "
                       f"{provenance}")


def _mergeable_recipes(recipes: list, ids_after: set) -> list:
    """Recipes whose every tier member will exist in the local store."""
    out = []
    for r in recipes:
        members = [x for tier in (r.get("tiers") or {}).values() for x in tier]
        if members and all(x in ids_after for x in members):
            out.append(r)
    return out


def plan_import(source: str, *, only=None, name: Optional[str] = None) -> dict:
    """Dry run: stage into a throw-away dir and report what an import would
    write. Nothing under the store is touched."""
    with tempfile.TemporaryDirectory(prefix="hermes-agent-synergia-import-") as tmp:
        root, provenance = _stage(source, Path(tmp))
        kind, cands, recipes = _discover(root, provenance, only=only, name=name)
        ext = _external_ids()
        local = load_index(use_cache=False)["entries"]
        rows = []
        for c in cands:
            _reject_symlinks(c["src"])
            files = _payload_files(c["src"])
            rows.append({
                "id": c["id"], "pack": c["pack"],
                "display_name": c["display_name"], "category": c["category"],
                "exists": c["id"] in local, "shadows_external": c["id"] in ext,
                "file_count": len(files), "files": files[:20],
                "warnings": _warnings(c["id"], c["src"]),
            })
        merged = _mergeable_recipes(recipes, set(local) | {c["id"] for c in cands})
    return {"source": provenance, "kind": kind, "entries": rows,
            "count": len(rows), "recipes": [r["id"] for r in merged]}


def _copy_fill(cand: dict) -> Callable[[Path], None]:
    def fill(dest: Path) -> None:
        _copy_payload(cand["src"], dest)
        if cand["rename"]:
            path = dest / "distribution.yaml"
            dist = _read_yaml(path)
            dist["name"] = cand["id"]
            path.write_text(yaml.safe_dump(dist, sort_keys=False, allow_unicode=True),
                            encoding="utf-8")
    return fill


def import_entries(source: str, *, only=None, name: Optional[str] = None,
                   replace: bool = False, expected_revision: Optional[int] = None,
                   progress: Optional[Callable[[str], None]] = None) -> dict:
    """Copy one distribution, or a pack repo's profiles, into the store."""
    report = progress or (lambda _msg: None)
    with tempfile.TemporaryDirectory(prefix="hermes-agent-synergia-import-") as tmp:
        report("staging source")
        root, provenance = _stage(source, Path(tmp))
        kind, cands, recipes = _discover(root, provenance, only=only, name=name)
        for c in cands:
            _reject_symlinks(c["src"])
        ids = [c["id"] for c in cands]
        _refuse_clashes(ids, replace=replace)
        warnings = [w for c in cands for w in _warnings(c["id"], c["src"])]
        items = [(c["id"], _copy_fill(c),
                  _row(c["pack"], c["display_name"], c["category"], provenance))
                 for c in cands]
        local = set(load_index(use_cache=False)["entries"])
        merged = _mergeable_recipes(recipes, local | set(ids))
        index = _commit(items, replace=replace, expected_revision=expected_revision,
                        recipes=tuple(merged), progress=report)
    return {"source": provenance, "kind": kind, "imported": ids, "count": len(ids),
            "recipes": [r["id"] for r in merged], "revision": index["revision"],
            "warnings": warnings}


# ── adopt: a live profile under the hermes root → a store entry ─────────────

def _adopt_fill(home: Path, eid: str, meta: dict) -> Callable[[Path], None]:
    """Copy the profile payload, then make it a valid distribution.

    ``plan_install`` (the substrate call ``provision`` makes) refuses a source
    without a ``distribution.yaml`` carrying a ``name``, and a hand-created
    profile has no manifest at all — so synthesise one. ``source``/
    ``installed_at`` are dropped: they describe where an *instance* came from
    and the substrate re-stamps them at install.
    """
    def fill(dest: Path) -> None:
        _copy_payload(home, dest)
        path = dest / "distribution.yaml"
        dist = _read_yaml(path)
        dist["name"] = eid
        for key in ("source", "installed_at"):
            dist.pop(key, None)
        if not dist.get("version"):
            dist["version"] = "0.1.0"
        if not dist.get("description") and meta.get("description"):
            dist["description"] = meta["description"]
        path.write_text(yaml.safe_dump(dist, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    return fill


def adopt_profile(profile_name, *, name=None, replace: bool = False,
                  expected_revision: Optional[int] = None) -> dict:
    """Copy a live profile under the hermes root into the plugin's store.

    The catalog never reads the profiles root on its own (there is no tier C);
    this is the one explicit path that turns a profile into a blueprint. Only
    ``_copy_payload`` runs, so ``USER_OWNED_EXCLUDE`` keeps ``.env``,
    ``auth.json``, ``state.db*``, ``sessions``, ``logs``, ``memories`` and the
    caches out of the store.
    """
    from . import catalog
    src_name = model._normalize_profile_name(str(profile_name or "").strip())
    live = {h.name: h for h in catalog._live_profile_homes()}
    home = live.get(src_name)
    if home is None:
        raise TeamError(
            E_TEAM_NO_BLUEPRINT,
            f"no live profile {src_name!r} under {catalog.root_profiles_dir()}",
            hint=("Live profiles: " + (_few(sorted(live)) if live else "(none)")
                  + ". A deleted profile stays unusable while its tombstone "
                    "is present."),
            remedy="hermes team profiles list --root",
        )
    eid = validate_id(name or src_name)
    _reject_symlinks(home, payload_only=True)
    _refuse_clashes([eid], replace=replace)
    meta = catalog.profile_meta(home)
    warnings = _warnings(eid, home)
    row = _row(LOCAL_PACK, meta.get("display_name") or eid,
               meta.get("category") or "", f"profile:{src_name}")
    index = _commit([(eid, _adopt_fill(home, eid, meta), row)],
                    replace=replace, expected_revision=expected_revision)
    return {"profile": src_name, "adopted": eid, "source": str(home),
            "revision": index["revision"], "warnings": warnings}


# ── edit ─────────────────────────────────────────────────────────────────────

def _require_local(eid: str, index: dict, verb: str) -> None:
    if eid in index["entries"]:
        return
    if eid in _external_ids():
        raise TeamError(
            E_TEAM_INVALID,
            f"{eid!r} comes from an external pack root, which the plugin "
            "never writes",
            hint="Hide it from the pickers instead; it stays resolvable "
                 "for existing teams.",
            remedy=f"hermes team profiles hide {eid}")
    near = difflib.get_close_matches(eid, list(index["entries"]), n=3, cutoff=0.4)
    raise TeamError(
        E_TEAM_NO_BLUEPRINT, f"no local catalog entry {eid!r}",
        hint=(f"Near matches: {', '.join(near)}" if near
              else f"Only entries in the plugin's own store can be {verb}."),
        remedy="hermes team profiles list --local")


def update_entry(entry_id, *, display_name: Optional[str] = None,
                 category: Optional[str] = None,
                 description: Optional[str] = None, soul: Optional[str] = None,
                 version: Optional[str] = None, author: Optional[str] = None,
                 license: Optional[str] = None, skills=(), skill_dirs=(),
                 remove_skills=(),
                 expected_revision: Optional[int] = None) -> dict:
    """Edit a local entry in place (the dashboard's Edit profile dialog and
    ``team profiles edit``).

    ``None`` leaves a field alone and ``""`` clears it. The payload is copied
    and changed in staging, so files the edit does not name survive, and the
    index row keeps ``pack``/``origin``/``added_at``. Profiles already
    instantiated from the entry are untouched (Q8).
    """
    eid = str(entry_id or "").strip().lower()
    _require_local(eid, load_index(use_cache=False), "edited")
    current = profiles_dir() / eid
    if not current.is_dir():
        raise TeamError(
            E_TEAM_NO_BLUEPRINT, f"local entry {eid!r} has no payload at {current}",
            hint="The index row points at nothing — store drift after a crash.",
            remedy=f"hermes team profiles remove {eid} --yes")
    skill_rows, dirs = _skill_sources(skills, skill_dirs)
    sdir = current / "skills"
    present = sorted(p.name for p in sdir.iterdir() if p.is_dir()) if sdir.is_dir() else []
    # Removal is matched against the directory listing, never joined as a
    # raw path, so a name cannot reach outside skills/.
    drop = sorted({str(n or "").strip() for n in remove_skills or ()} - {""})
    unknown = [n for n in drop if n not in present]
    if unknown:
        raise TeamError(
            E_TEAM_INVALID, f"{eid} has no skill(s) {', '.join(unknown)}",
            hint="Skills: " + (_few(present) if present else "(none)"),
            remedy=f"hermes team profiles show {eid}")
    both = sorted(({s["name"] for s in skill_rows} | {d.name for d in dirs}) & set(drop))
    if both:
        raise TeamError(
            E_TEAM_INVALID, f"skill(s) both written and removed: {', '.join(both)}",
            hint="One edit either writes or removes a skill, not both.",
            remedy="drop it from the skills or from the removals")
    manifest = {k: v for k, v in (("description", description), ("version", version),
                                  ("author", author), ("license", license))
                if v is not None}
    row_fields = {k: v for k, v in (("display_name", display_name),
                                    ("category", category)) if v is not None}
    changed = sorted([*manifest, *row_fields]
                     + (["soul"] if soul is not None else [])
                     + (["skills"] if skill_rows or dirs or drop else []))
    if not changed:
        raise TeamError(
            E_TEAM_INVALID, f"nothing to change on {eid!r}",
            hint="Pass at least one field, skill or skill removal.",
            remedy="hermes team profiles edit --help")

    def fill(dest: Path) -> None:
        ignore = shutil.ignore_patterns(*_SKIP_NAMES)
        shutil.copytree(current, dest, ignore=ignore)
        if manifest:
            path = dest / "distribution.yaml"
            dist = _read_yaml(path)
            dist.setdefault("name", eid)
            for key, value in manifest.items():
                if key == "version" and not value:
                    value = "0.1.0"
                if value:
                    dist[key] = value
                else:
                    dist.pop(key, None)
            path.write_text(yaml.safe_dump(dist, sort_keys=False, allow_unicode=True),
                            encoding="utf-8")
        if soul is not None:
            if soul.strip():
                (dest / "SOUL.md").write_text(soul.rstrip() + "\n", encoding="utf-8")
            else:
                (dest / "SOUL.md").unlink(missing_ok=True)
        sroot = dest / "skills"
        for name in drop:
            shutil.rmtree(sroot / name)
        for d in dirs:
            shutil.rmtree(sroot / d.name, ignore_errors=True)
            shutil.copytree(d, sroot / d.name, ignore=ignore)
        for s in skill_rows:
            path = sroot / s["name"] / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            front = _frontmatter(path)
            path.write_text(_skill_md(str(front.get("name") or s["name"]),
                                      s["description"], s["body"], extra=front),
                            encoding="utf-8")
        if sroot.is_dir() and not any(sroot.iterdir()):
            sroot.rmdir()

    def row(existing, stamp):
        if existing is None:
            raise TeamError(
                E_TEAM_NO_BLUEPRINT, f"no local catalog entry {eid!r}",
                hint="It was removed while this edit ran.",
                remedy="hermes team profiles list --local")
        out = dict(existing)
        for key, value in row_fields.items():
            if value:
                out[key] = value
            else:
                out.pop(key, None)
        out["updated_at"] = stamp
        return out

    index = _commit([(eid, fill, row)], replace=True,
                    expected_revision=expected_revision)
    return {"id": eid, "path": str(current), "revision": index["revision"],
            "changed": changed, "warnings": _warnings(eid, current)}


# ── remove / hide ────────────────────────────────────────────────────────────

def references(entry_id: str) -> dict:
    """Team members whose ``blueprint`` is this id, and the stored recipes a
    removal would drop."""
    reg = storage.load()
    return {
        "teams": sorted(f"{slug}/{m.profile}" for slug, t in reg.teams.items()
                        for m in t.members
                        if getattr(m, "blueprint", None) == entry_id),
        "recipes": sorted(r["id"] for r in load_index()["recipes"]
                          if any(entry_id in tier
                                 for tier in (r.get("tiers") or {}).values())),
    }


def remove_entry(entry_id, *, expected_revision: Optional[int] = None) -> dict:
    """Delete a local entry. Team members that name it keep working
    (``blueprint`` is provenance)."""
    eid = str(entry_id or "").strip().lower()
    _require_local(eid, load_index(use_cache=False), "removed")
    refs = references(eid)
    dropped: list = []

    def mutate(idx):
        idx["entries"].pop(eid, None)
        idx["hidden"] = [h for h in idx["hidden"] if h != eid]
        keep = []
        for r in idx["recipes"]:
            if any(eid in tier for tier in (r.get("tiers") or {}).values()):
                dropped.append(r["id"])
            else:
                keep.append(r)
        idx["recipes"] = keep

    index = _write_index(mutate, expected_revision=expected_revision)
    target = profiles_dir() / eid
    if target.exists():
        trash = local_root() / f".trash-{uuid.uuid4().hex[:12]}"
        os.replace(target, trash)
        shutil.rmtree(trash, ignore_errors=True)
    return {"removed": eid, "path": str(target), "revision": index["revision"],
            "team_refs": refs["teams"], "dropped_recipes": sorted(set(dropped))}


def set_hidden(entry_id, hidden: bool, *,
               expected_revision: Optional[int] = None) -> dict:
    """Keep an entry out of (or put it back into) the pickers. ``catalog.get``
    still resolves hidden ids, so team members that use one keep resolving."""
    from . import catalog
    entry = catalog.get(str(entry_id or "").strip().lower())

    def mutate(idx):
        ids = set(idx["hidden"])
        if hidden:
            ids.add(entry.id)
        else:
            ids.discard(entry.id)
        idx["hidden"] = sorted(ids)

    index = _write_index(mutate, expected_revision=expected_revision)
    return {"id": entry.id, "hidden": bool(hidden), "revision": index["revision"]}


# ── read helpers ─────────────────────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple:
    """``(frontmatter, body)``; no or unparseable frontmatter → ``({}, text)``."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, text
    rest = text[end + 4:]
    nl = rest.find("\n")
    return (data if isinstance(data, dict) else {}), (rest[nl + 1:] if nl >= 0 else "")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _frontmatter(path: Path) -> dict:
    return _split_frontmatter(_read_text(path))[0]


def describe(source_path: str) -> dict:
    """SOUL.md and every SKILL.md body of one entry's payload, capped. A
    ``*_truncated`` preview must never be saved back — it would cut the file."""
    home = Path(source_path)
    soul = _read_text(home / "SOUL.md")
    skills = []
    sdir = home / "skills"
    if sdir.is_dir():
        for d in sorted(p for p in sdir.iterdir() if p.is_dir()):
            front, body = _split_frontmatter(_read_text(d / "SKILL.md"))
            body = body.lstrip("\n")
            skills.append({"name": str(front.get("name") or d.name), "dir": d.name,
                           "description": str(front.get("description") or ""),
                           "body": body[:SOUL_PREVIEW_MAX],
                           "body_truncated": len(body) > SOUL_PREVIEW_MAX,
                           "files": sum(1 for p in d.rglob("*") if p.is_file())})
    return {"soul": soul[:SOUL_PREVIEW_MAX],
            "soul_truncated": len(soul) > SOUL_PREVIEW_MAX, "skills": skills}


def orphans() -> dict:
    """Store drift a crash can leave: directories with no index row, rows
    with no directory."""
    entries = set(load_index()["entries"])
    pdir = profiles_dir()
    on_disk = {p.name for p in pdir.iterdir() if p.is_dir()} if pdir.is_dir() else set()
    return {"dirs_without_row": sorted(on_disk - entries),
            "rows_without_dir": sorted(entries - on_disk)}
