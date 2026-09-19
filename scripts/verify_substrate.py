#!/usr/bin/env python3
"""S1 — substrate contract probe for hermes-agent-synergia.

One script, one clear failure: every symbol, signature and behaviour this
plugin depends on from the pinned ``thirdparty/hermes-agent`` substrate is
checked here, so a substrate bump produces readable ``FAIL`` lines
("one line per item") instead of twelve obscure ones. Re-run after any
substrate bump (spec §2 closing instruction; plan §0).

Pass/fail is explicit by construction: ``check()`` records a PASS only when
the check function returns a non-empty pass-detail string; every failure
branch **raises**, and the raised message becomes the FAIL detail. A check
therefore cannot report success by returning failure text. Exit 0 with
``substrate contract OK (N checks, M notices)`` only when every asserted
check passed. Two items are operator notices, printed but never asserted
(plan Do.8 / Do.13): the compat-removal date, and pack-root resolution — a
missing pack is a degradation, not a substrate break.

The pytest twin (``tests/test_substrate_contract.py``) imports ``run_checks()``
from this file so a bump breaks CI too.
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]         # the plugin checkout
WORKSPACE = REPO.parents[1]                        # the deployment repo around it
HA = REPO.parent / "hermes-agent"                  # pinned substrate, sibling checkout
if str(HA) not in sys.path:
    sys.path.insert(0, str(HA))

# --- Rules we COPY from the substrate (spec §5.2.1: copy, never import the
# private regexes). If either diverges from the substrate's behaviour, the
# Do.6 / Do.11 checks below fail loudly. ---
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")            # kb._BOARD_SLUG_RE
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")        # profiles._PROFILE_ID_RE
RESERVED_PROFILE_NAMES = frozenset({"hermes", "default", "test", "tmp", "root", "sudo"})

# Our plugin.yaml top-level keys, exactly as spec §11 declares them.
PLUGIN_YAML_KEYS = frozenset({
    "name", "version", "manifest_version", "api_version", "description", "author",
    "kind", "license", "homepage", "tags", "provides_tools", "provides_hooks",
    "python_dependencies", "config_schema",
})

TERMINAL_CWD_SENTINELS = frozenset({"", ".", "./", "auto", "cwd"})


def pack_root() -> Path:
    """Pack root: the container path, else this checkout's bind-mount source.

    ``/opt/profile-hermes`` exists inside the hermes-agent container only
    (compose.yml bind mount, :ro). On the host, ``$WORKSPACE/profile-hermes`` is
    the very same tree, so catalog probes run identically on both. When this
    checkout is a git worktree whose ``profile-hermes`` submodule was never
    initialised, fall back to the main checkout the ``.git`` pointer names —
    same read-only pack content. If no candidate holds a ``packs.json``,
    return the local path so the Do.13 notice prints it as MISSING (a
    degradation, not a substrate break) while the Do.10 pack-dependent
    checks fail loudly.
    """
    candidates = [Path("/opt/profile-hermes"), WORKSPACE / "profile-hermes"]
    dotgit = WORKSPACE / ".git"
    try:
        if dotgit.is_file():  # worktree: ".git" is a "gitdir: <path>" pointer
            text = dotgit.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                # <main>/.git/worktrees/<name> -> <main>
                candidates.append(gitdir.parents[2] / "profile-hermes")
    except (OSError, IndexError):
        pass
    for cand in candidates:
        if (cand / "packs.json").is_file():
            return cand
    return WORKSPACE / "profile-hermes"


# --------------------------------------------------------------------------
# check harness — pass/fail is explicit: failure branches raise
# --------------------------------------------------------------------------

class Result:
    __slots__ = ("label", "ok", "detail", "asserted")

    def __init__(self, label, ok, detail="", asserted=True):
        self.label, self.ok, self.detail, self.asserted = label, bool(ok), detail, asserted

    def line(self) -> str:
        if not self.asserted:
            return f"NOTE {self.label}: {self.detail}"
        return f"ok   {self.label}" if self.ok else f"FAIL {self.label}: {self.detail}"


def check(results: list, label: str, fn) -> None:
    """Run ``fn()``; PASS iff it returns a non-empty pass-detail string.

    Failures are signalled by raising (every failure branch raises) — the
    message becomes the FAIL detail. A falsy or non-string return is a
    harness bug and is also recorded as a FAIL, so a failure message can
    never be printed as ``ok``.
    """
    try:
        detail = fn()
        if not isinstance(detail, str) or not detail:
            raise AssertionError(f"check returned no pass detail: {detail!r}")
        results.append(Result(label, True, detail))
    except Exception as exc:  # noqa: BLE001 — the whole point of the probe
        results.append(Result(label, False, f"{type(exc).__name__}: {exc}"))


def _attrs(obj, names):
    missing = [n for n in names if not hasattr(obj, n)]
    if missing:
        raise AssertionError(f"missing: {', '.join(missing)}")
    return f"{len(names)} symbols present"


def _callable_attr(obj, name):
    val = getattr(obj, name, None)
    if val is None:
        raise AssertionError(f"missing attribute: {name}")
    if not callable(val):
        raise AssertionError(f"{name} is not callable")
    return f"{name} importable + callable"


def _kwargs_ok(fn_obj, kwargs):
    params = inspect.signature(fn_obj).parameters
    missing = [k for k in kwargs if k not in params]
    if missing:
        raise AssertionError(f"signature lost kwargs: {', '.join(missing)}")
    return f"kwargs {', '.join(kwargs)} accepted"


# --------------------------------------------------------------------------
# temp Hermes root — the whole probe runs against it, never a real ~/.hermes
# --------------------------------------------------------------------------

def _make_scratch_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="hermes-probe-root-"))
    (root / "profiles").mkdir()
    (root / "plugins").mkdir()
    (root / "kanban").mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(root)
    import hermes_constants

    hermes_constants._default_hermes_root_memo = None
    hermes_constants.reset_hermes_home_key_cache()
    return root


def _tree_snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def run_checks() -> list:
    import hermes_constants
    import hermes_cli.plugins as plugins_mod

    results: list[Result] = []
    root = _make_scratch_root()
    try:
        _run_checks_inner(results, root)
    finally:
        plugins_mod._reset_plugin_managers_for_tests()
        hermes_constants._default_hermes_root_memo = None
        hermes_constants.reset_hermes_home_key_cache()
        os.environ.pop("HERMES_HOME", None)
        shutil.rmtree(root, ignore_errors=True)
    return results


def _run_checks_inner(results: list, root: Path) -> None:
    import hermes_constants
    import utils
    from hermes_cli import config as hc_config
    from hermes_cli import config_defaults, kanban_db as kb
    from hermes_cli import main as cli_main
    from hermes_cli import plugin_compat, plugins as plugins_mod
    from hermes_cli import profile_distribution as pdist
    from hermes_cli import profiles as hc_profiles
    from hermes_cli import projects_db, subcommands

    # -- §0.1 symbol table (Do.1): one line per table row, plus the rows the
    #    Do list calls out item-by-item below. ---

    # 1. PluginContext registration + introspection surface (§0.1 row 1).
    def c_context():
        pc = plugins_mod.PluginContext
        methods = ["register_tool", "register_cli_command", "register_hook",
                   "register_system_prompt_section", "has_capability", "has_plugin",
                   "get_config", "set_config", "on_unload"]
        detail = _attrs(pc, methods)
        if not isinstance(inspect.getattr_static(pc, "profile_name"), property):
            raise AssertionError("profile_name is no longer a property")
        return detail + ", profile_name still a property"
    check(results, "PluginContext API (§0.1)", c_context)

    # 2-3. Hooks we register exist in VALID_HOOKS, one check each (Do.2).
    def _hook(hook):
        if hook not in plugins_mod.VALID_HOOKS:
            raise AssertionError(f"{hook!r} missing from VALID_HOOKS")
        return f"{hook!r} in VALID_HOOKS"
    check(results, "pre_tool_call ∈ VALID_HOOKS (Do.2)", lambda: _hook("pre_tool_call"))
    check(results, "kanban_task_claimed ∈ VALID_HOOKS (Do.2)", lambda: _hook("kanban_task_claimed"))

    # 4. profiles read/meta/name API (§0.1 row 3).
    check(results, "profiles read/meta/name API (§0.1)", lambda: _attrs(hc_profiles, [
        "list_profiles", "get_profile_dir", "profile_exists", "create_profile",
        "read_profile_meta", "write_profile_meta", "validate_profile_name",
        "normalize_profile_name",
    ]))

    # 5-9. profile_distribution API, one check per Do.9 symbol (§0.1 row 5).
    for _name in ("plan_install", "install_distribution", "read_manifest",
                  "DistributionManifest", "InstallPlan"):
        check(results, f"profile_distribution.{_name} (§0.1/Do.9)",
              lambda n=_name: _callable_attr(pdist, n))

    # 10. profile_distribution support attrs (§0.1 row 5 remainder;
    #     USER_OWNED_EXCLUDE is asserted as a set below, Do.12).
    check(results, "profile_distribution support attrs (§0.1)", lambda: _attrs(pdist, [
        "write_manifest", "MANIFEST_FILENAME", "DEFAULT_DIST_OWNED",
    ]))

    # 11-12. Pack root files (§0.1 row 7), one check per file. The degradation
    #        path (pack simply absent) is the Do.13 notice; here the default
    #        source's shape is asserted.
    def _pack_file(fname):
        pr = pack_root()
        if not (pr / fname).is_file():
            raise AssertionError(f"pack root {pr}: missing {fname}")
        return f"{fname} present in {pr}"
    check(results, "pack root packs.json (§0.1)", lambda: _pack_file("packs.json"))
    check(results, "pack root recipes.json (§0.1)", lambda: _pack_file("recipes.json"))

    # 13. kanban_db API (§0.1 row 8).
    check(results, "kanban_db API (§0.1)", lambda: _attrs(kb, [
        "create_board", "list_boards", "board_exists", "read_board_metadata",
        "write_board_metadata", "get_current_board", "scoped_current_board",
        "create_task", "list_tasks", "assign_task", "get_task",
    ]))

    # 14. projects_db API (§0.1 row 9).
    check(results, "projects_db API (§0.1)", lambda: _attrs(projects_db, [
        "create_project", "get_project", "update_project", "list_projects", "add_folder",
    ]))

    # 15. hermes_cli.config API (§0.1 row 10).
    check(results, "hermes_cli.config API (§0.1)", lambda: _attrs(hc_config, [
        "is_managed", "read_user_config_raw", "require_readable_config_before_write",
        "load_config", "load_config_readonly", "save_config",
    ]))

    # 16. utils atomic writers (§0.1 row 11).
    check(results, "utils atomic writers (§0.1)", lambda: _attrs(utils, [
        "atomic_yaml_write", "atomic_write_text",
    ]))

    # 17. hermes_constants home/root helpers (§0.1 row 12).
    check(results, "hermes_constants home/root helpers (§0.1)", lambda: _attrs(hermes_constants, [
        "get_hermes_home", "get_default_hermes_root", "set_hermes_home_override",
    ]))

    # 18. `team` does not collide with a built-in subcommand (Do.4).
    def c_team_free():
        if "team" in cli_main._BUILTIN_SUBCOMMANDS:
            raise AssertionError("collision — `hermes team` would be swallowed")
        return "not a built-in"
    check(results, '"team" ∉ _BUILTIN_SUBCOMMANDS (Do.4)', c_team_free)

    # 19. Manifest v2 accepted (Do.5a).
    from hermes_cli import plugins_manifest

    def c_manifest_ver():
        v = plugins_manifest.SUPPORTED_MANIFEST_VERSION
        if v < 2:
            raise AssertionError(f"only manifest v{v} supported")
        return f"v{v} supported"
    check(results, "SUPPORTED_MANIFEST_VERSION >= 2 (Do.5)", c_manifest_ver)

    # 20. Every key of our spec-§11 plugin.yaml is a known manifest field (Do.5b).
    def c_yaml_keys():
        unknown = sorted(PLUGIN_YAML_KEYS - plugins_manifest._KNOWN_MANIFEST_FIELDS)
        if unknown:
            raise AssertionError(f"unknown manifest fields: {', '.join(unknown)}")
        return f"all {len(PLUGIN_YAML_KEYS)} §11 keys known"
    check(results, "plugin.yaml keys ∈ _KNOWN_MANIFEST_FIELDS (Do.5)", c_yaml_keys)

    # 21. Every board ticks by default (§0.1 row 13).
    def c_ticks():
        if not config_defaults.DEFAULT_CONFIG["kanban"]["dispatch_in_gateway"]:
            raise AssertionError("dispatch_in_gateway default flipped to False — boards stop ticking")
        return "True"
    check(results, "kanban.dispatch_in_gateway default True (§0.1)", c_ticks)

    # 22. plugins doctor / compat exist as verification surfaces (§0.1 row 14).
    #     doctor/compat nest one level under `plugins`, so the honest probe is
    #     that `hermes plugins doctor|compat` actually parses from a top-level
    #     parser (the old outer _name_parser_map only ever held "plugins" —
    #     the assertion below is the one that can fail loudly).
    def c_doctor_compat():
        from hermes_cli.subcommands import plugins as sub_plugins

        parser = argparse.ArgumentParser(prog="hermes")
        sub = parser.add_subparsers(dest="command")
        sub_plugins.build_plugins_parser(sub, cmd_plugins=lambda _a: None)
        missing = []
        for action in ("doctor", "compat"):
            try:
                parser.parse_args(["plugins", action])
            except SystemExit:
                missing.append(action)
        if missing:
            raise AssertionError("`hermes plugins " + " / plugins ".join(missing) + "` does not parse")
        return "doctor + compat wired"
    check(results, "hermes plugins doctor/compat wired (§0.1)", c_doctor_compat)

    # 23. compat scanner usable as a CI lint (§0.1 row 15).
    def c_compat_scan():
        detail = _attrs(plugin_compat, ["scan_plugin", "COMPAT_REMOVAL_DATE", "days_until_removal"])
        if not callable(plugin_compat.scan_plugin):
            raise AssertionError("scan_plugin is not callable")
        return detail
    check(results, "plugin_compat.scan_plugin callable (§0.1)", c_compat_scan)

    # 24-26. Signatures still accept the kwargs spec §5.3 register(ctx) passes (Do.3).
    pc = plugins_mod.PluginContext
    check(results, "register_tool(name,toolset,schema,handler,check_fn,emoji) (Do.3)", lambda:
          _kwargs_ok(pc.register_tool, ["name", "toolset", "schema", "handler", "check_fn", "emoji"]))
    check(results, "register_cli_command(name,help,setup_fn,handler_fn) (Do.3)", lambda:
          _kwargs_ok(pc.register_cli_command, ["name", "help", "setup_fn", "handler_fn"]))
    check(results, "register_system_prompt_section(id,content,position,max_chars) (Do.3)", lambda:
          _kwargs_ok(pc.register_system_prompt_section, ["id", "content", "position", "max_chars"]))

    # 27. Copied board-slug regex agrees with kb.create_board over 20 slugs (Do.6).
    #     Fixture is lowercase/trimmed on purpose: the substrate normalises
    #     (strip+lower) *before* validating, so mixed-case probes would compare
    #     different things. Our model.py stays intentionally stricter on raw input.
    def c_slug_agreement():
        slugs = ["default", "a", "z9", "board-1", "team_board", "abc", "proj-x",
                 "0start", "x_1-2", "d" * 64,
                 "-lead", "_lead", "ab c", "ab.c", "ab/../cd", "d" * 65, "ab\tcd",
                 "", "__", "tèst"]
        assert len(slugs) == 20, len(slugs)
        bad = []
        for slug in slugs:
            want = bool(SLUG_RE.match(slug))
            try:
                kb.create_board(slug)
                got = True
            except ValueError:
                got = False
            if got != want:
                bad.append(f"{slug!r}: create_board {'accepted' if got else 'rejected'}, "
                           f"copied regex {'accepts' if want else 'rejects'}")
        if bad:
            raise AssertionError("; ".join(bad))
        return "20/20 slugs agree"
    check(results, "copied SLUG_RE agrees with kb.create_board (Do.6)", c_slug_agreement)

    # 28. Sentinel set unchanged (Do.7).
    def c_sentinels():
        from tools import file_tools_paths

        got = frozenset(file_tools_paths._TERMINAL_CWD_SENTINELS)
        if got != TERMINAL_CWD_SENTINELS:
            raise AssertionError(f"sentinels changed: +{sorted(got - TERMINAL_CWD_SENTINELS)} "
                                 f"-{sorted(TERMINAL_CWD_SENTINELS - got)}")
        return f"exact match: {sorted(got)}"
    check(results, "_TERMINAL_CWD_SENTINELS exact match (Do.7)", c_sentinels)

    # 29-30. rename-on-install mechanism signatures (Do.9) — the name/override_name
    #        kwargs are the whole mechanism, so a rename must fail loudly here.
    def c_install_sig():
        params = inspect.signature(pdist.install_distribution).parameters
        want = {"source": inspect.Parameter.POSITIONAL_OR_KEYWORD,
                "name": inspect.Parameter.POSITIONAL_OR_KEYWORD,
                "force": inspect.Parameter.POSITIONAL_OR_KEYWORD,
                "create_alias": inspect.Parameter.POSITIONAL_OR_KEYWORD}
        bad = [f"{k} expected {v.name}, got "
               f"{'missing' if k not in params else params[k].kind.name}"
               for k, v in want.items() if k not in params or params[k].kind != v]
        if bad:
            raise AssertionError(", ".join(bad))
        return "install_distribution(source, name=None, force=False, create_alias=False)"
    check(results, "install_distribution signature (Do.9)", c_install_sig)

    def c_plan_sig():
        params = inspect.signature(pdist.plan_install).parameters
        if "override_name" not in params:
            raise AssertionError("plan_install lost its override_name kwarg — the instance rename is dead")
        return "plan_install(source, workdir, override_name=None)"
    check(results, "plan_install signature (Do.9)", c_plan_sig)

    # 31-35. Rename-on-install still works against the real pack, staged in tmp
    #        (Do.10) — one check per sub-assertion the Do item lists.
    rename_state: dict = {}

    def _stage_rename():
        if not rename_state:
            source = pack_root() / "hermes-agency" / "profiles" / "agency-backend-engineer"
            if not source.is_dir():
                raise AssertionError(f"probe pack not found: {source}")
            before_home = _tree_snapshot(root)
            before_pack = _tree_snapshot(source.parent)
            with tempfile.TemporaryDirectory(prefix="hermes-probe-stage-") as tmp:
                plan = pdist.plan_install(str(source), Path(tmp), override_name="probe-be")
                rename_state.update(
                    plan=plan,
                    dirty_home=before_home != _tree_snapshot(root),
                    dirty_pack=before_pack != _tree_snapshot(source.parent),
                )
        return rename_state

    def c_rename_manifest_name():
        plan = _stage_rename()["plan"]
        if plan.manifest.name != "probe-be":
            raise AssertionError(f"manifest.name = {plan.manifest.name!r}")
        return "manifest.name == 'probe-be'"
    check(results, "rename-on-install: manifest.name (Do.10)", c_rename_manifest_name)

    def c_rename_target_dir():
        plan = _stage_rename()["plan"]
        if plan.target_dir.name != "probe-be":
            raise AssertionError(f"target_dir.name = {plan.target_dir.name!r}")
        return "target_dir.name == 'probe-be'"
    check(results, "rename-on-install: target_dir.name (Do.10)", c_rename_target_dir)

    def c_rename_existing_false():
        plan = _stage_rename()["plan"]
        if plan.existing:
            raise AssertionError("existing is True (target should be new)")
        return "existing is False"
    check(results, "rename-on-install: existing is False (Do.10)", c_rename_existing_false)

    def c_rename_provenance():
        plan = _stage_rename()["plan"]
        if not plan.provenance:
            raise AssertionError("provenance empty")
        return "provenance non-empty"
    check(results, "rename-on-install: provenance (Do.10)", c_rename_provenance)

    def c_rename_isolated():
        # Do.10 asks for "nothing created outside tmp". plan_install is a pure
        # planner: neither HERMES_HOME nor the :ro pack tree may be touched.
        # plan.staged_dir's *location* is not part of the contract — when the
        # source is already a distribution directory the substrate stages in
        # place (staged_dir == resolved source), which the old copy of this
        # check wrongly asserted against.
        st = _stage_rename()
        problems = []
        if st["dirty_home"]:
            problems.append("plan_install wrote inside HERMES_HOME")
        if st["dirty_pack"]:
            problems.append("plan_install wrote inside the pack tree")
        if problems:
            raise AssertionError("; ".join(problems))
        return "nothing created outside the temp workdir"
    check(results, "rename-on-install: staged only in tmp (Do.10)", c_rename_isolated)

    def c_distribution_owned_field():
        """``provision._owned_paths`` reimplements the substrate's private
        ``_owned_entries`` walk from PUBLIC inputs (§5.2.1 bans calling the
        private one). Those inputs are this dataclass field plus
        ``USER_OWNED_EXCLUDE`` (Do.12); the two walks are asserted equivalent
        in ``test_substrate_contract.py``. Lose the field and the copy is blind.
        """
        fields = {f.name for f in dataclasses.fields(pdist.DistributionManifest)}
        if "distribution_owned" not in fields:
            raise AssertionError(
                "DistributionManifest.distribution_owned is gone — the "
                "dist-owned allow-list our plan preview copies no longer exists")
        return "DistributionManifest.distribution_owned present"
    check(results, "distribution_owned allow-list field (Do.9)", c_distribution_owned_field)

    # 36. profiles lifecycle — Do.11's existence clause (§0.1 row 4).
    check(results, "profiles lifecycle (delete/seed/_PROFILE_DIRS) (§0.1/Do.11)", lambda: _attrs(hc_profiles, [
        "delete_profile", "seed_profile_skills", "write_profile_meta", "_PROFILE_DIRS",
    ]))

    # 37. validate_profile_name agrees with the copied regex + reserved set (Do.11).
    #     Documented divergence: the substrate lets ``default`` through as the
    #     root-profile alias; our copy rejects it (spec §5.2.1 reserved set).
    def c_profile_name_agreement():
        names = ["qa", "project-x-backend-engineer", "be_2", "a", "z9", "abc-def_ghi",
                 "d" * 64, "Be", "l-ead", "_x", "x y", "d" * 65,
                 "hermes", "tmp", "default"]
        assert len(names) >= 12, len(names)  # Do.11: 12-name fixture minimum
        bad = []
        for name in names:
            want = bool(PROFILE_ID_RE.match(name)) and name not in RESERVED_PROFILE_NAMES
            try:
                hc_profiles.validate_profile_name(name)
                got = True
            except ValueError:
                got = False
            if name == "default":
                if got is not True or want is not False:
                    bad.append("default: documented divergence (substrate alias / ours reserved) broken")
            elif got != want:
                bad.append(f"{name!r}: substrate {'accepts' if got else 'rejects'}, "
                           f"copied rule {'accepts' if want else 'rejects'}")
        if bad:
            raise AssertionError("; ".join(bad))
        return "15/15 names agree ('default' divergence as documented)"
    check(results, "validate_profile_name agrees with copied rule (Do.11)", c_profile_name_agreement)

    # 38. Distribution payloads can never carry another profile's data (Do.12).
    def c_user_owned():
        need = {".env", "memories", "sessions", "state.db"}
        missing = need - set(pdist.USER_OWNED_EXCLUDE)
        if missing:
            raise AssertionError(f"no longer user-owned: {sorted(missing)}")
        return f"{sorted(need)} still excluded"
    check(results, "USER_OWNED_EXCLUDE ⊇ {.env, memories, sessions, state.db} (Do.12)", c_user_owned)

    # ---- the approvals inbox and its operator push --------------------------

    # 39. The observer that feeds the inbox must stay a real hook name.
    check(results, "kanban_task_blocked ∈ VALID_HOOKS",
          lambda: _hook("kanban_task_blocked"))

    # 40. Read/write surface the inbox is built from: approvals.py reads the
    #     card, its events, comments and attachments, comments the decision and
    #     unblocks. One missing name silently empties the queue.
    def c_approvals_surface():
        detail = _attrs(kb, [
            "kanban_db_path", "get_task", "list_tasks", "list_events",
            "list_comments", "add_comment", "unblock_task",
            "list_attachments", "get_attachment",
        ])
        _callable_attr(hc_profiles, "get_active_profile_name")
        return detail + ", profiles.get_active_profile_name present"
    check(results, "approvals card/comment surface", c_approvals_surface)

    # 41. "Waiting on a person" is exactly block_kind=needs_input: the
    #     predicate is worthless if the substrate drops or renames the kind.
    def c_needs_input():
        if "needs_input" not in kb.VALID_BLOCK_KINDS:
            raise AssertionError(
                f"needs_input gone from VALID_BLOCK_KINDS: {sorted(kb.VALID_BLOCK_KINDS)}")
        fields = {f.name for f in dataclasses.fields(kb.Task)}
        if "block_kind" not in fields:
            raise AssertionError("Task no longer carries block_kind")
        return ("needs_input ∈ VALID_BLOCK_KINDS "
                f"({len(kb.VALID_BLOCK_KINDS)} kinds), Task.block_kind present")
    check(results, "blocked-on-a-human kind survives", c_needs_input)

    # 42. The push half: subscribe the card to every configured gateway
    #     home channel, then let the existing notifier deliver. Three surfaces,
    #     all outside kanban_db.
    def c_notify_surface():
        from hermes_cli import kanban_db_connect, kanban_db_notify
        sub = _kwargs_ok(kanban_db_notify.add_notify_sub, [
            "task_id", "platform", "chat_id", "thread_id", "notifier_profile"])
        conn_kw = _kwargs_ok(kanban_db_connect.connect_closing, ["db_path"])
        from gateway.config import load_gateway_config
        if not callable(load_gateway_config):
            raise AssertionError("gateway.config.load_gateway_config is not callable")
        return f"add_notify_sub {sub}; connect_closing {conn_kw}; load_gateway_config present"
    check(results, "home-channel notify surface", c_notify_surface)

    # ---- the attachment substrate behind team_attach_path ------------------

    # 43. One write path for dashboard, tools and CLI — the plugin uses it
    #     rather than writing blobs itself.
    check(results, "store_attachment_bytes signature",
          lambda: _kwargs_ok(kb.store_attachment_bytes,
                             ["content_type", "uploaded_by", "board", "max_bytes"]))

    # 44. The cap is the substrate's, not ours: team_attach_path refuses ahead
    #     of the write so a rejected read never becomes a truncated attachment.
    def c_attach_cap():
        cap = kb.KANBAN_ATTACHMENT_MAX_BYTES
        if cap != 25 * 1024 * 1024:
            raise AssertionError(f"attachment cap moved: {cap} bytes")
        if not issubclass(kb.AttachmentTooLarge, ValueError):
            raise AssertionError("AttachmentTooLarge no longer a ValueError")
        return f"cap {cap} bytes (25 MiB), AttachmentTooLarge ⊂ ValueError"
    check(results, "attachment cap + overflow error", c_attach_cap)

    # 45. Stored names are sanitised and never overwrite a sibling — the
    #     plugin passes a worker-supplied basename straight in.
    def c_attach_naming():
        raw = "../../etc/passwd"
        safe = kb._safe_attachment_name(raw)
        if "/" in safe or "\\" in safe or safe.startswith(".."):
            raise AssertionError(f"_safe_attachment_name kept traversal: {safe!r}")
        d = Path(tempfile.mkdtemp(prefix="hermes-probe-attach-"))
        try:
            first = kb._collision_free_path(d, "design.md")
            first.write_bytes(b"x")
            second = kb._collision_free_path(d, "design.md")
            if second == first or second.exists():
                raise AssertionError(f"_collision_free_path reused {second}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return f"{raw!r} -> {safe!r}; second design.md -> {second.name}"
    check(results, "attachment naming is safe + collision-free", c_attach_naming)

    # 46. Row shape the tool and the dashboard both read back.
    def c_attach_rows():
        detail = _attrs(kb, ["add_attachment", "list_attachments",
                             "get_attachment", "delete_attachment"])
        fields = {f.name for f in dataclasses.fields(kb.Attachment)}
        need = {"id", "task_id", "filename", "stored_path", "content_type", "size"}
        missing = need - fields
        if missing:
            raise AssertionError(f"Attachment lost fields: {sorted(missing)}")
        return detail + f", Attachment ⊇ {sorted(need)}"
    check(results, "attachment row surface", c_attach_rows)

    # 47. Blobs live under the per-board attachments root, and the env override
    #     workers run with still wins (the tool resolves paths against it).
    def c_attach_root():
        d = Path(tempfile.mkdtemp(prefix="hermes-probe-attroot-"))
        prev = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT")
        os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] = str(d)
        try:
            got = kb.task_attachments_dir("t-1", board="probe-board")
            if d not in got.parents and got != d:
                raise AssertionError(
                    f"HERMES_KANBAN_ATTACHMENTS_ROOT ignored: {got} not under {d}")
        finally:
            if prev is None:
                os.environ.pop("HERMES_KANBAN_ATTACHMENTS_ROOT", None)
            else:
                os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] = prev
            shutil.rmtree(d, ignore_errors=True)
        return f"task_attachments_dir honours the env root ({got.name} under it)"
    check(results, "attachments root override", c_attach_root)

    # ---- the worker exit guard ---------------------------------------------

    # 48. The wrapper is installed as $HERMES_BIN, and the dispatcher must
    #     re-read that variable on every spawn for the install to take effect.
    def c_hermes_bin():
        from hermes_cli import kanban_db_dispatch as kdd
        fake = Path(tempfile.mkdtemp(prefix="hermes-probe-bin-")) / "hermes-wrapper"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        prev = os.environ.get("HERMES_BIN")
        os.environ["HERMES_BIN"] = str(fake)
        try:
            argv = kdd._resolve_hermes_argv()
            if not argv or os.path.abspath(str(fake)) not in [str(a) for a in argv]:
                raise AssertionError(f"$HERMES_BIN not honoured: {argv}")
        finally:
            if prev is None:
                os.environ.pop("HERMES_BIN", None)
            else:
                os.environ["HERMES_BIN"] = prev
            shutil.rmtree(fake.parent, ignore_errors=True)
        return "_resolve_hermes_argv resolves $HERMES_BIN per spawn"
    check(results, "$HERMES_BIN wrapper hook", c_hermes_bin)

    # 49. The guard's only signal that the turn really started is the 40-char
    #     rule the turn mixin prints once credentials AND agent init succeeded.
    #     Copied, never imported (§5.2.1) — so its agreement is checked here.
    def c_turn_marker():
        src = (HA / "hermes_cli" / "cli_chat_turn_mixin.py").read_text(encoding="utf-8")
        if "'─' * 40" not in src and '"─" * 40' not in src:
            raise AssertionError(
                "cli_chat_turn_mixin no longer prints the 40-char ─ rule — "
                "worker_exit_guard would mark every worker dead-on-arrival")
        guard = (REPO / "src" / "worker_exit_guard.py").read_text(encoding="utf-8")
        if 'MARKER = "─" * 40' not in guard:
            raise AssertionError("worker_exit_guard MARKER drifted from the copied rule")
        return "40-char ─ turn-start rule agrees with worker_exit_guard.MARKER"
    check(results, "turn-start marker agrees with the guard", c_turn_marker)

    # ---- notices: printed, never asserted (Do.8, Do.13) ----

    # Compat-window operator notice (Do.8).
    results.append(Result(
        "compat window", True,
        f"COMPAT_REMOVAL_DATE={plugin_compat.COMPAT_REMOVAL_DATE.isoformat()} "
        f"(legacy hermes_cli import paths die in "
        f"{plugin_compat.days_until_removal()} days — keep imports on the modern paths)",
        asserted=False,
    ))

    # Catalog resolution notice (Do.13): packs.json parses, every pack
    # manifest parses, distribution.yaml exists for a sampled entry.
    def notice_catalog():
        pr = pack_root()
        lines = [f"pack root: {pr} ({'resolved' if pr.is_dir() else 'MISSING'})"]
        try:
            packs = json.loads((pr / "packs.json").read_text(encoding="utf-8"))
            lines.append(f"packs.json: {len(packs.get('packs', []))} packs")
            for pack in packs.get("packs", []):
                mpath = pr / pack["manifest"]
                try:
                    manifest = json.loads(mpath.read_text(encoding="utf-8"))
                    n = len(manifest.get("profiles", []))
                    lines.append(f"  {pack['name']}: manifest OK, {n} entries")
                    if pack["name"] == "hermes-agency" and n:
                        entry = manifest["profiles"][0]
                        d = pr / pack["path"] / "profiles" / entry["name"] / "distribution.yaml"
                        lines.append(f"  sampled {entry['name']}: distribution.yaml "
                                     f"{'exists' if d.is_file() else 'MISSING'}")
                except Exception as exc:  # noqa: BLE001 — notice only
                    lines.append(f"  {pack['name']}: manifest unreadable: {exc}")
        except Exception as exc:  # noqa: BLE001 — notice only
            lines.append(f"packs.json unreadable: {exc}")
        return "; ".join(lines)
    results.append(Result("catalog sources", True, notice_catalog(), asserted=False))


def main() -> int:
    results = run_checks()
    failures = 0
    for r in results:
        print(r.line())
        if not r.ok and r.asserted:
            failures += 1
    n_checks = sum(1 for r in results if r.asserted)
    n_notices = sum(1 for r in results if not r.asserted)
    if failures:
        print(f"substrate contract BROKEN: {failures}/{n_checks} checks failed "
              f"({n_notices} notices)")
        return 1
    print(f"substrate contract OK ({n_checks} checks, {n_notices} notices)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
