"""provision.py — the ONLY module that creates or deletes a profile.

Every ``--from`` member is an *instance*: a real profile copied out of a pack
directory, stamped with provenance, that the team owns (``managed: true``).
Adopted members (``--member``) are never touched here.

F6 — ``install_distribution`` alone is NOT the install. This module mirrors
the *pack installer's* behaviour, not the library call in isolation:
install → ``write_profile_meta`` → plugin symlink → enable. It must **never**
call ``seed_profile_skills()`` (§2.9 trap 2): a distribution ships its own
skills and may carry ``.no-bundled-skills`` on purpose.
``create_alias`` is pinned to ``False`` (§2.9 trap 3).

Resumability (§5.5): a failed create keeps the instances it made; re-running
the identical command adopts any whose stamped ``distribution.yaml:source``
matches the entry and creates only the rest.

All substrate imports are lazy (spec §5.2).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from .errors import (
    TeamError,
    E_TEAM_INVALID,
    E_TEAM_PROVISION_FAILED,
    E_TEAM_PROFILE_EXISTS,
)
from . import model

PLUGIN_NAME = "hermes-agent-synergia"


def _field(record, name: str, default=""):
    """One field of a catalog entry or member record: the ``catalog``/``model``
    dataclasses answer by attribute, the ``to_dict``/JSON shape by key.

    Presence decides, never truthiness. The reader this replaced was
    ``getattr(rec, k, None) or rec.get(k, d)``, which on a falsy-but-present
    attribute — an ``Entry`` whose ``description`` is "" — fell through to the
    mapping branch and raised ``'Entry' object has no attribute 'get'``,
    surfacing as E_TEAM_PROVISION_FAILED for every description-less blueprint.
    """
    value = (record.get(name, default) if isinstance(record, Mapping)
             else getattr(record, name, default))
    return default if value is None else value


def _dist():
    from hermes_cli import profile_distribution
    return profile_distribution


def _profiles():
    from hermes_cli import profiles
    return profiles


# ── naming ────────────────────────────────────────────────────────────────────

def render_name(team_slug: str, entry, template: Optional[str] = None) -> str:
    """``{team}-{short}`` by default; validated before anything stages."""
    entry_id = _field(entry, "id")
    return model.render_instance_name(team_slug, entry_id,
                                      template or model.DEFAULT_PROFILE_NAMING)


# ── planning (writes nothing outside staging) ────────────────────────────────

def _owned_paths(staged: Path, manifest) -> list:
    """Top-level paths the distribution owns — PUBLIC API only.

    §5.2.1 "private symbols": this mirrors ``profile_distribution._owned_entries``
    instead of calling it. Both inputs are public and both are pinned by the S1
    probe (``DistributionManifest.distribution_owned``, ``USER_OWNED_EXCLUDE``),
    and the probe asserts this copy still agrees with the substrate's own walk —
    so a rename there fails loudly in CI instead of silently emptying
    ``--dry-run``. An empty allow-list means "the whole payload minus the
    user-owned names", exactly as the substrate reads it.
    """
    from pathlib import PurePosixPath
    exclude = _dist().USER_OWNED_EXCLUDE
    explicit = [rel for rel in (str(r).strip().strip("/")
                                for r in (manifest.distribution_owned or [])) if rel]
    if not explicit:
        try:
            return sorted(e.name for e in staged.iterdir() if e.name not in exclude)
        except OSError:
            return []
    owned = []
    for rel in explicit:
        parts = PurePosixPath(rel).parts
        if not parts or parts[0] in exclude:
            continue
        if ".." in parts or PurePosixPath(rel).is_absolute():
            continue
        if staged.joinpath(*parts).exists():
            owned.append(str(PurePosixPath(*parts)))
    return sorted(owned)


def plan(entry, name: str) -> dict:
    """``plan_install`` into a throw-away staging dir. Serialisable summary.

    The staging temp dir is deleted on exit — nothing here can touch a live
    profile. This is what ``--dry-run`` and the dashboard ``?dry_run=true``
    return.
    """
    import tempfile
    dist = _dist()
    source = _field(entry, "source_path")
    with tempfile.TemporaryDirectory(prefix="hermes-agent-synergia-plan-") as tmp:
        p = dist.plan_install(source, Path(tmp), override_name=name)
        files = _owned_paths(p.staged_dir, p.manifest)
        return {
            "name": p.manifest.name,
            "source": p.provenance,
            "target_dir": str(p.target_dir),
            "existing": bool(p.existing),
            "file_count": len(files),
            "files": files[:20],          # preview only; count is authoritative
        }


# ── instantiation ─────────────────────────────────────────────────────────────

def _stamped_source(profile_home: Path) -> str:
    """The ``source:`` provenance of an installed profile, or ""."""
    import yaml
    try:
        data = yaml.safe_load(
            (profile_home / "distribution.yaml").read_text(encoding="utf-8"))
        return str((data or {}).get("source") or "").strip().rstrip("/")
    except (OSError, yaml.YAMLError):
        return ""


def _adopted_matches(name: str, source: str) -> bool:
    import hermes_constants
    home = _profiles().get_profile_dir(name)
    return (home.is_dir()
            and not hermes_constants.named_profile_is_deleted(home)
            and _stamped_source(home) == source.rstrip("/"))


def _tombstone_check(name: str) -> None:
    import hermes_constants
    home = _profiles().get_profile_dir(name)
    if home.is_dir() and hermes_constants.named_profile_is_deleted(home):
        raise TeamError(
            E_TEAM_INVALID,
            f"profile {name!r} is a tombstone (deleted profile marker present)",
            hint="Recreating over a tombstone is refused — the marker usually "
                 "means it was deleted on purpose. Never force it.",
            remedy=f"clear the marker manually under {home} if the deletion "
                   "was unintended, then re-run",
        )


def _enable_plugin(name: str) -> None:
    """F1 plumbing: per-profile symlink + ``plugins.enabled`` append."""
    profiles = _profiles()
    home = profiles.get_profile_dir(name)
    link = home / "plugins" / PLUGIN_NAME
    if not link.is_symlink():
        (home / "plugins").mkdir(parents=True, exist_ok=True)
        if link.exists():
            os.remove(link)
        # relative so the whole root can move; three levels up from
        # <home>/plugins is <root>, then plugins/hermes-agent-synergia (matches the
        # deployed convention on this stack).
        os.symlink(os.path.join("..", "..", "..", "plugins", PLUGIN_NAME), link)
    _append_enabled(name)


def _append_enabled(name: str) -> None:
    """Append ``hermes-agent-synergia`` to the profile's ``plugins.enabled`` (F1).

    Routed through :func:`resolve.write_profile_config_keys` — the plugin's
    ONE config writer (S8). That matters: this used to hand-roll a
    ``yaml.safe_load`` + ``atomic_yaml_write`` round trip, which on an
    unparseable ``config.yaml`` fell back to ``{}`` and rewrote the file down
    to this single key. The writer instead applies every config-writer guard — managed
    install, allow-list, unreadable/corrupt target, tombstone, ``.bak``
    before the first byte, one atomic replace, idempotent no-op.
    """
    from . import resolve
    try:
        cfg = resolve.read_profile_config(name)
    except Exception as exc:  # noqa: BLE001 — a corrupt config is refused, never rewritten
        raise TeamError(
            E_TEAM_INVALID,
            f"cannot read {name}'s config.yaml, so refusing to enable the "
            f"plugin in it: {exc}",
            hint="An unparseable profile config is never overwritten (writer guard 2).",
            remedy=f"repair {name}'s config.yaml, then re-run the same "
                   "hermes team create (it adopts what it already made)",
        ) from exc
    plugins = cfg.get("plugins")
    current = plugins.get("enabled") if isinstance(plugins, dict) else None
    enabled = [str(x) for x in current] if isinstance(current, list) else []
    if PLUGIN_NAME in enabled:
        return
    resolve.write_profile_config_keys(name, {"plugins.enabled": enabled + [PLUGIN_NAME]})


def instantiate(entry, name: str, *, role: str = "member") -> model.Member:
    """Create the instance. Order is contractual (F6): install → meta →
    plugin symlink → enable. Returns the Member record to store."""
    profiles = _profiles()
    dist = _dist()
    source = str(_field(entry, "source_path"))
    description = str(_field(entry, "description"))
    display_name = str(_field(entry, "display_name") or name)
    entry_id = str(_field(entry, "id"))
    pack = str(_field(entry, "pack"))

    _tombstone_check(name)
    home = profiles.get_profile_dir(name)
    if _adopted_matches(name, source):
        # resumable re-run (§5.5): identical provenance → adopt, never re-install.
        # Gated on provenance ALONE, not profile_exists(): a create that died
        # mid-install leaves distribution.yaml stamped but no identity marker
        # (config.yaml/profile.yaml), so profile_exists() is False while the
        # substrate's plan_install() calls the target existing (plain is_dir,
        # profile_distribution.py:330) and refuses without force.
        _write_meta_and_enable(home, description, display_name, name)
        return model.Member(
            profile=name, role=role, blueprint=entry_id,
            blueprint_source=f"pack:{pack}", managed=True, primary=True,
        )
    # Same is_dir rule the substrate installs by: anything it would call
    # existing and is not ours gets the actionable error, not a raw
    # DistributionError rewrapped as E_TEAM_PROVISION_FAILED.
    if home.is_dir():
        raise TeamError(
            E_TEAM_PROFILE_EXISTS,
            f"profile {name!r} already exists and is not an instance of "
            f"{entry_id!r}",
            hint="Adopt it instead (--member) or render under another "
                 "name (--name).",
            remedy=f"hermes team create <team> --from {entry_id} --name "
                   f"<other-name>",
        )

    try:
        p = dist.install_distribution(source, name=name, force=False,
                                      create_alias=False)
    except dist.DistributionError as exc:
        raise TeamError(
            E_TEAM_PROVISION_FAILED,
            f"install_distribution({source}) failed: {exc}",
            hint="Already-created instances are kept; a re-run adopts them "
                 "(§5.5 resumable create).",
            remedy=f"hermes team create ... --from {entry_id}  (re-run)",
        ) from exc

    # F6: the library call is not the whole install —
    # profile.yaml carries the description the picker/kanban decomposer read.
    _write_meta_and_enable(p.target_dir, description, display_name, name)
    # seed_profile_skills is NEVER called here (§2.9 trap 2, F6).
    _seed_root_model(name)

    return model.Member(
        profile=name, role=role, blueprint=entry_id,
        blueprint_source=f"pack:{pack}", managed=True, primary=True,
    )


def _seed_root_model(name: str) -> None:
    """Pin the root config's model into a fresh instance that ships none.

    ``install_distribution`` copies the pack's ``config.yaml`` verbatim and,
    unlike ``profiles.create_profile``, never seeds a model block. Without
    one the instance has no provider: its kanban workers print "No inference
    provider configured" and exit 0 before the first turn. A member-level
    model from the roster still wins — reconcile projects it afterwards.
    """
    from . import resolve
    if resolve.read_profile_config(name).get("model"):
        return
    root = resolve.default_model()
    if not root.get("model"):
        return
    updates = {"model.default": root["model"]}
    if root.get("provider"):
        updates["model.provider"] = root["provider"]
    if root.get("base_url"):
        updates["model.base_url"] = root["base_url"]
    resolve.write_profile_config_keys(name, updates)


def _write_meta_and_enable(home: Path, description: str, display_name: str,
                           name: str) -> None:
    """F6 tail shared by fresh install and adopt-on-re-run."""
    profiles = _profiles()
    profiles.write_profile_meta(
        home, description=description,
        description_auto=False, display_name=display_name)
    _enable_plugin(name)


# ── deletion: the plugin only deletes what it created ────────────────────────

def delete(member) -> None:
    """Delete a managed instance. Refuses adopted members."""
    profile = _field(member, "profile")
    managed = _field(member, "managed", False)
    if not managed:
        raise TeamError(
            E_TEAM_INVALID,
            f"member {profile!r} is adopted (managed: false) — the plugin "
            "deletes only profiles it created",
            hint="Remove the member from the team; the profile itself stays.",
            remedy=f"hermes profile delete {profile}   (manual)",
        )
    _tombstone_check(profile)
    import contextlib
    import sys as _sys
    try:
        # delete_profile prints a human summary on stdout; §8.2 keeps stdout
        # for the single JSON envelope — reroute chatter to stderr.
        with contextlib.redirect_stdout(_sys.stderr):
            _profiles().delete_profile(profile, yes=True)
    except FileNotFoundError:
        pass                       # already gone — deletion is idempotent


# ── .env linking (F7) ────────────────────────────────────────────────────────

def link_env(profile: str) -> None:
    """Symlink ``<profile-home>/.env`` → root ``.env``. **Symlink only**, never
    a copy, only under ``--link-env`` (§13)."""
    import hermes_constants
    root = hermes_constants.get_default_hermes_root()
    root_env = root / ".env"
    if not root_env.is_file():
        return
    home = _profiles().get_profile_dir(profile)
    target = home / ".env"
    if target.is_symlink():
        if os.path.realpath(target) == os.path.realpath(root_env):
            return
        target.unlink()
    if target.exists():
        return                     # a real .env belongs to the user — hands off
    home.mkdir(parents=True, exist_ok=True)
    os.symlink(root_env, target)
