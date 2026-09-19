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
