"""Shared fixtures for the hermes-agent-synergia plugin test suite (S0).

Every later step builds on the two fixtures here:

- ``hermes_root`` — a temp Hermes root that the substrate treats as *the*
  Hermes home: ``HERMES_HOME`` points at it, the ``get_default_hermes_root()``
  memo and the ``hermes_home_key()`` cache are cleared around it, and the
  plugin/env state that could leak between tests is reset on teardown.
  Spec §14: the suite must load plugins through real discovery, which reads
  ``get_hermes_home()/plugins`` — hence the env var, not an override.
- ``profile`` — factory creating ``<root>/profiles/<name>/config.yaml``.

Imported at module scope (pytest-asyncio mode aside, the substrate imports
cleanly on the host — plan F4).
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys

import pytest

import hermes_constants
import hermes_cli.plugins

# ── import disambiguation ────────────────────────────────────────────────────
# PluginManager loads a *directory* plugin as a package
# (``hermes_plugins.<slug>`` via spec_from_file_location on __init__.py with
# submodule_search_locations=[plugin_dir], plugins_loader._load_directory_module).
# Intra-plugin imports are therefore relative (``from .errors import …``).
# Mirror that here: load the package under the name ``hermes_agent_synergia``, then
# alias its submodules onto the top-level names the tests use (``import cli``,
# ``from errors import …``). Alias-only — the plugin dir is *not* on
# pythonpath, so there is exactly one module object per name and test modules,
# the package, and the container all agree. The substrate's own top-level
# ``cli`` (chat runtime) and the unbroker scripts' ``storage`` never load in
# this process.
_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "src"

_SUBMODULES = ("errors", "model", "storage", "catalog_store", "catalog",
               "provision", "resolve", "reconcile", "cli")


def _preload_plugin_package() -> None:
    if "hermes_agent_synergia" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "hermes_agent_synergia", _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hermes_agent_synergia"] = pkg
    try:
        spec.loader.exec_module(pkg)
        for name in _SUBMODULES:
            mod = importlib.import_module(f"hermes_agent_synergia.{name}")
            sys.modules.setdefault(name, mod)
    except Exception:
        for name in _SUBMODULES + ("hermes_agent_synergia",):
            sys.modules.pop(name, None)
        raise


_preload_plugin_package()


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    """A temp Hermes root wired into the substrate's own resolution order."""
    root = tmp_path / "hermes"
    (root / "profiles").mkdir(parents=True)
    (root / "plugins").mkdir()
    (root / "kanban").mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(root))

    # The substrate memoises the default root and resolves home keys through
    # a process-wide cache; without these clears, get_default_hermes_root()
    # or hermes_home_key() could keep pointing at a previous test's tmp_path
    # (or the developer's real ~/.hermes).
    hermes_constants._default_hermes_root_memo = None
    hermes_constants.reset_hermes_home_key_cache()

    # Team-plugin env must not leak in from the ambient shell.
    for var in (
        "HERMES_TEAM_HOME",
        "HERMES_TENANT",
        "HERMES_KANBAN_BOARD",
        "HERMES_TEAM",
    ):
        monkeypatch.delenv(var, raising=False)

    yield root

    # Teardown parity with the substrate's own conftest: drop the PluginManager
    # singletons (and the keyed per-home cache) so a manager built against this
    # test's tmp_path can never serve the next test.
    hermes_constants._default_hermes_root_memo = None
    hermes_constants.reset_hermes_home_key_cache()
    hermes_cli.plugins._reset_plugin_managers_for_tests()


@pytest.fixture
def profile(hermes_root):
    """Factory: create ``<root>/profiles/<name>/`` with a ``config.yaml``.

    ``config`` may be a dict (serialised with YAML) or a raw string (written
    verbatim — useful for the fragment/shape fixtures in S8). Defaults to an
    empty mapping. Returns the profile home ``Path``.
    """

    def _make(name: str, config: object = None) -> "hermes_constants.Path":
        import yaml

        home = hermes_root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        cfg = home / "config.yaml"
        if isinstance(config, str):
            cfg.write_text(config, encoding="utf-8")
        elif config is None:
            cfg.write_text("{}\n", encoding="utf-8")
        else:
            cfg.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return home

    return _make


def _git(workdir, *args) -> str:
    import subprocess
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(workdir), *args],
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def result_thread(hermes_root):
    """Factory: a worktree thread as the dispatcher leaves it — each card on
    its own ``wt/<name>`` branch off the workdir HEAD, nothing merged back.

    ``root`` (no commits of its own) has two children: ``design``, a sibling
    that diverges, and ``step1`` → ``step2``, a step chain, so ``wt/step2``
    contains ``wt/step1``. Cards complete in that order; names in ``running``
    stay running. Names in ``worktrees`` also get the linked worktree the
    dispatcher leaves at ``<workdir>/.worktrees/<card id>``; by default the
    branches stand alone (the state a substrate cleanup leaves). ``titles``
    overrides a card's title. Returns ``SimpleNamespace(workdir, start, ids,
    git)`` with ``ids`` mapping name → card id and ``git`` bound to the workdir.
    """
    import functools
    import types
    from hermes_cli import kanban_db, kanban_db_connect

    cards = [("root", None, None, ()),
             ("design", "master", "design.md", ("root",)),
             ("step1", "master", "step1.py", ("root",)),
             ("step2", "wt/step1", "step2.py", ("step1",))]

    def build(workdir, board, tenant, *, running=(), worktrees=(), titles=None):
        workdir = pathlib.Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        git = functools.partial(_git, workdir)
        git("init", "-q")
        git("symbolic-ref", "HEAD", "refs/heads/master")
        (workdir / "README.md").write_text("seed\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "initial snapshot")
        start = git("rev-parse", "HEAD")
        kanban_db.create_board(board, name=board)
        ids: dict = {}
        with kanban_db_connect.connect_closing(
                db_path=kanban_db.kanban_db_path(board)) as conn:
            for n, (name, off, filename, parents) in enumerate(cards, 1):
                branch = f"wt/{name}"
                if filename is None:
                    git("branch", branch, start)
                else:
                    git("checkout", "-q", "-b", branch, off)
                    (workdir / filename).write_text(f"{name}\n", encoding="utf-8")
                    git("add", filename)
                    git("commit", "-q", "-m", f"{name}: add {filename}")
                    git("checkout", "-q", "master")
                tid = kanban_db.create_task(
                    conn, title=(titles or {}).get(name, name), tenant=tenant,
                    workspace_kind="worktree",
                    branch_name=branch, parents=[ids[p] for p in parents],
                    initial_status="running", board=board)
                if name in worktrees:
                    git("worktree", "add", "-q",
                        str(workdir / ".worktrees" / tid), branch)
                # created_at is whole seconds: pin it so `root` roots the thread.
                done = name not in running
                conn.execute(
                    "UPDATE tasks SET status = ?, created_at = ?, started_at = ?, "
                    "completed_at = ? WHERE id = ?",
                    ("done" if done else "running", 1_000 + n, 1_000 + n,
                     2_000 + n if done else None, tid))
                conn.commit()
                ids[name] = tid
        return types.SimpleNamespace(workdir=workdir, start=start, ids=ids, git=git)

    return build
