"""CLI surface for ``hermes team`` — registry commands (S6).

Commands here (spec §8.1 subset): ``list``, ``show``, ``create``, ``destroy``,
``add-member``, ``remove-member``, ``doctor``, ``sync``, ``set-board``,
``set-workdir`` (S11), ``merge`` (fast-forward the workdir to a finished
thread's result branch), ``profiles``/``recipes`` with S6a; ``clone``/``import``/``export`` (S17);
``adopt`` (S18, §8.1: an existing board becomes a team, tenant backfill),
``status`` (S18b: the read side of adopt — tenant-scoped counts + open
cards; same read path as the ``team_status`` tool and the dashboard's
``GET /teams/{slug}/status``).

Two contracts every handler honors (spec §8.2):

* ``--json`` prints **exactly one JSON object on stdout and nothing else**;
  human text goes to stderr.
* Exit codes: 0 success, 1 failure, 2 usage (argparse), 3 locked/CAS,
  4 unfixable drift.

Substrate calls (profile/board existence) are lazy imports inside handlers,
per spec §5.2's module-scope rule.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


class _OrderedFlag(argparse.Action):
    """Append a ``(kind, value)`` pair, preserving command-line order.

    ``--from A --name X --from B`` must interleave: the ``--name`` binds to
    the *preceding* ``--from`` (spec §8.1), which two independent append
    lists cannot express.
    """

    def __init__(self, *args, kind=None, **kw):
        super().__init__(*args, **kw)
        self.kind = kind

    def __call__(self, parser, namespace, values, option_string):
        store = getattr(namespace, self.dest, None)
        if store is None:
            store = []
            setattr(namespace, self.dest, store)
        store.append((self.kind, values))

from .errors import (
    TeamError,
    E_TEAM_BOARD_TAKEN,
    E_TEAM_BUSY,
    E_TEAM_EXISTS,
    E_TEAM_INVALID,
    E_TEAM_NOT_FOUND,
    E_TEAM_NOT_MEMBER,
    E_TEAM_NO_BLUEPRINT,
    E_TEAM_NO_PROFILE,
    E_TEAM_NO_BOARD,
    E_TEAM_PROVISION_FAILED,
)
from . import model
from . import storage

# ── output helpers ───────────────────────────────────────────────────────────

def _emit(payload: dict, *, json_mode: bool) -> None:
    """Success output: exactly one JSON object on stdout, or human lines on stderr.

    ``--json`` shape: ``{"ok": true, "data": { … }}`` — the ``_human`` key is
    display-only and stripped in JSON mode (spec §8.2).
    """
    payload = dict(payload)
    human = payload.pop("_human", None)
    if json_mode:
        print(json.dumps({"ok": True, "data": payload},
                         ensure_ascii=False, default=str))
    else:
        for line in (human or [json.dumps(payload, default=str)]):
            print(line, file=sys.stderr)


def _fail(err: TeamError, *, json_mode: bool, team: Optional[str] = None) -> int:
    if json_mode:
        print(json.dumps(err.to_envelope(team=team), ensure_ascii=False))
    else:
        print(f"{err.code}: {err.message}", file=sys.stderr)
        print(f"  hint:   {err.hint}", file=sys.stderr)
        print(f"  remedy: {err.remedy}", file=sys.stderr)
    return err.exit_code


# ── argument parsing ─────────────────────────────────────────────────────────

def parse_member_spec(spec: str):
    """``PROFILE[:ROLE[:CAPACITY]]`` -> dict for Member.from_dict."""
    parts = spec.split(":")
    profile = parts[0].strip()
    if not profile:
        raise TeamError(
            E_TEAM_INVALID,
            f"empty profile name in member spec {spec!r}",
            hint="Format: PROFILE[:ROLE[:CAPACITY]]",
            remedy="hermes team create --help",
        )
    d = {"profile": profile}
    if len(parts) > 1 and parts[1].strip():
        d["role"] = parts[1].strip()
    if len(parts) > 2 and parts[2].strip():
        try:
            d["capacity"] = int(parts[2])
        except ValueError:
            raise TeamError(
                E_TEAM_INVALID,
                f"capacity {parts[2]!r} in member spec {spec!r} is not an integer",
                hint="Capacity must be a positive integer.",
                remedy="hermes team create --help",
            ) from None
    if len(parts) > 3:
        raise TeamError(
            E_TEAM_INVALID,
            f"member spec {spec!r} has too many ':' fields",
            hint="Format: PROFILE[:ROLE[:CAPACITY]]",
            remedy="hermes team create --help",
        )
    return d


def setup_parser(p):
    """Build the ``hermes team`` argparse tree (registry subset)."""
    p = _setup_parser_impl(p)
    return p


def _setup_parser_impl(p):
    subs = p.add_subparsers(dest="team_command")

    sp = subs.add_parser("list", help="List teams")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("show", help="Show one team")
    sp.add_argument("team")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("create", help="Create a team")
    sp.add_argument("team")
    sp.add_argument("--member", action="append", default=[],
                    metavar="PROFILE[:ROLE[:CAPACITY]]",
                    help="Adopt an existing profile (repeatable)")
    sp.add_argument("--from", dest="from_specs", action=_OrderedFlag, kind="from",
                    default=None, metavar="ENTRY[:ROLE[:CAPACITY]]",
                    help="Instantiate a catalog entry (repeatable)")
    sp.add_argument("--name", dest="from_specs", action=_OrderedFlag, kind="name",
                    default=None, metavar="PROFILE",
                    help="Override the rendered name of the preceding --from")
    sp.add_argument("--from-recipe", dest="from_recipe", default=None,
                    metavar="ID", help="Expand a recipes.json roster preset")
    sp.add_argument("--tier", dest="tier", default="recommended",
                    choices=("minimal", "recommended", "expanded"),
                    help="Recipe tier (with --from-recipe; default recommended)")
    sp.add_argument("--orchestrator", default=None)
    sp.add_argument("--board", default=None, help="Board slug (default: <team>)")
    # create always makes a missing board and workdir now; the flags are kept
    # so existing scripts still parse.
    sp.add_argument("--create-board", action="store_true",
                    help="No-op: a missing board is always created")
    sp.add_argument("--allow-shared-board", action="store_true")
    sp.add_argument("--workdir", default=None)
    sp.add_argument("--create-workdir", action="store_true",
                    help="No-op: a missing workdir is always created")
    sp.add_argument("--workspace-policy", choices=sorted(model.WORKSPACE_POLICIES),
                    default="worktree")
    sp.add_argument("--policy", choices=sorted(model.POLICY_MODES), default=None,
                    help="off|advise|enforce (default: default_policy setting)")
    sp.add_argument("--display-name", default=None)
    sp.add_argument("--description", default=None)
    sp.add_argument("--profile-naming", default=None)
    sp.add_argument("--create-missing", action="store_true",
                    help="Create missing --member profiles")
    sp.add_argument("--clone-from", default=None)
    sp.add_argument("--clone-secrets", action="store_true")
    sp.add_argument("--link-env", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    # Hidden debug/test seam: the team is not ready until `team sync --apply`.
    sp.add_argument("--no-sync", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(roster=None)

    sp = subs.add_parser(
        "adopt",
        help="Adopt an EXISTING board as a team (§8.1, S18): no board is "
             "created; --members-from-board derives the roster from the "
             "board's assignees; --backfill-tenant stamps untenant'd rows "
             "(explicit, confirmed, reversible)")
    sp.add_argument("team")
    sp.add_argument("--board", default=None,
                    help="Existing board slug (REQUIRED; adopt never creates one)")
    sp.add_argument("--members-from-board", dest="members_from_board",
                    action="store_true",
                    help="Roster = distinct non-null assignees on the board's tasks")
    sp.add_argument("--backfill-tenant", dest="backfill_tenant",
                    action="store_true",
                    help="Stamp rows with tenant IS NULL as tenant=<team>. "
                         "Requires --yes unless confirmed interactively.")
    sp.add_argument("--workdir", default=None,
                    help="Team workdir (default: the board's default_workdir "
                         "metadata; adopt never creates it)")
    sp.add_argument("--yes", action="store_true",
                    help="Confirm the --backfill-tenant write (bypasses the "
                         "interactive prompt)")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "status",
        help="The team's work on its board, tenant-scoped (S18b, §8.1): "
             "per-status counts + open cards; the read side of adopt — the "
             "same seam the team_status tool and the dashboard status route "
             "read (untenant'd cards read zero here until backfilled)")
    sp.add_argument("team")
    sp.add_argument("--status", dest="status", default=None, metavar="S",
                    help="Restrict the card list to this status "
                         "(todo|ready|running|review|done|blocked)")
    sp.add_argument("--assignee", default=None, metavar="PROFILE",
                    help="Restrict the card list to this assignee")
    sp.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Max cards to report (1..100, default 25)")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "approvals",
        help="The operator inbox, tenant-scoped: every card nothing moves on "
             "its own — waiting on a human decision, blocked with nobody to "
             "answer, finished and waiting in review, routed to triage, or "
             "held back by the dispatcher's respawn guard. No <team> lists "
             "every team's queue")
    sp.add_argument("team", nargs="?", default=None,
                    help="Restrict the queue to one team (default: all teams)")
    sp.add_argument("--kind", dest="kind", default="all",
                    choices=["all", "needs_input", "stuck", "review",
                             "triage", "guarded"],
                    help="Restrict the queue to one lane (default: all). "
                         "needs_input and review take approve/reject; stuck, "
                         "triage and guarded take release")
    sp.add_argument("--show", dest="show", default=None, metavar="CARD",
                    help="Print one card in full: body, comments, attachments")
    sp.add_argument("--artifact", dest="artifact", default=None, metavar="NAME|ID",
                    help="With --show: stream that attachment to stdout "
                         "(text types only, size-capped)")
    sp.add_argument("--bytes", dest="max_bytes", type=int, default=None,
                    metavar="N", help="Artifact preview cap (default 262144)")
    sp.add_argument("--summary", dest="summary", action="store_true",
                    help="With --show: summarize what the card is asking and "
                         "list the options (one model call per card revision, "
                         "cached)")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "approve",
        help="Record an approval on a waiting card and return it to the pool. "
             "The assignee resumes and completes the card itself")
    sp.add_argument("card")
    sp.add_argument("--note", required=True, metavar="TEXT",
                    help="The decision the agent resumes by reading")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "reject",
        help="Record requested changes on a waiting card; unblocks so "
             "the agent reworks, or --park to leave it blocked")
    sp.add_argument("card")
    sp.add_argument("--reason", required=True, metavar="TEXT",
                    help="What must change")
    sp.add_argument("--park", action="store_true",
                    help="Record the comment but leave the card blocked")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "release",
        help="Return a stuck card to the pool with a note (blocked for "
             "anything other than a human decision). Comment + unblock: the "
             "retry budget resets, the assignee runs it again")
    sp.add_argument("card")
    sp.add_argument("--note", required=True, metavar="TEXT",
                    help="What the assignee should do differently on the re-run")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "note",
        help="Steer a card that is already moving: leave a note the running "
             "worker picks up mid-run (comment only — no unblock)")
    sp.add_argument("card")
    sp.add_argument("--text", required=True, metavar="TEXT",
                    help="What the assignee should take into account right now")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "pause",
        help="Pause the card's whole thread: park every running, ready and "
             "todo card and stop the running workers. Resume starts a fresh "
             "run; files in the workdir are kept")
    sp.add_argument("card")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "resume",
        help="Re-queue every paused card of the card's thread")
    sp.add_argument("card")
    sp.add_argument("--team", default=None, metavar="TEAM",
                    help="Disambiguate when two teams hold the same card id")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("destroy", help="Delete a team and its managed profiles")
    sp.add_argument("team")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--purge-board", action="store_true")
    # --purge-profiles is the default now; kept so existing scripts still parse.
    sp.add_argument("--purge-profiles", dest="purge_profiles", action="store_true")
    sp.add_argument("--keep-profiles", dest="purge_profiles", action="store_false",
                    help="Keep the managed profiles (delete the record only)")
    sp.set_defaults(purge_profiles=True)
    _add_busy_flags(sp)
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("add-member", help="Add a member to a team")
    sp.add_argument("team")
    sp.add_argument("profile")
    sp.add_argument("--role", default="member")
    sp.add_argument("--capacity", type=int, default=None)
    sp.add_argument("--primary", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("remove-member", help="Remove a member from a team")
    sp.add_argument("team")
    sp.add_argument("profile")
    sp.add_argument("--delete-profile", action="store_true")
    _add_busy_flags(sp)
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("doctor", help="Registry health: invariants, ownership, catalog")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("profiles",
                         help="Browse and manage the profile catalog")
    psubs = sp.add_subparsers(dest="profiles_command")

    def _catalog_cas(parser):
        parser.add_argument("--expected-revision", dest="expected_revision",
                            type=int, default=None,
                            help="CAS: fail (exit 3) unless catalog.yaml still "
                                 "carries this revision")
        parser.add_argument("--json", action="store_true")

    pl = psubs.add_parser("list", help="List available profiles")
    pl.add_argument("--pack", default=None)
    pl.add_argument("--category", default=None)
    pl.add_argument("--role", default=None)
    pl.add_argument("--search", dest="q", default=None)
    pl.add_argument("--installed", action="store_true",
                    help="Only entries that already have an installed instance")
    pl.add_argument("--hidden", dest="include_hidden", action="store_true",
                    help="Include entries hidden from the pickers")
    pl.add_argument("--local", dest="local_only", action="store_true",
                    help="Only entries in the plugin's own store")
    pl.add_argument("--root", dest="root_only", action="store_true",
                    help="Instead of catalog entries, list the live profiles "
                         "under the hermes root that `profiles adopt` can take")
    pl.add_argument("--json", action="store_true")
    ps = psubs.add_parser("show", help="Show one catalog entry")
    ps.add_argument("entry")
    ps.add_argument("--json", action="store_true")
    pr = psubs.add_parser("sources",
                          help="The plugin's store + configured pack roots, "
                               "and whether each resolved")
    pr.add_argument("--json", action="store_true")
    pa = psubs.add_parser("add", help="Author a new entry in the plugin's store")
    pa.add_argument("entry", help="Entry id (a valid profile name)")
    pa.add_argument("--soul-file", default=None, metavar="FILE",
                    help="Markdown file written as the entry's SOUL.md")
    pa.add_argument("--display-name", default=None)
    pa.add_argument("--category", default=None)
    pa.add_argument("--description", default=None)
    pa.add_argument("--skill-dir", action="append", default=[], metavar="DIR",
                    help="Skill directory holding SKILL.md (repeatable)")
    pa.add_argument("--version", default=None)
    pa.add_argument("--author", default=None)
    pa.add_argument("--license", default=None)
    pa.add_argument("--replace", action="store_true",
                    help="Overwrite an existing entry in the store")
    _catalog_cas(pa)
    pe = psubs.add_parser(
        "edit",
        help="Change an entry in the plugin's store in place (profiles "
             "already instantiated from it are untouched)")
    pe.add_argument("entry")
    pe.add_argument("--soul-file", default=None, metavar="FILE",
                    help="Markdown file that replaces SOUL.md (an empty file "
                         "removes it)")
    pe.add_argument("--display-name", default=None)
    pe.add_argument("--category", default=None)
    pe.add_argument("--description", default=None)
    pe.add_argument("--version", default=None)
    pe.add_argument("--author", default=None)
    pe.add_argument("--license", default=None)
    pe.add_argument("--skill-dir", action="append", default=[], metavar="DIR",
                    help="Skill directory that replaces skills/<name>/ "
                         "(repeatable)")
    pe.add_argument("--remove-skill", action="append", default=[],
                    metavar="NAME", help="Delete skills/<NAME>/ (repeatable)")
    _catalog_cas(pe)
    pi = psubs.add_parser(
        "import",
        help="Copy entries into the plugin's store from a directory or git "
             "URL: one distribution, or every profile of a pack repo")
    pi.add_argument("source",
                    help="git URL (github.com/user/repo, https://…, git@…) "
                         "or a directory holding distribution.yaml or packs.json")
    pi.add_argument("--only", default=None, metavar="ID,...",
                    help="Pack repos: import only these ids")
    pi.add_argument("--name", default=None, metavar="ID",
                    help="Single distribution: import under this id")
    pi.add_argument("--replace", action="store_true",
                    help="Overwrite entries already in the store")
    pi.add_argument("--dry-run", action="store_true",
                    help="Stage the source and report; write nothing")
    _catalog_cas(pi)
    pad = psubs.add_parser(
        "adopt",
        help="Copy a live profile under the hermes root into the plugin's "
             "store, so it can be used as a blueprint")
    pad.add_argument("profile", help="Name of a live profile (see --root)")
    pad.add_argument("--name", default=None, metavar="ID",
                     help="Store the entry under this id instead")
    pad.add_argument("--replace", action="store_true",
                     help="Overwrite an entry already in the store")
    _catalog_cas(pad)
    prm = psubs.add_parser("remove",
                           help="Delete an entry from the plugin's store")
    prm.add_argument("entry")
    prm.add_argument("--yes", action="store_true", help="Confirm the delete")
    _catalog_cas(prm)
    for verb, text in (("hide", "Keep an entry out of the pickers "
                                "(teams that use it keep resolving it)"),
                       ("unhide", "Put a hidden entry back in the pickers")):
        pv = psubs.add_parser(verb, help=text)
        pv.add_argument("entry")
        _catalog_cas(pv)

    sp = subs.add_parser("recipes", help="Roster presets from recipes.json")
    rsubs = sp.add_subparsers(dest="recipes_command")
    rl = rsubs.add_parser("list", help="List recipes")
    rl.add_argument("--pack", default=None)
    rl.add_argument("--search", dest="q", default=None)
    rl.add_argument("--json", action="store_true")

    sp = subs.add_parser("sync", help="Reconcile desired vs observed (§9); dry run is the default")
    sp.add_argument("team", nargs="?", default=None)
    sp.add_argument("--all", action="store_true", help="Every team in the registry")
    sp.add_argument("--apply", action="store_true",
                    help="Write fixes (§9.3); without it this command never mutates")
    sp.add_argument("--only", default=None, metavar="CHECK,...",
                    help="Restrict to these check ids (comma-separated), "
                         "e.g. member.terminal_cwd,member.plugin_link")
    sp.add_argument("--reinstantiate", action="store_true",
                    help="Allow re-instantiating deleted managed instances (§9.3: never implicit)")
    sp.add_argument("--link-env", action="store_true",
                    help="Allow replacing a seeded placeholder .env with the root symlink")
    sp.add_argument("--create-missing", action="store_true",
                    help="Allow creating the team's missing board and workdir (§9.3)")
    sp.add_argument("--create-board", action="store_true",
                    help="Lift the board.exists gate alone (the remedy strings print this)")
    sp.add_argument("--create-workdir", action="store_true",
                    help="Lift the workdir.exists gate alone (the remedy strings print this)")
    _add_busy_flags(sp)
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("set-board",
                         help="Point a team at a different board, then re-reconcile (§6)")
    sp.add_argument("team")
    sp.add_argument("board")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser("set-workdir",
                         help="Move a team's shared workdir, then re-reconcile (§6)")
    sp.add_argument("team")
    sp.add_argument("workdir")
    sp.add_argument("--apply", action="store_true",
                    help="Apply now (sync --apply --create-workdir): create "
                         "the dir, move the board default_workdir, init git "
                         "(worktree) and every primary member's terminal.cwd")
    _add_busy_flags(sp)
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "merge",
        help="Fast-forward the team's workdir to the result branch of the "
             "card's thread (preview unless --apply)")
    sp.add_argument("team")
    sp.add_argument("card", help="Any card of the thread, usually its root")
    sp.add_argument("--apply", action="store_true",
                    help="Fast-forward now; without it, only preview")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "clone",
        help="Run one roster on a second project: copy roster/roles/policy, "
             "re-declare every binding (S17, §8.1)")
    sp.add_argument("src", help="Team to clone (left byte-unchanged)")
    sp.add_argument("new", help="New team slug")
    sp.add_argument("--workdir", default=None,
                    help="REQUIRED — a workdir is never copied (§8.1, I4)")
    sp.add_argument("--create-workdir", action="store_true")
    sp.add_argument("--board", default=None,
                    help="Board slug (default: <new>; src's board is never copied)")
    sp.add_argument("--create-board", action="store_true")
    sp.add_argument("--allow-shared-board", action="store_true")
    sp.add_argument("--tenant", default=None,
                    help="Tenant label (default: <new>; src's tenant is never copied)")
    sp.add_argument("--orchestrator", default=None,
                    help="Override the copied orchestrator (re-checked against I3)")
    sp.add_argument("--drop-member", action="append", default=[],
                    metavar="PROFILE", help="Drop a member (repeatable)")
    sp.add_argument("--project", default=None,
                    help="Bind a project slug (default: mode none)")
    sp.add_argument("--instantiate", action="store_true",
                    help="Re-provision each member from its blueprint under the "
                         "clone's own naming template — the clone gets its OWN "
                         "<new>-* profiles (§5.7)")
    sp.add_argument("--display-name", default=None)
    sp.add_argument("--description", default=None)
    sp.add_argument("--no-sync", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = subs.add_parser(
        "export",
        help="Dump a team record as YAML (bindings are never re-imported from it)")
    sp.add_argument("team")
    sp.add_argument("-o", dest="out", default=None, metavar="FILE",
                    help="Write to FILE instead of stdout")
    sp.add_argument("--relative", action="store_true",
                    help="Write workdir-rooted paths relative to the workdir "
                         "root (v1.0 documented the flag but never declared it)")

    sp = subs.add_parser(
        "import",
        help="Create a team from an exported YAML file — the clone's rules, "
             "roster from a file (S17)")
    sp.add_argument("file")
    sp.add_argument("--rename", default=None, metavar="NAME",
                    help="Import under a different slug")
    sp.add_argument("--workdir", default=None,
                    help="REQUIRED — a workdir is never copied (§8.1, I4)")
    sp.add_argument("--create-workdir", action="store_true")
    sp.add_argument("--board", default=None,
                    help="Board slug; without it the file's board must be free (I1)")
    sp.add_argument("--create-board", action="store_true")
    sp.add_argument("--allow-shared-board", action="store_true")
    sp.add_argument("--tenant", default=None,
                    help="Tenant label (default: the imported slug)")
    sp.add_argument("--no-sync", action="store_true")
    sp.add_argument("--json", action="store_true")


# ── plugin settings (raw read; no hermes_cli) ────────────────────────────────

def _settings() -> dict:
    """``plugins.entries.hermes-agent-synergia.settings`` from the ROOT config."""
    import yaml
    import hermes_constants
    try:
        cfg_file = hermes_constants.get_default_hermes_root() / "config.yaml"
        if not cfg_file.is_file():
            return {}
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        entry = ((data.get("plugins") or {}).get("entries") or {}).get("hermes-agent-synergia") or {}
        return dict(entry.get("settings") or {})
    except Exception:
        return {}


def _root() -> str:
    import hermes_constants
    return str(hermes_constants.get_default_hermes_root())


# ── handlers ─────────────────────────────────────────────────────────────────

def _cmd_list(args) -> int:
    reg = storage.load()
    teams = [
        {"slug": t.slug, "board": t.board, "workdir": t.workdir,
         "orchestrator": t.orchestrator, "members": len(t.members)}
        for t in reg.teams.values()
    ]
    human = [f"{'slug':<20}{'board':<20}{'orchestrator':<28}members"]
    for row in sorted(teams, key=lambda r: r["slug"]):
        human.append(f"{row['slug']:<20}{row['board']:<20}"
                     f"{row['orchestrator']:<28}{row['members']}")
    if not reg.teams:
        human.append("(no teams)")
    _emit({"teams": teams, "_human": human}, json_mode=args.json)
    return 0


def _cmd_show(args) -> int:
    from . import resolve
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        return _fail(TeamError(
            E_TEAM_NOT_FOUND,
            f"no team {args.team!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        ), json_mode=args.json, team=args.team)
    d = t.to_dict()
    d["_resolved"] = {
        "board_exists": resolve.board_exists(t.board),
        "workdir_exists": __import__("os").path.isdir(t.workdir),
        "members": {m.profile: {"exists": resolve.profile_exists(m.profile),
                                "managed": m.managed}
                    for m in t.members},
    }
    human = [f"team {t.slug}  board={t.board}  workdir={t.workdir}",
             f"  orchestrator: {t.orchestrator}  (revision {t.revision})"]
    for m in t.members:
        flags = "".join(
            c for c, on in (("P", m.primary), ("M", m.managed)) if on)
        human.append(f"  - {m.profile:<32} role={m.role:<14} "
                     f"{'exists' if d['_resolved']['members'][m.profile]['exists'] else 'MISSING':<8}{flags}")
    _emit({"team": d, "_human": human}, json_mode=args.json)
    return 0


def _default_orchestrator(members: list) -> str:
    for m in members:
        if m.get("role") == "orchestrator":
            return m["profile"]
    raise TeamError(
        E_TEAM_INVALID,
        "no member has role=orchestrator and --orchestrator was not given",
        hint="A team needs an orchestrator (I3). Pass --orchestrator PROFILE or "
             "--member PROFILE:orchestrator.",
        remedy="hermes team create --help",
    )


def _cmd_create(args) -> int:
    """CLI wrapper over :func:`_create_impl` (S19 factoring: the dashboard
    POST /teams route needs the payload, not stdout — one body, no fork)."""
    try:
        payload = _create_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "team", None))
    _emit(payload, json_mode=args.json)
    # §8.2 exit 4: the record stands but the team cannot take work yet. A
    # --no-sync create defers the setup on purpose, so it is not a failure.
    ready = payload.get("ready")
    if ready and not ready["ready"] and not getattr(args, "no_sync", False):
        return 4
    return 0


def _create_impl(args) -> dict:
    """Spec §5.5 sequence, in order. Registry write is LAST.

    Returns the success payload (what the CLI's ``--json`` envelope wraps in
    ``data``); raises :class:`TeamError` on every refusal. The dry-run stop
    returns the plan payload without writing anything.
    """
    slug = model._normalize_profile_name(args.team)

    naming = (args.profile_naming
              or _settings().get("default_profile_naming")
              or model.DEFAULT_PROFILE_NAMING)

    # 1/2. catalog resolution + name rendering (§5.5; writes nothing).
    from_members, from_triples = _plan_from_specs(args, slug, naming)
    roster_members, roster_triples, roster_main = _plan_roster(
        getattr(args, "roster", None), slug, naming, dry_run=args.dry_run)
    from_members += roster_members
    from_triples += roster_triples

    # 3. --member specs (adopted, managed: false). The FIRST team to
    # claim a profile owns plane 1, so an adopted member is primary iff no
    # team already in the registry claims that profile as primary; later
    # teams share it as primary: false (I2 keeps the pair consistent).
    # Managed --from members are always primary (S9).
    reg_before = storage.load()

    def _adopt_primary(profile_name: str) -> bool:
        return not any(m.primary
                       for t in reg_before.teams.values()
                       for m in t.members
                       if m.profile == profile_name)

    members = list(from_members)
    for s in args.member:
        spec = parse_member_spec(s)
        spec["primary"] = _adopt_primary(spec["profile"])
        members.append(model.Member.from_dict(spec))

    if not members:
        raise TeamError(
            E_TEAM_INVALID,
            "no members: pass --member PROFILE[:ROLE[:CAPACITY]] or --from ENTRY",
            hint="A team needs at least one member.",
            remedy="hermes team create --help",
        )
    specs = [m.to_dict() for m in members]

    # 3b. render/normalize names (no-op for adopted members; done in S6b pass)
    members = [model.Member.from_dict(s) for s in specs]
    team_draft = model.Team.from_dict({
        "slug": slug,
        "display_name": args.display_name or slug,
        "description": args.description or "",
        "revision": 1,
        "created_at": model.now_iso(),
        "updated_at": model.now_iso(),
        "board": args.board or slug,
        "shared_board": bool(args.allow_shared_board),
        "workdir": args.workdir or "",
        "workspace_policy": args.workspace_policy,
        "tenant": slug,
        "tenant_stamping": "auto",
        "profile_naming": naming,
        "orchestrator": (args.orchestrator or roster_main
                         or _default_orchestrator(specs)),
        "members": specs,
        "policy": {"mode": args.policy or _settings().get("default_policy", "advise")},
        "sync": {},
        "metadata": {},
    })

    # 4-12. validate, invariants, instantiate, adopt, board, workdir, write,
    # reconcile — the shared tail, verbatim (S17: clone/import join here).
    # create must leave the team ready for work, so the board, the workdir
    # and the embedded sync are not opt-in here (clone/import keep their flags).
    return _finish_team(
        team_draft, from_triples, slug=slug,
        create_board=True, create_workdir=True,
        create_missing=args.create_missing,
        clone_from=args.clone_from, clone_secrets=args.clone_secrets,
        link_env=args.link_env, dry_run=args.dry_run,
        expected_revision=getattr(args, "expected_revision", None),
        no_sync=args.no_sync,
    )


def _finish_team(team_draft, from_triples, *, slug: str,
                 create_board: bool, create_workdir: bool,
                 create_missing: bool = False,
                 clone_from=None, clone_secrets: bool = False,
                 link_env: bool = False, dry_run: bool = False,
                 expected_revision=None, no_sync: bool = False) -> dict:
    """:func:`_create_impl` steps 4-12, shared verbatim by ``create``,
    ``clone`` and ``import`` (§5.7: "…identical to ``team create`` from
    here"). Registry write is LAST; raises :class:`TeamError` on every
    refusal; returns the success payload (same keys as ``create --json``)."""
    import dataclasses
    import os
    from . import resolve

    # 4. validate (§4.2) — before touching disk.
    model.validate_team(team_draft, _root())

    # 5/6. CAS snapshot + invariants (I1–I7).
    reg = storage.load()
    if slug in reg.teams:
        # create is not an editor. Without this the upsert below merges, and a
        # re-run silently replaces a live team's roster, board and workdir
        # while exiting 0 (Appendix C E_TEAM_EXISTS exists for exactly this).
        existing = reg.teams[slug]
        raise TeamError(
            E_TEAM_EXISTS,
            f"team {slug!r} already exists (revision {existing.revision}, "
            f"board {existing.board!r}, {len(existing.members)} member(s))",
            hint="`create` never edits an existing team — that would replace "
                 "its roster and bindings in place.",
            remedy=f"hermes team show {slug}   # or choose another slug, or: "
                   f"hermes team destroy {slug} --yes",
        )
    # Not an I-invariant in check_invariants: add-member/set-* re-check the
    # whole record, and registries that predate this rule may hold duplicates.
    clash = model.name_conflict(reg, slug, team_draft.display_name)
    if clash:
        other = reg.teams[clash]
        raise TeamError(
            E_TEAM_EXISTS,
            f"team name {team_draft.display_name or slug!r} is taken by team "
            f"{clash!r} ({other.display_name or clash!r})",
            hint="Team names are unique: names that differ only in case, "
                 "spacing or punctuation are the same name.",
            remedy=f"choose another name   # or: hermes team show {clash}",
        )
    errs = model.check_invariants(reg, team_draft)
    if errs:
        raise errs[0]

    # 6b/7. --dry-run STOPS HERE, having written nothing.
    if dry_run:
        return {
            "team": team_draft.to_dict(),
            "plans": [t[2] for t in from_triples if t[2]],
            "dry_run": True,
            "_human": [f"dry run: team {slug} would be created with "
                       f"{len(team_draft.members)} members; nothing written"],
        }

    # 8. instantiate --from members (registry write stays LAST; §5.5).
    instantiated = _instantiate_from(from_triples, slug, link_env=link_env)

    # 8. adopted members must exist (or --create-missing).
    adopted = [m.profile for m in team_draft.members if not m.managed]
    dangling = resolve.profiles_exist(adopted)
    if dangling and not create_missing:
        raise TeamError(
            E_TEAM_NO_PROFILE,
            "member profile(s) do not exist: " + ", ".join(sorted(dangling)),
            hint="Either profiles exist or the team adopts phantoms. Create them "
                 "with --create-missing or drop the members.",
            remedy=f"hermes team create {slug} ... --create-missing",
        )
    if dangling:
        cf = clone_from or None
        if clone_secrets:
            print(f"WARNING: --clone-secrets copies credentials from profile "
                  f"{cf or '(the active profile)'!r}.", file=sys.stderr)
        for p in dangling:
            role = next((m.role for m in team_draft.members if m.profile == p), "member")
            resolve.create_profile_for(p, clone_from=cf,
                                       clone_config=clone_secrets,
                                       description=f"created by hermes team create: role {role}")

    # 9. board: exists or --create-board.
    if not resolve.board_exists(team_draft.board):
        if not create_board:
            raise TeamError(
                E_TEAM_NO_BOARD,
                f"board {team_draft.board!r} does not exist",
                hint="Create it with --create-board or pick an existing board.",
                remedy=f"hermes team create {slug} ... --create-board",
            )
        resolve.ensure_board(team_draft.board, name=model.board_label(team_draft),
                             default_workdir=team_draft.workdir)

    # 10. workdir: mkdir -p with --create-workdir; else must exist.
    wd = os.path.normpath(os.path.expanduser(team_draft.workdir))
    if not os.path.isdir(wd):
        if create_workdir:
            os.makedirs(wd, exist_ok=True)
        else:
            raise TeamError(
                E_TEAM_INVALID,
                f"workdir {team_draft.workdir!r} does not exist",
                hint="Pass --create-workdir to create it.",
                remedy=f"hermes team create {slug} ... --create-workdir",
            )

    # 10b. worktree tasks branch off HEAD: init + snapshot the workdir, also
    # under --no-sync (the reconcile below would do it otherwise). When git
    # refuses, the team falls back to scratch instead of failing the create.
    git_inited = False
    warning = ""
    if team_draft.workspace_policy in ("worktree", "shared"):
        try:
            git_inited = resolve.ensure_worktree_repo(wd)
        except TeamError as exc:
            warning = (f"worktree setup failed ({exc.message}); team {slug} "
                       "uses workspace_policy scratch")
            print(f"WARNING: {warning}", file=sys.stderr)
            team_draft = dataclasses.replace(team_draft, workspace_policy="scratch")

    # 11. registry write (last). S19 g1 / fix 1c: CAS with the step-5
    # snapshot's revision — never None — so an operator racing the CLI and
    # the dashboard cannot silently lose a team (every other write path
    # already passes reg.revision). A dashboard body's expected_revision
    # wins when supplied.
    stored = storage.upsert(team_draft,
                            expected_revision=(reg.revision if expected_revision
                                               is None else expected_revision))

    # 12. reconcile (S11): a create without --no-sync IS a write verb, so the
    # embedded run applies (the create's own --create-board/--create-workdir/
    # --link-env flags lift the matching §9.3 gates). The reconciled report
    # rides along in the payload; a team created with --no-sync simply reports
    # its drift on the first ``sync``.
    from . import reconcile
    reconciled = None
    human_tail: list = []
    residual = None
    if not no_sync:
        report = reconcile.apply(
            stored, reinstantiate=False,
            link_env=bool(link_env),
            create_board=bool(create_board),
            create_workdir=bool(create_workdir),
        )
        reconciled = report.to_dict()
        human_tail = _sync_table(report)
        residual = report.drift

    # 13. readiness: can a dispatched worker take a card right now?
    ready = reconcile.readiness(stored, residual)
    if ready["ready"]:
        ready_lines = [f"ready for tasks: board {stored.board}, "
                       f"orchestrator {stored.orchestrator}"]
    else:
        ready_lines = [f"NOT ready for tasks ({len(ready['blocking'])} blocking):"]
        ready_lines += [f"  {b['check']} {b['subject']}: {b['remedy']}"
                        for b in ready["blocking"]]

    payload = {
        "team": stored.to_dict(),
        "reconciled": reconciled,
        "instantiated": instantiated,
        "ready": ready,
        "_human": [f"team {slug} created (revision {stored.revision})"
                   + (f"; instantiated {', '.join(instantiated)}"
                      if instantiated else "")]
                  + ([f"git: initialized {wd}"] if git_inited else [])
                  + ([f"WARNING: {warning}"] if warning else [])
                  + human_tail + ready_lines,
    }
    if warning:
        payload["warning"] = warning
    return payload


def _plan_from_specs(args, slug: str, naming: str):
    """§5.5 steps 1/2 only: catalog resolution + name rendering.

    Returns ``(draft_members, triples)`` where triples are
    ``(member, entry, plan_or_None)``; plans are computed (staged into a
    temp dir, nothing written) only under ``--dry-run``. Instantiation
    happens after invariants + the dry-run stop.
    """
    specs = getattr(args, "from_specs", None) or []
    recipe = getattr(args, "from_recipe", None)
    if not specs and not recipe:
        return [], []
    from . import catalog
    from . import provision

    entries = []                                  # (id, role, cap, name_override)
    for kind, value in specs:
        if kind == "name":
            # binds to the PRECEDING --from (spec §8.1)
            if entries and entries[-1][3] is None:
                entries[-1] = entries[-1][:3] + (value,)
            else:
                raise TeamError(
                    E_TEAM_INVALID,
                    "--name must follow a --from it overrides",
                    hint="Pair them: --from ENTRY --name PROFILE.",
                    remedy="hermes team create --help",
                )
            continue
        spec = parse_member_spec(value)
        entries.append((spec["profile"], spec.get("role"),
                        spec.get("capacity"), None))
    if recipe:
        rs = {r["id"]: r for r in catalog.recipes()}
        if recipe not in rs:
            raise TeamError(
                E_TEAM_INVALID, f"no recipe {recipe!r}",
                hint="List them: hermes team recipes list.",
                remedy="hermes team recipes list --json",
            )
        tier = getattr(args, "tier", "recommended")
        roster = rs[recipe]["tiers"].get(tier)
        if not roster:
            raise TeamError(
                E_TEAM_INVALID, f"recipe {recipe!r} has no {tier!r} tier",
                hint="Tiers: " + ", ".join(sorted(rs[recipe]["tiers"])),
                remedy=f"hermes team create ... --from-recipe {recipe} --tier minimal",
            )
        for entry_id in roster:
            entries.append((entry_id, None, None, None))

    members, triples = [], []
    for entry_id, role, cap, name_override in entries:
        entry = catalog.get(entry_id)
        role = role or entry.suggested_role or "member"
        name = (model._normalize_profile_name(name_override) if name_override
                else provision.render_name(slug, entry, naming))
        draft = model.Member.from_dict({
            "profile": name, "role": role, "capacity": cap or 1,
            "blueprint": entry.id,
            "blueprint_source": f"pack:{entry.pack}",
            "managed": True, "primary": True,
        })
        plan = provision.plan(entry, name) if args.dry_run else None
        members.append(draft)
        triples.append((draft, entry, plan))
    return members, triples


def _apply_default_model(draft: dict, default) -> None:
    """A member without its own model takes *default* (the root config's).
    ``base_url`` rides along whenever the member's provider is the
    default's: a bare ``custom`` provider is unusable without it."""
    if not str(draft.get("model") or "").strip() and default.get("model"):
        draft["model"] = default["model"]
        if default.get("provider"):
            draft["provider"] = default["provider"]
        else:
            draft.pop("provider", None)
    if draft.get("model") and draft.get("provider") and default.get("base_url") \
            and draft["provider"] == default.get("provider"):
        draft["base_url"] = default["base_url"]


def _roster_invalid(message: str) -> TeamError:
    return TeamError(
        E_TEAM_INVALID, message,
        hint="A roster is a list of catalog profiles; exactly one carries "
             "main: true and becomes the orchestrator (I3).",
        remedy='roster: [{"blueprint": "<entry-id>", "role": "…", '
               '"main": true}, {"blueprint": "<entry-id>"}]',
    )


def _plan_roster(roster, slug: str, naming: str, *, dry_run: bool):
    """The dashboard's roster — catalog profiles picked in the New Team
    dialog — rendered into the same ``(draft, entry, plan)`` triples
    ``--from`` builds, so provisioning, dry-run plans and resumable create
    are shared. Returns ``(members, triples, main_profile)``.

    ``skills``/``model``/``provider`` land on the draft for reconcile to
    project; a member naming no model is stamped with the root config's.
    """
    if not roster:
        return [], [], ""
    from . import catalog, provision, resolve
    mains = sum(1 for row in roster if row.get("main"))
    if mains != 1:
        raise _roster_invalid(
            f"roster has {mains} orchestrator(s); exactly one member must "
            "carry main: true")
    default = resolve.default_model()
    members, triples, main = [], [], ""
    for row in roster:
        bp = str(row.get("blueprint") or "").strip()
        if not bp:
            raise _roster_invalid("a roster member has no blueprint")
        entry = catalog.get(bp)                   # E_TEAM_NO_BLUEPRINT
        role = str(row.get("role") or "").strip()
        if not role or role == "member":
            role = entry.suggested_role or "member"
        try:
            capacity = int(row.get("capacity") or 1)
        except (TypeError, ValueError):
            raise _roster_invalid(
                f"roster member {bp!r}: capacity must be an integer") from None
        d = {"profile": provision.render_name(slug, entry, naming),
             "role": role, "capacity": capacity,
             "blueprint": entry.id, "blueprint_source": f"pack:{entry.pack}",
             "managed": True, "primary": True}
        # Same truthiness rule as the add-member picker: [] means "not
        # managed", never "disable every skill".
        if isinstance(row.get("skills"), list) and row["skills"]:
            d["skills"] = list(row["skills"])
        for key in ("model", "provider"):
            if str(row.get(key) or "").strip():
                d[key] = str(row[key]).strip()
        _apply_default_model(d, default)
        draft = model.Member.from_dict(d)
        members.append(draft)
        triples.append((draft, entry,
                        provision.plan(entry, draft.profile) if dry_run else None))
        if row.get("main"):
            main = draft.profile
    return members, triples, main


def _instantiate_from(triples, slug: str, *, link_env: bool = False) -> list:
    """Create each instance (§5.5 step 8). Created instances are kept on
    failure; a re-run adopts them (resumable create)."""
    if not triples:
        return []
    from . import provision
    created = []
    try:
        for draft, entry, _plan in triples:
            provision.instantiate(entry, draft.profile, role=draft.role)
            created.append(draft.profile)
    except TeamError:
        raise
    except Exception as exc:
        raise TeamError(
            E_TEAM_PROVISION_FAILED,
            f"provisioning failed: {exc}",
            hint="Already-created instances are KEPT; re-running the same "
                 "command adopts them and creates only the rest (§5.5).",
            remedy=f"hermes team create {slug} ...  (identical re-run)",
        ) from exc
    if link_env:
        for draft, _e, _p in triples:
            provision.link_env(draft.profile)
    return created


def _cmd_destroy(args) -> int:
    """CLI wrapper over :func:`_destroy_impl` (S19 factoring for the
    dashboard DELETE route — one body, no fork)."""
    try:
        payload = _destroy_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "team", None))
    _emit(payload, json_mode=args.json)
    return 0


BUSY_MODES = ("refuse", "stop", "wait")
BUSY_WAIT_TIMEOUT = 1800
_BUSY_POLL_SECONDS = 5


def _add_busy_flags(sp) -> None:
    sp.add_argument("--on-busy", choices=BUSY_MODES, default="refuse",
                    help="When team cards are running: refuse (default), "
                         "stop them (block + kill the worker), or wait for "
                         "them to finish")
    sp.add_argument("--wait-timeout", type=int, default=BUSY_WAIT_TIMEOUT,
                    metavar="SECONDS",
                    help=f"--on-busy wait gives up after this long "
                         f"(default {BUSY_WAIT_TIMEOUT})")


def _busy_error(team, cards, remedy: str, *, waited=None) -> TeamError:
    listing = "; ".join(f"{c['id']} {c['title']!r} ({c['assignee'] or 'unassigned'})"
                        for c in cards)
    state = f"still busy after {waited}s" if waited is not None else "busy"
    return TeamError(
        E_TEAM_BUSY,
        f"team {team.slug} is {state}: {len(cards)} running card(s): {listing}",
        hint="Stop the running cards now (--on-busy stop: each card is "
             "blocked and its worker killed), or wait for them to finish "
             "(--on-busy wait [--wait-timeout SECONDS]).",
        remedy=f"{remedy} --on-busy stop   # or --on-busy wait",
    )


def _ensure_idle(team, op: str, *, remedy: str, assignee=None,
                 on_busy: str = "refuse", timeout: int = BUSY_WAIT_TIMEOUT,
                 sleep=None, clock=None) -> dict:
    """Guard a change to *team* against its running cards (or *assignee*'s
    alone): refuse with E_TEAM_BUSY, stop them, or wait until none run.

    Wait re-reads the board each poll, so a card the dispatcher starts
    meanwhile keeps the team busy: the change never cuts across a run."""
    import time
    from . import resolve
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    idle = {"stopped": [], "not_killed": []}
    cards = resolve.running_cards(team.board, assignee)
    if not cards:
        return idle
    if on_busy == "stop":
        return resolve.stop_cards(team.board, [c["id"] for c in cards],
                                  f"stopped by hermes team {op}")
    if on_busy != "wait":
        raise _busy_error(team, cards, remedy)
    deadline = clock() + timeout
    shown = None
    while cards:
        ids = [c["id"] for c in cards]
        if ids != shown:
            print(f"waiting for {len(ids)} running card(s) on board "
                  f"{team.board}: {', '.join(ids)}", file=sys.stderr)
            shown = ids
        if clock() >= deadline:
            raise _busy_error(team, cards, remedy, waited=timeout)
        sleep(_BUSY_POLL_SECONDS)
        cards = resolve.running_cards(team.board, assignee)
    return idle


def _busy_fields(busy: dict, resumed=None) -> tuple:
    """``(payload fields, human lines)`` for what a guard stopped/resumed;
    empty when the team was idle, so idle payloads keep their old shape."""
    fields: dict = {}
    lines: list = []
    if busy["stopped"]:
        fields["stopped"] = busy["stopped"]
        lines.append("stopped running card(s): " + ", ".join(busy["stopped"]))
    if busy["not_killed"]:
        fields["not_killed"] = busy["not_killed"]
        lines.append("worker not killed (another host, or still alive): "
                     + ", ".join(busy["not_killed"]))
    if resumed:
        fields["resumed"] = resumed
        lines.append("resumed card(s): " + ", ".join(resumed))
    return fields, lines


def _destroy_impl(args) -> dict:
    """Delete one team record and its managed profiles (unless
    --keep-profiles). Adopted profiles are never deleted."""
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"no team {args.team!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    if args.purge_board and not args.yes:
        raise TeamError(
            E_TEAM_INVALID,
            f"--purge-board requires --yes (wanted to purge board {t.board!r})",
            hint="Destructive purge: confirm with --yes.",
            remedy=f"hermes team destroy {t.slug} --yes --purge-board",
        )
    managed = [m.profile for m in t.members if m.managed]
    if args.purge_profiles and managed and not args.yes:
        raise TeamError(
            E_TEAM_INVALID,
            f"destroy deletes managed profiles {', '.join(managed)} — "
            "requires --yes",
            hint="Confirm with --yes, or keep the profiles with --keep-profiles.",
            remedy=f"hermes team destroy {t.slug} --yes   # or --keep-profiles",
        )
    busy = _ensure_idle(
        t, "destroy",
        remedy=(f"hermes team destroy {t.slug} --yes"
                + (" --purge-board" if args.purge_board else "")
                + ("" if args.purge_profiles else " --keep-profiles")),
        on_busy=getattr(args, "on_busy", "refuse"),
        timeout=getattr(args, "wait_timeout", BUSY_WAIT_TIMEOUT))
    purged = []
    skipped_purge: list = []
    if args.purge_board:
        import shutil
        import hermes_constants
        # root-anchored: board dirs live at <root>/kanban/boards/<slug>
        board_dir = (hermes_constants.get_default_hermes_root()
                     / "kanban" / "boards" / t.board)
        print(f"removing board directory {board_dir}", file=sys.stderr)
        shutil.rmtree(board_dir, ignore_errors=True)
        purged.append(f"board:{t.board}")
    if args.purge_profiles:
        done, skipped_purge = _purge_managed_members(t)
        purged.extend(done)
    storage.delete(t.slug, expected_revision=reg.revision)
    fields, lines = _busy_fields(busy)
    payload = {"destroyed": t.slug, "purged": purged, **fields,
               "_human": lines + [f"team {t.slug} destroyed"
                                  + (f" (purged: {', '.join(purged)})" if purged else "")]}
    if skipped_purge:
        payload["skipped_purge"] = skipped_purge
    return payload


def _purge_managed_members(team) -> tuple:
    """Delete only managed members (I7); adopted members are skipped.

    S19: returns ``(done, skipped)`` so the dashboard DELETE payload can
    report the skipped set as data, not just a stderr aside (g5).
    """
    try:
        from . import provision
    except ImportError:
        provision = None
    done, skipped = [], []
    for m in team.members:
        if not m.managed:
            skipped.append(m.profile)
            continue
        if provision is None:
            skipped.append(m.profile + " (no provision module)")
            continue
        provision.delete(m)
        done.append(f"profile:{m.profile}")
    if skipped:
        print("skipped (not managed by this team): " + ", ".join(skipped),
              file=sys.stderr)
    return done, skipped


def _cmd_add_member(args) -> int:
    """CLI wrapper over :func:`_add_member_impl` (S19 factoring for the
    dashboard POST /members route — one body, no fork)."""
    try:
        payload = _add_member_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "team", None))
    _emit(payload, json_mode=args.json)
    return 0


def _add_member_impl(args) -> dict:
    """Adopt an existing profile as a member (adopted-only path; the
    managed/blueprint path lives in the dashboard's jobs runner, which
    reuses ``catalog``/``provision`` directly)."""
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"no team {args.team!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    profile = model._normalize_profile_name(args.profile)
    new_member = {"profile": profile, "role": args.role,
                  "primary": bool(args.primary)}
    if args.capacity is not None:
        new_member["capacity"] = args.capacity
    specs = [m.to_dict() for m in t.members]
    if any(s["profile"] == profile for s in specs):
        raise TeamError(
            E_TEAM_INVALID,
            f"profile {profile!r} is already a member of team {t.slug}",
            hint="Remove it first: hermes team remove-member "
                 f"{t.slug} {profile}",
            remedy="hermes team remove-member --help",
        )
    candidate = model.Team.from_dict({**t.to_dict(),
                                      "members": specs + [new_member]})
    errs = model.check_invariants(reg, candidate)
    if errs:
        raise errs[0]
    if args.dry_run:
        return {"team": candidate.to_dict(), "dry_run": True,
                "_human": [f"dry run: would add {profile} to {t.slug}"]}
    from . import resolve
    if not resolve.profile_exists(profile):
        raise TeamError(
            E_TEAM_NO_PROFILE,
            f"profile {profile!r} does not exist",
            hint="Adopted members must exist. Create it first, or use --from "
                 "once the catalog ships.",
            remedy=f"hermes profile create {profile} --clone",
        )
    # S19b F2 (g1): a client-supplied expected_revision (the dashboard's
    # adopt job threads the body's value through) wins over this run's own
    # fresh snapshot — otherwise a registry move during the RUN_LOCK queue
    # window would silently pass. Same discipline as fix 1c on create.
    _exp = getattr(args, "expected_revision", None)
    stored = storage.upsert(candidate,
                            expected_revision=(reg.revision if _exp is None
                                               else _exp))
    return {"team": stored.to_dict(),
            "_human": [f"added {profile} to {t.slug} (revision {stored.revision})"]}


def _cmd_remove_member(args) -> int:
    """CLI wrapper over :func:`_remove_member_impl` (S19 factoring for the
    dashboard DELETE /members route — one body, no fork)."""
    try:
        payload = _remove_member_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "team", None))
    _emit(payload, json_mode=args.json)
    return 0


def _remove_member_impl(args) -> dict:
    """Remove one member from a team; optional ``delete_profile`` is
    managed-only — an adopted member is reported as skipped, never deleted."""
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        raise TeamError(
            E_TEAM_NOT_FOUND, f"no team {args.team!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    profile = model._normalize_profile_name(args.profile)
    member = next((m for m in t.members if m.profile == profile), None)
    if member is None:
        raise TeamError(
            E_TEAM_NOT_MEMBER,
            f"{profile} is not a member of team {t.slug}",
            hint="Members: " + ", ".join(m.profile for m in t.members),
            remedy=f"hermes team show {t.slug}",
        )
    remaining = [m.to_dict() for m in t.members if m.profile != profile]
    if not remaining:
        raise TeamError(
            E_TEAM_INVALID,
            "refusing to remove the last member",
            hint="A team with no roster is a typo. Use hermes team destroy.",
            remedy=f"hermes team destroy {t.slug} --yes",
        )
    if t.orchestrator == profile and not any(m.get("role") == "orchestrator" for m in remaining):
        raise TeamError(
            E_TEAM_INVALID,
            f"cannot remove {profile}: it is the orchestrator",
            hint="Repoint the orchestrator first, e.g. "
                 f"hermes team add-member {t.slug} <new-orch> --role orchestrator",
            remedy=f"hermes team show {t.slug}",
        )
    candidate = model.Team.from_dict({**t.to_dict(), "members": remaining})
    candidate = candidate  # orchestrator unchanged; I3 re-checked by validate
    model.validate_team(candidate, _root())
    busy = _ensure_idle(
        t, "remove-member", assignee=profile,
        remedy=(f"hermes team remove-member {t.slug} {profile}"
                + (" --delete-profile" if args.delete_profile else "")),
        on_busy=getattr(args, "on_busy", "refuse"),
        timeout=getattr(args, "wait_timeout", BUSY_WAIT_TIMEOUT))
    deleted = None
    skipped_delete = None
    if args.delete_profile:
        if not member.managed:
            print(f"skipped --delete-profile: {profile} is an adopted member "
                  "(managed: false) — a team command never deletes what it did "
                  "not create.", file=sys.stderr)
            skipped_delete = profile
        else:
            from . import provision
            provision.delete(member)
            deleted = profile
    stored = storage.upsert(candidate, expected_revision=reg.revision)
    fields, lines = _busy_fields(busy)
    payload = {"team": stored.to_dict(), "deleted_profile": deleted, **fields,
               "_human": lines + [f"removed {profile} from {t.slug} (revision {stored.revision})"
                                  + (f"; deleted profile {deleted}" if deleted else "")]}
    if skipped_delete is not None:
        payload["skipped_delete_profile"] = skipped_delete
    return payload


def _naming_pattern(slug: str, template: str):
    """Regex matching the instance names a team's naming template renders.

    ``{team}-{short}`` for team ``px`` matches ``px-<anything>`` — the handle
    on the §5.5 interrupted-create case, where the profiles exist but the
    registry record was never written (the registry write is LAST).
    """
    import re
    parts = (template or model.DEFAULT_PROFILE_NAMING).split("{short}")
    body = ".+".join(re.escape(part.replace("{team}", slug)) for part in parts)
    return re.compile("^" + body + "$")


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} B"


def _cmd_doctor(args) -> int:
    """CLI wrapper over :func:`_doctor_impl` (S19 factoring for the
    dashboard GET /doctor route — one body, no fork)."""
    payload = _doctor_impl(args)
    payload["_human"] = _doctor_human(payload)
    _emit(payload, json_mode=args.json)
    return 0


# The three lanes nothing in the substrate ever moves on its own. doctor is
# where an operator looks when "nothing is happening", so each one says what
# holds the card, not just that it is parked.
_DOCTOR_LANE_WHY = {
    "review": "finished and waiting for a human verdict; with "
              "kanban.review_dispatch off no agent run will ever pick them up",
    "triage": "routed to triage after repeated blocks for the same cause; "
              "nothing schedules a triage card",
    "guarded": "held back by the dispatcher's respawn guard; it will not "
               "start them again until the cause is cleared",
}


def _doctor_impl(args) -> dict:
    """Registry-wide invariant report (§8.2 data block)."""
    import hermes_constants
    from . import resolve
    reg = storage.load()
    root = str(hermes_constants.get_default_hermes_root())
    findings = []   # {severity, check, message[, remedy]}

    # I1–I7, per team (I3/I5 via validate; others via check_invariants).
    for slug, t in sorted(reg.teams.items()):
        try:
            model.validate_team(t, root)
        except TeamError as exc:
            findings.append({"severity": "error", "check": f"validate:{slug}",
                             "message": str(exc)})
        for exc in model.check_invariants(reg, t):
            findings.append({"severity": "error", "check": f"invariants:{slug}",
                             "message": str(exc)})
        try:
            resolve.require_worktree_workdir(t)
        except TeamError as exc:
            findings.append({"severity": "error", "check": "workdir.git",
                             "message": exc.message, "remedy": exc.remedy})

    # A card parked on a human stalls the team silently — doctor is where
    # an operator looks when "nothing is happening", so the queue is reported
    # there. Messenger push only exists if a platform has a home channel, so
    # say plainly when it does not rather than let the operator assume pings.
    from . import approvals
    waiting_total = 0
    stuck_total = 0
    parked_total = dict.fromkeys(approvals.QUEUE_KINDS, 0)
    for slug, t in sorted(reg.teams.items()):
        lanes = approvals.lane_counts(t)
        for name, n in lanes.items():
            parked_total[name] = parked_total.get(name, 0) + n
        for name in ("review", "triage", "guarded"):
            if lanes.get(name):
                findings.append({
                    "severity": "warn", "check": f"parked_{name}",
                    "message": f"team {slug!r} has {lanes[name]} card(s) "
                               f"{_DOCTOR_LANE_WHY[name]}",
                    "remedy": f"hermes team approvals {slug} --kind {name}"})
        waiting = approvals.count_pending(t)
        waiting_total += waiting
        if waiting:
            findings.append({
                "severity": "warn", "check": "approvals_waiting",
                "message": f"team {slug!r} has {waiting} card(s) waiting on a "
                           "human decision; its work is stalled until they are "
                           "answered",
                "remedy": f"hermes team approvals {slug}"})
        # A stuck card has nobody to answer it: the dispatcher gave up, or an
        # agent parked it on a capability it lacks. Nothing moves it back on
        # its own, so it stalls the same way but silently.
        cards_stuck = approvals.count_stuck(t)
        stuck_total += cards_stuck
        if cards_stuck:
            findings.append({
                "severity": "warn", "check": "stuck_cards",
                "message": f"team {slug!r} has {cards_stuck} stuck card(s) "
                           "(blocked with nobody to answer); nothing retries "
                           "them until they are released",
                "remedy": f"hermes team approvals {slug} --kind stuck"})
    if (waiting_total or stuck_total) and not approvals.configured_home_channels():
        findings.append({
            "severity": "info", "check": "approvals_notify",
            "message": "no gateway platform has a home channel, so approval "
                       "requests and stuck cards are not pushed to any "
                       "messenger — they are only visible in the CLI and the "
                       "dashboard",
            "remedy": "set one from the messenger via /sethome, or configure "
                      "gateway.platforms.<platform>.home_channel in config.yaml"})

    # ownership view: who is primary where, who is primary nowhere.
    primary_of: dict = {}
    seen_profile: dict = {}
    roles_of: dict = {}
    for slug, t in reg.teams.items():
        for m in t.members:
            seen_profile.setdefault(m.profile, []).append(slug)
            roles_of.setdefault(m.profile, {}).setdefault(m.role, []).append(slug)
            if m.primary:
                primary_of.setdefault(m.profile, []).append(slug)
    multi = {p: slugs for p, slugs in primary_of.items() if len(slugs) > 1}
    no_primary = sorted(p for p in seen_profile if p not in primary_of)
    for p, slugs in sorted(multi.items()):
        findings.append({"severity": "error", "check": "I2",
                         "message": f"profile {p!r} claims primary in "
                                    f"{', '.join(sorted(slugs))}"})
    for p in no_primary:
        findings.append({
            "severity": "warn", "check": "primary_none",
            "message": f"profile {p!r} has no primary team; its terminal.cwd is "
                       "owned by nobody",
            "remedy": f"hermes team add-member <team> {p} --primary"})

    # Every multi-team profile with its primary owner (the whole
    # list — not just the conflicting ones).
    shared_profiles = [
        {"profile": p, "teams": sorted(slugs),
         "primary": (sorted(primary_of.get(p, [])) or [None])[0]}
        for p, slugs in sorted(seen_profile.items()) if len(slugs) > 1
    ]

    # A member whose role differs across teams is info —
    # roster drift between clones is real and unmanaged, and this report is the
    # trigger detector for Q6 (template/instance split).
    for p, by_role in sorted(roles_of.items()):
        if len(by_role) > 1:
            detail = "; ".join(f"{role} in {', '.join(sorted(slugs))}"
                               for role, slugs in sorted(by_role.items()))
            findings.append({
                "severity": "info", "check": "role_drift",
                "message": f"profile {p!r} holds different roles across teams: {detail}",
                "remedy": "clones drift by hand; align with "
                          "hermes team add-member <team> <profile> --role <role>"})

    # Instance sprawl is MEASURED, not hidden.
    managed_instances = []
    for slug, t in sorted(reg.teams.items()):
        for m in t.members:
            if not m.managed:
                continue
            exists = resolve.profile_exists(m.profile)
            managed_instances.append({
                "profile": m.profile, "team": slug, "exists": exists,
                "bytes": resolve.profile_footprint_bytes(m.profile) if exists else 0,
            })
    managed_count = len(managed_instances)
    managed_bytes = sum(i["bytes"] for i in managed_instances)

    # §5.5 interrupted create: instances on disk that no registry record claims.
    # Two signals, because the registry write is LAST — after a failed create
    # there is no team record whose template could match, and the only evidence
    # left is the F1 plumbing (a symlink only THIS plugin installs) on a profile
    # carrying pack provenance.
    claimed = {m.profile for t in reg.teams.values() for m in t.members}
    patterns = [(slug, _naming_pattern(slug, t.profile_naming))
                for slug, t in sorted(reg.teams.items())]
    unclaimed = []
    for info in resolve.profile_inventory():
        name = info["name"]
        if name in claimed:
            continue
        source = resolve.distribution_source(name)
        provisioned = bool(source) and resolve.plugin_link_present(name)
        matched = [slug for slug, rx in patterns if rx.match(name)]
        if not (provisioned or matched):
            continue
        why = ("carries pack provenance and this plugin's per-profile symlink"
               if provisioned else
               f"name matches the naming template of team(s) {', '.join(matched)}")
        unclaimed.append({"profile": name, "source": source or None,
                          "matches_teams": matched,
                          "bytes": resolve.profile_footprint_bytes(name)})
        findings.append({
            "severity": "warn", "check": "instance_unclaimed",
            "message": f"profile {name!r} looks like a team instance no team "
                       f"claims — {why} ({_human_bytes(unclaimed[-1]['bytes'])} on disk)",
            "remedy": "re-run the interrupted `hermes team create` (it adopts "
                      f"matching instances), or: hermes profile delete {name}"})

    # F8: no port allocator exists, so two gateways in one team collide. Only
    # members that actually run or declare a gateway are counted — every
    # profile *could* start one, and warning about that would be noise.
    inventory = {i["name"]: i for i in resolve.profile_inventory()}
    for slug, t in sorted(reg.teams.items()):
        by_port: dict = {}
        for m in t.members:
            if not resolve.profile_exists(m.profile):
                continue
            facts = resolve.gateway_facts(m.profile)
            running = bool(inventory.get(m.profile, {}).get("gateway_running"))
            if facts["explicit"] or running:
                by_port.setdefault(facts["port"], []).append(m.profile)
        for port, profiles in sorted(by_port.items()):
            if len(profiles) > 1:
                findings.append({
                    "severity": "warn", "check": "gateway_port",
                    "message": f"team {slug!r}: {', '.join(sorted(profiles))} each "
                               f"run or declare a gateway on port {port} — they "
                               "cannot bind it at the same time (F8: there is no "
                               "port allocator)",
                    "remedy": "give each instance its own API_SERVER_PORT in its "
                              ".env (or platforms.api_server.extra.port)"})

    # catalog view: degraded catalog is info, not failure.
    catalog_sources = []
    try:
        from . import catalog as _cat
        for code, message in _cat.load().infos:
            findings.append({"severity": "info", "check": code, "message": message})
        catalog_sources = _cat.sources()
    except Exception as exc:                       # catalog must never break doctor
        findings.append({"severity": "info", "check": "catalog.error",
                         "message": f"catalog unreadable: {exc}"})

    # The worker-exit guard is invisible until a worker dies quietly, so
    # doctor reports what it resolved to — setting, wrapper path, exec bit, and
    # whether $HERMES_BIN actually points at it in this process.
    guard = _worker_exit_guard_view()
    if guard["mode"] == "wrapper" and not guard["installed"]:
        findings.append({
            "severity": "warn", "check": "worker_exit_guard",
            "message": "worker_exit_guard is 'wrapper' but $HERMES_BIN does not "
                       f"point at {guard['path'] or 'the wrapper'}: a worker that "
                       "fails before its first turn will exit 0 and be booked as "
                       "a protocol violation",
            "remedy": "restart the agent so the plugin re-installs it, or set "
                      "plugins.entries.hermes-agent-synergia.settings.worker_exit_guard: off"})

    payload = {
        "registry_path": str(storage.registry_path()),
        "hermes_root": root,
        "teams": sorted(reg.teams),
        "findings": findings,
        "shared_profiles": shared_profiles,
        "managed_profile_count": managed_count,
        "managed_footprint_bytes": managed_bytes,
        "managed_instances": managed_instances,
        "unclaimed_instances": unclaimed,
        "approvals_waiting": waiting_total,
        "stuck_cards": stuck_total,
        "parked_lanes": parked_total,
        "catalog_sources": catalog_sources,
        "worker_exit_guard": guard,
    }
    return payload


def _worker_exit_guard_view() -> dict:
    """What the worker-exit guard resolved to in THIS process (``hermes team doctor`` runs
    with the plugin loaded, so ``$HERMES_BIN`` is already installed if it took)."""
    import os
    mode = str(_settings().get("worker_exit_guard", "wrapper")).strip().lower() or "wrapper"
    shipped = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "worker_exit_guard.py")
    current = (os.environ.get("HERMES_BIN") or "").strip()
    installed = bool(current) and os.path.basename(current) == "worker_exit_guard.py"
    return {
        "mode": mode,
        "path": current or shipped,
        "executable": os.access(shipped, os.X_OK),
        "installed": installed,
        "chains_to": (os.environ.get("HERMES_REAL_BIN") or "").strip() or None,
    }


def _doctor_human(payload: dict) -> list:
    lines = [f"registry: {payload['registry_path']}",
             f"root:     {payload['hermes_root']}",
             f"teams:    {', '.join(payload['teams']) or '(none)'}",
             f"managed profiles: {payload['managed_profile_count']} "
             f"({_human_bytes(payload['managed_footprint_bytes'])} on disk)"]
    guard = payload.get("worker_exit_guard") or {}
    if guard:
        state = ("installed" if guard.get("installed")
                 else ("off" if guard.get("mode") != "wrapper" else "NOT installed"))
        lines.append(f"worker exit guard: {state} ({guard.get('path')})")
    if payload.get("approvals_waiting"):
        lines.append(f"awaiting approval: {payload['approvals_waiting']} card(s)"
                     "  (hermes team approvals)")
    if payload.get("stuck_cards"):
        lines.append(f"stuck: {payload['stuck_cards']} card(s)"
                     "  (hermes team approvals --kind stuck)")
    for name in ("review", "triage", "guarded"):
        n = (payload.get("parked_lanes") or {}).get(name)
        if n:
            lines.append(f"{name}: {n} card(s) parked"
                         f"  (hermes team approvals --kind {name})")
    if payload["unclaimed_instances"]:
        lines.append("unclaimed instances: "
                     + ", ".join(i["profile"] for i in payload["unclaimed_instances"]))
    for shared in payload["shared_profiles"]:
        lines.append(f"shared:   {shared['profile']} in "
                     f"{', '.join(shared['teams'])} "
                     f"(primary: {shared['primary'] or 'nobody'})")
    for f in payload["findings"]:
        lines.append(f"  {f['severity']:<6} {f['check']:<18} {f['message']}")
    if not payload["findings"]:
        lines.append("  ok     —            all invariants hold")
    return lines


def _cmd_profiles(args) -> int:
    """``team profiles list|show|sources`` — the profile picker — plus
    ``add|edit|import|remove|hide|unhide`` on the plugin's own store
    (``catalog_store``)."""
    jm = getattr(args, "json", False)
    cmd = getattr(args, "profiles_command", None) or "list"
    impl = _PROFILES_IMPLS.get(cmd)
    if impl is None:
        print("usage: hermes team profiles "
              "{list,show,sources,add,edit,import,adopt,remove,hide,unhide}",
              file=sys.stderr)
        return 2
    try:
        payload = impl(args)
    except TeamError as err:
        return _fail(err, json_mode=jm)
    _emit(payload, json_mode=jm)
    return 0


def _entries_word(n: int) -> str:
    return f"{n} entr" + ("y" if n == 1 else "ies")


def _catalog_revision():
    """The store's CAS revision, or None when its index is unreadable (the
    read verbs still work — the catalog degrades)."""
    from . import catalog_store
    try:
        return catalog_store.load_index()["revision"]
    except TeamError:
        return None


def _profiles_list_impl(args) -> dict:
    from . import catalog
    if getattr(args, "root_only", False):
        return _profiles_list_root_impl()
    rows = catalog.entries(pack=getattr(args, "pack", None),
                           category=getattr(args, "category", None),
                           role=getattr(args, "role", None),
                           q=getattr(args, "q", None),
                           include_hidden=getattr(args, "include_hidden", False),
                           local_only=getattr(args, "local_only", False))
    if getattr(args, "installed", False):
        rows = [e for e in rows if e["installed_as"]]
    human = [_entries_word(len(rows))]
    for e in rows:
        flags = (" [hidden]" if e["hidden"] else "") + (
            f"  [installed: {','.join(e['installed_as'])}]" if e["installed_as"] else "")
        human.append(f"{e['id']:<42} {e['pack']:<16} {e['category']:<16} "
                     f"{e['suggested_role']:<12} {e['display_name']}{flags}")
    return {"entries": rows, "count": len(rows), "revision": _catalog_revision(),
            "_human": human}


def _profiles_list_root_impl() -> dict:
    """``profiles list --root`` — live profiles under the hermes root.

    These are not catalog entries: the catalog never reads the profiles root
    (there is no tier C). The listing exists so a user can pick one for
    ``profiles adopt``.
    """
    from . import catalog
    rows = catalog.root_profiles()
    human = [f"{len(rows)} live profile" + ("" if len(rows) == 1 else "s")
             + f" under {catalog.root_profiles_dir()}"
             + " (not catalog entries — adopt one to use it as a blueprint)"]
    for r in rows:
        flag = f"  [adopted as: {r['adopted_as']}]" if r["adopted_as"] else ""
        soul = "" if r["has_soul"] else "  [no SOUL.md]"
        human.append(f"{r['id']:<42} {r['category']:<16} "
                     f"{r['suggested_role']:<12} {r['display_name']}{flag}{soul}")
    if not rows:
        human.append("(none)")
    return {"profiles": rows, "count": len(rows), "_human": human}


def _profiles_show_impl(args) -> dict:
    from . import catalog, catalog_store
    entry = catalog.get(args.entry)
    d = entry.to_dict()
    detail = catalog_store.describe(entry.source_path)
    refs = catalog_store.references(entry.id)
    human = [f"{d['id']}  ({d['pack']})",
             f"  display_name:    {d['display_name']}",
             f"  category:        {d['category'] or '-'}",
             f"  suggested_role:  {d['suggested_role']}",
             f"  version:         {d['version'] or '-'}",
             f"  author:          {d['author'] or '-'}",
             f"  license:         {d['license'] or '-'}",
             f"  source_path:     {d['source_path']}",
             f"  local / hidden:  {'yes' if d['local'] else 'no'} / "
             f"{'yes' if d['hidden'] else 'no'}",
             f"  installed_as:    {', '.join(d['installed_as']) or '-'}",
             f"  skills:          "
             f"{', '.join(s['name'] for s in detail['skills']) or '-'}",
             f"  team members:    {', '.join(refs['teams']) or '-'}",
             f"  recipes:         {', '.join(refs['recipes']) or '-'}",
             f"  description:     {d['description'] or '-'}"]
    soul = detail["soul"].splitlines()
    if soul:
        human.append("  SOUL.md:")
        human += ["    " + line for line in soul[:20]]
        if len(soul) > 20:
            human.append(f"    … ({len(soul) - 20} more lines)")
    return {"entry": d, "soul": detail["soul"],
            "soul_truncated": detail["soul_truncated"], "skills": detail["skills"],
            "referenced_by": refs, "_human": human}


def _profiles_sources_impl(args) -> dict:
    from . import catalog
    srcs = catalog.sources()
    human = [f"{s['path']}: "
             + ("resolved" if s["resolved"] else f"UNRESOLVED ({s.get('error', '?')})")
             + (f"  (plugin store, {_entries_word(s['packs'])})" if s.get("local") else "")
             for s in srcs]
    return {"sources": srcs, "revision": _catalog_revision(), "_human": human}


def _read_soul_file(path: str, verb: str, entry: str) -> str:
    from pathlib import Path
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise TeamError(
            E_TEAM_INVALID, f"cannot read --soul-file {path}: {exc}",
            hint="Pass a readable Markdown file; in the container, "
                 "/root/workspace is the host's ./workspace.",
            remedy=f"hermes team profiles {verb} {entry} "
                   "--soul-file /root/workspace/SOUL.md",
        ) from exc


def _profiles_edit_impl(args) -> dict:
    from . import catalog_store
    soul = (None if args.soul_file is None
            else _read_soul_file(args.soul_file, "edit", args.entry))
    result = catalog_store.update_entry(
        args.entry, display_name=args.display_name, category=args.category,
        description=args.description, soul=soul, version=args.version,
        author=args.author, license=args.license, skill_dirs=args.skill_dir,
        remove_skills=args.remove_skill,
        expected_revision=args.expected_revision)
    human = [f"edited {result['id']}: {', '.join(result['changed'])} "
             f"(revision {result['revision']})"]
    human += [f"warning: {w}" for w in result["warnings"]]
    return {**result, "_human": human}


def _profiles_add_impl(args) -> dict:
    from . import catalog_store
    soul = (_read_soul_file(args.soul_file, "add", args.entry)
            if args.soul_file else "")
    result = catalog_store.author_entry(
        args.entry, display_name=args.display_name or "",
        category=args.category or "", description=args.description or "",
        soul=soul, skill_dirs=args.skill_dir or [], version=args.version or "",
        author=args.author or "", license=args.license or "",
        replace=args.replace, expected_revision=args.expected_revision)
    human = [f"added {result['id']} to the catalog ({result['path']}, "
             f"revision {result['revision']})"]
    human += [f"warning: {w}" for w in result["warnings"]]
    return {**result, "_human": human}


def _profiles_import_impl(args) -> dict:
    from . import catalog_store
    if args.dry_run:
        plan = catalog_store.plan_import(args.source, only=args.only, name=args.name)
        human = [f"dry run: {_entries_word(plan['count'])} from {plan['source']} "
                 f"({plan['kind']}) — nothing written"]
        for e in plan["entries"]:
            flag = (" [exists: needs --replace]" if e["exists"]
                    else " [shadows an external entry: refused]"
                    if e["shadows_external"] else "")
            human.append(f"  {e['id']:<42} {e['pack']:<16} "
                         f"{e['file_count']:>4} files{flag}")
        if plan["recipes"]:
            human.append("recipes: " + ", ".join(plan["recipes"]))
        return {**plan, "dry_run": True, "_human": human}
    progress = None if args.json else (lambda msg: print(msg, file=sys.stderr))
    result = catalog_store.import_entries(
        args.source, only=args.only, name=args.name, replace=args.replace,
        expected_revision=args.expected_revision, progress=progress)
    human = [f"imported {_entries_word(result['count'])} from {result['source']} "
             f"(revision {result['revision']})"]
    if result["recipes"]:
        human.append("recipes: " + ", ".join(result["recipes"]))
    human += [f"warning: {w}" for w in result["warnings"]]
    return {**result, "_human": human}


def _profiles_remove_impl(args) -> dict:
    from . import catalog_store
    eid = str(args.entry).strip().lower()
    path = catalog_store.profiles_dir() / eid
    recipes = catalog_store.references(eid)["recipes"]
    also = f" and drops recipe(s) {', '.join(recipes)}" if recipes else ""
    if not args.yes:
        raise TeamError(
            E_TEAM_INVALID,
            f"removing catalog entry {eid!r} deletes {path}{also} — confirm with --yes",
            hint="Destructive: the stored copy is deleted, with every stored "
                 "recipe that names it. Profiles already instantiated from it "
                 "keep working.",
            remedy=f"hermes team profiles remove {eid} --yes",
        )
    print(f"removing catalog entry {eid} ({path}){also}", file=sys.stderr)
    result = catalog_store.remove_entry(eid, expected_revision=args.expected_revision)
    human = [f"removed {eid} (revision {result['revision']})"]
    if result["team_refs"]:
        human.append("still named as blueprint (provenance only): "
                     + ", ".join(result["team_refs"]))
    if result["dropped_recipes"]:
        human.append("dropped recipes: " + ", ".join(result["dropped_recipes"]))
    return {**result, "_human": human}


def _profiles_visibility_impl(args, hidden: bool) -> dict:
    from . import catalog_store
    result = catalog_store.set_hidden(args.entry, hidden,
                                      expected_revision=args.expected_revision)
    state = "hidden from the pickers" if hidden else "visible in the pickers"
    return {**result,
            "_human": [f"{result['id']} {state} (revision {result['revision']})"]}


def _profiles_adopt_impl(args) -> dict:
    from . import catalog_store
    result = catalog_store.adopt_profile(
        args.profile, name=args.name, replace=args.replace,
        expected_revision=args.expected_revision)
    human = [f"adopted profile {result['profile']} as catalog entry "
             f"{result['adopted']} (revision {result['revision']})",
             f"  copied from {result['source']}"]
    human += [f"warning: {w}" for w in result["warnings"]]
    return {**result, "_human": human}


_PROFILES_IMPLS = {
    "list": _profiles_list_impl,
    "show": _profiles_show_impl,
    "sources": _profiles_sources_impl,
    "add": _profiles_add_impl,
    "edit": _profiles_edit_impl,
    "import": _profiles_import_impl,
    "adopt": _profiles_adopt_impl,
    "remove": _profiles_remove_impl,
    "hide": lambda args: _profiles_visibility_impl(args, True),
    "unhide": lambda args: _profiles_visibility_impl(args, False),
}


def _cmd_recipes(args) -> int:
    """``team recipes list`` — roster presets."""
    from . import catalog
    jm = getattr(args, "json", False)
    cmd = getattr(args, "recipes_command", None)
    if cmd not in (None, "list"):
        print("usage: hermes team recipes list", file=sys.stderr)
        return 2
    rows = catalog.recipes()
    if getattr(args, "pack", None):
        rows = [r for r in rows if r["pack"] == args.pack]
    if getattr(args, "q", None):
        n = args.q.lower()
        rows = [r for r in rows if n in (r["id"] + " " + r["display_name"] + " "
                + " ".join(r["keywords"])).lower()]
    human = [f"{r['id']:<24} pack={r['pack']:<10} "
             + "/".join(f"{k}:{len(v)}" for k, v in r["tiers"].items())
             + f"  — {r['display_name']}" for r in rows]
    human.insert(0, f"{len(rows)} recipe(s)")
    _emit({"recipes": rows, "_human": human}, json_mode=jm)
    return 0


# ── sync / set-board / set-workdir (S11, spec §9) ────────────────────────────

def _fmt_obs(v) -> str:
    """One observed/desired cell: None is the em-dash, structures are JSON."""
    if v is None:
        return "—"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _sync_table(report, *, only=None) -> list:
    """§9.2 human table for one report: severity check subject
    ``observed → desired`` remedy (the CLI is already unicode — doctor's
    em-dashes — so the arrow is a real arrow)."""
    drift = [d for d in report.drift if only is None or d.check in only]
    lines: list = []
    if not drift:
        lines.append("  ok       —                            "
                     "no drift")
    for d in drift:
        obs = f"{_fmt_obs(d.observed)} → {_fmt_obs(d.desired)}"
        lines.append(f"  {d.severity:<8}{d.check:<28}{d.subject:<18}"
                     f"{obs:<42} {d.remedy}".rstrip())
    n_ok = sum(1 for a in report.applied if a.ok)
    n_no = len(report.applied) - n_ok
    if report.applied:
        lines.append(f"  applied {n_ok} item(s)"
                     + (f", {n_no} refused/failed" if n_no else ""))
    for a in report.applied:
        if not a.ok:
            lines.append(f"    FAILED {a.check} {a.subject}: {a.error}")
    return lines


def _dry_report(reconcile_mod, team, only) -> "reconcile_mod.ApplyReport":
    """Read-only stand-in with the ApplyReport shape, so sync and the
    set-* re-sync share one render/payload path."""
    items = reconcile_mod.diff(team)
    if only is not None:
        items = [d for d in items if d.check in only]
    return reconcile_mod.ApplyReport(slug=team.slug, applied=[], drift=items,
                                    profiles_updated=0, restart_notice=None,
                                    exit_code=0)


def _sync_payload(team, report, *, dry: bool, only) -> dict:
    """§7.3/§8.2 data-object: the DriftItems verbatim (§9.1 to_dict)."""
    keep = (lambda d: only is None or d.check in only)
    return {"team": team.slug,
            "dry_run": dry,
            "drift": [d.to_dict() for d in report.drift if keep(d)],
            "applied": [a.to_dict() for a in report.applied],
            "restart_notice": report.restart_notice}


def _sync_human(team, report, *, dry: bool, only) -> list:
    lines = [f"team {team.slug}"
             + ("  (dry run — nothing written)" if dry else "  (applied)")]
    lines += _sync_table(report, only=only)
    return lines


def _only_set(raw):
    if raw is None:
        return None
    only = frozenset(c.strip() for c in str(raw).split(",") if c.strip())
    if not only:
        raise TeamError(
            E_TEAM_INVALID, "--only needs at least one check id",
            hint="Example: hermes team sync px --only member.terminal_cwd",
            remedy="hermes team sync TEAM --only CHECK[,CHECK...]",
        )
    return only


def _cmd_sync(args) -> int:
    """§9: report drift (default), or fix it under --apply. Exit codes §8.2:
    drift alone is a report → 0; --apply with unfixable residue → 4."""
    from . import reconcile
    jm = args.json
    if args.all and args.team:
        print("usage: hermes team sync TEAM|--all  (not both)", file=sys.stderr)
        return 2
    if not args.all and not args.team:
        print("usage: hermes team sync TEAM|--all [options]", file=sys.stderr)
        return 2
    only = _only_set(args.only)
    reg = storage.load()
    if args.all:
        slugs = sorted(reg.teams)
    else:
        if args.team not in reg.teams:
            return _fail(
                TeamError(E_TEAM_NOT_FOUND, f"no team {args.team!r}",
                          hint="Known teams: " + (", ".join(sorted(reg.teams))
                                                  or "(none)"),
                          remedy="hermes team list"),
                json_mode=jm, team=args.team)
        slugs = [args.team]
    teams = [reg.teams[s] for s in slugs]
    dry = not args.apply
    notes: list = []
    skipped_busy: list = []
    if dry:
        reports = [_dry_report(reconcile, t, only) for t in teams]
        notes = [({}, [])] * len(teams)
    else:
        opts = dict(reinstantiate=bool(args.reinstantiate),
                    link_env=bool(args.link_env),
                    create_board=bool(args.create_missing or args.create_board),
                    create_workdir=bool(args.create_missing or args.create_workdir),
                    only=only, on_busy=args.on_busy,
                    wait_timeout=args.wait_timeout)
        applied_teams, reports = [], []
        for t in teams:
            try:
                report, fields, lines = _sync_one_apply(t, **opts)
            except TeamError as err:
                if err.code != E_TEAM_BUSY:
                    raise
                if not args.all:
                    return _fail(err, json_mode=jm, team=t.slug)
                # --all: one busy team must not hold back the idle ones.
                skipped_busy.append({"team": t.slug, "message": err.message})
                continue
            applied_teams.append(t)
            reports.append(report)
            notes.append((fields, lines))
        teams = applied_teams
    payloads = [dict(_sync_payload(t, r, dry=dry, only=only), **fields)
                for t, r, (fields, _lines) in zip(teams, reports, notes)]
    if args.all:
        data: dict = {"teams": payloads, "dry_run": dry}
        if skipped_busy:
            data["skipped_busy"] = skipped_busy
    else:
        data = payloads[0]
    human = [line for t, r, (_fields, lines) in zip(teams, reports, notes)
             for line in _sync_human(t, r, dry=dry, only=only) + lines]
    human += [f"skipped busy team {s['team']}: {s['message']}"
              for s in skipped_busy]
    _emit(dict(data, _human=human or ["(no teams)"]), json_mode=jm)
    if dry:
        return 0
    code = reconcile.run_exit_code(reports) if reports else 0
    return code or (3 if skipped_busy else 0)


def _sync_one_apply(team, *, reinstantiate: bool = False, link_env: bool = False,
                    create_board: bool = False, create_workdir: bool = False,
                    only=None, on_busy: str = "refuse",
                    wait_timeout: int = BUSY_WAIT_TIMEOUT) -> tuple:
    """One team's ``sync --apply`` behind the busy guard, shared with
    ``POST /teams/{slug}/sync``: ``(report, busy fields, human lines)``.
    Only an apply that would write is guarded; stopped cards resume
    afterwards under the new config."""
    from . import reconcile, resolve
    opts = dict(reinstantiate=reinstantiate, link_env=link_env,
                create_board=create_board, create_workdir=create_workdir,
                only=only)
    busy = {"stopped": [], "not_killed": []}
    if reconcile.would_write(team, **opts):
        busy = _ensure_idle(team, "sync",
                            remedy=f"hermes team sync {team.slug} --apply",
                            on_busy=on_busy, timeout=wait_timeout)
    try:
        report = reconcile.apply(team, **opts)
    finally:
        resumed = resolve.resume_cards(team.board, busy["stopped"])
    fields, lines = _busy_fields(busy, resumed)
    return report, fields, lines


def _set_field(args, field: str) -> int:
    """set-board / set-workdir: mutate the record through the single writer
    (``storage.upsert``, never a second one), then re-run the reconcile
    report path — same output and exit-code behavior as ``sync``."""
    from . import reconcile
    jm = args.json
    reg = storage.load()
    team = reg.teams.get(args.team)
    if team is None:
        return _fail(
            TeamError(E_TEAM_NOT_FOUND, f"no team {args.team!r}",
                      hint="Known teams: " + (", ".join(sorted(reg.teams))
                                              or "(none)"),
                      remedy="hermes team list"),
            json_mode=jm, team=args.team)
    value = getattr(args, field)
    if field == "board" and not model.SLUG_RE.match(str(value or "")):
        return _fail(
            TeamError(
                E_TEAM_INVALID, f"board slug {value!r} is invalid",
                hint=f"Board slugs match {model.SLUG_RE.pattern} (substrate rule, "
                     "copied).",
                remedy=f"hermes team set-board {team.slug} <valid-slug>"),
            json_mode=jm, team=team.slug)
    candidate = model.Team.from_dict({**team.to_dict(), field: value})
    try:
        model.validate_team(candidate, _root())
    except TeamError as err:
        return _fail(err, json_mode=jm, team=team.slug)
    errs = model.check_invariants(reg, candidate)
    if errs:
        return _fail(errs[0], json_mode=jm, team=team.slug)
    stored = storage.upsert(candidate, expected_revision=reg.revision)
    report = _dry_report(reconcile, stored, None)
    note = f"team {stored.slug} {field.replace('_', '-')} -> {value} " \
           f"(revision {stored.revision})"
    payload = dict(_sync_payload(stored, report, dry=True, only=None),
                   revision=stored.revision)
    _emit(dict(payload, _human=[note] + _sync_human(stored, report,
                                                    dry=True, only=None)),
          json_mode=jm)
    return 0


def _cmd_set_board(args) -> int:
    return _set_field(args, "board")


def _set_workdir_impl(slug: str, workdir: str, *, apply: bool = False,
                      preview: bool = False,
                      expected_revision: Optional[int] = None,
                      on_busy: str = "refuse",
                      wait_timeout: int = BUSY_WAIT_TIMEOUT) -> dict:
    """set-workdir core, shared with ``POST /teams/{slug}/workdir``. Moves
    the record through the single writer; ``apply`` then runs the same
    reconcile as ``sync --apply --create-workdir`` so the board default, the
    git repo (worktree) and every primary member's terminal.cwd follow.
    ``preview`` writes nothing and diffs the candidate. A real move is
    busy-guarded; stopped cards resume afterwards in the new root. Raises
    TeamError."""
    import os
    from . import reconcile, resolve
    reg = storage.load()
    team = reg.teams.get(slug)
    if team is None:
        raise TeamError(E_TEAM_NOT_FOUND, f"no team {slug!r}",
                        hint="Known teams: " + (", ".join(sorted(reg.teams))
                                                or "(none)"),
                        remedy="hermes team list")
    candidate = model.Team.from_dict({**team.to_dict(), "workdir": workdir})
    model.validate_team(candidate, _root())
    errs = model.check_invariants(reg, candidate)
    if errs:
        raise errs[0]
    changed = (os.path.normpath(os.path.expanduser(workdir))
               != os.path.normpath(os.path.expanduser(team.workdir or "")))
    busy = {"stopped": [], "not_killed": []}
    if changed and apply and not preview:
        busy = _ensure_idle(
            team, "set-workdir",
            remedy=f"hermes team set-workdir {slug} {workdir} --apply",
            on_busy=on_busy, timeout=wait_timeout)
    stored = team
    try:
        if changed and not preview:
            stored = storage.upsert(candidate, expected_revision=(
                reg.revision if expected_revision is None
                else int(expected_revision)))
        target = candidate if preview else stored
        dry = preview or not apply
        report = (_dry_report(reconcile, target, None) if dry
                  else reconcile.apply(target, create_workdir=True))
    finally:
        # A failed move changed nothing, so its stopped cards resume too.
        resumed = resolve.resume_cards(team.board, busy["stopped"])
    note = (f"team {slug} workdir -> {workdir} (revision {stored.revision})"
            if changed else f"team {slug} workdir unchanged ({team.workdir})")
    fields, lines = _busy_fields(busy, resumed)
    return dict(_sync_payload(target, report, dry=dry, only=None),
                revision=stored.revision, changed=changed,
                workdir=target.workdir,
                profiles_updated=report.profiles_updated,
                exit_code=0 if dry else reconcile.run_exit_code([report]),
                **fields,
                _human=[note] + lines + _sync_human(target, report, dry=dry,
                                                    only=None))


def _cmd_set_workdir(args) -> int:
    try:
        payload = _set_workdir_impl(args.team, args.workdir,
                                    apply=bool(getattr(args, "apply", False)),
                                    on_busy=args.on_busy,
                                    wait_timeout=args.wait_timeout)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=args.team)
    _emit(payload, json_mode=args.json)
    return payload["exit_code"]


def _merge_lines(res: dict) -> list:
    if not res["card"]:
        return [f"thread {res['root']} ({res['team']}): no finished worktree "
                "branch to merge"]
    n = res["ahead"]
    lines = [f"result of thread {res['root']} ({res['team']}): {res['branch']} "
             f"— {res['card']}: {res['title']}"]
    if res["merged"]:
        lines.append(f"  merged {n} commit(s) into {res['base']}: "
                     f"{res['from'][:12]}..{res['to'][:12]}")
    elif not n:
        lines.append(f"  already in {res['base']}")
    else:
        lines.append(f"  {n} commit(s) ahead of {res['base']}")
    lines += [f"    {c['sha']} {c['subject']}" for c in res["commits"]]
    if not res["merged"] and n:
        if res["blocker"]:
            lines.append(f"  blocked: {res['blocker']['message']}")
            lines.append(f"  remedy: {res['blocker']['remedy']}")
        else:
            lines.append(f"  apply with: hermes team merge {res['team']} "
                         f"{res['root']} --apply")
    lines += [f"  not included: {o['branch']} — {o['card']}: {o['title']}"
              for o in res["others"]]
    lines.append(f"  files: {res['workdir']}")
    return lines


def _merge_impl(slug: str, card: str, *, apply: bool = False) -> dict:
    """merge core, shared with ``/teams/{slug}/tasks/{card}/merge``: preview
    or fast-forward the workdir to the result branch of *card*'s thread
    (:func:`resolve.merge_result`). Raises TeamError."""
    from . import approvals, resolve
    team = approvals.team_or_raise(slug)
    res = resolve.merge_result(team, card, apply=apply)
    res["_human"] = _merge_lines(res)
    return res


def _cmd_merge(args) -> int:
    try:
        payload = _merge_impl(args.team, args.card, apply=bool(args.apply))
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=args.team)
    _emit(payload, json_mode=args.json)
    return 0


# ── clone / export / import (S17, §5.7, §8.1) ────────────────────────────────

def _binding_required_error(cmd: str, operands: str) -> TeamError:
    """clone/import share one rule: the workdir is never copied (§8.1 row 7)."""
    return TeamError(
        E_TEAM_INVALID,
        f"`{cmd}` requires --workdir PATH — a workdir is never copied (§8.1)",
        hint="Two teams on one workdir stomp each other (I4); the clone always "
             "gets its own. Pass --create-workdir to mkdir it.",
        remedy=f"hermes team {cmd} {operands} --workdir /path/to/new --create-workdir",
    )


def _clone_impl(args) -> dict:
    """:func:`model.derive_clone` + the ordinary create path (§5.7). The clone
    writes through ``storage.upsert`` only (never a second writer into
    ``teams.yaml``) and never writes to ``<src>``:
    roster/roles/policy are copied, every binding is re-declared, every member
    lands ``managed: false`` (I7) and ``primary: false`` (I2 recomputed)."""
    reg = storage.load()
    src = reg.teams.get(args.src)
    if src is None:
        raise TeamError(
            E_TEAM_NOT_FOUND,
            f"no team {args.src!r} to clone",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        )
    new_slug = model._normalize_profile_name(args.new)
    if not args.workdir:
        raise _binding_required_error("clone", f"{src.slug} {new_slug}")

    ov = {
        "board": args.board,
        "workdir": args.workdir,
        "tenant": args.tenant,
        "project": args.project,
        "orchestrator": args.orchestrator,
        "display_name": args.display_name,
        "description": args.description,
        "drop_members": list(args.drop_member or ()),
        "allow_shared_board": bool(args.allow_shared_board),
    }
    draft = model.derive_clone(src, new_slug, ov)

    triples: list = []
    if args.instantiate:
        draft, triples = _instantiate_clone(draft, src.slug)

    payload = _finish_team(
        draft, triples, slug=new_slug,
        create_board=args.create_board, create_workdir=args.create_workdir,
        create_missing=False, link_env=False, dry_run=False,
        no_sync=args.no_sync,
    )
    payload["cloned_from"] = src.slug
    return payload


def _instantiate_clone(draft, src_slug: str):
    """``--instantiate`` (§5.7/§8.1 row 2): re-run provisioning from each
    member's ``blueprint`` under the CLONE's own naming template, so the clone
    owns its ``<new>-*`` instances — the roster is reusable, the instances
    never are. Renamed members are ``managed: true`` (the clone created them)
    and ``primary: true`` (fresh profiles no other team can claim, S9)."""
    from . import catalog, provision
    no_bp = sorted(m.profile for m in draft.members if not m.blueprint)
    if no_bp:
        raise TeamError(
            E_TEAM_NO_BLUEPRINT,
            "cannot instantiate: member(s) carry no blueprint: "
            + ", ".join(no_bp),
            hint="--instantiate re-provisions every member from its "
                 "blueprint; a roster adopted with --member has none.",
            remedy=f"hermes team clone {src_slug} {draft.slug}   "
                   "# drop --instantiate to clone the shared roster instead",
        )
    members, triples, rename = [], [], {}
    for m in draft.members:
        entry = catalog.get(m.blueprint)
        name = provision.render_name(draft.slug, entry, draft.profile_naming)
        d = m.to_dict()
        d["profile"] = name
        d["managed"] = True
        d["primary"] = True
        nm = model.Member.from_dict(d)
        members.append(nm)
        triples.append((nm, entry, None))
        rename[m.profile] = name
    orch = rename.get(draft.orchestrator, draft.orchestrator)
    draft = model.Team.from_dict({**draft.to_dict(),
                                  "members": [m.to_dict() for m in members],
                                  "orchestrator": orch})
    return draft, triples


def _cmd_clone(args) -> int:
    """CLI wrapper over :func:`_clone_impl` (same envelope discipline as
    ``create``)."""
    try:
        payload = _clone_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "src", None))
    _emit(payload, json_mode=args.json)
    return 0


def _doc_relative(obj, base: str):
    """``export --relative``: every string in the document that IS *base* or
    lives under it becomes workdir-relative (``.`` / ``./sub``). Pure document
    transform; ``import`` never reads bindings from the file anyway."""
    import os
    b = os.path.normpath(str(base or ""))
    if not b or b in ("/", ".", "..") or len(b) <= 1:
        return obj
    def _swap(v):
        if isinstance(v, str):
            s = os.path.normpath(v)
            if s == b:
                return "."
            if s.startswith(b + os.sep):
                return "./" + s[len(b) + 1:]
        return v
    if isinstance(obj, dict):
        return {k: _doc_relative(v, base) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_doc_relative(v, base) for v in obj]
    return _swap(obj)


def _cmd_export(args) -> int:
    """Dump a team record as YAML. ``-o FILE`` or stdout; ``--relative``
    rewrites workdir-rooted paths (the flag v1.0 documented but never
    declared). No ``--json``: the YAML document IS the output."""
    import yaml
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        return _fail(
            TeamError(E_TEAM_NOT_FOUND, f"no team {args.team!r}",
                      hint="Known teams: " + (", ".join(sorted(reg.teams))
                                              or "(none)"),
                      remedy="hermes team list"),
            json_mode=False, team=args.team)
    doc = t.to_dict()
    if args.relative:
        doc = _doc_relative(doc, t.workdir)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    if args.out:
        try:
            with open(_os_path_expanduser(args.out), "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            return _fail(
                TeamError(E_TEAM_INVALID,
                          f"cannot write export file {args.out!r}: {exc}",
                          hint="Pick a path the operator can write.",
                          remedy=f"hermes team export {t.slug} -o /tmp/{t.slug}.yaml"),
                json_mode=False, team=t.slug)
        print(f"team {t.slug} exported to {args.out}"
              + (" (paths relative to the workdir)" if args.relative else ""),
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _os_path_expanduser(p: str) -> str:
    import os
    return os.path.expanduser(p)


def _import_impl(args) -> dict:
    """§8.1: import follows exactly the clone's rules, reading the roster from
    a file instead of a sibling record. The exported ``board`` is a PROPOSAL:
    with neither ``--board`` nor a freeing ``--rename`` it must still be free,
    or the import refuses ``E_TEAM_BOARD_TAKEN`` — never two teams on one board
    silently (a gap closed here)."""
    import yaml
    path = _os_path_expanduser(args.file)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise TeamError(
            E_TEAM_INVALID, f"cannot read {args.file!r}: {exc}",
            hint="Import needs the file an earlier `team export -o` wrote.",
            remedy=f"hermes team export <team> -o {args.file}",
        ) from exc
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TeamError(
            E_TEAM_INVALID, f"{args.file!r} is not parseable YAML: {exc}",
            hint="Import reads the document `team export` wrote.",
            remedy=f"hermes team export <team> -o {args.file}",
        ) from exc
    if not isinstance(doc, dict) or not doc.get("members"):
        raise TeamError(
            E_TEAM_INVALID,
            f"{args.file!r} does not hold a team record (no members found)",
            hint="Import reads the document `team export` wrote.",
            remedy=f"hermes team export <team> -o {args.file}",
        )
    src = model.Team.from_dict(doc)
    new_slug = model._normalize_profile_name(args.rename or src.slug)
    if not args.workdir:
        raise _binding_required_error("import", args.file)

    board = args.board or src.board or new_slug
    shared = bool(args.allow_shared_board)
    reg = storage.load()
    if not args.board:
        # The file's board is a proposal, never a silent binding (I1).
        for slug, t in reg.teams.items():
            if slug == new_slug or t.board != board:
                continue
            if shared and t.shared_board:
                continue
            raise TeamError(
                E_TEAM_BOARD_TAKEN,
                f"board {board!r} named in {args.file!r} is already claimed by "
                f"team {slug!r}",
                hint="An import never lands a second team on one board "
                     "silently (I1): pass --board <free-slug> (with "
                     "--create-board for a new one), --rename so the source "
                     "slug frees, or --allow-shared-board when that team "
                     "shares too.",
                remedy=f"hermes team import {args.file} --rename {new_slug} "
                       "--board <free-slug> --create-board",
            )

    draft = model.derive_clone(src, new_slug, {
        "board": board,
        "workdir": args.workdir,
        "tenant": args.tenant,
        "allow_shared_board": shared,
    })
    payload = _finish_team(
        draft, [], slug=new_slug,
        create_board=args.create_board, create_workdir=args.create_workdir,
        create_missing=False, link_env=False, dry_run=False,
        no_sync=args.no_sync,
    )
    payload["imported_from"] = args.file
    return payload


def _cmd_import(args) -> int:
    try:
        payload = _import_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json,
                     team=getattr(args, "rename", None) or args.file)
    _emit(payload, json_mode=args.json)
    return 0


# ── adopt (S18, §8.1) ────────────────────────────────────────────────────────
#
# An existing hand-made board becomes a team without losing its history.
# Spec-silent decisions (documented per S18 brief):
#   * roster role — §8.1 names no role for an adopted member, so every board
#     assignee lands role "member" (the §4.1 Member default);
#   * orchestrator — I3 demands one; §8.1 is silent, so the DISTINCT ASSIGNEES
#     ARE SORTED AND THE FIRST becomes the orchestrator (both its member role
#     and the record's ``orchestrator``). Deterministic, no prompts;
#   * ``--workdir`` — the §8.1 grammar line is abbreviated (compare
#     ``hermes team status``, which it also omits). ``validate_team`` (§4.2)
#     requires a real absolute workdir, so adopt takes one: flag first, else
#     the board's ``default_workdir`` metadata (which ``team create`` stamps);
#     it must already exist — adopt, like ``--backfill-tenant``, never acts
#     implicitly;
#   * backfill write ORDER — the tenant UPDATE happens BEFORE the registry
#     write (registry stays LAST, §5.5): a crash between the two leaves the
#     rows stamped and no team, and the identical re-run is then a clean
#     no-op backfill (0 unstamped rows) + registry write. Resumable.
#   * the tenant UPDATE itself lives in ``_backfill_tenant`` below — the
#     substrate has no backfill primitive (verified by grep over
#     ``hermes_cli/kanban_db.py``: only INSERT/filter use of ``tenant``).

def _board_tasks_conn(board: str):
    """Context-managed connection to *board*'s DB, or None when it has no DB
    file yet (a board with zero tasks still exists — a reader never mints
    one, same rule as ``resolve.task_counts``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect
    db_path = kb.kanban_db_path(board)
    if not db_path.is_file():
        return None
    return kanban_db_connect.connect_closing(db_path=db_path)


def _board_assignees(board: str) -> list:
    """Distinct non-null assignees on the board's tasks, sorted (§8.1
    ``--members-from-board`` roster source)."""
    ctx = _board_tasks_conn(board)
    if ctx is None:
        return []
    with ctx as conn:
        rows = conn.execute(
            "select distinct assignee from tasks"
            " where assignee is not null and assignee <> ''"
            " order by assignee").fetchall()
    return sorted({str(r[0]) for r in rows})


def _count_unstamped(board: str) -> int:
    """Rows on *board* with ``tenant IS NULL`` (the §12 'status empty though
    cards exist' row; the backfill's affected-row count)."""
    ctx = _board_tasks_conn(board)
    if ctx is None:
        return 0
    with ctx as conn:
        return int(conn.execute(
            "select count(*) from tasks where tenant is null").fetchone()[0])


def _backfill_tenant(board: str, tenant: str) -> int:
    """Stamp every ``tenant IS NULL`` row on *board* with *tenant* (the
    explicit, confirmed, reversible migration). Returns rows written."""
    ctx = _board_tasks_conn(board)
    if ctx is None:
        return 0
    with ctx as conn:
        cur = conn.execute("update tasks set tenant=? where tenant is null",
                           (tenant,))
        conn.commit()
        return int(cur.rowcount or 0)


def _adopt_impl(args) -> dict:
    """``hermes team adopt`` body (raises TeamError; ``_cmd_adopt`` renders).
    Same invariant/CAS discipline as create (I1..I7, registry write LAST);
    ``create``'s E_TEAM_EXISTS guard applies — adopt is not an editor."""
    import os
    from . import resolve

    slug = model._normalize_profile_name(args.team)
    board = str(args.board or "").strip()
    if not board:
        raise TeamError(
            E_TEAM_INVALID,
            "`adopt` requires --board SLUG — adopt binds a team to an EXISTING "
            "board and never creates one",
            hint="§8.1: hermes team adopt <team> --board SLUG "
                 "[--members-from-board] [--backfill-tenant].",
            remedy=f"hermes team adopt {slug} --board <existing-slug> "
                   "--members-from-board",
        )
    if not model.SLUG_RE.match(board):
        raise TeamError(
            E_TEAM_INVALID, f"board slug {board!r} is invalid",
            hint=f"Board slugs match {model.SLUG_RE.pattern} (substrate rule, "
                 "copied).",
            remedy=f"hermes team adopt {slug} --board <valid-slug>",
        )
    if not resolve.board_exists(board):
        raise TeamError(
            E_TEAM_NO_BOARD,
            f"board {board!r} does not exist — adopt attaches a team to an "
            "EXISTING board; it never creates one",
            hint="Make the board first (the kanban CLI, or any `team create` "
                 "--create-board), then re-run the adopt.",
            remedy=f"hermes team create {slug} --board {board} --create-board"
                   " …   # create+bind in one step instead of adopting",
        )

    reg = storage.load()
    if slug in reg.teams:
        raise TeamError(
            E_TEAM_EXISTS,
            f"team {slug!r} already exists (revision "
            f"{reg.teams[slug].revision}) — adopt is not an editor",
            hint="Adopt the board under a new team slug, or destroy the "
                 "existing record first.",
            remedy=f"hermes team show {slug}   # or: hermes team destroy "
                   f"{slug} --yes",
        )

    # ── roster (§8.1): distinct board assignees. Spec-silent on roles —
    # everyone is role "member" (the §4.1 default); I3 demands an
    # orchestrator, so the first assignee in SORTED order takes role
    # "orchestrator" deterministically. Everyone lands primary/managed
    # false: the adopt claims nothing it did not create (I7).
    if not args.members_from_board:
        raise TeamError(
            E_TEAM_INVALID,
            "`adopt` needs a roster: pass --members-from-board (§8.1 derives "
            "the members from the board's assignees)",
            hint="A team with no members is invalid (§4.2); the board's "
                 "distinct assignees are the only roster an adopt can see.",
            remedy=f"hermes team adopt {slug} --board {board} "
                   "--members-from-board",
        )
    assignees = _board_assignees(board)
    if not assignees:
        raise TeamError(
            E_TEAM_INVALID,
            f"board {board!r} has no tasks with an assignee — --members-from-"
            "board has nothing to derive a roster from",
            hint="Seed at least one assigned card on the board first, or add "
                 "members after adopting: hermes team add-member.",
            remedy=f"hermes team add-member {slug} <profile>   # after adopt",
        )
    orchestrator = assignees[0]
    members = [model.Member.from_dict({
        "profile": p,
        "role": "orchestrator" if p == orchestrator else "member",
        "primary": False,      # the board's history claims no plane-1 owner
        "managed": False,      # adopt creates nothing (I7)
    }) for p in assignees]

    # adopted members must exist (§5.5's rule, verbatim: profiles exist or the
    # team adopts phantoms).
    dangling = resolve.profiles_exist(assignees)
    if dangling:
        raise TeamError(
            E_TEAM_NO_PROFILE,
            "member profile(s) do not exist: " + ", ".join(sorted(dangling)),
            hint="The board's assignees name profiles; a legacy board may "
                 "point at deleted ones. Create them before adopting.",
            remedy=f"hermes profile create {sorted(dangling)[0]}",
        )

    # workdir: flag, else the board's own default (stamped by `team create`
    # §5.5 step 9 / kanban board metadata); must exist — no implicit mkdir.
    meta = resolve.board_meta(board) or {}
    wd = args.workdir or str(meta.get("default_workdir") or "")
    wd = os.path.normpath(os.path.expanduser(wd)) if wd else ""
    if not wd or not os.path.isdir(wd):
        raise TeamError(
            E_TEAM_INVALID,
            f"workdir {wd!r} does not exist — adopt never creates one "
            "(pass --workdir, or point the board's default_workdir at it)",
            hint="§4.2 requires a real absolute workdir; the §8.1 grammar line "
                 "for adopt is abbreviated, so the flag is spelled out here.",
            remedy=f"hermes team adopt {slug} --board {board} "
                   "--members-from-board --workdir /path/to/workdir",
        )

    team_draft = model.Team.from_dict({
        "slug": slug,
        "display_name": slug,
        "description": f"adopted existing board {board!r} (S18)",
        "revision": 1,
        "created_at": model.now_iso(),
        "updated_at": model.now_iso(),
        "board": board,
        "shared_board": False,          # I1: adopt takes the board whole
        "workdir": wd,
        "workspace_policy": "scratch",
        "tenant": slug,
        "tenant_stamping": "auto",
        "orchestrator": orchestrator,
        "members": [m.to_dict() for m in members],
        "policy": {"mode": _settings().get("default_policy", "advise")},
        "sync": {},
        "metadata": {"adopted_from_board": board},
    })

    # validate + I1..I7 against the same CAS snapshot create takes (a second
    # team on the adopted board surfaces here as E_TEAM_BOARD_TAKEN).
    model.validate_team(team_draft, _root())
    errs = model.check_invariants(reg, team_draft)
    if errs:
        raise errs[0]

    # ── tenant backfill: EXPLICIT, CONFIRMED, REVERSIBLE, NEVER IMPLICIT ─────
    warning = None
    backfill = None
    unstamped = _count_unstamped(board)
    if args.backfill_tenant:
        # The confirmation contract: print the affected-row count BEFORE any
        # write, then refuse without a confirmation.
        line = (f"{unstamped} task row(s) on board {board!r} carry no tenant "
                f"stamp and would be stamped tenant={slug!r}")
        print(line, file=sys.stderr)
        if unstamped:
            if not args.yes and not (sys.stdin and sys.stdin.isatty()):
                raise TeamError(
                    E_TEAM_INVALID,
                    f"--backfill-tenant refused without confirmation: "
                    f"{unstamped} task row(s) would be stamped "
                    f"tenant={slug!r}",
                    hint="The tenant backfill is explicit and CONFIRMED — "
                         "re-run with --yes (a tty prompt is offered when no "
                         "--yes flag is passed).",
                    remedy=f"hermes team adopt {slug} --board {board} "
                           f"--members-from-board --backfill-tenant --yes",
                )
            if not args.yes:
                try:
                    reply = input(f"stamp {unstamped} task row(s) with "
                                  f"tenant={slug!r}? [y/N] ").strip().lower()
                except EOFError:
                    reply = ""
                if reply not in ("y", "yes"):
                    raise TeamError(
                        E_TEAM_INVALID,
                        "--backfill-tenant aborted at the confirmation prompt",
                        hint="Nothing was written; re-run with --yes to "
                             "confirm non-interactively.",
                        remedy=f"hermes team adopt {slug} --board {board} "
                               f"--members-from-board --backfill-tenant --yes",
                    )
            written = _backfill_tenant(board, slug)
            undo_sql = f"UPDATE tasks SET tenant=NULL WHERE tenant='{slug}'"
            db_path = str(_board_db_path(board))
            undo_cmd = f'sqlite3 {db_path} "{undo_sql};"'
            # Reversible, and the reversibility is PRINTED: verbatim SQL.
            print(f"stamped {written} task row(s) tenant={slug!r}",
                  file=sys.stderr)
            print(f"undo: {undo_cmd}", file=sys.stderr)
            backfill = {"rows_stamped": written, "undo_sql": undo_sql,
                        "undo_cmd": undo_cmd}
    elif unstamped:
        # §12: "team status empty though cards exist" — say so, name the flag.
        warning = (f"{unstamped} task row(s) on board {board!r} carry no "
                   f"tenant stamp — team status reads zero until they are "
                   f"stamped; run: hermes team adopt {slug} --board {board} "
                   f"--backfill-tenant --yes")
        print(f"WARNING: {warning}", file=sys.stderr)

    # ── registry write (LAST, same as create §5.5/§5.7).
    stored = storage.upsert(team_draft, expected_revision=reg.revision)

    human = [f"team {slug} adopted board {board!r} (revision "
             f"{stored.revision}): {len(members)} member(s) from the board — "
             f"orchestrator {orchestrator}"]
    if backfill:
        human.append(f"backfilled {backfill['rows_stamped']} task row(s) with "
                     f"tenant={slug!r}; undo: {backfill['undo_cmd']}")
    if warning:
        human.append(f"WARNING: {warning}")
    payload = {
        "team": stored.to_dict(),
        "adopted_board": board,
        "members_from_board": list(assignees),
        "unstamped_rows": unstamped,
        "backfilled": backfill,
        "_human": human,
    }
    if warning:
        payload["warning"] = warning
    return payload


def _board_db_path(board: str):
    from hermes_cli import kanban_db as kb
    return kb.kanban_db_path(board)


def _cmd_adopt(args) -> int:
    """CLI wrapper over :func:`_adopt_impl` (same envelope discipline as
    ``create``/``clone``)."""
    try:
        payload = _adopt_impl(args)
    except TeamError as err:
        return _fail(err, json_mode=args.json, team=getattr(args, "team", None))
    _emit(payload, json_mode=args.json)
    return 0


# ── status (S18b): the read side of adopt ────────────────────────────────────

def _age_h(ts, now: int) -> str:
    """``created_at`` epoch → compact age string for the human report."""
    if ts is None:
        return "-"
    secs = max(0, int(now) - int(ts))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _cmd_status(args) -> int:
    """``hermes team status <team>`` — per-status counts + open cards for
    ONE team, tenant-scoped (spec §8.1 grammar, S18b).

    Same read path as the ``team_status`` tool (``tools._h_status``) and the
    dashboard's ``GET /teams/{slug}/status``: :func:`resolve.task_counts`
    for the six §4.3 buckets, :func:`tools._list_team_tasks` for the rows
    (both gate on the DB FILE existing, §5.2.1), and the same
    filter/sort/slice and limit rules — tool, API and CLI agree by
    construction.

    The CLI acts AS THE OPERATOR: no membership gate (every ``cli.py``
    command reads the registry directly — ``create``/``adopt``/``show``
    have none; the member test lives in ``tools._resolve_team``, which the
    CLI deliberately does not use).

    Tenant-less team (hand-edited record): the honest scope is the
    conventional ``tenant = <slug>`` — the same label
    ``tools._list_team_tasks`` falls back to — which reads EMPTY for cards
    predating stamping; the §12 note names the backfill remedy.
    """
    import time
    from . import approvals, resolve, tools
    reg = storage.load()
    t = reg.teams.get(args.team)
    if t is None:
        return _fail(TeamError(
            E_TEAM_NOT_FOUND,
            f"no team {args.team!r}",
            hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
            remedy="hermes team list",
        ), json_mode=args.json, team=args.team)

    status = (getattr(args, "status", None) or "").strip() or None
    assignee = (getattr(args, "assignee", None) or "").strip() or None
    limit = (tools._STATUS_DEFAULT_LIMIT if args.limit is None
             else int(args.limit))
    if not 1 <= limit <= tools._STATUS_MAX_LIMIT:
        raise TeamError(
            E_TEAM_INVALID,
            f"limit {limit} out of range",
            hint=f"limit must be 1..{tools._STATUS_MAX_LIMIT} "
                 f"(default {tools._STATUS_DEFAULT_LIMIT}).",
            remedy=f"hermes team status {t.slug}   # default limit "
                   f"{tools._STATUS_DEFAULT_LIMIT}",
        )
    tenant = t.tenant or t.slug          # the convention, §12
    counts = resolve.task_counts(t.board, tenant=tenant)
    if status and status not in counts:
        raise TeamError(
            E_TEAM_INVALID,
            f"unknown status {status!r}",
            hint="Statuses: " + ", ".join(sorted(counts)) + ".",
            remedy=f"hermes team status {t.slug}   # counts name the statuses",
        )
    rows = tools._list_team_tasks(t)
    awaiting = [r for r in rows if approvals.is_pending(r)]
    stuck = [r for r in rows if approvals.is_stuck(r)]
    # Additive, beside the frozen six-bucket `counts` (§4.3) — never inside
    # it. `counts` answers "where is the work"; `parked` answers "what is
    # waiting on me", which is the question `counts` cannot express.
    parked = approvals.lane_counts(t)
    open_rows = [r for r in rows
                 if (not status and r.status in tools._OPEN_STATUSES)
                 or (status and r.status == status)]
    if assignee:
        open_rows = [r for r in open_rows if r.assignee == assignee]
    open_rows.sort(key=lambda r: (-int(r.priority or 0), str(r.created_at or "")))
    shown = open_rows[:limit]

    warning = None
    if not t.tenant:
        warning = (f"team {t.slug} carries no tenant label — this view is "
                   f"scoped to the conventional tenant={t.slug!r} and reads "
                   f"EMPTY for cards predating stamping (§12: 'team status "
                   f"empty though cards exist'); stamp them: hermes team "
                   f"adopt {t.slug} --board {t.board} --backfill-tenant "
                   f"--yes")
        # Same discipline as adopt's §12 warning: stderr in BOTH modes
        # (stdout keeps the --json contract of exactly one JSON object).
        print(f"WARNING: {warning}", file=sys.stderr)

    now = int(time.time())
    human = [f"team {t.slug}  board={t.board}  tenant={tenant}",
             "  " + "  ".join(f"{k}={counts[k]}" for k in resolve.COUNT_STATUSES)]
    if awaiting:
        human.append(f"  awaiting your approval: {len(awaiting)}  "
                     f"(hermes team approvals {t.slug})")
    if stuck:
        human.append(f"  stuck: {len(stuck)}  "
                     f"(hermes team release <card> --note \"…\")")
    for name in ("review", "triage", "guarded"):
        if parked.get(name):
            human.append(f"  {name}: {parked[name]}  "
                         f"(hermes team approvals {t.slug} --kind {name})")
    human.append(f"  {len(open_rows)} {'matching' if status else 'open'} "
                 f"card(s), showing {len(shown)} (limit {limit})")
    awaiting_ids = {r.id for r in awaiting}
    for r in shown:
        mark = " *" if r.id in awaiting_ids else "  "
        human.append(f"  {mark}{r.id:<8} {r.status:<8} "
                     f"{(r.assignee or '-'):<16} {_age_h(r.created_at, now):>4}  "
                     f"{r.title}")
    if awaiting_ids & {r.id for r in shown}:
        human.append("    (* awaits your approval)")
    if not shown:
        human.append("    (none)")
    # (a warning, if any, was already printed to stderr in both modes)

    payload = {
        "team": t.slug,
        "board": t.board,
        "tenant": tenant,
        "counts": counts,
        "open_cards": [tools._task_view(r) for r in shown],
        "awaiting_approval": len(awaiting),
        "awaiting_approval_ids": [r.id for r in awaiting],
        "stuck": len(stuck),
        "stuck_ids": [r.id for r in stuck],
        "parked": parked,
        "total_matching": len(open_rows),
        "status": status,
        "assignee": assignee,
        "limit": limit,
        "_human": human,
    }
    if warning:
        payload["warning"] = warning
    _emit(payload, json_mode=args.json)
    return 0


# ── approvals: the operator inbox ────────────────────────────────────────────
#
# The queue is a VIEW over the board, never new state. Two lanes, both inside
# the team's tenant: a card waits on a human exactly when
# block_kind='needs_input' and status is 'blocked' (or 'triage' after a repeat
# block), and it is stuck when it is blocked for anything else (the dispatcher
# gave up on it, or an agent parked it on a capability it does not have). The predicates, the comment bodies and the refusals all live
# in ``approvals.py`` so this CLI and the dashboard routes cannot drift.


def _find_card(card: str, team, reg):
    """``(team_record, detail)`` for *card*. Without *team* every team is tried,
    which is what lets the operator paste a card id straight out of a ping."""
    from . import approvals
    if team:
        t = approvals.team_or_raise(team, reg=reg)
        return t, approvals.detail(t, card)
    for slug in sorted(reg.teams):
        try:
            t = reg.teams[slug]
            return t, approvals.detail(t, card)
        except (TeamError, OSError):
            continue
    raise TeamError(
        E_TEAM_NOT_FOUND,
        f"no card {card!r} on any team",
        hint="Known teams: " + (", ".join(sorted(reg.teams)) or "(none)"),
        remedy="hermes team approvals")


# One headline noun and one row marker per lane, in QUEUE_KINDS order.
_LANE_NOUN = {
    "needs_input": "awaiting your approval",
    "stuck": "stuck",
    "review": "waiting in review",
    "triage": "in triage",
    "guarded": "held by the dispatcher",
}
_LANE_MARK = {"stuck": "STUCK   ", "review": "REVIEW  ",
              "triage": "TRIAGE  ", "guarded": "GUARDED "}

# One sentence per lane for ``--show``: why it is parked, and what ends it.
_LANE_NOTE = {
    "stuck": "this card is STUCK — blocked with nobody to answer; release "
             "it to put it back in the pool",
    "review": "the work is finished and waiting for your verdict; approving "
              "completes the card and releases anything waiting behind it",
    "triage": "this card was routed to triage after repeated blocks for the "
              "same cause; releasing it sends it back to todo",
    "guarded": "the dispatcher is holding this card back and will not respawn "
               "it; fix the cause first, then release it",
}

# ``check_respawn_guard``'s reason codes, said in operator words. The codes
# themselves are the substrate's; only this gloss is ours.
_GUARD_GLOSS = {
    "blocker_auth": "a quota / auth / billing error is still on the card — "
                    "fix it outside Hermes, then release",
    "rate_limit_cooldown": "waiting out a provider rate-limit cooldown; this "
                           "one clears itself",
    "recent_success": "a run completed recently — release only if it really "
                      "should run again",
    "active_pr": "a pull request is open on this card; re-spawning risks a "
                 "duplicate",
}


def _queue_headline(rows: list) -> str:
    """``N awaiting your approval, M stuck, …`` — whichever lanes exist."""
    from . import approvals
    counts: dict = {}
    for r in rows:
        counts[r.get("reason_kind")] = counts.get(r.get("reason_kind"), 0) + 1
    parts = [f"{counts[k]} card(s) {_LANE_NOUN[k]}"
             for k in approvals.QUEUE_KINDS if counts.get(k)]
    return ", ".join(parts)


def _lane_verbs(r: dict) -> list:
    """The command(s) that move this card, one per line. A lane with no verb
    printed is how the Release button became a dead end."""
    tid = r["id"]
    if r.get("reason_kind") in ("needs_input", "review"):
        return [f"      approve: hermes team approve {tid} --note \"…\"",
                f"      reject:  hermes team reject {tid} --reason \"…\""]
    return [f"      release: hermes team release {tid} --note \"…\""]


def _lane_why(r: dict) -> list:
    """Why this card is parked, in at most a couple of lines."""
    lane = r.get("reason_kind")
    lines = []
    if lane in ("stuck", "guarded"):
        gloss = _GUARD_GLOSS.get(r.get("guard_reason") or "")
        if gloss:
            lines.append(f"      {r['guard_reason']}: {gloss}")
        why = r.get("failure_error") or r.get("block_reason")
        if why and not (lane == "guarded" and gloss):
            lines.append(f"      {str(why).splitlines()[0]}")
        if lane == "stuck":
            kind = r.get("block_kind")
            if kind == "capability":
                lines.append("      block_kind=capability — the agent could "
                             "not do it; fix the cause before releasing")
            elif not kind:
                lines.append("      the dispatcher gave up; a release is the "
                             "whole remedy")
            if r.get("consecutive_failures"):
                lines.append(f"      {r['consecutive_failures']} consecutive "
                             "failure(s) before it was blocked")
        return lines
    if lane == "review":
        lines.append("      the work is finished and waiting for your verdict")
    elif lane == "triage":
        lines.append(f"      routed to triage after "
                     f"{r.get('block_recurrences') or 'repeated'} blocks for "
                     "the same cause")
    if r.get("headline"):
        lines.append(f"      {r['headline']}")
    elif r.get("block_reason"):
        lines.append(f"      {r['block_reason']}")
    return lines


def _approval_lines(rows: list, now: int, *, with_team: bool) -> list:
    """The queue as the operator reads it: one card per block, then what it is
    waiting for, then the exact command that ends the wait."""
    if not rows:
        return ["nothing is waiting on you"]
    lines = [_queue_headline(rows)]
    for r in rows:
        age = _age_h((now - r["age_seconds"]) if r["age_seconds"] is not None else None, now)
        who = f"{r['team']:<12} " if with_team else ""
        mark = _LANE_MARK.get(r.get("reason_kind"), "")
        lines.append(f"  {r['id']:<12} {who}{(r['assignee'] or '-'):<24} "
                     f"{age:>4}  {mark}{r['title']}")
        lines.extend(_lane_why(r))
        if r.get("attachments"):
            lines.append("      artifacts: " + ", ".join(
                f"{a['filename']} [#{a['id']}, {_human_bytes(a['size'])}]"
                for a in r["attachments"]))
        lines.append(f"      read:    hermes team approvals --show {r['id']}")
        lines.extend(_lane_verbs(r))
    return lines


def _summary_lines(summary) -> list:
    """``--summary``: the briefing and its options, or why there is none."""
    if not summary:
        return []
    state = summary.get("state")
    if state == "off":
        return ["  --- summary ---",
                "    off (settings.approval_summary)"]
    if state == "error":
        return ["  --- summary ---",
                f"    unavailable: {summary.get('error')}"]
    if state != "ready":
        return []
    lines = ["  --- summary ---"]
    lines.extend("    " + ln for ln in str(summary["summary"]).splitlines())
    if summary.get("question"):
        lines.append(f"    decide: {summary['question']}")
    for n, opt in enumerate(summary.get("options") or [], 1):
        star = " (recommended)" if opt.get("recommended") else ""
        lines.append(f"    {n}. [{opt['verb']}] {opt['label']}{star}")
        lines.extend("       " + ln for ln in str(opt["detail"]).splitlines())
    for risk in summary.get("risks") or []:
        lines.append(f"    risk: {risk}")
    return lines


def _detail_lines(t, d: dict) -> list:
    lines = [f"{d['id']}  team={t.slug}  board={t.board}  "
             f"status={d['status']}/{d.get('block_kind') or '-'}",
             f"  {d['title']}",
             f"  assignee: {d.get('assignee') or '-'}"]
    lane = d.get("reason_kind")
    if lane in ("stuck", "guarded"):
        lines.append(f"  NOTE: {_LANE_NOTE[lane]}")
        if d.get("guard_reason"):
            gloss = _GUARD_GLOSS.get(d["guard_reason"], "")
            lines.append(f"  guard: {d['guard_reason']}"
                         + (f" — {gloss}" if gloss else ""))
        if d.get("failure_error"):
            lines.append(f"  last failure: {d['failure_error']}")
        if d.get("consecutive_failures"):
            lines.append(f"  consecutive failures: {d['consecutive_failures']}")
        lines.append(f"  release: hermes team release {d['id']} --note \"…\"")
    elif lane == "triage":
        lines.append(f"  NOTE: {_LANE_NOTE[lane]}")
        lines.append(f"  release: hermes team release {d['id']} --note \"…\"")
    elif lane == "review":
        lines.append(f"  NOTE: {_LANE_NOTE[lane]}")
        lines.append(f"  approve: hermes team approve {d['id']} --note \"…\"")
        lines.append(f"  reject:  hermes team reject {d['id']} --reason \"…\"")
    elif lane != "needs_input":
        lines.append("  NOTE: this card is not waiting on a human right now")
    if d.get("block_reason"):
        lines.append(f"  blocked because: {d['block_reason']}")
    lines.extend(_summary_lines(d.get("summary")))
    if d.get("body"):
        lines.append("  --- body ---")
        lines.extend("  " + ln for ln in str(d["body"]).splitlines())
    for c in d.get("comments", []):
        lines.append(f"  --- comment #{c['id']} by {c['author']} ---")
        lines.extend("  " + ln for ln in str(c["body"] or "").splitlines())
    if d.get("attachments"):
        lines.append("  --- artifacts ---")
        for a in d["attachments"]:
            mark = "" if a["previewable"] else "  (binary — not previewable)"
            lines.append(f"    #{a['id']:<4} {a['filename']}  "
                         f"{_human_bytes(a['size'])}{mark}")
        lines.append(f"  read one: hermes team approvals --show {d['id']} "
                     f"--artifact {d['attachments'][0]['filename']}")
    return lines


def _pick_attachment(d: dict, wanted: str) -> dict:
    """Attachment by id (``#12``/``12``) or by exact filename."""
    atts = d.get("attachments") or []
    raw = str(wanted).lstrip("#")
    if raw.isdigit():
        match = [a for a in atts if int(a["id"]) == int(raw)]
    else:
        match = [a for a in atts if a["filename"] == wanted]
    if not match:
        raise TeamError(
            E_TEAM_INVALID,
            f"card {d['id']} has no artifact {wanted!r}",
            hint="Artifacts: " + (", ".join(a["filename"] for a in atts) or "(none)"),
            remedy=f"hermes team approvals --show {d['id']}")
    return match[0]


def _cmd_approvals(args) -> int:
    """``hermes team approvals [<team>] [--show CARD [--artifact NAME]]``."""
    import time
    from . import approvals
    reg = storage.load()
    now = int(time.time())

    if args.show:
        t, d = _find_card(args.show, args.team, reg)
        if not args.artifact:
            # Opt-in: the listing above is read constantly and must stay free,
            # and this process is too short-lived to amortize a model call.
            if getattr(args, "summary", False):
                from . import summarize
                d["summary"] = summarize.summary(t, d["id"])
            d["_human"] = _detail_lines(t, d)
            _emit(d, json_mode=args.json)
            return 0
        att = _pick_attachment(d, args.artifact)
        blob = approvals.artifact(
            t, att["id"],
            max_bytes=(args.max_bytes or approvals.ARTIFACT_TAIL_DEFAULT))
        if args.json:
            _emit({"team": t.slug, "card": d["id"], "artifact": blob},
                  json_mode=True)
            return 0
        # Artifact bytes are DATA, not the human report: stdout, so
        # `… --artifact design.md | head -40` works. Everything about the
        # artifact goes to stderr, keeping the two streams separable.
        print(f"# {att['filename']}  ({_human_bytes(att['size'])})",
              file=sys.stderr)
        if blob.get("text") is None:
            print(f"  {blob.get('note') or 'not previewable'}", file=sys.stderr)
            return 0
        sys.stdout.write(blob["text"])
        if blob.get("truncated"):
            print("\n# … truncated; raise the cap with --bytes N",
                  file=sys.stderr)
        return 0

    kind = (getattr(args, "kind", None) or "all").strip()
    # ``None`` — not the whole tuple — so the default view honours the
    # ``parked_lanes`` setting; naming a lane overrides it.
    kinds = None if kind == "all" else (kind,)
    if args.team:
        t = approvals.team_or_raise(args.team, reg=reg)
        rows = approvals.queue(t, kinds=kinds)
        with_team = False
    else:
        rows = approvals.queue_all(reg, kinds=kinds)
        with_team = True
    payload = {"team": args.team, "kind": kind, "count": len(rows),
               "counts": _lane_counts(rows), "approvals": rows,
               "_human": _approval_lines(rows, now, with_team=with_team)}
    _emit(payload, json_mode=args.json)
    return 0


def _lane_counts(rows: list) -> dict:
    """``{lane: n}`` over every lane — what each surface shows as its badge.

    Every lane is present even at zero, so a consumer can index it without
    a ``.get`` and a badge never flickers between two key sets.
    """
    from . import approvals
    out = dict.fromkeys(approvals.QUEUE_KINDS, 0)
    for r in rows:
        kind = r.get("reason_kind")
        if kind in out:
            out[kind] += 1
    return out


_DECISION_VERBS = {"approve": "approved", "reject": "changes requested on",
                   "release": "released", "note": "noted on"}

# What a note's operator reads instead of the unblock advice: a note never
# unblocks, so the only question left is when the agent hears it.
_NOTE_DELIVERY = {
    "mid-run": "  the running worker picks it up within ~6s",
    "next_run": "  the card is not running — delivered when it next runs",
}


def _transition_line(res: dict) -> str:
    """What the card did, in the words of the move that did it. Five lanes
    share three verbs, so the transition — not the verb — names the outcome."""
    who = res.get("assignee") or "its assignee"
    move = res.get("transition")
    if move == "completed":
        return (f"  card is now {res['status']!r} — anything waiting behind "
                "it can start")
    if move == "reopened":
        return f"  card is back with {who} for another round"
    if move == "specified":
        return "  card is back in the pool as 'todo'"
    if move == "cleared_guard":
        return (f"  the dispatcher's hold is cleared — card is "
                f"{res['status']!r} and can be picked up again")
    return f"  card is now {res['status']!r} — {who} resumes it"


def _decision_lines(res: dict) -> list:
    verb = _DECISION_VERBS.get(res["action"], res["action"])
    lines = [f"{verb} {res['task_id']} ({res['team']}): {res['title']}",
             f"  comment #{res['comment_id']} by {res['author']}"]
    if res["action"] == "note":
        lines.append(_NOTE_DELIVERY.get(res.get("delivery"),
                                        f"  card is {res['status']!r}"))
    elif res["unblocked"]:
        lines.append(_transition_line(res))
    else:
        lines.append(f"  card stays {res['status']!r} (parked); unblock it with: "
                     f"hermes kanban unblock {res['task_id']}")
    if res.get("priority_raised"):
        lines.append(f"  priority {res['priority']} — the dispatcher takes "
                     "this ahead of the backlog")
    return lines


def _cmd_approve(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.approve(t, args.card, args.note)
    res["_human"] = _decision_lines(res)
    _emit(res, json_mode=args.json)
    return 0


def _cmd_reject(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.reject(t, args.card, args.reason, park=args.park)
    res["_human"] = _decision_lines(res)
    _emit(res, json_mode=args.json)
    return 0


def _cmd_release(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.release(t, args.card, args.note)
    res["_human"] = _decision_lines(res)
    _emit(res, json_mode=args.json)
    return 0


def _cmd_note(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.note(t, args.card, args.text)
    res["_human"] = _decision_lines(res)
    _emit(res, json_mode=args.json)
    return 0


def _pause_lines(res: dict) -> list:
    lines = [f"paused {len(res['paused'])} card(s) in thread {res['root_id']} "
             f"({res['team']}): {', '.join(res['paused'])}"]
    for skip in res["skipped"]:
        lines.append(f"  left running: {skip['id']} ({skip['reason']})")
    if res["not_killed"]:
        lines.append("  worker not stopped (another host, or already gone): "
                     + ", ".join(res["not_killed"]))
    lines.append(f"  resume with: hermes team resume {res['task_id']}")
    return lines


def _cmd_pause(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.pause(t, args.card)
    res["_human"] = _pause_lines(res)
    _emit(res, json_mode=args.json)
    return 0


def _cmd_resume(args) -> int:
    from . import approvals
    reg = storage.load()
    t, _d = _find_card(args.card, args.team, reg)
    res = approvals.resume(t, args.card)
    res["_human"] = (
        [f"resumed {len(res['resumed'])} card(s) in thread {res['root_id']} "
         f"({res['team']})"]
        + [f"  {tid}: {status}" for tid, status in res["statuses"].items()])
    _emit(res, json_mode=args.json)
    return 0


_COMMANDS = {
    "list": _cmd_list,
    "show": _cmd_show,
    "create": _cmd_create,
    "adopt": _cmd_adopt,
    "status": _cmd_status,
    "approvals": _cmd_approvals,
    "approve": _cmd_approve,
    "reject": _cmd_reject,
    "release": _cmd_release,
    "note": _cmd_note,
    "pause": _cmd_pause,
    "resume": _cmd_resume,
    "destroy": _cmd_destroy,
    "add-member": _cmd_add_member,
    "remove-member": _cmd_remove_member,
    "doctor": _cmd_doctor,
    "profiles": _cmd_profiles,
    "recipes": _cmd_recipes,
    "sync": _cmd_sync,
    "set-board": _cmd_set_board,
    "set-workdir": _cmd_set_workdir,
    "merge": _cmd_merge,
    "clone": _cmd_clone,
    "export": _cmd_export,
    "import": _cmd_import,
}


def handle(args) -> int:
    """Dispatch a parsed ``hermes team`` invocation; returns the exit code."""
    cmd = getattr(args, "team_command", None)
    if cmd is None:
        print("usage: hermes team {list,show,create,adopt,status,approvals,"
              "approve,reject,release,note,pause,resume,destroy,add-member,"
              "remove-member,doctor,"
              "profiles,recipes,sync,set-board,set-workdir,merge,clone,export,"
              "import} ...",
              file=sys.stderr)
        return 2
    fn = _COMMANDS.get(cmd)
    if fn is None:
        print(f"hermes team: unknown subcommand {cmd!r}", file=sys.stderr)
        return 2
    try:
        return fn(args)
    except TeamError as err:
        return _fail(err, json_mode=getattr(args, "json", False),
                     team=getattr(args, "team", None))
