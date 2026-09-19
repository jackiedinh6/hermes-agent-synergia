#!/usr/bin/env python3
"""S21 — import/contract lint for hermes-agent-synergia (spec §14.5, §5.2).

Two lint gates in one standalone script, plus the schema-hash updater:

1. **Compat scan** — ``hermes_cli.plugin_compat.scan_plugin($SRC)`` with the
   substrate's own ``compat_manifest.json``. Fail on **any** hit: once
   ``COMPAT_REMOVAL_DATE`` passes, ``PluginManager`` skips a hitting plugin
   *entirely* (see ``disable_reason`` in plugin_compat.py) — this is a
   shipping blocker, not a warning. The scan covers the whole plugin dir,
   ``dashboard/`` included (the scanner's own ``_SKIP_DIRS`` applies; there
   is no tests dir inside the plugin).

2. **Layering** — AST over every plugin module (dashboard ``*.py`` included,
   ``dist/`` and ``__pycache__`` excluded), enforcing the §5.2 graph:
   the module set that imports ``hermes_cli`` is *frozen* (spec-sanctioned
   plus the documented divergences below); ``hermes_cli.profile_distribution``
   and profile-deleting symbols stay inside ``provision.py``; ``catalog.py``
   imports nothing from ``hermes_cli``; live ``teams.yaml`` string constants
   live only in ``storage.py``; ``policy.py`` imports only ``__future__`` /
   ``typing`` (same allow-list as the §5.2 guard in test_policy_matrix.py).

   Clean exit prints exactly ``no compat hits, layering OK`` and exits 0.

3. **Schema hashes** — ``--update-schema-hashes`` regenerates
   ``tests/data/schema_hashes.json`` (SHA-256 of
   ``json.dumps(schema, sort_keys=True)`` per schema, all nine of
   ``schemas.ALL``). The freeze test
   (``tests/test_contracts.py::test_schema_freeze_fixture_matches_schemas``)
   never writes the fixture: a human re-runs *this* flag, reviews the diff
   of model-facing text, and commits. The updater is here rather than in the
   test so the test module stays assertion-only and the plugin package stays
   loadable without pytest machinery.

Documented divergences from §5.2 as written (committed, verified code — the
lint freezes the *observed* graph rather than failing red on S11/S14/S19):

* §5.2 says "only ``resolve.py`` and ``provision.py`` import ``hermes_cli``".
  ``cli.py`` / ``tools.py`` (S11/S14) import ``hermes_cli.kanban_db`` and
  ``hermes_cli.kanban_db_connect`` directly for the kanban CLI/tool paths,
  ``approvals.py`` imports ``hermes_cli.kanban_db``,
  ``kanban_db_connect``, ``kanban_db_notify`` and
  ``hermes_cli.profiles.get_active_profile_name`` for the human-decision
  inbox, and ``dashboard/plugin_api.py`` (S19) imports ``hermes_cli.web_server``,
  ``hermes_cli.web_server`` (vendored session check, S21b) and
  ``hermes_cli.kanban_db(_connect)`` as the API
  layer — §5.2's graph has no arrow for the dashboard's server-side half.
  The graph's *intent* (two import surfaces to fix if upstream moves) holds
  for the plugin core; the four divergent files are listed in
  ``EXPECTED_HERMES_CLI_IMPORTERS`` so no *fourth* surface can appear
  silently. If §5.2 is amended, amend the set here in the same commit.
* §14.5 says to hash "the tool-description strings"; we hash the whole
  canonical schema (``json.dumps(schema, sort_keys=True)``). Descriptions
  ride along, and a parameters/``required`` change is just as model-facing.
  The §7.4 verbatim-dict guards in test_tools.py still pin the two spec
  dicts byte-for-byte.
* §14.5 says only ``storage.py`` "writes ``teams.yaml``". Data-flow tracking
  of write calls is brittle, and prose legitimately names the file; the
  rule implemented is stricter than write-context detection and simpler
  than nothing: any *live* string constant containing ``teams.yaml`` may
  only appear in ``storage.py``. Docstrings and bare string statements
  (prose) are exempt. A comment is never an AST node and never flagged.

The profile-deleting symbols are the real names in the pinned substrate,
``hermes_cli/profiles.py``: ``delete_profile`` (line ~1134) and
``remove_wrapper_script`` (line ~344). ``_print_delete_summary`` is private,
never called from the plugin, and deliberately not a checked name. The
``delete_profile`` *keyword* in ``cli.py``/``plugin_api.py`` is a wire/CLI-flag
field name, not a symbol reference — the rule tracks imports, call targets,
and exact string constants only, so those do not trip it.

Run standalone (host, .venv-team or any interpreter that can import the
substrate): ``$PY scripts/lint_imports.py``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HA = REPO.parent / "hermes-agent"          # pinned substrate, sibling checkout
SRC = REPO / "src"
FIXTURE = REPO / "tests" / "data" / "schema_hashes.json"

# Directories never walked by the layering lint. The first five mirror the
# substrate scanner's _SKIP_DIRS; "dist" is dashboard JS build output.
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", "dist"}

# §5.2 + documented divergences (see header). Exact set: a new hermes_cli
# importer fails, and so does one that silently disappears (the surface must
# be *fixed*, not merely bounded).
EXPECTED_HERMES_CLI_IMPORTERS = frozenset({
    "resolve.py",                      # §5.2 sanctioned
    "provision.py",                    # §5.2 sanctioned
    "cli.py",                          # divergence: kanban_db(_connect), S11
    "tools.py",                        # divergence: kanban_db(_connect), S14
    "dashboard/plugin_api.py",         # divergence: API layer, S19
    "approvals.py",                    # divergence: kanban_db(_connect/_notify)
                                       # + profiles.get_active_profile_name
})

# Real substrate names, hermes_cli/profiles.py (see header for why not
# _print_delete_summary, and why kwarg mentions do not count).
PROFILE_DELETING_SYMBOLS = frozenset({"delete_profile", "remove_wrapper_script"})

# §14.5/§5.2: policy.py is pure — same allow-list as test_policy_matrix.py.
_POLICY_ALLOWED_ROOTS = frozenset({"__future__", "typing"})


# ── helpers ──────────────────────────────────────────────────────────────────

def _iter_py(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(fns):
            if f.endswith(".py"):
                yield Path(dp) / f


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def _import_roots(tree: ast.Module):
    """(root, full dotted name, lineno, imported names) for every absolute
    import, at any nesting depth — a lazy ``from hermes_cli import …``
    inside a function is still an import surface (§5.2 counts the surface,
    not the timing)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module.split(".")[0], node.module, node.lineno,
                        frozenset(a.name for a in node.names)))
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name.split(".")[0], a.name, node.lineno,
                            frozenset({a.name})))
    return out


def _prose_node_ids(tree: ast.Module) -> set:
    """ids of string constants that are prose: docstrings and bare string
    statements. Everything else that mentions teams.yaml is live code."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            ids.add(id(node.value))
    return ids


def load_schemas():
    """Load schemas.py standalone (it imports only ``__future__`` — proven
    by test_schemas_module_imports_only_typing), no package, no substrate."""
    spec = importlib.util.spec_from_file_location("_s21_schemas", SRC / "schemas.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def schema_hashes() -> dict:
    """``{tool_name: sha256(canonical-json)}`` over ``schemas.ALL``."""
    return {
        s["name"]: hashlib.sha256(
            json.dumps(s, sort_keys=True).encode("utf-8")).hexdigest()
        for s in load_schemas().ALL
    }


def update_fixture() -> int:
    hashes = schema_hashes()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(REPO)}: {len(hashes)} schema hashes "
          f"— review the model-facing diff before committing")
    return 0


# ── check 1: compat scan ─────────────────────────────────────────────────────

def compat_hits():
    """Hits from the substrate's own scanner against the repo's manifest."""
    if str(HA) not in sys.path:
        sys.path.insert(0, str(HA))
    from hermes_cli import plugin_compat
    return plugin_compat.scan_plugin(SRC)


# ── check 2: layering ────────────────────────────────────────────────────────

def layering_failures() -> list:
    """One string per violation; empty list means the frozen graph holds."""
    fails: list = []
    importers: set = set()
    pdist_importers: set = set()
    deleter_files: set = set()

    for p in _iter_py(SRC):
        rel = str(p.relative_to(SRC)).replace("\\", "/")
        try:
            tree = _parse(p)
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError) as exc:
            fails.append(f"{rel}: unparsable: {exc}")
            continue

        roots = _import_roots(tree)

        # hermes_cli import surface, frozen.
        for root, full, lineno, names in roots:
            if root == "hermes_cli":
                importers.add(rel)
                if full == "hermes_cli.profile_distribution" \
                        or (full == "hermes_cli"
                            and "profile_distribution" in names):
                    pdist_importers.add(rel)

        # profile-deleting symbols: imports, call targets, dotted string
        # targets (patch/import_module style — mirrors scan_plugin's string
        # rule). A bare ``body.get("delete_profile")`` or ``args.delete_profile``
        # is a wire/CLI field of the same name, NOT a symbol reference.
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.ImportFrom) and node.level == 0 \
                    and node.module and node.module.split(".")[0] == "hermes_cli":
                hit = any(a.name in PROFILE_DELETING_SYMBOLS for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in PROFILE_DELETING_SYMBOLS:
                hit = True
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "." in node.value:
                mod, _, name = node.value.rpartition(".")
                hit = name in PROFILE_DELETING_SYMBOLS \
                    and mod.split(".")[0] == "hermes_cli"
            if hit:
                deleter_files.add(rel)
                break

        # catalog.py must never reach hermes_cli (§5.2: degrade, don't break).
        if rel == "catalog.py" and any(r == "hermes_cli" for r, *_ in roots):
            fails.append("catalog.py imports hermes_cli — §5.2: catalog reads "
                         "pack files only and must degrade, never break")

        # Only storage.py carries a live teams.yaml string (prose exempt).
        if rel != "storage.py":
            prose = _prose_node_ids(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and "teams.yaml" in node.value and id(node) not in prose:
                    fails.append(f"{rel}:{node.lineno}: live teams.yaml string — "
                                 f"§5.2/§14.5: only storage.py touches teams.yaml")

        # policy.py purity (§14.5; mirrors test_policy_matrix.py).
        if rel == "policy.py":
            for root, full, lineno, _names in roots:
                if root not in _POLICY_ALLOWED_ROOTS:
                    fails.append(f"policy.py:{lineno}: forbidden import "
                                 f"{full!r} — §5.2: policy is pure")

    extra = importers - EXPECTED_HERMES_CLI_IMPORTERS
    missing = EXPECTED_HERMES_CLI_IMPORTERS - importers
    for f in sorted(extra):
        fails.append(f"{f}: imports hermes_cli but is not in the frozen "
                     f"importer set (§5.2 + documented divergences — amend "
                     f"§5.2 and EXPECTED_HERMES_CLI_IMPORTERS together)")
    for f in sorted(missing):
        fails.append(f"{f}: no longer imports hermes_cli — the §5.2 surface "
                     f"shrank silently; update the frozen set and §5.2 "
                     f"in the same commit")
    if pdist_importers != {"provision.py"}:
        fails.append(f"hermes_cli.profile_distribution imported by "
                     f"{sorted(pdist_importers)} — §5.2: provision.py only")
    if deleter_files != {"provision.py"}:
        fails.append(f"profile-deleting symbol(s) referenced by "
                     f"{sorted(deleter_files)} — §5.2: provision.py is the "
                     f"only module that creates or deletes a profile")
    return fails


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list) -> int:
    ap = argparse.ArgumentParser(
        prog="lint_imports.py",
        description="S21 §14.5 lint: compat scan + §5.2 layering "
                    "(plus --update-schema-hashes for the schema freeze).")
    ap.add_argument("--update-schema-hashes", action="store_true",
                    help="regenerate tests/data/schema_hashes.json from "
                         "schemas.ALL and exit 0")
    args = ap.parse_args(argv)
    if args.update_schema_hashes:
        return update_fixture()

    hits = compat_hits()
    for h in hits:
        print(f"FAIL compat {h.file}:{h.line}: {h.old} -> {h.new}")
    if hits:
        # plugin_compat.disable_reason: after the date the plugin is
        # not loaded at all — no warning, no hook, no tools.
        print(f"FAIL compat: {len(hits)} hit(s) — after COMPAT_REMOVAL_DATE "
              f"({compat_removal_date()}) PluginManager SKIPS this plugin "
              f"entirely: shipping blocker, not a warning")

    fails = layering_failures()
    for f in fails:
        print(f"FAIL layering: {f}")

    if not hits and not fails:
        print("no compat hits, layering OK")
        return 0
    n = len(hits) + len(fails)
    print(f"lint_imports: {n} problem(s)")
    return 1


def compat_removal_date() -> str:
    if str(HA) not in sys.path:
        sys.path.insert(0, str(HA))
    from hermes_cli import plugin_compat
    return plugin_compat.COMPAT_REMOVAL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
