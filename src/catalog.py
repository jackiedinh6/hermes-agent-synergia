"""catalog.py — the list of profiles a team can be built from.

Two halves, merged local-first:

* the plugin's own store (``catalog_store``, ``<registry_dir>/catalog/``) —
  entries authored or imported through ``team profiles add|import`` or the
  dashboard, plus the ``hidden`` set and merged recipes;
* optional external pack roots, read-only: ``catalog_paths`` (default
  ``[]``) → ``packs.json`` → each pack's manifest →
  ``profiles/<name>/distribution.yaml``.

Layering (§5.2, S21 lint): **imports nothing from ``hermes_cli``** —
``hermes_constants`` only, for root resolution. Hot-path rule (§5.4): never
called from ``pre_tool_call``; ``hooks.py`` must not import this module.

Every failure degrades, never raises (``get`` aside, which raises
``E_TEAM_NO_BLUEPRINT`` per Appendix C): a missing root, an unparseable
``packs.json`` or catalog index, or a row naming an absent profile directory
each drops the entry or the source and records a ``catalog.source_missing``
info.

Both halves are memoised on ``(mtime_ns, size)`` of the catalog index,
``packs.json`` and each manifest — the same pattern as ``storage.py``.
``installed_as`` and ``hidden`` are annotation steps recomputed per call
because installed profiles change without touching the packs.

Profiles living under the hermes root are **not** a third half. The catalog
never derives an entry from ``<root>/profiles/`` — a root profile becomes a
blueprint only when the user adopts it into the store
(``catalog_store.adopt_profile``). ``root_profiles()`` is the read side of
that picker and is never called by ``load()``.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import yaml

import hermes_constants
from . import catalog_store, model
from .errors import E_TEAM_NO_BLUEPRINT, TeamError

DEFAULT_CATALOG_PATHS: list = []
_BUILTIN_ROLE = "member"

# Role hints — built-in layer. Name globs are checked before categories
# (plan S6a.4 order: role_hints setting → name glob → category → member).
_BUILTIN_NAME_GLOBS = (
    ("*-orchestrator", "orchestrator"),
    ("*-technical-lead", "orchestrator"),
    ("*-reviewer", "reviewer"),
    ("*-researcher", "researcher"),
    ("*-analyst", "researcher"),
    ("*-writer", "writer"),
)
_BUILTIN_CATEGORIES = {
    "qa": "verifier",
    "engineering": "implementer",
}


@dataclass(frozen=True)
class CatalogSource:
    path: str
    resolved: bool
    packs: int = 0
    error: str = ""
    local: bool = False

    def to_dict(self) -> dict:
        d = {"path": self.path, "resolved": self.resolved, "packs": self.packs,
             "local": self.local}
        if self.error:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class Entry:
    id: str
    pack: str
    display_name: str
    category: str
    description: str
    version: str
    author: str
    license: str
    source_path: str
    installed_as: tuple = ()
    suggested_role: str = _BUILTIN_ROLE
    local: bool = False
    hidden: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "pack": self.pack, "display_name": self.display_name,
            "category": self.category, "description": self.description,
            "version": self.version, "author": self.author, "license": self.license,
            "source_path": self.source_path,
            "installed_as": list(self.installed_as),
            "suggested_role": self.suggested_role,
            "local": self.local, "hidden": self.hidden,
        }


@dataclass(frozen=True)
class Recipe:
    id: str
    display_name: str
    description: str
    pack: str = ""
    when_to_use: str = ""
    keywords: tuple = ()
    tiers: dict = field(default_factory=dict)   # {minimal|recommended|expanded: [entry_id]}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "display_name": self.display_name,
            "description": self.description, "pack": self.pack,
            "when_to_use": self.when_to_use, "keywords": list(self.keywords),
            "tiers": {k: list(v) for k, v in self.tiers.items()},
        }


@dataclass
class Catalog:
    sources: list = field(default_factory=list)      # CatalogSource
    entries: dict = field(default_factory=dict)      # id -> Entry (store + packs)
    recipes: list = field(default_factory=list)      # Recipe
    infos: list = field(default_factory=list)        # (code, message) degradation records


# ── settings / roots ──────────────────────────────────────────────────────────

def _settings() -> dict:
    try:
        cfg = hermes_constants.get_default_hermes_root() / "config.yaml"
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        return ((((raw.get("plugins") or {}).get("entries") or {})
                 .get("hermes-agent-synergia") or {}).get("settings") or {})
    except Exception:
        return {}


def _roots() -> list:
    paths = _settings().get("catalog_paths")
    if paths is None:
        paths = DEFAULT_CATALOG_PATHS
    return [str(p) for p in paths] if isinstance(paths, list) else [str(paths)]


def _role_hints() -> dict:
    hints = _settings().get("role_hints")
    return hints if isinstance(hints, dict) else {}


def root_profiles_dir() -> Path:
    return hermes_constants.get_default_hermes_root() / "profiles"


def _live_profile_homes() -> list:
    """Live named profile homes under the hermes root.

    Drops dot-dirs such as ``.deleted`` and anything carrying a delete
    tombstone — the marker the substrate writes *before* ``rmtree`` so a
    racing gateway write cannot relist a deleted name.
    """
    try:
        homes = sorted(p for p in root_profiles_dir().iterdir() if p.is_dir())
    except OSError:
        return []
    return [h for h in homes
            if model.PROFILE_NAME_RE.match(h.name)
            and h.name not in model.RESERVED_NAMES
            and not hermes_constants.named_profile_is_deleted(h)]


def profile_meta(home: Path) -> dict:
    """Presentation metadata for one profile home, ``profile.yaml`` first.

    ``profile.yaml`` is what ``write_profile_meta`` maintains (display_name,
    description); ``distribution.yaml`` carries the rest for a profile that
    was installed from a pack. Absent or unreadable files degrade to ``{}``.
    """
    meta = _read_yaml(home / "profile.yaml")
    dist = _read_yaml(home / "distribution.yaml")
    return {
        "display_name": meta.get("display_name") or dist.get("display_name") or "",
        "description": meta.get("description") or dist.get("description") or "",
        "category": dist.get("category") or "",
        "version": dist.get("version") or "",
        "author": dist.get("author") or "",
        "license": dist.get("license") or "",
    }


# ── pack reading ──────────────────────────────────────────────────────────────

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def suggested_role(id_or_entry, category: str = "") -> str:
    """Role suggestion. Accepts an Entry or ``(id, category)``."""
    if isinstance(id_or_entry, Entry):
        entry_id, cat = id_or_entry.id, id_or_entry.category
    else:
        entry_id, cat = id_or_entry, category
    hints = _role_hints()
    # Structured form: role_hints: {names: {glob: role}, categories: {cat: role}}
    names = hints.get("names")
    if isinstance(names, dict):
        for pattern, role in names.items():
            if fnmatch.fnmatch(entry_id, str(pattern)):
                return str(role)
    categories = hints.get("categories")
    if isinstance(categories, dict) and cat in categories:
        return str(categories[cat])
    # Flat form: role_hints: {qa: verifier, "*-reviewer": reviewer}
    for pattern, role in hints.items():
        if pattern in ("names", "categories"):
            continue
        if fnmatch.fnmatch(entry_id, str(pattern)):
            return str(role)
        if entry_id == pattern or cat == pattern:
            return str(role)
    for pattern, role in _BUILTIN_NAME_GLOBS:
        if fnmatch.fnmatch(entry_id, pattern):
            return role
    if cat in _BUILTIN_CATEGORIES:
        return _BUILTIN_CATEGORIES[cat]
    return _BUILTIN_ROLE


def _build(root_strs: list):
    """Build the pack-derived half of the catalog. Never raises."""
    sources, entries, recipes, infos = [], {}, [], []
    for root_str in root_strs:
        root = Path(root_str)
        packs_file = root / "packs.json"
        if not packs_file.is_file():
            sources.append(CatalogSource(path=root_str, resolved=False,
                                         error="packs.json missing"))
            infos.append(("catalog.source_missing", f"{packs_file} missing"))
            continue
        packs = _read_json(packs_file)
        pack_list = (packs or {}).get("packs")
        if not isinstance(pack_list, list) or not pack_list:
            sources.append(CatalogSource(path=root_str, resolved=False,
                                         error="packs.json unreadable or empty"))
            infos.append(("catalog.source_missing", f"{packs_file} unreadable"))
            continue
        count, manifest_memo = 0, {}
        for p in pack_list:
            if not isinstance(p, dict) or not p.get("name") or not p.get("manifest"):
                continue
            manifest_path = root / str(p["manifest"])
            manifest_memo[str(manifest_path)] = manifest_path
            manifest = _read_json(manifest_path)
            if manifest is None:
                infos.append(("catalog.source_missing",
                              f"manifest {manifest_path} unreadable"))
                continue
            pack_name = str(p["name"])
            pack_path = str(p.get("path") or pack_name)
            profile_root = str((manifest.get("distribution") or {})
                               .get("profile_root") or "profiles")
            for m_entry in manifest.get("profiles") or []:
                if not isinstance(m_entry, dict) or not m_entry.get("name"):
                    continue
                name = str(m_entry["name"])
                src_dir = root / pack_path / profile_root / name
                if not src_dir.is_dir():
                    # manifest names an absent profile dir → drop the entry
                    infos.append(("catalog.source_missing",
                                  f"manifest entry {name}: {src_dir} absent"))
                    continue
                dist = _read_yaml(src_dir / "distribution.yaml")
                entry = Entry(
                    id=name,
                    pack=pack_name,
                    display_name=str(m_entry.get("display_name") or name),
                    category=str(m_entry.get("category") or ""),
                    description=str(dist.get("description") or ""),
                    version=str(dist.get("version") or ""),
                    author=str(dist.get("author") or ""),
                    license=str(dist.get("license") or ""),
                    source_path=str(src_dir),
                    suggested_role=suggested_role(name,
                                                  str(m_entry.get("category") or "")),
                )
                entries.setdefault(name, entry)
                count += 1
        sources.append(CatalogSource(path=root_str, resolved=True, packs=count))
        if not recipes:
            recipes = _read_recipes(root / "recipes.json")
    return sources, entries, recipes, infos


def _build_local():
    """The plugin's own store (``catalog_store``). Never raises."""
    path = str(catalog_store.local_root())
    try:
        index = catalog_store.load_index()
    except TeamError as exc:
        return (CatalogSource(path=path, resolved=False, local=True,
                              error=exc.message),
                {}, [], [("catalog.source_missing", exc.message)], frozenset())
    entries, infos = {}, []
    pdir = catalog_store.profiles_dir()
    for eid, row in sorted(index["entries"].items()):
        src_dir = pdir / eid
        if not src_dir.is_dir():
            infos.append(("catalog.source_missing",
                          f"local entry {eid}: {src_dir} absent"))
            continue
        dist = _read_yaml(src_dir / "distribution.yaml")
        category = str(row.get("category") or "")
        entries[eid] = Entry(
            id=eid,
            pack=str(row.get("pack") or catalog_store.LOCAL_PACK),
            display_name=str(row.get("display_name") or eid),
            category=category,
            description=str(dist.get("description") or ""),
            version=str(dist.get("version") or ""),
            author=str(dist.get("author") or ""),
            license=str(dist.get("license") or ""),
            source_path=str(src_dir),
            suggested_role=suggested_role(eid, category),
            local=True,
        )
    source = CatalogSource(path=path, resolved=True, packs=len(entries), local=True)
    return (source, entries, _parse_recipes(index["recipes"]), infos,
            frozenset(index["hidden"]))


def _parse_recipes(rows) -> list:
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        tiers = r.get("tiers") if isinstance(r.get("tiers"), dict) else {}
        out.append(Recipe(
            id=str(r["id"]),
            display_name=str(r.get("display_name") or r["id"]),
            description=str(r.get("description") or ""),
            pack=str(r.get("pack") or ""),
            when_to_use=str(r.get("when_to_use") or ""),
            keywords=tuple(str(k) for k in (r.get("keywords") or [])),
            tiers={str(k): tuple(str(x) for x in v)
                   for k, v in tiers.items() if isinstance(v, list)},
        ))
    return out


def _read_recipes(path: Path) -> list:
    return _parse_recipes((_read_json(path) or {}).get("recipes"))


# ── installed_as + hidden ────────────────────────────────────────────────────

def _installed_index() -> dict:
    """``source:`` string → installed profile names, from every profile's
    ``distribution.yaml`` (the provenance the substrate stamps on install)."""
    index: dict = {}
    for home in _live_profile_homes():
        dist = _read_yaml(home / "distribution.yaml")
        src = str(dist.get("source") or "").strip().rstrip("/")
        if src:
            index.setdefault(src, []).append(home.name)
    return index


def _annotate(entries: dict, installed: dict, hidden: frozenset) -> dict:
    """Return entries with ``installed_as`` and ``hidden`` filled from live
    state."""
    out = {}
    for eid, entry in entries.items():
        names = installed.get(entry.source_path.rstrip("/"), ())
        changes = {}
        if names:
            changes["installed_as"] = tuple(sorted(names))
        if eid in hidden:
            changes["hidden"] = True
        out[eid] = replace(entry, **changes) if changes else entry
    return out


# ── public API ────────────────────────────────────────────────────────────────

_MEMO = None       # (file key) -> (local half, pack half)
_BUILD_COUNT = 0   # lets tests assert the memo actually hits


def _stat_key(p: Path) -> tuple:
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), 0, -1)


def _file_key(roots: list) -> tuple:
    key = [_stat_key(hermes_constants.get_default_hermes_root() / "config.yaml"),
           _stat_key(catalog_store.index_path())]
    for r in roots:
        for name in ("packs.json", "recipes.json"):
            key.append(_stat_key(Path(r) / name))
        pk = Path(r) / "packs.json"
        for m in (_read_json(pk) or {}).get("packs") or []:
            if isinstance(m, dict) and m.get("manifest"):
                key.append(_stat_key(Path(r) / str(m["manifest"])))
    return tuple(key)


def load(*, use_cache: bool = True) -> Catalog:
    """Load the catalog. Both halves are memoised on the index/pack files'
    mtimes; ``installed_as`` and ``hidden`` are recomputed live."""
    global _MEMO, _BUILD_COUNT
    roots = _roots()
    key = _file_key(roots)
    if use_cache and _MEMO is not None and _MEMO[0] == key:
        local_half, pack_half = _MEMO[1]
    else:
        local_half, pack_half = _build_local(), _build(roots)
        _BUILD_COUNT += 1
        _MEMO = (key, (local_half, pack_half))
    local_src, local_entries, local_recipes, local_infos, hidden = local_half
    sources, pack_entries, pack_recipes, infos = pack_half
    merged = dict(local_entries)
    for eid, entry in pack_entries.items():
        merged.setdefault(eid, entry)
    seen = {r.id for r in local_recipes}
    recipes = list(local_recipes) + [r for r in pack_recipes if r.id not in seen]
    installed = _installed_index()
    entries = _annotate(merged, installed, hidden)
    return Catalog(sources=[local_src] + list(sources), entries=entries,
                   recipes=recipes, infos=list(local_infos) + list(infos))


def sources() -> list:
    """The local store, then every configured root and whether it resolved
    (degradation probe)."""
    return [s.to_dict() for s in load().sources]


def entries(*, pack: Optional[str] = None, category: Optional[str] = None,
            role: Optional[str] = None, q: Optional[str] = None,
            include_hidden: bool = False, local_only: bool = False,
            use_cache: bool = True) -> list:
    """Filtered catalog entries as plain dicts (JSON-envelope ready). Hidden
    entries are left out unless ``include_hidden`` — the pickers' view."""
    cat = load(use_cache=use_cache)
    pool = list(cat.entries.values())
    out = []
    needle = q.lower() if q else ""
    for e in pool:
        if e.hidden and not include_hidden:
            continue
        if local_only and not e.local:
            continue
        if pack and e.pack != pack:
            continue
        if category and e.category != category:
            continue
        if role and e.suggested_role != role:
            continue
        if needle and needle not in (e.id + " " + e.display_name + " "
                                     + e.description).lower():
            continue
        out.append(e.to_dict())
    return out


def get(entry_id: str) -> Entry:
    """One entry by id, hidden or not; ``E_TEAM_NO_BLUEPRINT`` with near
    matches in the hint."""
    cat = load()
    entry = cat.entries.get(entry_id)
    if entry is None:
        near = difflib.get_close_matches(entry_id, list(cat.entries), n=3, cutoff=0.4)
        raise TeamError(
            E_TEAM_NO_BLUEPRINT,
            f"No catalog entry {entry_id!r}.",
            hint=(f"Near matches: {', '.join(near)}" if near
                  else "Run `hermes team profiles list` to browse. Profiles under "
                       "the hermes root are NOT catalog entries — list them with "
                       "`hermes team profiles list --root`."),
            remedy="Seed the catalog with `hermes team profiles import "
                   "<git-url|dir>` or `hermes team profiles adopt <profile>`, "
                   "or check `hermes team profiles sources`.",
        )
    return entry


def recipes() -> list:
    """Roster presets: the store's merged recipes, then external roots'."""
    return [r.to_dict() for r in load().recipes]


def root_profiles() -> list:
    """Live profiles under the hermes root, as plain dicts.

    These are **not** catalog entries: they never reach ``entries()`` or
    ``get()``. This is the read side of the adopt picker — the user chooses
    one and ``catalog_store.adopt_profile`` copies it into the store.
    ``adopted_as`` names the store entry already adopted from a profile, so
    the picker can show what has been taken.
    """
    try:
        index = catalog_store.load_index()
    except TeamError:
        index = {"entries": {}}
    adopted = {}
    for eid, row in (index.get("entries") or {}).items():
        origin = str((row or {}).get("origin") or "")
        if origin.startswith("profile:"):
            adopted.setdefault(origin[len("profile:"):], eid)
    out = []
    for home in _live_profile_homes():
        name = home.name
        meta = profile_meta(home)
        category = str(meta.get("category") or "")
        out.append({
            "id": name,
            "display_name": str(meta.get("display_name") or name),
            "category": category,
            "description": str(meta.get("description") or ""),
            "suggested_role": suggested_role(name, category),
            "has_soul": (home / "SOUL.md").is_file(),
            "adopted_as": adopted.get(name, ""),
            "path": str(home),
        })
    return out


def _reset_caches_for_tests() -> None:
    global _MEMO, _BUILD_COUNT
    _MEMO = None
    _BUILD_COUNT = 0
    catalog_store._reset_caches_for_tests()
