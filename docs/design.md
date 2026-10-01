# Synergia Design

This document describes how **hermes-agent-synergia** works: the data it owns, the parts of Hermes it hooks into, and the rules that keep a team's agents inside the team's board and work directory. For installation, see the [README](../README.md).

---

## 1. Goals

Hermes gives you profiles, Kanban boards, and work directories as separate things. Nothing ties them together. Synergia adds the missing unit, the **team**:

- A team groups existing or new profiles and gives each one a role.
- A team binds those profiles to **one** Kanban board and **one** shared work directory.
- Synergia keeps that binding true over time. It reports drift and repairs it on request.
- Synergia keeps team agents inside the binding. A card that an agent creates lands on the team board, in the team tenant, assigned to a team member.
- An operator can follow the work, answer questions that agents park for a human, and collect the result, from the CLI or the dashboard.

### Non-goals

- Synergia does not replace the Kanban dispatcher. Hermes still claims, runs, and retries cards.
- Synergia does not police file access. `terminal`, `write_file`, `patch`, and `execute_code` are never guarded. Path policy belongs to Hermes' own approval system.
- Synergia does not keep a member profile in sync with the catalog entry it came from. After instantiation, `SOUL.md`, `skills/`, and the body of `config.yaml` belong to the operator.
- Agents cannot clone a team or change a roster. Those actions write other profiles' configuration, so they are CLI and dashboard actions where a human approves them.

---

## 2. Concepts

| Term | Meaning |
|---|---|
| **Team** | A record in the registry (`teams.yaml`). It names a slug, a board, a work directory, a tenant, an orchestrator, a policy, and a member list. |
| **Member** | A Hermes profile on a team, with a `role`. A member is either **adopted** (an existing profile; Synergia never deletes it) or **managed** (an instance that Synergia created from a catalog entry and owns). |
| **Orchestrator** | The member that receives work by default. It decomposes work and routes cards to other members. Only the orchestrator sees the routing tools. |
| **Primary** | A flag on a member. It marks the team a profile acts for when nothing else identifies the team. A profile is primary on at most one team. |
| **Board** | The Kanban board the team works on. By default one board belongs to one team. Two teams can share a board only when both set `shared_board`. |
| **Tenant** | A label stamped on every card the team creates. It separates the team's cards on a shared board. An empty tenant means the team slug. |
| **Work directory** (`workdir`) | The absolute path where the team's work lands. |
| **Workspace policy** | How a card gets its workspace: `scratch` (temporary, deleted on completion), `worktree` (a git worktree of the workdir per card), or `shared` (the workdir itself). |
| **Thread** | A root card and every card under it. A root is the card an operator created; the cards that agents create from it form the thread. |
| **Step label** | The human name of a card (`step-<n>-<slug>`), derived from the `step <n>` in its title and the rest of the title. Synergia shows cards and names retired branches by this label instead of the raw card id. |
| **Catalog** | The list of profile blueprints that a team can instantiate members from. |
| **Lane** | A class of parked card in the approval inbox: `needs_input`, `review`, `stuck`, `triage`, or `guarded`. |

---

## 3. Architecture

Synergia is a standalone Hermes plugin. One Python core serves three surfaces.

```
                 ┌──────────────────┐  ┌───────────────────┐  ┌────────────────────────┐
  surfaces       │  hermes team CLI │  │  team toolset     │  │  Synergia dashboard tab│
                 │  cli.py          │  │  tools.py         │  │  plugin_api.py + dist/ │
                 └────────┬─────────┘  └─────────┬─────────┘  └───────────┬────────────┘
                          │                      │                        │
                          ▼                      ▼                        ▼
  core           storage · model · catalog · catalog_store · provision · reconcile
                 resolve · approvals · summarize
                          │
  hot path       hooks ──► policy, prompt          (pure, no I/O)
                          │
                          ▼
  substrate      hermes_cli (kanban_db, profiles, config, profile_distribution, …)
                 hermes_constants · agent.plugin_llm
```

The CLI and the HTTP API are thin wrappers over the same core functions. No operation has a second implementation, so the surfaces cannot drift apart.

### 3.1 Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | `register(ctx)`: registers tools, the CLI command, hooks, prompt sections, the worker-exit guard, and sample seeding. |
| `model.py` | Team records, field validation, registry-wide invariants, clone derivation, step labels. Pure, no file system access. |
| `storage.py` | The only writer of `teams.yaml`. Registry resolution, parsing, caching, and the shared locked write path. |
| `policy.py` | The `pre_tool_call` decision table and team resolution. Pure, imports only `typing`. |
| `prompt.py` | The team-contract and attachment sections of the system prompt. Pure. |
| `hooks.py` | The I/O edge of the hot path: registry freshness, the policy hook, and the Kanban observers. |
| `schemas.py` | The ten tool schemas. Pure. Their hashes are frozen by a test. |
| `tools.py` | The ten tool handlers, their visibility checks, and the response envelope. |
| `resolve.py` | The only reader of Hermes state, plus the only `config.yaml` writer, git operations on the workdir, merges, and thread reports. |
| `reconcile.py` | Desired versus observed state: the drift report (`diff`) and the repair (`apply`). |
| `provision.py` | The only module that creates or deletes a profile. |
| `catalog.py` | The merged, read-only view of the catalog. |
| `catalog_store.py` | The plugin's own writable catalog store and sample seeding. |
| `approvals.py` | The approval inbox, operator verbs, pause and resume, notifications, auto-merge, and the review-loop guard. |
| `summarize.py` | Model-written summaries of decision cards, cached per card revision. |
| `worker_exit_guard.py` | The `$HERMES_BIN` wrapper that makes a dead-on-arrival worker exit non-zero. |
| `errors.py` | `TeamError` and the error-code table. |
| `cli.py` | The `hermes team` command tree. |
| `dashboard/plugin_api.py` | The HTTP API under `/api/plugins/hermes-agent-synergia/`. |
| `dashboard/jobs.py` | The in-process job registry for long dashboard operations. |
| `dashboard/dist/` | The dashboard UI: a hand-written React bundle with no build step. |
| `samples/profiles/` | Five bundled sample profiles for a software team. |

### 3.2 Layering rules

The layering keeps upstream changes cheap to absorb and keeps the hot path fast. `scripts/lint_imports.py` enforces the rules on every module.

- Module scope imports nothing heavy. Every handler imports its dependencies lazily, so plugin discovery costs one `import` and the argparse wiring.
- Only six files import `hermes_cli`: `resolve.py`, `provision.py`, `cli.py`, `tools.py`, `approvals.py`, and `dashboard/plugin_api.py`. The lint fails if a seventh appears.
- `hermes_cli.profile_distribution` and the profile-deleting symbols appear only in `provision.py`.
- `catalog.py` and `catalog_store.py` import nothing from `hermes_cli`.
- `policy.py`, `prompt.py`, and `schemas.py` import only `__future__` and `typing`.
- Only `storage.py` holds a live string constant that names `teams.yaml`.
- Only public substrate symbols are used, with a small set of named, documented exceptions in `resolve.py`. Where a private helper is needed (for example, the board slug regex), the plugin keeps its own copy and a test asserts that the copy matches.
- `scripts/lint_imports.py` also runs the Hermes plugin compatibility scanner. Any hit blocks a release, because Hermes skips a flagged plugin entirely after its compatibility removal date.

---

## 4. Data model

### 4.1 Registry location

The registry is host-global, not per profile. `storage.registry_dir()` resolves it in this order:

1. The `HERMES_TEAM_HOME` environment variable.
2. The `registry_dir` plugin setting in the root `config.yaml`.
3. `<hermes root>/plugin-data/hermes-agent-synergia/`.

Synergia never uses `plugin_data_dir()`, because that path follows `HERMES_HOME`. Under `hermes -p <profile>`, it would point at a profile home and split the registry per profile.

The directory holds:

```
<registry_dir>/
  teams.yaml            the registry
  teams.yaml.bak        the previous version
  teams.yaml.lock       the writer lock
  catalog/
    catalog.yaml        the catalog index
    profiles/<id>/      one distribution per catalog entry
```

### 4.2 Team record

`teams.yaml` is a mapping with `schema_version`, `revision`, and `teams`. Each team record has these fields (`model.Team`):

| Field | Notes |
|---|---|
| `slug` | `^[a-z0-9][a-z0-9\-_]{0,63}$`. Never `default`. |
| `display_name`, `description` | Free text. Sanitized before it reaches a prompt. |
| `revision`, `created_at`, `updated_at` | Bookkeeping. |
| `board`, `shared_board` | The bound board, and whether another team may also bind it. |
| `workdir` | Absolute after `expanduser`, with no `..` component. |
| `workspace_policy` | `scratch`, `worktree`, or `shared`. |
| `tenant`, `tenant_stamping` | The card label, and `auto`, `always`, or `never`. |
| `profile_naming` | The template for instance names. Default `{team}-{short}`. |
| `project` | An optional Hermes project binding (`mode`, `slug`, `id`). |
| `orchestrator` | A member profile. |
| `members[]` | `profile`, `role`, `primary`, `capacity`, `blueprint`, `blueprint_source`, `managed`, `skills`. |
| `policy` | `mode` (`off`, `advise`, `enforce`), `allow_foreign_board`, `allow_non_member_assignee`, `guarded_tools`. |
| `sync` | Switches for what reconciliation writes: `terminal_cwd`, `board_default_workdir`, `profile_descriptions`, `orchestrator_config`, `toolsets`, `write_agents_md`. |
| `metadata` | Free-form. |

Unknown keys round-trip unchanged at every level, so a newer writer does not lose data when an older reader saves.

### 4.3 Write discipline

Every YAML store under the registry directory goes through one function, `storage.save_yaml_file`:

1. Take an exclusive `flock` on `<file>.lock`. Retry five times, 200 ms apart, then fail with `E_TEAM_LOCKED`.
2. Re-read the file under the lock, without the cache.
3. Compare the current `revision` with the caller's `expected_revision`. On a mismatch, fail with `E_TEAM_CONFLICT`. The caller re-reads and retries.
4. Copy the current file to `.bak`.
5. Write the new content with `revision + 1` atomically (temporary file, then rename).

Every mutating CLI command and dashboard route passes the registry revision it read. Two concurrent edits therefore produce one success and one retryable conflict, never a lost write.

Reads are cached on the `(st_mtime_ns, st_size)` pair of the file. A warm read costs one `stat`.

---

## 5. Integration with Hermes

`register(ctx)` in `__init__.py` connects the plugin to Hermes:

| Registration | Purpose |
|---|---|
| 10 tools in the `team` toolset | The agent surface (section 7). |
| CLI command `team` | The operator surface (section 12). |
| `pre_tool_call` hook | The routing policy (section 6). Registered only for a profile that is on a team with `policy.mode != off`, so other profiles pay nothing. |
| System prompt section `hermes-agent-synergia.contract` | The team contract (section 6.4). |
| System prompt section `hermes-agent-synergia.attachments` | Guidance for `team_attach_path`. |
| `kanban_task_claimed` | Logs the tenant of a claimed card. Observability only. |
| `kanban_task_blocked` | Pushes a card that is blocked on a human to the home channels. |
| `kanban_task_completed` | Auto-merges a finished worktree thread (section 9). |
| `on_kanban_dispatch_tick` | Handles events that no other hook reports (section 8.4). |
| Worker-exit guard | Installs the `$HERMES_BIN` wrapper (section 10). |
| Sample seeding | Adds the bundled sample profiles to the catalog once. |
| `on_unload` | Drops the caches. |

All hooks are observers that never raise. A failure in Synergia must never be the reason that Hermes cannot run a tool or dispatch a card.

---

## 6. Keeping agents inside the team

### 6.1 Which team a session acts for

The `pre_tool_call` payload has no profile field. `policy.resolve_team` identifies the team from the environment, in this order:

1. `HERMES_TENANT` names the team whose effective tenant matches. The dispatcher sets it for a worker, so this step is exact.
2. `HERMES_KANBAN_BOARD` names the team that owns the board.
3. `HERMES_TEAM` names the team by slug.
4. The calling profile's primary team.
5. Otherwise, no team.

If one step matches more than one team, the result is no team. Synergia does not guess, because acting for the wrong team is worse than not acting.

### 6.2 The hot path

`pre_tool_call` runs once for every tool call on the host, inside a timeout that fails closed. The hook is therefore a short ladder:

1. If the tool is not in `GUARDED_TOOLS`, return. This is a `frozenset` lookup, and most calls end here.
2. Check registry freshness with one `stat`. On the same `(mtime, size)`, reuse the cached parse.
3. Look up the profile in a prebuilt profile-to-teams index.
4. Call `policy.resolve_team` and `policy.decide`. Both are pure.

The hook opens no network connection, subprocess, database, or lock. If the registry is unreadable, the hook uses the last good parse (or no teams) and logs once. Any internal exception becomes "allow unchanged".

### 6.3 The decision table

`policy.decide(team, profile, tool_name, args)` returns `{"modify": {...}}`, `{"block": message}`, or `None`. Rows run top-down. Modify rows merge into one directive. A block row ends evaluation. Directives replace whole top-level argument keys only, never dotted paths.

`GUARDED_TOOLS` is `kanban_create`, `kanban_link`, `kanban_unblock`, and `kanban_list`. Every other tool reaches no rule.

**`kanban_create`**

| Condition | `advise` | `enforce` |
|---|---|---|
| `board` is absent or not the team board | Set `board` to the team board. | Same. |
| `tenant` is absent and stamping is not `never` | Set `tenant` to the effective tenant. | Same. |
| `assignee` is absent | Set `assignee` to the orchestrator. | Same. |
| `assignee` is not a member | Set `assignee` to the orchestrator and log a note. | Block, naming the members and the `add-member` command. |
| No workspace argument, and the policy is `worktree` or `shared` | Set `workspace_kind` and `workspace_path` to the team workdir. | Same. |

The workspace row exists because Hermes falls back to a scratch workspace when a card has no workspace arguments. Scratch is deleted on completion, so the work would never reach the team workdir.

**`kanban_link`, `kanban_unblock`, `kanban_list`**

| Condition | `advise` | `enforce` |
|---|---|---|
| `board` is absent or not the team board | Set `board` to the team board. | Block `kanban_link` and `kanban_unblock`, naming the legal board. Rewrite `kanban_list`. |

`mode: off` disables every row. Every block message names the team, the violation, the legal values, and a remedy, because a model that sees only "denied" retries the same call.

### 6.4 The team contract

At session start, Synergia renders a block of at most 1000 characters into the system prompt:

```
## Team: <display name> (<slug>)
Description: <one line>
You are `<profile>`, role: <role>.
Board: <board>   Work directory: <workdir>   Tenant: <tenant>
Members: <profile> (<role>[; primary][; you]), ...
Rules: create and assign cards only on board `<board>`, only to the members above.
Use team_create_task / team_assign. Card `tenant` must be `<tenant>`.
<role-specific closing line>
```

The six known roles (`orchestrator`, `implementer`, `reviewer`, `researcher`, `verifier`, `writer`) each have a closing line that states the role's boundary. Other roles get a generic line.

`teams.yaml` content reaches the system prompt, so the registry is a possible injection vector. Every interpolated field has its control characters, format characters (zero-width and bidi), and line breaks removed, and its whitespace collapsed. A description therefore cannot start a new line or forge a section header. When the block is over the cap, the description line is dropped first, then the tail is truncated.

---

## 7. Agent tools

The `team` toolset has ten tools:

| Tool | Visible to | What it does |
|---|---|---|
| `team_whoami` | All members | The caller's teams, role, board, workdir, and roster. |
| `team_list` | All members | The teams this profile belongs to. |
| `team_show` | All members | One team in full, with a resolved view of its world. |
| `team_status` | All members | Card counts and open cards in the team tenant. |
| `team_attach_path` | All members | Attach a file from the caller's task workspace to a card (25 MB cap). |
| `team_create_task` | Orchestrator | Create a card on the team board. Route by `assignee` or by `role`. |
| `team_assign` | Orchestrator | Reassign a team card to a member. |
| `team_handoff` | Orchestrator | Complete the current card and create its successor in one call. |
| `team_sync` | Orchestrator | The drift report. Dry run only; the handler has no path to `apply`. |
| `team_catalog` | Orchestrator | Browse the catalog to propose a roster change. Writes nothing. |

**Visibility.** A tool is invisible in a `delegate_task` child context and to a profile that is on no team. Orchestrator-only tools are visible only to the orchestrator of its team. Every visibility check fails closed: a tool that cannot tell where it runs is hidden, and its mutations are refused.

**Routing.** The board, tenant, and workspace come from the team record, never from tool arguments. A `team` argument can only narrow the choice to a team that the caller is on.

**Envelope.** Every handler returns one of:

```json
{"ok": true,  "team": "<slug>", "data": {...}}
{"ok": false, "team": "<slug>", "error": {"code": "...", "message": "...", "hint": "...", "remedy": "..."}}
```

**Schema freeze.** The schema text is model-facing behavior. `tests/data/schema_hashes.json` holds a SHA-256 hash of each schema, and a test fails when a schema changes. To accept a change, run `scripts/lint_imports.py --update-schema-hashes`, review the diff, and commit it. The ten schemas together stay under about 1200 tokens.

---

## 8. Provisioning, catalog, and reconciliation

### 8.1 Catalog

`catalog.load()` merges two sources, local first:

1. The plugin's own store, `<registry_dir>/catalog/`. Entries come from `hermes team profiles add|import|adopt`, from the dashboard, and from the bundled samples. The index also records hidden ids and merged recipes.
2. Read-only external pack roots from the `catalog_paths` setting. Each root holds `packs.json`, which leads to pack manifests and `profiles/<name>/distribution.yaml`.

Profiles under the Hermes root are not a catalog source. A root profile becomes a blueprint only when an operator adopts it into the store.

Every catalog failure degrades instead of raising. A missing root, an unparseable index, or a row that names a missing directory drops that entry and records a `catalog.source_missing` item.

The store writes a profile directory before the index row that names it, and removes the directory after the row. A crash therefore leaves at worst an orphan directory, which `doctor` reports, and never a row that points at nothing.

On the first load after an install or upgrade, `seed_samples()` adds each bundled sample profile once. A sample that the operator removed stays removed.

### 8.2 Instantiation

`provision.instantiate(entry, name, role)` creates a managed member. The order is fixed:

1. Install the distribution into a new profile.
2. Write the profile metadata, stamped with the source of the distribution.
3. Create the plugin symlink `<profile>/plugins/hermes-agent-synergia`.
4. Add the plugin to the profile's `plugins.enabled`.

Provisioning never seeds the bundled Hermes skills, because a distribution ships its own skills and can opt out of the bundled ones on purpose. It never creates a shell alias. A fresh instance that has no model pin gets the root model.

A failed create is resumable. Re-running the same command adopts each instance whose stamped source matches the entry and creates only the rest. The registry write is the last step, so an interrupted create either left no team record or left one that the re-run completes.

### 8.3 Reconciliation

`reconcile.diff(team)` compares the team record with what Hermes holds and returns a list of drift items. Each item has a check id, a severity (`error`, `warn`, `info`), a subject, whether it is auto-fixable, and a remedy command. `diff` is a pure read.

The checks cover:

- **Members**: the profile exists and is not tombstoned, a managed instance is present, its blueprint is known, the plugin link and `plugins.enabled` entry exist, `terminal.cwd`, capacity, toolsets, description, profile metadata, `.env` link, and model.
- **Board**: it exists, its default workdir, its display name, its project id, and board exclusivity.
- **Workdir**: it exists, it is a git repository (for `worktree`), and the optional `AGENTS.md`.
- **Orchestrator**: it is a member, and its Kanban configuration.
- **Other**: the project resolves, untagged cards on the board, the policy hook, catalog sources, and store orphans.

A subset of checks (`READY_CHECKS`) decides whether a team can take a task now. `hermes team create` and the dashboard drift route report the result as `ready`, with the checks that block it. The dashboard shows them on the team card and after a create.

`reconcile.apply` repairs drift in a fixed order: board, workdir, project, profile instances, then per-member configuration. It collects every key for one `config.yaml` into a single write. A failed item is recorded as not applied, and the run continues. When unfixable drift remains, the exit code is 4.

Four checks are never applied implicitly. Each one needs its own flag:

| Check | Flag |
|---|---|
| `board.exists` | `--create-board` |
| `workdir.exists` | `--create-workdir` |
| `member.instance_missing` | `--reinstantiate` |
| `member.env_link` | `--link-env` |

`--create-missing` lifts the board and workdir gates together.

When a profile configuration changes, the report carries a notice that the profile's gateway must restart to see it.

### 8.4 Writing profile configuration

`resolve.write_profile_config_keys` is the only writer of a profile's `config.yaml`. It edits the YAML text in place, so comments and layout survive. It writes only these keys:

```
terminal.cwd                      toolsets
kanban.orchestrator_profile       kanban.default_assignee
kanban.max_in_progress_per_profile
plugins.enabled                   skills.disabled
model    provider                 model.default    model.provider    model.base_url
```

A `provider` without a `model` in the same call is refused, because a bare provider would resolve the profile's model against a different backend. Before it writes, the writer saves `config.yaml.hermes-agent-synergia.bak` beside the file.

---

## 9. Human decisions and results

### 9.1 The approval inbox

The card is the whole state. `approvals.py` sorts parked cards into five disjoint lanes:

| Lane | Predicate | Verbs |
|---|---|---|
| `needs_input` | `blocked` or `triage`, and `block_kind = needs_input` | approve, reject |
| `review` | `review` | approve, reject |
| `stuck` | `blocked` with any other kind, or none | release |
| `triage` | `triage` with any other kind | release |
| `guarded` | `ready`, and the newest event is `respawn_guarded` | release |

The first two are **decision lanes**: an agent is waiting for a human answer. The others are **release lanes**: a card is parked and nothing would move it otherwise. For example, the dispatcher's circuit breaker blocks a card without firing `kanban_task_blocked`, and the review lane starves when `kanban.review_dispatch` is off.

Each verb is a comment plus one transition that depends on the lane:

| Lane | Approve | Reject | Release |
|---|---|---|---|
| `needs_input` | Comment `APPROVED: …`, unblock | Comment `CHANGES REQUESTED: …`, unblock | — |
| `review` | Complete the card | Reopen the card | — |
| `stuck` | — | — | Unblock |
| `triage` | — | — | Specify the triage card |
| `guarded` | — | — | Clear the guard |

Approving a `review` card is the one place where Synergia completes a card for the operator. The operator has made the decision that the card waited for, and an agent run would cost a spawn that cannot happen while review dispatch is off. In every other lane, the assignee resumes, reads the decision, and completes the card itself.

`note` adds a comment without moving the card. A verb on a card in the wrong lane fails with an error that names the lane and the verb that applies.

### 9.2 Getting the operator's attention

- **Push.** When an agent blocks a card on a human (`needs_input`), or the dispatcher gives up on a card, Synergia sends the card to every configured gateway home channel (setting `approvals_notify`).
- **Priority.** A card in a decision lane is raised to priority 9 (setting `decision_priority`; `0` turns this off), so the work that follows the answer runs before other ready work.
- **Summary.** On request, `summarize.py` asks the host's active model for a summary of a decision card: what it asks, and two to four concrete options. Each option carries its verb and a draft comment. The operator edits the draft before sending; nothing is decided automatically. Summaries are cached per card fingerprint, in memory and on disk, so a polling dashboard pays one model call per card revision.
- **Review-loop guard.** Each review round is counted. After `review_round_limit` rounds (default 3), the card is parked for a human instead of cycling between implementer and reviewer.

### 9.3 Pause and resume

Pause and resume act on a whole thread. Pause moves every running, ready, and todo card in the thread to `scheduled` with the reason `paused by operator`, and kills the running workers. A running reviewer is skipped and finishes its verdict.

Pause uses `scheduled`, not `blocked`. Every block counts toward Hermes' block-recurrence limit, so a second pause of the same card would send it to triage. A `scheduled` card is in no approval lane, fires no hook, and is never promoted by the dispatcher.

Resume re-queues every `scheduled` card in the thread. Each card is re-gated on its parents, so a child whose parent is not done returns to `todo`. A resumed card starts a fresh run: it keeps the workdir and the card history, but not the previous agent's context.

### 9.4 Merging a worktree thread

Hermes never merges a worktree card's branch into the workdir, and it keeps a finished worktree forever when the repository has no remote. Without Synergia, a finished thread's work stays under `<workdir>/.worktrees/<id>`, and the next thread branches from a HEAD that never saw it.

When the last card of a thread on a `worktree` team finishes, `on_task_completed` calls `resolve.merge_result(team, root, apply=True)`:

1. Find the thread's **heads**: done worktree cards whose branch no other done card's branch contains. A sequential chain of steps has one head, its last card.
2. Refuse if the main checkout has tracked changes.
3. Land the heads on the workdir's checked-out branch, oldest completion first. A fast-forward is used while the base is behind; otherwise a merge commit.
4. On the first conflict, abort that merge and stop. The heads already landed stay landed. The conflicting head and the heads after it are reported for a human merge.
5. Retire every card that the new HEAD contains: remove its worktree and rename its `wt/<id>` branch to `step-<n>-<slug>`.
6. Post a comment on the root card as `team:auto-merge`, naming the merged branches, the commit, and anything left behind with the command to finish it.

`hermes team merge <team> <card>` runs the same function by hand and shows a dry run without `--apply`. Set `auto_merge: false` to leave every merge to that command.

`resolve.thread_report` summarizes a finished thread for the dashboard: each card's run time and the tokens its worker spent, read from the member's session database in read-only mode.

---

## 10. Worker-exit guard

When a dispatched Hermes worker fails before it reaches the model (for example, a missing API key or an unreachable endpoint), Hermes prints the reason and exits with code 0. The dispatcher sees a clean exit on a card that is still running and books a protocol violation. After three of those, it parks the card with advice that has nothing to do with the real cause.

No hook can change a child's exit code, so `register()` installs `worker_exit_guard.py` as `$HERMES_BIN`. The dispatcher reads that variable on every spawn.

- If the process is not a dispatcher worker, or runs in fully quiet mode, the wrapper `exec`s the real Hermes with no extra process.
- Otherwise, the wrapper runs Hermes, relays its stdout unchanged, and watches for the turn-start marker that Hermes prints only after credentials and agent initialization succeed. Exit code 0 with no marker becomes exit code 1. Every other exit code passes through, including the rate-limit code 75.
- A child killed by a signal makes the wrapper die by the same signal, so the dispatcher still classifies it as signaled.
- Signals are forwarded to the child. On Linux, the child gets `PR_SET_PDEATHSIG`, so a killed wrapper cannot leave an orphan worker.

Set `worker_exit_guard: off` to disable the wrapper.

---

## 11. Dashboard

### 11.1 HTTP API

Hermes mounts `dashboard/plugin_api.py` at `/api/plugins/hermes-agent-synergia/`. Hermes loads this file by path, not as a package submodule, so it reaches the plugin's modules by the name that the plugin manager registered.

| Area | Routes |
|---|---|
| Teams | `GET /teams`, `GET /teams/{slug}`, `GET /teams/{slug}/status`, `POST /teams`, `DELETE /teams/{slug}`, `POST /teams/{slug}/members`, `DELETE /teams/{slug}/members/{profile}`, `POST /teams/{slug}/workdir` |
| Work | `POST /teams/{slug}/tasks`, `GET /teams/{slug}/tasks/{id}`, `GET …/log`, `POST …/pause`, `POST …/resume`, `GET …/report`, `GET`/`POST …/merge` |
| Approvals | `GET /approvals`, `GET /teams/{slug}/approvals`, `GET …/approval`, `GET …/summary`, `POST …/approve`, `…/reject`, `…/release`, `…/note`, `GET /teams/{slug}/attachments/{id}` |
| Workdir | `GET /teams/{slug}/files`, `GET …/files/read`, `GET …/files/download` |
| Health | `GET /teams/{slug}/drift`, `POST /teams/{slug}/sync`, `GET /doctor` |
| Catalog | `GET`/`POST /catalog`, `GET`/`PATCH`/`DELETE /catalog/{id}`, `POST /catalog/adopt`, `POST /catalog/import`, `POST /catalog/{id}/visibility`, `GET /root-profiles`, `GET /recipes`, `GET /skills` |
| Jobs | `GET /jobs/{id}` |

**Authentication.** The plugin has no credential system of its own. Hermes' dashboard session is the gate: the session token on a loopback dashboard, or the OAuth cookie session on a gated one. Each route re-checks that the request carries one of them, and fails closed when it does not.

**CSRF.** A mutating route requires a JSON content type and a same-origin `Host` and `Origin`. The origin check is a copy of the guard that Hermes runs on its own mutating WebSocket routes.

**Read-only mode.** With `dashboard_writes: false`, every non-GET route except a dry-run sync answers `405 DASHBOARD_READONLY`.

**Long operations.** `POST /teams` and `POST /teams/{slug}/members` provision profiles, which can take a while. They answer `202 {job_id}` and run in the background under one in-process lock, so two creates never interleave in the profile machinery. The client polls `GET /jobs/{id}`. Jobs live in memory; a restart drops them safely, because a re-run of the same request resumes the create (section 8.2).

**Workdir browsing.** File routes resolve paths inside the team workdir only and refuse credential files such as `auth.json`, `config.yaml`, `.git-credentials`, and OAuth token files, and the `mcp-tokens` and `pairing` directories. Previews are capped at 2 MB and downloads at 25 MB.

### 11.2 UI

`dashboard/dist/index.js` is a hand-written IIFE with no build step. It takes React from the host's plugin SDK, so it shares hook state with the dashboard shell. Every request goes through `SDK.fetchJSON`, which works in both authentication modes. Every CSS class has the `ht-` prefix.

The **Synergia** tab sits after **Skills** in the sidebar and opens on the team list.

- **Team list.** A card per team, with lane counts and the registry revision. Opening a card opens its chat. A **Profiles** panel manages the catalog: add, edit, import, adopt, hide, and remove entries.
- **Team chat.** A rail lists the teams. The open team folds out its members and their status. The main pane has three tabs:
  - **Chat**: pick a thread, read one entry per card transition, follow the running card's worker log, answer a parked card in place (with the artifacts and summary beside the decision), pause or resume the thread, and give a new task or steer the running one. A finished thread shows its run report and merge result.
  - **Kanban**: the team's board, tenant-scoped.
  - **Workdir**: browse, preview, and download files in the team workdir.
- **Team detail.** Roster, drift report and sync, workdir, and destroy.

Every mutation sends the registry revision it read. A `409` conflict asks the operator to reload instead of overwriting.

---

## 12. CLI

`hermes team` provides:

| Group | Commands |
|---|---|
| Registry | `list`, `show`, `create`, `destroy`, `add-member`, `remove-member`, `set-board`, `set-workdir` |
| Lifecycle | `clone`, `adopt`, `import`, `export` |
| Health | `doctor`, `sync`, `status` |
| Approvals | `approvals`, `approve`, `reject`, `release`, `note`, `pause`, `resume` |
| Results | `merge` |
| Catalog | `profiles list|show|sources|add|edit|import|adopt|remove|hide|unhide`, `recipes list` |

Every command honors two contracts:

- With `--json`, the command prints exactly one JSON object on stdout and nothing else. Human-readable text goes to stderr.
- Exit codes: `0` success, `1` failure, `2` usage error, `3` locked or revision conflict (retryable), `4` unfixable drift.

`clone` copies a team's roster and settings but never its board, workdir, or tenant. Cloned members are adopted, not managed, because the clone did not create them. `adopt` turns an existing board into a team and backfills the tenant on its cards.

---

## 13. Errors

`errors.TeamError` is the one error type. Its constructor requires a `hint` (the legal values) and a `remedy` (a command to run), because an error that the model cannot act on wastes a turn. Every surface renders the same envelope: the tool response, the CLI's JSON output, and the HTTP body.

| Exit code | Codes |
|---|---|
| 1 | `E_TEAM_NOT_FOUND`, `E_TEAM_EXISTS`, `E_TEAM_INVALID`, `E_TEAM_NO_PROFILE`, `E_TEAM_NO_BOARD`, `E_TEAM_BOARD_TAKEN`, `E_TEAM_PRIMARY_CONFLICT`, `E_TEAM_CAPACITY_CONFLICT`, `E_TEAM_NOT_MEMBER`, `E_TEAM_MANAGED`, `E_TEAM_NO_BLUEPRINT`, `E_TEAM_CATALOG_UNAVAILABLE`, `E_TEAM_PROFILE_EXISTS`, `E_TEAM_PROVISION_FAILED`, `E_TEAM_MANAGED_CONFLICT` |
| 3 | `E_TEAM_LOCKED`, `E_TEAM_CONFLICT`, `E_TEAM_BUSY` |
| 4 | `E_TEAM_DRIFT_UNFIXED` |

---

## 14. Configuration

Settings live under `plugins.entries.hermes-agent-synergia.settings` in the root `config.yaml`:

| Setting | Default | Effect |
|---|---|---|
| `registry_dir` | `""` | Override the registry directory. |
| `tenant_prefix` | `""` | Prefix for stamped tenant labels, for example `team:`. |
| `default_policy` | `advise` | Policy mode for new teams. |
| `cache_ttl_ms` | `1000` | Minimum interval between registry `stat` checks on the hot path. |
| `strict_startup` | `false` | Refuse to load when the registry is unparseable, instead of degrading. |
| `catalog_paths` | `[]` | Extra read-only pack roots. |
| `role_hints` | `{}` | Category or name glob to role, to pre-fill the role picker. |
| `default_profile_naming` | `{team}-{short}` | Instance naming template. |
| `dashboard_writes` | `true` | `false` makes the dashboard read-only. |
| `approvals_notify` | `home` | `off` stops pushes to home channels. |
| `review_round_limit` | `3` | Review rounds before a card is parked. `0` disables the guard. |
| `decision_priority` | `9` | Priority floor for decision-lane cards. `0` leaves priorities alone. Read by the code but not declared in `plugin.yaml`. |
| `parked_lanes` | `true` | Show `review`, `triage`, and `guarded` lanes in the inbox. |
| `auto_merge` | `true` | Merge a finished worktree thread automatically. |
| `seed_samples` | `true` | Seed the bundled sample profiles on first load. |
| `worker_exit_guard` | `wrapper` | `off` disables the `$HERMES_BIN` wrapper. |

---

## 15. Sample team

`src/samples/profiles/` ships a five-member software team: `software_manager`, `software_architect`, `frontend_engineer`, `backend_engineer`, and `software_tester`. Their `SOUL.md` files encode a workflow that uses the mechanisms above:

1. The manager is the single entry point. It clarifies the goal and creates a design card for the architect.
2. The architect attaches the design and parks the card with `kanban_block(kind="needs_input")`, so the design lands in the operator's `needs_input` lane.
3. A planning card for the manager waits on the design card through parent gating. Implementation cards are created only after an operator `APPROVED: …` comment.
4. Each implementation card is one module plus its tests. The implementer hands it to the tester with `kanban_request_review` on the same card, so a failing verdict returns the card to its implementer while dependent cards wait.
5. When the last card finishes, auto-merge lands the thread on the workdir branch.

---

## 16. Verification

- `tests/` holds the pytest suite. `pytest.ini` puts the pinned Hermes checkout (`../hermes-agent`) on the path. `conftest.py` loads `src/` as a directory package, the same way the plugin manager does. Hermes compatibility warnings are errors.
- `scripts/devenv.sh` creates `.venv-team` with the pinned test dependencies.
- `scripts/verify_substrate.py` checks every Hermes symbol, signature, and behavior that the plugin depends on, one line per item. Run it after every Hermes upgrade. `tests/test_substrate_contract.py` runs the same checks in CI.
- `scripts/lint_imports.py` runs the compatibility scan and the layering rules, and updates the schema hashes.
- `tests/test_policy_matrix.py` covers the decision table exhaustively and asserts the two hard rules: no never-guarded tool is touched, and no directive has a dotted key.

---

## 17. Security considerations

- **Install as a user plugin only.** Hermes refuses the API file of a project plugin (`./.hermes/plugins/`), and loads a user plugin's Python only when the plugin is in `plugins.enabled`.
- **Prompt injection.** Registry text is sanitized and capped before it reaches a system prompt (section 6.4).
- **Fail open on the hot path, fail closed on visibility.** A broken registry never blocks a tool call. A tool that cannot tell where it runs is hidden, and its mutations are refused.
- **Dashboard.** Requests are authenticated by the Hermes session, mutations need a same-origin JSON request, and file routes stay inside the workdir and refuse sensitive names.
- **Profile writes.** Only `provision.py` creates or deletes a profile, and only `resolve.write_profile_config_keys` writes a profile's `config.yaml`, through a fixed allow-list and with a backup.

---

## 18. Invariants

`model.validate_team` checks one record. `model.check_invariants` checks a candidate record against the whole registry. `clone` and `import` enforce I4 by requiring a new `--workdir`:

| Id | Rule | Error |
|---|---|---|
| I1 | A board belongs to one team, unless both teams set `shared_board`. | `E_TEAM_BOARD_TAKEN` |
| I2 | A profile is primary on at most one team. | `E_TEAM_PRIMARY_CONFLICT` |
| I3 | The orchestrator is a member of the team. | `E_TEAM_INVALID` |
| I4 | Two teams never share a workdir. | `E_TEAM_INVALID` |
| I5 | No team is named `default`. | `E_TEAM_INVALID` |
| I6 | When several teams set `capacity` for the same profile, the values agree. | `E_TEAM_CAPACITY_CONFLICT` |
| I7 | A profile is managed by at most one team. | `E_TEAM_MANAGED_CONFLICT` |
