"""hermes-agent-synergia — team abstraction over Hermes profiles, boards and work dirs.

S15: ``register(ctx)`` is the full §5.3 shape — the ten ``team`` tools, the
``team`` CLI command, the policy hook (registered only for profiles that are
in a team with ``policy != off``, §5.3), the contract prompt section, the
kanban claim observer, the worker-exit guard, and the cache teardown on
unload.

Module scope imports nothing heavy (spec §5.2): every handler imports lazily,
so plugin discovery costs one ``import`` + argparse wiring.
"""


def register(ctx):
    from . import cli, hooks, prompt, schemas, tools, worker_exit_guard

    for name, schema, handler, emoji, gate in tools.TOOL_TABLE:
        ctx.register_tool(name=name, toolset=hooks.TOOLSET, schema=schema,
                          handler=handler, check_fn=gate, emoji=emoji)

    ctx.register_cli_command(
        name="team",
        help="Manage Hermes Teams (profiles + board + shared work directory)",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
    )

    # T2/T3 policy. Registered ONLY when this profile is in a team with
    # policy != off, so non-team profiles pay nothing on the tool hot path.
    # ctx.profile_name is the PluginContext property (hermes_cli/plugins.py
    # :392 — derived from HERMES_HOME via profiles.get_active_profile_name).
    if hooks.policy_needed(ctx.profile_name):
        ctx.register_hook("pre_tool_call", hooks.pre_tool_call)

    # T0 context. Callable → rendered once per session, persisted by core.
    ctx.register_system_prompt_section(
        id="hermes-agent-synergia.contract", content=hooks.render_contract,
        position="after_memory", max_chars=1000,
    )

    # The attachment guidance ships with team_attach_path instead of
    # being patched into the substrate's own KANBAN_GUIDANCE.
    ctx.register_system_prompt_section(
        id="hermes-agent-synergia.attachments", content=hooks.render_attachments,
        position="after_memory", max_chars=prompt.ATTACHMENTS_MAX_CHARS,
    )

    # Stamp `tenant` on cards the dispatcher hands this profile, for
    # observability only (hooks.on_task_claimed logs; it does not write).
    ctx.register_hook("kanban_task_claimed", hooks.on_task_claimed)

    # A card blocked on a HUMAN is pushed to the configured home
    # channels, so the operator learns about it without polling a surface.
    # No-op when no messaging platform has a home channel.
    ctx.register_hook("kanban_task_blocked", hooks.on_task_blocked)

    # The substrate never merges a worktree card's branch: when a thread's
    # last card finishes, fast-forward the workdir to its result, then retire
    # each card's worktree onto a step-named branch. Settings key
    # `auto_merge`; false leaves it to `hermes team merge`.
    ctx.register_hook("kanban_task_completed", hooks.on_task_completed)

    # The stuck lane's push: the dispatcher's circuit breaker blocks a card
    # with a raw UPDATE and fires no kanban_task_blocked, so the give-up ids
    # are read off DispatchResult.auto_blocked once per tick instead.
    ctx.register_hook("on_kanban_dispatch_tick", hooks.on_dispatch_tick)

    # Dispatcher workers go through $HERMES_BIN, so a worker that never
    # reached the model exits non-zero instead of looking like a clean exit
    # (which the dispatcher books as a protocol violation). Settings key
    # `worker_exit_guard`; "off" disables it.
    worker_exit_guard.install(cli._settings().get("worker_exit_guard", "wrapper"))

    ctx.on_unload(hooks.teardown)                  # drop caches, close nothing else
