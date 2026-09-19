(function () {
  "use strict";
  // hermes-agent-synergia dashboard plugin (S20) — hand-written IIFE, no build step.
  //
  // Idiom copied verbatim from the shipped reference bundle
  // ($HA/plugins/hermes-achievements/dashboard/dist/index.js): the host's
  // React comes off window.__HERMES_PLUGIN_SDK__ (a second copy would not
  // share hook state with the shell), the tree is built with
  // React.createElement, every class is `ht-`-prefixed, and the file ends
  // with window.__HERMES_PLUGINS__.register().
  //
  // Every fetch goes through SDK.fetchJSON so auth works in BOTH dashboard
  // modes (loopback X-Hermes-Session-Token header / gated OAuth cookie).
  // Never read window.__HERMES_SESSION_TOKEN__, never call bare fetch.
  // fetchJSON rejects non-2xx as Error("<status>: <body text>")
  // (web/src/lib/api.ts:180-183) — errStatus()/errEnvelope() below parse the
  // status back off the message so 409 (CAS) and 405 DASHBOARD_READONLY
  // (dashboard_writes false) get their own, actionable rendering.
  //
  // Backend contract: team_plugin/hermes-agent-synergia/dashboard/plugin_api.py.
  // Every path below is one of its router decorators; every body key is one
  // the route reads. Nothing here invents a route.

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks || {};
  const C = SDK.components || {};

  const cn = (SDK.utils && SDK.utils.cn) || function () {
    const out = [];
    for (let i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(" ");
  };

  // Older host dashboards may not expose useI18n; shim it like the reference
  // bundle does so the plugin still renders against an older host SDK.
  const useI18n = SDK.useI18n || function () { return { t: {}, locale: "en" }; };

  function tx(t, path, fallback) {
    let node = t && t.team;
    if (node) {
      const parts = path.split(".");
      for (let i = 0; i < parts.length; i++) {
        if (node && typeof node === "object" && parts[i] in node) node = node[parts[i]];
        else { node = null; break; }
      }
    }
    return typeof node === "string" ? node : fallback;
  }

  // ── component guards ──────────────────────────────────────────────────────
  // SDK contract 1.1.0 exposes all of these, but an older host might not.
  // Degrade to a plain-DOM equivalent rather than white-screening the tab.

  function fallback(tag, cls, strip) {
    return function (props) {
      const p = {};
      for (const k in props) {
        if (k === "children" || k === "className") continue;
        if (strip && strip.indexOf(k) >= 0) continue;
        p[k] = props[k];
      }
      p.className = cn(cls, props && props.className);
      return h(tag, p, props ? props.children : null);
    };
  }

  function FallbackCheckbox(props) {
    return h("input", {
      type: "checkbox",
      id: props.id,
      className: cn("ht-fallback-checkbox", props.className),
      checked: !!props.checked,
      disabled: !!props.disabled,
      onChange: function (e) {
        if (props.onCheckedChange) props.onCheckedChange(e.target.checked);
      },
    });
  }

  function FallbackSelect(props) {
    return h("select", {
      id: props.id,
      className: cn("ht-fallback-select", props.className),
      value: props.value === undefined || props.value === null ? "" : props.value,
      disabled: !!props.disabled,
      onChange: function (e) { if (props.onValueChange) props.onValueChange(e.target.value); },
    }, props.children);
  }

  function FallbackDialog(props) {
    if (!props.open) return null;
    return h("div", {
      className: "ht-modal-backdrop",
      onClick: function (e) {
        if (e.target === e.currentTarget && props.onOpenChange) props.onOpenChange(false);
      },
    }, props.children);
  }

  function FallbackConfirmDialog(props) {
    if (!props.open) return null;
    return h("div", { className: "ht-modal-backdrop" },
      h("div", { className: "ht-modal ht-modal-sm" },
        h("h2", { className: "ht-modal-title" }, props.title),
        props.description ? h("p", { className: "ht-muted" }, props.description) : null,
        h("div", { className: "ht-row ht-row-end" },
          h("button", { type: "button", className: "ht-btn", onClick: props.onCancel },
            props.cancelLabel || "Cancel"),
          h("button", {
            type: "button", className: "ht-btn ht-btn-danger",
            onClick: props.onConfirm, disabled: !!props.loading,
          }, props.loading ? "…" : (props.confirmLabel || "Confirm"))
        )
      )
    );
  }

  function FallbackToast(props) {
    if (!props.toast) return null;
    return h("div", {
      className: cn("ht-toast", props.toast.type === "error" && "ht-toast-error"),
    }, props.toast.message);
  }

  const UI = {
    Card: C.Card || fallback("div", "ht-card"),
    CardHeader: C.CardHeader || fallback("div", "ht-card-head"),
    CardTitle: C.CardTitle || fallback("div", "ht-card-title"),
    CardContent: C.CardContent || fallback("div", "ht-card-body"),
    Badge: C.Badge || fallback("span", "ht-badge", ["tone"]),
    Button: C.Button || fallback("button", "ht-btn",
      ["outlined", "ghost", "destructive", "size", "prefix"]),
    Checkbox: C.Checkbox || FallbackCheckbox,
    Input: C.Input || fallback("input", "ht-input"),
    Label: C.Label || fallback("label", "ht-label"),
    Select: C.Select || FallbackSelect,
    SelectOption: C.SelectOption || fallback("option", ""),
    Separator: C.Separator || fallback("div", "ht-sep"),
    Dialog: C.Dialog || FallbackDialog,
    DialogContent: C.DialogContent || fallback("div", "ht-modal"),
    DialogHeader: C.DialogHeader || fallback("div", "ht-modal-head"),
    DialogTitle: C.DialogTitle || fallback("h2", "ht-modal-title"),
    DialogDescription: C.DialogDescription || fallback("p", "ht-muted"),
    DialogFooter: C.DialogFooter || fallback("div", "ht-row ht-row-end"),
    ConfirmDialog: C.ConfirmDialog || FallbackConfirmDialog,
    Toast: C.Toast || FallbackToast,
  };

  // The host's useConfirmDelete is the sanctioned single-id delete state
  // machine; keep a behavioural twin so the bundle survives an older SDK.
  const useConfirmDelete = hooks.useConfirmDelete || function (opts) {
    const [pendingId, setPendingId] = hooks.useState(null);
    const [isDeleting, setDeleting] = hooks.useState(false);
    return {
      pendingId: pendingId,
      isOpen: pendingId !== null,
      isDeleting: isDeleting,
      requestDelete: function (id) { setPendingId(id); },
      cancel: function () { setPendingId(null); },
      confirm: function () {
        setDeleting(true);
        return Promise.resolve(opts.onDelete(pendingId)).then(function () {
          setDeleting(false); setPendingId(null);
        }, function () { setDeleting(false); setPendingId(null); });
      },
    };
  };

  const useToast = hooks.useToast || function () {
    const [toast, setToast] = hooks.useState(null);
    const timer = hooks.useRef(null);
    // Clear the auto-dismiss on unmount: without this the timer fires into a
    // dead component and React warns (or worse, on an older host, leaks).
    hooks.useEffect(function () {
      return function () { if (timer.current) clearTimeout(timer.current); };
    }, []);
    return {
      toast: toast,
      showToast: function (message, type) {
        setToast({ message: message, type: type || "success" });
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(function () {
          timer.current = null;
          setToast(null);
        }, 4000);
      },
    };
  };

  // ── API plumbing ──────────────────────────────────────────────────────────

  const API_BASE = "/api/plugins/hermes-agent-synergia";

  // Every route answers {"ok": true, "data": {...}} (_ok() in plugin_api.py).
  function unwrap(payload) {
    if (payload && typeof payload === "object" && "data" in payload) return payload.data;
    return payload;
  }

  function apiGet(path) {
    return SDK.fetchJSON(API_BASE + path).then(unwrap);
  }

  // Mutations: plugin_api._mutation_guard refuses anything whose content-type
  // is not exactly application/json (415), so always send the header AND a
  // JSON object body — DELETEs included (_body() requires one).
  function apiSend(method, path, body) {
    return SDK.fetchJSON(API_BASE + path, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(unwrap);
  }

  function errStatus(err) {
    const m = /^(\d{3}):/.exec(String((err && err.message) || err));
    return m ? Number(m[1]) : null;
  }

  function errEnvelope(err) {
    const raw = String((err && err.message) || err);
    const m = /^\d{3}:\s*([\s\S]*)$/.exec(raw);
    if (!m) return null;
    try {
      const parsed = JSON.parse(m[1]);
      if (parsed && parsed.error) return parsed.error;
    } catch (_) { /* non-JSON body */ }
    return null;
  }

  function errText(err) {
    const env = errEnvelope(err);
    if (env) return String(env.message || env.code || "").trim() || "refused";
    const raw = String((err && err.message) || err);
    const m = /^\d{3}:\s*([\s\S]*)$/.exec(raw);
    return String(m ? m[1] : raw).slice(0, 500);
  }

  const READONLY_MESSAGE =
    "Writes are disabled for this dashboard (plugins.entries.hermes-agent-synergia." +
    "settings.dashboard_writes: false, §11). Reads and the dry-run sync " +
    "still work; every other change stays on the CLI.";

  const STALE_BACKEND_MESSAGE =
    "The dashboard's plugin API is older than this page and has no route for " +
    "this action. Restart it (docker compose restart hermes-agent), then reload.";

  function describeError(err, what) {
    const status = errStatus(err);
    const detail = errText(err);
    if (status === 409) {
      // 409 also carries E_TEAM_EXISTS / E_TEAM_DRIFT_UNFIXED — only a CAS
      // miss is fixed by reloading.
      const env = errEnvelope(err);
      if (!env || env.code === "E_TEAM_CONFLICT") {
        return (what || "This record") + " changed since you loaded it — reload " +
          "and reapply your change (CAS conflict). " + detail;
      }
      return "Refused: " + detail + (env.remedy ? " — " + env.remedy : "");
    }
    if (status === 405) return isReadOnly(err) ? READONLY_MESSAGE : STALE_BACKEND_MESSAGE;
    if (status === 423) return "Locked: " + detail;
    if (status === 415) return "Refused: mutations must be application/json. " + detail;
    if (status === 403) return "Refused by the same-origin guard: " + detail;
    if (status === 401) return "The dashboard session is not authenticated — reload the dashboard.";
    if (status === 404) return "Not found: " + detail;
    return (status ? status + ": " : "") + detail;
  }

  function isConflict(err) { return errStatus(err) === 409; }
  // A 405 without the envelope is Starlette's "Method Not Allowed": the running
  // plugin_api.py predates the route, which says nothing about dashboard_writes.
  function isReadOnly(err) {
    const env = errEnvelope(err);
    return errStatus(err) === 405 && !!env && env.code === "DASHBOARD_READONLY";
  }

  // ── shared constants, mirrored from the owning Python modules ─────────────

  // resolve.COUNT_STATUSES — the six §4.3 buckets, in order.
  const LANES = ["todo", "ready", "running", "review", "done", "blocked"];
  // model.PACK_NAMESPACES / model.DEFAULT_PROFILE_NAMING / model.SLUG_RE.
  const PACK_NAMESPACES = ["agency-", "council-", "academy-"];
  const DEFAULT_PROFILE_NAMING = "{team}-{short}";
  const SLUG_RE = /^[a-z0-9][a-z0-9\-_]{0,63}$/;
  const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
  const WORKSPACE_POLICIES = ["scratch", "worktree"];
  const POLICY_MODES = ["off", "advise", "enforce"];

  function shortEntryId(entryId) {
    let short = String(entryId || "");
    for (let i = 0; i < PACK_NAMESPACES.length; i++) {
      const ns = PACK_NAMESPACES[i];
      if (short.indexOf(ns) === 0) return short.slice(ns.length);
    }
    return short;
  }

  // model.render_instance_name, client side — only to PRE-FILL the name field.
  // The server renders and validates it again (provision.render_name).
  function renderInstanceName(teamSlug, entryId, naming) {
    return String(naming || DEFAULT_PROFILE_NAMING)
      .split("{team}").join(String(teamSlug || ""))
      .split("{short}").join(shortEntryId(entryId));
  }

  function normalizeProfile(name) {
    const s = String(name || "").trim();
    return s.toLowerCase() === "default" ? "default" : s.toLowerCase();
  }

  // model.slugify, client side — only for the name field's hint and the
  // duplicate-name check. The server derives the slug again. "" = unusable.
  function slugify(name) {
    const slug = String(name || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
      .trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/-{2,}/g, "-")
      .replace(/^[-_]+|[-_]+$/g, "").slice(0, 64).replace(/[-_]+$/, "");
    return SLUG_RE.test(slug) && slug !== "default" ? slug : "";
  }

  function ageOf(epochSeconds) {
    const ts = Number(epochSeconds || 0);
    if (!ts) return "—";
    const delta = Date.now() / 1000 - ts;
    if (delta < 60) return "just now";
    if (delta < 3600) return Math.floor(delta / 60) + "m";
    if (delta < 86400) return Math.floor(delta / 3600) + "h";
    return Math.floor(delta / 86400) + "d";
  }

  function durationOf(startSeconds, endSeconds) {
    const start = Number(startSeconds || 0);
    if (!start) return "—";
    const end = endSeconds ? Number(endSeconds) : Date.now() / 1000;
    const s = Math.max(0, Math.floor(end - start));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  }

  // Roles are free-form strings, so well-known ones get a fixed hue and
  // everything else a stable hash — a role always looks the same everywhere.
  const ROLE_SLOTS = {
    orchestrator: 1, lead: 1, main: 1,
    verifier: 2, reviewer: 2, qa: 2, tester: 2,
    engineer: 3, developer: 3, coder: 3, backend: 3,
    researcher: 4, analyst: 4,
    designer: 5, writer: 5,
    ops: 6, sre: 6, infra: 6,
    member: 7,
  };

  function roleClass(role) {
    const key = String(role || "member").toLowerCase();
    if (ROLE_SLOTS[key]) return "ht-role-" + ROLE_SLOTS[key];
    let hashed = 0;
    for (let i = 0; i < key.length; i++) hashed = (hashed * 31 + key.charCodeAt(i)) % 8;
    return "ht-role-" + (hashed + 1);
  }

  // ── host navigation (reuse, don't rebuild) ────────────────────────────────
  // The shell is a react-router BrowserRouter mounted with
  // basename={HERMES_BASE_PATH} (web/src/main.tsx:16), and plugin tabs are
  // ordinary routes keyed on manifest.tab.path (App.tsx buildRoutes). The SDK
  // exposes no navigate(), so: push the history entry react-router's own
  // history writes (usr/key/idx) and fire popstate. If the router does not
  // pick it up, our own root node is still mounted a tick later — fall back
  // to a real navigation then. Links keep their href so middle/⌘-click and
  // "open in new tab" behave normally.

  function basePath() {
    const raw = window.__HERMES_BASE_PATH__ || "";
    if (!raw) return "";
    const lead = raw.charAt(0) === "/" ? raw : "/" + raw;
    return lead.replace(/\/+$/, "");
  }

  function hostHref(path) { return basePath() + path; }

  let pendingNavTimer = null;

  function cancelPendingNav() {
    if (pendingNavTimer !== null) {
      clearTimeout(pendingNavTimer);
      pendingNavTimer = null;
    }
    window.removeEventListener("popstate", cancelPendingNav);
  }

  function navigateHost(path, rootEl) {
    const href = hostHref(path);
    try {
      cancelPendingNav();
      const state = window.history.state || {};
      const idx = typeof state.idx === "number" ? state.idx + 1 : 0;
      window.history.pushState({
        usr: null,
        key: Math.random().toString(36).slice(2, 10),
        idx: idx,
      }, "", href);
      window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
      // Hard-navigation fallback, armed only for the case the router ignored
      // the event. Three guards so it can never fire as a surprise: ANY
      // further popstate cancels it (a real SPA route change, or the user
      // pressing back), our own root must still be mounted, and the address
      // bar must still be on the target we pushed — a second click elsewhere
      // in the 150ms window disarms this one instead of yanking the page back.
      window.addEventListener("popstate", cancelPendingNav);
      pendingNavTimer = setTimeout(function () {
        pendingNavTimer = null;
        window.removeEventListener("popstate", cancelPendingNav);
        if (rootEl && rootEl.isConnected && window.location.pathname === href) {
          window.location.assign(href);
        }
      }, 150);
      return;
    } catch (_) { /* fall through to a hard navigation */ }
    window.location.assign(href);
  }

  function HostLink(props) {
    // props: to, rootRef, className, title, children
    return h("a", {
      href: hostHref(props.to),
      className: cn("ht-link", props.className),
      title: props.title,
      onClick: function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        navigateHost(props.to, props.rootRef && props.rootRef.current);
      },
    }, props.children);
  }

  // ── small presentational pieces ───────────────────────────────────────────

  function Muted(props) { return h("span", { className: "ht-muted" }, props.children); }

  function Note(props) {
    // tone: info | warn | error | ok
    return h("div", { className: cn("ht-note", "ht-note-" + (props.tone || "info")) },
      props.title ? h("strong", null, props.title) : null,
      h("p", null, props.children),
      props.actions || null
    );
  }

  function ErrorNote(props) {
    const env = errEnvelope(props.error);
    return h("div", { className: "ht-note ht-note-error" },
      h("strong", null, props.title || "Refused"),
      h("p", null, describeError(props.error, props.what)),
      env && env.hint ? h("p", { className: "ht-hint" }, env.hint) : null,
      env && env.remedy ? h("code", { className: "ht-remedy" }, env.remedy) : null,
      props.onRetry ? h("div", { className: "ht-row ht-row-end" },
        h(UI.Button, { size: "sm", outlined: true, onClick: props.onRetry }, "Reload")
      ) : null
    );
  }

  // E_TEAM_BUSY (409): the team has running cards. The operator picks Stop
  // (resend with on_busy: "stop": each card is blocked and its worker killed)
  // or Wait (resend the plain request every BUSY_POLL_MS until the team is
  // idle). The wait lives in this tab: closing it abandons the change.
  const BUSY_POLL_MS = 5000;

  function isBusyError(err) {
    const env = errEnvelope(err);
    return !!(env && env.code === "E_TEAM_BUSY");
  }

  function useBusyGuard() {
    const [busy, setBusy] = hooks.useState(null);   // {message, waiting, stopping}
    // gen invalidates in-flight responses and timers after cancel/stop/unmount.
    const ref = hooks.useRef({ gen: 0, timer: null, req: null });

    hooks.useEffect(function () {
      return function () { reset(); };
    }, []);

    function reset() {
      const cur = ref.current;
      cur.gen += 1;
      if (cur.timer) { clearTimeout(cur.timer); cur.timer = null; }
    }

    function attempt(onBusy, waiting) {
      const cur = ref.current;
      const gen = cur.gen;
      const req = cur.req;
      return req.send(onBusy).then(function (data) {
        if (gen !== cur.gen) return;
        setBusy(null);
        req.onDone(data);
      }, function (err) {
        if (gen !== cur.gen) return;
        if (isBusyError(err) && onBusy !== "stop") {
          setBusy({ message: errText(err), waiting: waiting, stopping: false });
          if (waiting) {
            cur.timer = setTimeout(function () {
              cur.timer = null;
              if (gen === cur.gen) attempt("refuse", true);
            }, BUSY_POLL_MS);
          }
          return;
        }
        setBusy(null);
        req.onError(err);
      });
    }

    return {
      busy: busy,
      // req: {send(onBusy) -> Promise, onDone(data), onError(err), onCancel?()}
      run: function (req) {
        reset();
        ref.current.req = req;
        setBusy(null);
        return attempt("refuse", false);
      },
      stop: function () {
        reset();
        setBusy(function (b) { return b && Object.assign({}, b, { stopping: true }); });
        return attempt("stop", false);
      },
      wait: function () {
        reset();
        setBusy(function (b) { return b && Object.assign({}, b, { waiting: true }); });
        attempt("refuse", true);
      },
      cancel: function () {
        reset();
        setBusy(null);
        const req = ref.current.req;
        if (req && req.onCancel) req.onCancel();
      },
    };
  }

  function BusyNote(props) {
    const guard = props.guard;
    const b = guard.busy;
    if (!b) return null;
    return h(Note, {
      tone: "warn",
      title: b.waiting ? "Waiting for the running cards to finish…" : "The team is working on a task",
      actions: h("div", { className: "ht-row ht-row-end" },
        h(UI.Button, { size: "sm", outlined: true, onClick: guard.cancel }, "Cancel"),
        b.waiting ? null : h(UI.Button, {
          size: "sm", outlined: true, disabled: b.stopping, onClick: guard.wait,
        }, "Wait until finished"),
        h(UI.Button, { size: "sm", destructive: true, disabled: b.stopping, onClick: guard.stop },
          b.stopping ? "Stopping…" : (b.waiting ? "Stop now" : "Stop running cards"))
      ),
    }, b.message + (b.waiting
      ? " The change runs as soon as they finish. Keep this tab open."
      : " Stop blocks each card and kills its worker. Wait runs the change once they finish."));
  }

  function busyOutcome(data) {
    if (!data) return "";
    return (data.stopped && data.stopped.length
        ? "Stopped " + data.stopped.length + " running card(s). " : "") +
      (data.not_killed && data.not_killed.length
        ? "Worker not killed: " + data.not_killed.join(", ") + ". " : "") +
      (data.resumed && data.resumed.length
        ? "Resumed " + data.resumed.length + " card(s). " : "");
  }

  function Counters(props) {
    const counts = props.counts || {};
    return h("div", { className: "ht-counters" }, LANES.map(function (lane) {
      return h("div", { key: lane, className: cn("ht-counter", "ht-lane-" + lane) },
        h("span", { className: "ht-counter-value" }, Number(counts[lane] || 0)),
        h("span", { className: "ht-counter-label" }, lane)
      );
    }));
  }

  // "main" and "primary" are two different backend facts and must never be
  // read from each other:
  //   main    = team.orchestrator — the ONE profile the record names (I3,
  //             validate_team), the default assignee post_team_task falls
  //             back to, and the roster row marked orchestrator at create.
  //   primary = this team owns that profile's terminal.cwd / kanban.* plane
  //             (I2). Managed members are ALWAYS primary, so several
  //             members of one team can carry it — it is not "the main one".
  // Every "main" label in this bundle resolves through isMain() and nothing
  // else; `primary` is only ever rendered as its own, explicitly-titled badge.
  function isMain(member, team) {
    return !!(member && team && member.profile === team.orchestrator);
  }

  function MemberChip(props) {
    const member = props.member;
    const main = isMain(member, props.team);
    return h("span", {
      className: cn("ht-chip", roleClass(member.role)),
      title: member.profile + " · " + (member.role || "member") +
        (member.managed ? " · managed" : " · adopted") +
        (main ? " · main (the team's orchestrator)" : ""),
    },
      h("span", { className: "ht-chip-dot" }),
      member.profile,
      main ? h("span", { className: "ht-chip-main" }, "main") : null
    );
  }

  function Field(props) {
    return h("div", { className: cn("ht-field", props.className) },
      props.label ? h(UI.Label, { htmlFor: props.htmlFor, className: "ht-field-label" }, props.label) : null,
      props.children,
      props.hint ? h("p", { className: "ht-hint" }, props.hint) : null
    );
  }

  function TextField(props) {
    return h(Field, { label: props.label, htmlFor: props.id, hint: props.hint },
      h(UI.Input, {
        id: props.id,
        value: props.value === undefined || props.value === null ? "" : props.value,
        placeholder: props.placeholder,
        disabled: props.disabled,
        type: props.type,
        onChange: function (e) { props.onChange(e.target.value); },
      })
    );
  }

  function CheckField(props) {
    return h("label", {
      className: cn("ht-check", props.disabled && "ht-check-disabled"),
      title: props.title,
    },
      h(UI.Checkbox, {
        checked: !!props.checked,
        disabled: !!props.disabled,
        onCheckedChange: function (v) { props.onChange(v === true); },
      }),
      h("span", null, props.label)
    );
  }

  function PillBar(props) {
    return h("div", { className: "ht-pills", role: "tablist" }, props.items.map(function (item) {
      return h("button", {
        key: item.value,
        type: "button",
        role: "tab",
        "aria-selected": props.value === item.value,
        className: cn("ht-pill", props.value === item.value && "ht-pill-active"),
        onClick: function () { props.onChange(item.value); },
      }, item.label, item.badge !== undefined && item.badge !== null
        ? h("span", { className: "ht-pill-badge" }, item.badge) : null);
    }));
  }

  function Loading(props) {
    return h("div", { className: "ht-loading" }, props.label || "Loading…");
  }

  function Empty(props) {
    return h("div", { className: "ht-empty" },
      h("strong", null, props.title),
      props.children ? h("p", null, props.children) : null
    );
  }

  function SkillPicker(props) {
    // props: skills [{name, enabled}] | null, selected [names], onToggle, error
    if (props.error) return h("p", { className: "ht-hint" }, props.error);
    if (props.skills === null) return h("p", { className: "ht-hint" }, "Loading skills…");
    if (!props.skills.length) return h("p", { className: "ht-hint" }, "No installed skills to choose from.");
    return h("div", { className: "ht-skill-grid" }, props.skills.map(function (skill) {
      const on = props.selected.indexOf(skill.name) >= 0;
      return h("label", { key: skill.name, className: cn("ht-skill", on && "ht-skill-on") },
        h(UI.Checkbox, {
          checked: on,
          onCheckedChange: function () { props.onToggle(skill.name); },
        }),
        h("span", { className: "ht-skill-name" }, skill.name),
        skill.enabled ? null : h("span", { className: "ht-skill-off" }, "off")
      );
    }));
  }

  function RestartBanner(props) {
    if (!props.notice) return null;
    // §9.4: the profiles-updated notice reaches the UI as a FIELD
    // (restart_notice), never prose to scrape — render it verbatim.
    return h("div", { className: "ht-note ht-note-info ht-restart" },
      h("strong", null, "Profiles updated"),
      h("pre", null, String(props.notice))
    );
  }

  // POST /teams/{slug}/workdir = set-workdir --apply: record, board default,
  // git (worktree) and every primary member's terminal.cwd move together.
  function WorkdirEditor(props) {
    const team = props.team;
    const [value, setValue] = hooks.useState(team.workdir || "");
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const [result, setResult] = hooks.useState(null);
    const busyGuard = useBusyGuard();

    function submit() {
      setBusy(true);
      setError(null);
      setResult(null);
      const body = { workdir: value.trim(), expected_revision: props.revision };
      busyGuard.run({
        send: function (onBusy) {
          return apiSend("POST", "/teams/" + encodeURIComponent(team.slug) + "/workdir",
            Object.assign({}, body, { on_busy: onBusy }));
        },
        onDone: function (data) {
          setBusy(false);
          setResult(data || {});
          props.onChanged();
        },
        onError: function (err) {
          setBusy(false);
          setError(err);
          if (isReadOnly(err)) props.onReadOnly();
        },
        onCancel: function () { setBusy(false); },
      });
    }

    return h("div", { className: "ht-panel" },
      error ? h(ErrorNote, { error: error, what: "This team" }) : null,
      h(BusyNote, { guard: busyGuard }),
      result ? h(Note, { tone: "ok", title: result.changed ? "Team moved" : "Unchanged" },
        busyOutcome(result) + "Root folder: " + (result.workdir || value)) : null,
      result ? h(RestartBanner, { notice: result.restart_notice }) : null,
      h("div", { className: "ht-form" },
        h(TextField, {
          id: "ht-workdir", label: "Root folder", value: value, onChange: setValue,
          disabled: props.readOnly || busy,
          hint: "Absolute path. Every team profile moves here: new chats and " +
            "tasks use it at once; run /new in open chats.",
        }),
        h("div", { className: "ht-row ht-row-end" },
          h(UI.Button, { size: "sm", outlined: true, onClick: props.onClose }, "Close"),
          h(UI.Button, {
            size: "sm",
            disabled: props.readOnly || busy || !value.trim(),
            onClick: submit,
          }, busy ? "Moving…" : "Move team")
        )
      )
    );
  }

  function JobProgress(props) {
    const job = props.job;
    if (!job) return null;
    const state = job.state || "pending";
    return h("div", { className: cn("ht-job", "ht-job-" + state) },
      h("div", { className: "ht-job-head" },
        h("span", { className: "ht-job-dot" }),
        h("strong", null, job.title || job.kind || "job"),
        h("span", { className: "ht-job-state" }, state)
      ),
      h("p", { className: "ht-hint" }, String(job.progress || "")),
      job.error && job.error.error
        ? h("div", { className: "ht-note ht-note-error" },
            h("p", null, String(job.error.error.message || "job failed")),
            job.error.error.hint ? h("p", { className: "ht-hint" }, job.error.error.hint) : null,
            job.error.error.remedy ? h("code", { className: "ht-remedy" }, job.error.error.remedy) : null)
        : null
    );
  }

  function PlanPreview(props) {
    const plan = props.plan;
    if (!plan) return null;
    return h("div", { className: "ht-plan" },
      h("div", { className: "ht-plan-head" },
        h("strong", null, "Dry run — nothing written yet"),
        h(UI.Badge, { tone: plan.existing ? "warning" : "secondary" },
          plan.existing ? "target exists" : "new instance")
      ),
      h("dl", { className: "ht-kv" },
        h("dt", null, "profile"), h("dd", null, String(plan.name || "—")),
        h("dt", null, "source"), h("dd", null, String(plan.source || "—")),
        h("dt", null, "target"), h("dd", null, String(plan.target_dir || "—")),
        h("dt", null, "files"), h("dd", null, String(plan.file_count || 0))
      ),
      plan.files && plan.files.length
        ? h("ul", { className: "ht-plan-files" }, plan.files.map(function (file, i) {
            return h("li", { key: i }, String(file));
          }))
        : null
    );
  }

  // ── job polling ───────────────────────────────────────────────────────────
  // 202 {job_id} → GET /jobs/{id} until state is done|error (jobs.py). The
  // interval is cleared on a terminal state AND in the effect cleanup, so no
  // poll survives an unmount or a finished job.

  function useJobPoll(onDone) {
    const [jobId, setJobId] = hooks.useState(null);
    const [job, setJob] = hooks.useState(null);
    const [jobError, setJobError] = hooks.useState(null);
    const doneRef = hooks.useRef(onDone);
    doneRef.current = onDone;

    hooks.useEffect(function () {
      if (!jobId) return undefined;
      let cancelled = false;
      let timer = null;
      function stop() { if (timer) { clearInterval(timer); timer = null; } }
      function tick() {
        apiGet("/jobs/" + encodeURIComponent(jobId)).then(function (data) {
          if (cancelled) return;
          const snapshot = (data && data.job) || null;
          setJob(snapshot);
          if (snapshot && (snapshot.state === "done" || snapshot.state === "error")) {
            stop();
            if (doneRef.current) doneRef.current(snapshot);
          }
        }, function (err) {
          if (cancelled) return;
          stop();
          setJobError(describeError(err, "This job"));
        });
      }
      tick();
      timer = setInterval(tick, 1200);
      return function () { cancelled = true; stop(); };
    }, [jobId]);

    return {
      job: job,
      jobError: jobError,
      // A failed poll ends the watch: the error branch above stops the
      // interval, so the job can never reach a terminal snapshot from here.
      // Without `!jobError` the flag would latch true forever and leave every
      // create/adopt button permanently disabled after one dropped request —
      // `job` may still be null, so the terminal-state test alone can't clear
      // it. Recovery is reset(), wired to the Dismiss action on that error.
      running: !!jobId && !jobError &&
        !(job && (job.state === "done" || job.state === "error")),
      watch: function (id) { setJob(null); setJobError(null); setJobId(id); },
      reset: function () { setJobId(null); setJob(null); setJobError(null); },
    };
  }

  // The Dismiss affordance for a dropped poll: clears the watch so the dialog
  // falls back to its form and the operator can retry. This is the ONLY
  // caller of reset() — if it ever goes away, delete reset() with it.
  function JobErrorNote(props) {
    if (!props.jobs.jobError) return null;
    return h(Note, {
      tone: "error",
      title: "Job polling failed",
      actions: h("div", { className: "ht-row ht-row-end" },
        h(UI.Button, { size: "sm", outlined: true, onClick: props.jobs.reset },
          "Dismiss")),
    }, props.jobs.jobError);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Catalog picker — pack / category / search facets over GET /catalog
  // ═══════════════════════════════════════════════════════════════════════════

  function useCatalog() {
    const [entries, setEntries] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);
    hooks.useEffect(function () {
      let cancelled = false;
      apiGet("/catalog").then(function (data) {
        if (cancelled) return;
        setEntries((data && data.entries) || []);
      }, function (err) {
        if (cancelled) return;
        setError(describeError(err, "The catalog"));
      });
      return function () { cancelled = true; };
    }, []);
    return { entries: entries, error: error };
  }

  function facetValues(entries, key) {
    const seen = {};
    (entries || []).forEach(function (entry) {
      const value = String(entry[key] || "").trim();
      if (value) seen[value] = true;
    });
    return Object.keys(seen).sort();
  }

  function filterEntries(entries, facets) {
    const needle = String(facets.q || "").toLowerCase();
    return (entries || []).filter(function (entry) {
      if (facets.pack && entry.pack !== facets.pack) return false;
      if (facets.category && entry.category !== facets.category) return false;
      if (needle) {
        const hay = (entry.id + " " + entry.display_name + " " + entry.description).toLowerCase();
        if (hay.indexOf(needle) < 0) return false;
      }
      return true;
    });
  }

  function CatalogFacets(props) {
    const packs = facetValues(props.entries, "pack");
    const categories = facetValues(props.entries, "category");
    return h("div", { className: "ht-facets" },
      h(UI.Input, {
        className: "ht-facet-search",
        placeholder: "search blueprints…",
        value: props.facets.q,
        onChange: function (e) { props.onChange({ q: e.target.value }); },
      }),
      h(UI.Select, {
        value: props.facets.pack,
        onValueChange: function (v) { props.onChange({ pack: v }); },
      },
        h(UI.SelectOption, { value: "" }, "all packs"),
        packs.map(function (pack) {
          return h(UI.SelectOption, { key: pack, value: pack }, pack);
        })
      ),
      h(UI.Select, {
        value: props.facets.category,
        onValueChange: function (v) { props.onChange({ category: v }); },
      },
        h(UI.SelectOption, { value: "" }, "all categories"),
        categories.map(function (category) {
          return h(UI.SelectOption, { key: category, value: category }, category);
        })
      )
    );
  }

  function CatalogList(props) {
    if (props.error) return h(Note, { tone: "error", title: "Catalog unavailable" }, props.error);
    if (props.entries === null) return h(Loading, { label: "Loading catalog…" });
    const rows = filterEntries(props.entries, props.facets);
    if (!rows.length) return h(Empty, { title: "No blueprint matches those facets." });
    return h("div", { className: "ht-catalog" }, rows.slice(0, 200).map(function (entry) {
      const selected = props.selectedId === entry.id;
      return h("button", {
        key: entry.id,
        type: "button",
        className: cn("ht-catalog-row", selected && "ht-catalog-row-on"),
        onClick: function () { props.onSelect(entry); },
      },
        h("div", { className: "ht-catalog-main" },
          h("strong", null, entry.display_name || entry.id),
          h("code", { className: "ht-code" }, entry.id)
        ),
        h("p", { className: "ht-catalog-desc" }, entry.description || ""),
        h("div", { className: "ht-catalog-meta" },
          h(UI.Badge, { tone: "secondary" }, entry.pack || "pack"),
          entry.category ? h(UI.Badge, { tone: "outline" }, entry.category) : null,
          entry.suggested_role ? h("span", { className: cn("ht-chip", roleClass(entry.suggested_role)) },
            h("span", { className: "ht-chip-dot" }), entry.suggested_role) : null,
          (entry.installed_as || []).length
            ? h(Muted, null, "installed as " + entry.installed_as.join(", ")) : null,
          props.rowExtra ? props.rowExtra(entry) : null
        )
      );
    }));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Add-member dialog: catalog picker → dry-run plan → 202 job → poll
  // ═══════════════════════════════════════════════════════════════════════════

  function AddMemberDialog(props) {
    const team = props.team;
    const catalog = useCatalog();
    const [facets, setFacets] = hooks.useState({ q: "", pack: "", category: "" });
    const [entry, setEntry] = hooks.useState(null);
    const [name, setName] = hooks.useState("");
    const [role, setRole] = hooks.useState("");
    const [capacity, setCapacity] = hooks.useState("1");
    const [skills, setSkills] = hooks.useState([]);
    const [skillRows, setSkillRows] = hooks.useState(null);
    const [skillError, setSkillError] = hooks.useState(null);
    const [plan, setPlan] = hooks.useState(null);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);

    const jobs = useJobPoll(function (snapshot) {
      if (snapshot.state === "done") {
        props.onCreated(snapshot.result || null);
      }
    });

    // The skill universe is per-profile (GET /skills?profile=). A brand-new
    // managed instance has no profile yet, so read the universe from an
    // existing installation of the same blueprint (entry.installed_as), else
    // from the team's orchestrator — both are real profiles on this host.
    const skillSource = entry && (entry.installed_as || [])[0]
      ? entry.installed_as[0]
      : (team.orchestrator || "");

    hooks.useEffect(function () {
      if (!skillSource) { setSkillRows([]); return undefined; }
      let cancelled = false;
      setSkillRows(null);
      setSkillError(null);
      apiGet("/skills?profile=" + encodeURIComponent(skillSource)).then(function (data) {
        if (cancelled) return;
        setSkillRows((data && data.skills) || []);
      }, function (err) {
        if (cancelled) return;
        setSkillRows([]);
        setSkillError(describeError(err, "The skill list"));
      });
      return function () { cancelled = true; };
    }, [skillSource]);

    function pick(next) {
      setEntry(next);
      setPlan(null);
      setError(null);
      setRole(next.suggested_role || "member");
      setName(renderInstanceName(team.slug, next.id, team.profile_naming || DEFAULT_PROFILE_NAMING));
      setSkills([]);
    }

    function toggleSkill(skillName) {
      setSkills(function (current) {
        const at = current.indexOf(skillName);
        if (at < 0) return current.concat([skillName]);
        const next = current.slice();
        next.splice(at, 1);
        return next;
      });
    }

    function body(extra) {
      // POST /teams/{slug}/members — managed (blueprint) path. Field names
      // are exactly the ones post_team_member() reads.
      const payload = {
        blueprint: entry.id,
        name: name,
        role: role || "member",
        capacity: Number(capacity) || 1,
        skills: skills,
      };
      if (props.revision !== null && props.revision !== undefined) {
        payload.expected_revision = props.revision;
      }
      return Object.assign(payload, extra || {});
    }

    function preview() {
      if (!entry) return;
      setBusy(true);
      setError(null);
      apiSend("POST", "/teams/" + encodeURIComponent(team.slug) + "/members?dry_run=true", body())
        .then(function (data) {
          setPlan((data && data.plan) || {});
          setBusy(false);
        }, function (err) {
          setBusy(false);
          setError(err);
          if (isReadOnly(err)) props.onReadOnly();
        });
    }

    function create() {
      if (!entry) return;
      setBusy(true);
      setError(null);
      apiSend("POST", "/teams/" + encodeURIComponent(team.slug) + "/members", body())
        .then(function (data) {
          setBusy(false);
          if (data && data.job_id) jobs.watch(data.job_id);
        }, function (err) {
          setBusy(false);
          setError(err);
          if (isReadOnly(err)) props.onReadOnly();
        });
    }

    const nameValid = PROFILE_NAME_RE.test(name);

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) props.onClose(); },
    },
      h(UI.DialogContent, { className: "ht-dialog ht-dialog-wide" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, "Add a member to " + team.slug),
          h(UI.DialogDescription, null,
            "Pick a catalog blueprint, review the dry-run plan, then create the " +
            "managed instance. Nothing is written until you confirm.")
        ),
        h("div", { className: "ht-dialog-body" },
          jobs.job ? h("div", null,
            h(JobProgress, { job: jobs.job }),
            jobs.job.state === "done"
              ? h("div", null,
                  h(Note, { tone: "ok", title: "Member created" },
                    "Instantiated " + ((jobs.job.result && (jobs.job.result.created || []).join(", ")) || name) + "."),
                  h(RestartBanner, { notice: jobs.job.result && jobs.job.result.restart_notice }))
              : null
          ) : h("div", null,
            h(CatalogFacets, {
              entries: catalog.entries,
              facets: facets,
              onChange: function (patch) { setFacets(Object.assign({}, facets, patch)); },
            }),
            h(CatalogList, {
              entries: catalog.entries,
              error: catalog.error,
              facets: facets,
              selectedId: entry && entry.id,
              onSelect: pick,
            }),
            entry ? h("div", { className: "ht-form" },
              h(UI.Separator, { className: "ht-sep" }),
              h("div", { className: "ht-grid-2" },
                h(TextField, {
                  id: "ht-member-name",
                  label: "Profile name",
                  value: name,
                  onChange: function (v) { setName(v); setPlan(null); },
                  hint: nameValid ? "Rendered from profile_naming (" +
                    (team.profile_naming || DEFAULT_PROFILE_NAMING) + ")."
                    : "Must match " + String(PROFILE_NAME_RE),
                }),
                h(TextField, {
                  id: "ht-member-role",
                  label: "Role",
                  value: role,
                  onChange: function (v) { setRole(v); setPlan(null); },
                  hint: "Pre-filled from the entry's suggested_role.",
                }),
                h(TextField, {
                  id: "ht-member-capacity",
                  label: "Capacity",
                  type: "number",
                  value: capacity,
                  onChange: function (v) { setCapacity(v); },
                  hint: "kanban.max_in_progress_per_profile for this member.",
                })
              ),
              h(Field, {
                label: "Skills",
                hint: "Read from " + (skillSource || "—") +
                  " (GET /skills?profile=). The selection is stored on the " +
                  "record as Member.skills and reaches the instance's config " +
                  "on the next sync. Leave it empty to let the new instance " +
                  "keep its own skills — the team then manages none of them.",
              },
                h(SkillPicker, {
                  skills: skillRows,
                  selected: skills,
                  error: skillError,
                  onToggle: toggleSkill,
                })
              )
            ) : null,
            plan ? h(PlanPreview, { plan: plan }) : null,
            error ? h(ErrorNote, { error: error, what: "This team" }) : null,
            h(JobErrorNote, { jobs: jobs })
          )
        ),
        h(UI.DialogFooter, null,
          jobs.job && (jobs.job.state === "done" || jobs.job.state === "error")
            ? h(UI.Button, { onClick: props.onClose }, "Close")
            : h(React.Fragment, null,
                h(UI.Button, { outlined: true, onClick: props.onClose, disabled: busy }, "Cancel"),
                h(UI.Button, {
                  outlined: true,
                  disabled: !entry || busy || !nameValid || jobs.running,
                  onClick: preview,
                }, busy && !plan ? "Planning…" : "Dry run"),
                h(UI.Button, {
                  disabled: !entry || busy || !nameValid || !plan || jobs.running,
                  onClick: create,
                }, jobs.running ? "Working…" : "Create member")
              )
        )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Adopt an existing profile (the other half of POST /members)
  // ═══════════════════════════════════════════════════════════════════════════

  function AdoptMemberDialog(props) {
    const team = props.team;
    const [profile, setProfile] = hooks.useState("");
    const [role, setRole] = hooks.useState("member");
    const [primary, setPrimary] = hooks.useState(false);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const jobs = useJobPoll(function (snapshot) {
      if (snapshot.state === "done") props.onCreated(snapshot.result || null);
    });

    function payload() {
      const out = { profile: normalizeProfile(profile), role: role || "member", primary: primary };
      if (props.revision !== null && props.revision !== undefined) {
        out.expected_revision = props.revision;
      }
      return out;
    }

    function adopt() {
      setBusy(true);
      setError(null);
      apiSend("POST", "/teams/" + encodeURIComponent(team.slug) + "/members", payload())
        .then(function (data) {
          setBusy(false);
          if (data && data.job_id) jobs.watch(data.job_id);
        }, function (err) {
          setBusy(false);
          setError(err);
          if (isReadOnly(err)) props.onReadOnly();
        });
    }

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) props.onClose(); },
    },
      h(UI.DialogContent, { className: "ht-dialog" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, "Adopt a profile into " + team.slug),
          h(UI.DialogDescription, null,
            "Adopted members are managed: false — this team never deletes them.")
        ),
        h("div", { className: "ht-dialog-body" },
          jobs.job ? h(JobProgress, { job: jobs.job }) : h("div", { className: "ht-form" },
            h(TextField, {
              id: "ht-adopt-profile", label: "Existing profile", value: profile,
              onChange: setProfile, hint: "Must already exist on this host.",
            }),
            h(TextField, { id: "ht-adopt-role", label: "Role", value: role, onChange: setRole }),
            h(CheckField, {
              label: "primary (this team owns terminal.cwd for the profile — I2)",
              checked: primary, onChange: setPrimary,
            })
          ),
          error ? h(ErrorNote, { error: error, what: "This team" }) : null,
          h(JobErrorNote, { jobs: jobs })
        ),
        h(UI.DialogFooter, null,
          jobs.job && (jobs.job.state === "done" || jobs.job.state === "error")
            ? h(UI.Button, { onClick: props.onClose }, "Close")
            : h(React.Fragment, null,
                h(UI.Button, { outlined: true, onClick: props.onClose, disabled: busy }, "Cancel"),
                h(UI.Button, {
                  disabled: busy || !profile.trim() || jobs.running, onClick: adopt,
                }, jobs.running ? "Working…" : "Adopt")
              )
        )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Roster panel
  // ═══════════════════════════════════════════════════════════════════════════

  function RosterPanel(props) {
    const team = props.team;
    const resolved = props.resolved || {};
    const resolvedMembers = resolved.members || {};
    const [adding, setAdding] = hooks.useState(false);
    const [adopting, setAdopting] = hooks.useState(false);
    const [removeError, setRemoveError] = hooks.useState(null);
    const [deleteProfile, setDeleteProfile] = hooks.useState(false);
    const [reassignTo, setReassignTo] = hooks.useState("");
    const [outcome, setOutcome] = hooks.useState(null);
    const removeGuard = useBusyGuard();

    const pending = hooks.useRef(null);
    // Read every option through a ref inside onDelete: the host's
    // useConfirmDelete may memoise the callback, and a stale closure would
    // silently send last render's checkbox values.
    const optionsRef = hooks.useRef({});
    optionsRef.current = {
      deleteProfile: deleteProfile,
      reassignTo: reassignTo,
      revision: props.revision,
    };

    const remover = useConfirmDelete({
      onDelete: function (profileName) {
        const member = pending.current;
        const options = optionsRef.current;
        // g5: DELETE /teams/{slug}/members/{profile} refuses without
        // {"confirm": "<normalised profile>"} (_confirm(), plugin_api.py).
        const payload = { confirm: normalizeProfile(profileName) };
        // Delete_profile acts on managed members only. The checkbox is
        // disabled for adopted members, and we never send it for them.
        if (member && member.managed && options.deleteProfile) payload.delete_profile = true;
        if (options.reassignTo) payload.reassign_open_to = options.reassignTo;
        if (options.revision !== null && options.revision !== undefined) {
          payload.expected_revision = options.revision;
        }
        setRemoveError(null);
        const path = "/teams/" + encodeURIComponent(team.slug) + "/members/" +
          encodeURIComponent(normalizeProfile(profileName));
        return removeGuard.run({
          send: function (onBusy) {
            return apiSend("DELETE", path, Object.assign({}, payload, { on_busy: onBusy }));
          },
          onDone: function (data) {
            setOutcome(data || null);
            setDeleteProfile(false);
            setReassignTo("");
            props.onChanged();
          },
          onError: function (err) {
            setRemoveError(err);
            if (isReadOnly(err)) props.onReadOnly();
          },
        });
      },
    });

    function requestRemove(member) {
      pending.current = member;
      setDeleteProfile(false);
      setReassignTo("");
      setOutcome(null);
      setRemoveError(null);
      remover.requestDelete(member.profile);
    }

    const pendingMember = pending.current;

    return h("div", { className: "ht-panel" },
      h("div", { className: "ht-panel-head" },
        h("h3", null, "Roster"),
        h("div", { className: "ht-row" },
          h(UI.Button, {
            size: "sm", outlined: true, disabled: props.readOnly,
            onClick: function () { setAdopting(true); },
          }, "Adopt profile"),
          h(UI.Button, {
            size: "sm", disabled: props.readOnly,
            onClick: function () { setAdding(true); },
          }, "Add member")
        )
      ),
      props.readOnly ? h(Note, { tone: "warn", title: "Read-only dashboard" }, READONLY_MESSAGE) : null,
      outcome ? h(Note, { tone: "ok", title: "Member removed" },
        busyOutcome(outcome) +
        (outcome.deleted_profile ? "Deleted profile " + outcome.deleted_profile + ". " : "") +
        (outcome.skipped_delete_profile
          ? "Kept " + outcome.skipped_delete_profile + " — adopted members are never deleted. "
          : "") +
        (outcome.reassigned ? "Reassigned " + outcome.reassigned + " open card(s). " : "") +
        (outcome.reassign_skipped ? "Reassign skipped: " + outcome.reassign_skipped.reason : "")
      ) : null,
      removeError ? h(ErrorNote, { error: removeError, what: "This team" }) : null,
      h(BusyNote, { guard: removeGuard }),
      h("div", { className: "ht-table-wrap" },
        h("table", { className: "ht-table" },
          h("thead", null, h("tr", null,
            h("th", null, "profile"),
            h("th", null, "role"),
            h("th", null, "ownership"),
            h("th", null, "blueprint"),
            h("th", null, "terminal.cwd"),
            h("th", null, "")
          )),
          h("tbody", null, (team.members || []).map(function (member) {
            const view = resolvedMembers[member.profile] || {};
            return h("tr", { key: member.profile },
              h("td", null,
                h(HostLink, {
                  to: "/profiles",
                  rootRef: props.rootRef,
                  title: "Open the Profiles page (the host's own profile surface)",
                }, member.profile),
                isMain(member, team)
                  ? h(UI.Badge, { tone: "success", className: "ht-inline-badge",
                      title: "the team's orchestrator — the default assignee" },
                      "main") : null,
                view.exists === false
                  ? h(UI.Badge, { tone: "destructive", className: "ht-inline-badge" }, "missing") : null
              ),
              h("td", null, h("span", { className: cn("ht-chip", roleClass(member.role)) },
                h("span", { className: "ht-chip-dot" }), member.role || "member")),
              h("td", null,
                h(UI.Badge, { tone: member.managed ? "secondary" : "outline" },
                  member.managed ? "managed" : "adopted"),
                member.primary ? h(UI.Badge, {
                  tone: "outline", className: "ht-inline-badge",
                  title: "this team owns the profile's terminal.cwd and " +
                    "kanban.* keys (I2) — not the same thing as main",
                }, "primary") : null
              ),
              h("td", null,
                member.blueprint
                  ? h("div", null,
                      h("code", { className: "ht-code" }, member.blueprint),
                      member.blueprint_source
                        ? h(Muted, null, " " + member.blueprint_source) : null,
                      view.blueprint_in_catalog === false
                        ? h(UI.Badge, { tone: "warning", className: "ht-inline-badge" }, "not in catalog") : null,
                      view.distribution_source
                        ? h("div", { className: "ht-hint" }, "installed from " + view.distribution_source) : null)
                  : h(Muted, null, "—")
              ),
              h("td", null,
                view.terminal_cwd
                  ? h("code", {
                      className: cn("ht-code", view.terminal_cwd_matches ? "ht-ok" : "ht-warn"),
                    }, view.terminal_cwd)
                  : h(Muted, null, "unset")
              ),
              h("td", { className: "ht-cell-actions" },
                h(UI.Button, {
                  size: "sm", outlined: true, destructive: true, disabled: props.readOnly,
                  onClick: function () { requestRemove(member); },
                }, "Remove")
              )
            );
          }))
        )
      ),
      adding ? h(AddMemberDialog, {
        team: team,
        revision: props.revision,
        onReadOnly: props.onReadOnly,
        onClose: function () { setAdding(false); props.onChanged(); },
        onCreated: function () { props.onChanged(); },
      }) : null,
      adopting ? h(AdoptMemberDialog, {
        team: team,
        revision: props.revision,
        onReadOnly: props.onReadOnly,
        onClose: function () { setAdopting(false); props.onChanged(); },
        onCreated: function () { props.onChanged(); },
      }) : null,
      remover.isOpen && pendingMember ? h(UI.Dialog, {
        open: true,
        onOpenChange: function (open) { if (!open) remover.cancel(); },
      },
        h(UI.DialogContent, { className: "ht-dialog" },
          h(UI.DialogHeader, null,
            h(UI.DialogTitle, null, "Remove " + pendingMember.profile + "?"),
            h(UI.DialogDescription, null,
              "The member leaves the roster. The profile on disk is only touched " +
              "if you ask for it below — and only when this team created it.")
          ),
          h("div", { className: "ht-dialog-body ht-form" },
            h(CheckField, {
              label: pendingMember.managed
                ? "also delete the profile from disk"
                : "also delete the profile (disabled — adopted member)",
              checked: pendingMember.managed && deleteProfile,
              disabled: !pendingMember.managed,
              onChange: setDeleteProfile,
              title: pendingMember.managed
                ? "This team created the instance, so it may delete it."
                : "managed: false — a team command never deletes what it did not create.",
            }),
            h(Field, { label: "Reassign this member's open cards to" },
              h(UI.Select, {
                value: reassignTo,
                onValueChange: setReassignTo,
              },
                h(UI.SelectOption, { value: "" }, "leave them assigned"),
                (team.members || []).filter(function (m) {
                  return m.profile !== pendingMember.profile;
                }).map(function (m) {
                  return h(UI.SelectOption, { key: m.profile, value: m.profile }, m.profile);
                })
              )
            )
          ),
          h(UI.DialogFooter, null,
            h(UI.Button, { outlined: true, onClick: remover.cancel, disabled: remover.isDeleting }, "Cancel"),
            h(UI.Button, {
              destructive: true, disabled: remover.isDeleting, onClick: remover.confirm,
            }, remover.isDeleting ? "…" : "Remove member")
          )
        )
      ) : null
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Start work panel — POST /teams/{slug}/tasks
  // ═══════════════════════════════════════════════════════════════════════════

  function StartWorkPanel(props) {
    const team = props.team;
    // Default assignee = the team's main profile, which is the record's
    // orchestrator and nothing else — the same fallback post_team_task()
    // itself applies (assignee = t.orchestrator). NOT member.primary: managed
    // members are all primary, so it would pick an arbitrary one. The
    // dropdown is populated ONLY from the roster — a non-member is refused
    // with E_TEAM_NOT_MEMBER.
    const mainProfile = team.orchestrator ||
      ((team.members || [])[0] || {}).profile || "";

    const [title, setTitle] = hooks.useState("");
    const [taskBody, setTaskBody] = hooks.useState("");
    const [priority, setPriority] = hooks.useState("0");
    const [assignee, setAssignee] = hooks.useState(mainProfile);
    const [skills, setSkills] = hooks.useState([]);
    const [skillRows, setSkillRows] = hooks.useState(null);
    const [skillError, setSkillError] = hooks.useState(null);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const [created, setCreated] = hooks.useState(null);

    hooks.useEffect(function () { setAssignee(mainProfile); }, [mainProfile]);

    hooks.useEffect(function () {
      if (!assignee) { setSkillRows([]); return undefined; }
      let cancelled = false;
      setSkillRows(null);
      setSkillError(null);
      apiGet("/skills?profile=" + encodeURIComponent(assignee)).then(function (data) {
        if (cancelled) return;
        setSkillRows((data && data.skills) || []);
      }, function (err) {
        if (cancelled) return;
        setSkillRows([]);
        setSkillError(describeError(err, "The skill list"));
      });
      return function () { cancelled = true; };
    }, [assignee]);

    function toggleSkill(name) {
      setSkills(function (current) {
        const at = current.indexOf(name);
        if (at < 0) return current.concat([name]);
        const next = current.slice();
        next.splice(at, 1);
        return next;
      });
    }

    function submit() {
      setBusy(true);
      setError(null);
      setCreated(null);
      // Body keys read by post_team_task(): title, body, assignee (or role),
      // priority, skills, parents. Board / tenant / workspace come from the
      // record — never from here. No expected_revision: this route writes the
      // board DB, not teams.yaml, so gating it on the registry revision would
      // fail a task for an unrelated roster edit.
      apiSend("POST", "/teams/" + encodeURIComponent(team.slug) + "/tasks", {
        title: title,
        body: taskBody || null,
        assignee: assignee,
        priority: Number(priority) || 0,
        skills: skills,
      }).then(function (data) {
        setBusy(false);
        setCreated(data || null);
        setTitle("");
        setTaskBody("");
        setSkills([]);
        props.onCreated();
      }, function (err) {
        setBusy(false);
        setError(err);
        if (isReadOnly(err)) props.onReadOnly();
      });
    }

    return h("div", { className: "ht-panel" },
      h("div", { className: "ht-panel-head" },
        h("h3", null, "Start work"),
        h(Muted, null, "queued on the board as a card the dispatcher picks up")
      ),
      props.readOnly ? h(Note, { tone: "warn", title: "Read-only dashboard" }, READONLY_MESSAGE) : null,
      created ? h(Note, { tone: "ok", title: "Card created" },
        "Task " + created.task_id + " on board " + created.board +
        " assigned to " + (created.assignee || "nobody") + ".") : null,
      error ? h(ErrorNote, { error: error, what: "This team" }) : null,
      h("div", { className: "ht-form" },
        h(TextField, {
          id: "ht-task-title", label: "Title", value: title, onChange: setTitle,
          disabled: props.readOnly,
        }),
        h(Field, { label: "Body", htmlFor: "ht-task-body" },
          h("textarea", {
            id: "ht-task-body",
            className: "ht-textarea",
            rows: 5,
            value: taskBody,
            disabled: props.readOnly,
            onChange: function (e) { setTaskBody(e.target.value); },
          })
        ),
        h("div", { className: "ht-grid-2" },
          h(Field, { label: "Assignee", htmlFor: "ht-task-assignee",
            hint: "Roster only; defaults to the team's main profile." },
            h(UI.Select, {
              id: "ht-task-assignee",
              value: assignee,
              disabled: props.readOnly,
              onValueChange: setAssignee,
            }, (team.members || []).map(function (m) {
              return h(UI.SelectOption, { key: m.profile, value: m.profile },
                m.profile + (m.profile === mainProfile ? " (main)" : "") + " · " + (m.role || "member"));
            }))
          ),
          h(TextField, {
            id: "ht-task-priority", label: "Priority", type: "number",
            value: priority, onChange: setPriority, disabled: props.readOnly,
            hint: "Higher runs first.",
          })
        ),
        h(Field, { label: "Skills for this card",
          hint: "Installed skills of " + (assignee || "—") + "." },
          h(SkillPicker, {
            skills: skillRows, selected: skills, error: skillError, onToggle: toggleSkill,
          })
        ),
        h("div", { className: "ht-row ht-row-end" },
          h(UI.Button, {
            disabled: props.readOnly || busy || !title.trim() || !assignee,
            onClick: submit,
          }, busy ? "Queuing…" : "Start work")
        )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Activity panel — polled GET /teams/{slug}/status. A started task opens
  // its run: GET /teams/{slug}/tasks/{id}, and GET …/tasks/{id}/log per card.
  // ═══════════════════════════════════════════════════════════════════════════

  // Poll, don't socket: a team board is low-traffic and polling has no
  // reconnect story to get wrong. Cleared on unmount / path change.
  // `extraDeps` must keep the same length across renders.
  function usePoll(path, active, extraDeps) {
    const [data, setData] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);
    const [nonce, setNonce] = hooks.useState(0);
    hooks.useEffect(function () {
      let cancelled = false;
      function tick() {
        apiGet(path).then(function (d) {
          if (cancelled) return;
          setData(d);
          setError(null);
        }, function (err) {
          if (!cancelled) setError(err);
        });
      }
      tick();
      const timer = active ? setInterval(tick, 5000) : null;
      return function () { cancelled = true; if (timer) clearInterval(timer); };
    }, [path, active, nonce].concat(extraDeps || []));
    return {
      data: data, error: error,
      reload: function () { setNonce(function (n) { return n + 1; }); },
    };
  }

  function StatusChip(props) {
    return h("span", { className: cn("ht-status", "ht-lane-" + props.status) }, props.status || "—");
  }

  function AssigneeChip(props) {
    return props.assignee
      ? h("span", { className: "ht-chip ht-role-7" },
          h("span", { className: "ht-chip-dot" }), props.assignee)
      : h(Muted, null, "unassigned");
  }

  function clickableRow(key, selected, onOpen, title) {
    return {
      key: key, className: cn("ht-row-click", selected && "ht-row-selected"),
      tabIndex: 0, title: title, onClick: onOpen,
      onKeyDown: function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); }
      },
    };
  }

  function TaskCell(props) {
    return h("td", null,
      h("code", { className: "ht-code" }, String(props.task.id).slice(0, 8)),
      " ",
      h("span", { className: "ht-task-title" }, props.task.title || "")
    );
  }

  function ActivityPanel(props) {
    const [openTask, setOpenTask] = hooks.useState(null);
    hooks.useEffect(function () { setOpenTask(null); }, [props.team.slug]);
    if (openTask) {
      return h(TaskRunView, {
        team: props.team, taskId: openTask, rootRef: props.rootRef,
        onBack: function () { setOpenTask(null); },
      });
    }
    return h(ActivityList, {
      team: props.team, refreshKey: props.refreshKey, rootRef: props.rootRef,
      onOpen: setOpenTask,
    });
  }

  function ActivityList(props) {
    const slug = props.team.slug;
    const poll = usePoll("/teams/" + encodeURIComponent(slug) + "/status", true,
      [props.refreshKey]);
    const status = poll.data;
    const error = poll.error ? describeError(poll.error, "This team") : null;

    if (error && !status) return h(Note, { tone: "error", title: "Activity unavailable" }, error);
    if (!status) return h(Loading, { label: "Reading the board…" });

    const tasks = status.tasks || [];
    const byId = {};
    const byLane = {};
    LANES.forEach(function (lane) { byLane[lane] = []; });
    const other = [];
    tasks.forEach(function (task) {
      byId[task.id] = task;
      if (byLane[task.status]) byLane[task.status].push(task);
      else other.push(task);
    });

    function open(task) {
      return function () { props.onOpen(task.root_id || task.id); };
    }

    function rootsBlock() {
      const roots = (status.roots || []).filter(function (r) { return byId[r.id]; });
      if (!roots.length) return null;
      return h("div", { key: "roots", className: "ht-lane" },
        h("div", { className: "ht-lane-head" },
          h("span", { className: "ht-lane-name" }, "started tasks"),
          h("span", { className: "ht-lane-count" }, roots.length),
          h(Muted, null, "open one to see its subtasks and logs")
        ),
        h("div", { className: "ht-table-wrap" },
          h("table", { className: "ht-table" },
            h("thead", null, h("tr", null,
              h("th", null, "task"),
              h("th", null, "status"),
              h("th", null, "assignee"),
              h("th", null, "subtasks done"),
              h("th", null, "age")
            )),
            h("tbody", null, roots.map(function (r) {
              const task = byId[r.id];
              return h("tr", clickableRow(r.id, false, open(task), "Show this task's run"),
                h(TaskCell, { task: task }),
                h("td", null, h(StatusChip, { status: task.status })),
                h("td", null, h(AssigneeChip, { assignee: task.assignee })),
                h("td", null, r.done + "/" + r.total),
                h("td", null, ageOf(task.created_at))
              );
            }))
          )
        )
      );
    }

    function laneBlock(lane, rows) {
      if (!rows.length) return null;
      return h("div", { key: lane, className: cn("ht-lane", "ht-lane-" + lane) },
        h("div", { className: "ht-lane-head" },
          h("span", { className: "ht-lane-name" }, lane),
          h("span", { className: "ht-lane-count" }, rows.length)
        ),
        h("div", { className: "ht-table-wrap" },
          h("table", { className: "ht-table" },
            h("thead", null, h("tr", null,
              h("th", null, "task"),
              h("th", null, "assignee"),
              h("th", null, "prio"),
              h("th", null, "age")
            )),
            h("tbody", null, rows.map(function (task) {
              return h("tr", clickableRow(task.id, false, open(task), "Show the task run of this card"),
                h(TaskCell, { task: task }),
                h("td", null, h(AssigneeChip, { assignee: task.assignee })),
                h("td", null, String(task.priority === undefined ? 0 : task.priority)),
                h("td", null, ageOf(task.created_at))
              );
            }))
          )
        )
      );
    }

    return h("div", { className: "ht-panel" },
      h("div", { className: "ht-panel-head" },
        h("h3", null, "Activity"),
        h("div", { className: "ht-row" },
          h(Muted, null, "board "),
          h(HostLink, { to: "/kanban", rootRef: props.rootRef, title: "Open the Kanban tab" },
            status.board),
          h(Muted, null, " · polling every 5s")
        )
      ),
      h(Counters, { counts: status.counts }),
      status.board_db_path ? null
        : h(Note, { tone: "info", title: "No board database yet" },
            "The board is created on the first card — counters read as zero until then."),
      error ? h(Note, { tone: "warn", title: "Last poll failed" }, error) : null,
      tasks.length === 0
        ? h(Empty, { title: "No cards for this team's tenant yet." },
            "Start work from the Start work panel; the card lands here.")
        : h("div", { className: "ht-lanes" },
            [rootsBlock()]
              .concat(LANES.map(function (lane) { return laneBlock(lane, byLane[lane]); }))
              .concat([laneBlock("other", other)])
          )
    );
  }

  function runTime(task) {
    if (!task.started_at) return h(Muted, null, "queued");
    if (task.completed_at || task.status === "running") {
      return durationOf(task.started_at, task.completed_at);
    }
    return "—";
  }

  function lastStarted(tasks) {
    for (let i = tasks.length - 1; i >= 0; i--) if (tasks[i].started_at) return tasks[i];
    return tasks[0] || null;
  }

  function TaskRunView(props) {
    const base = "/teams/" + encodeURIComponent(props.team.slug) + "/tasks/";
    const poll = usePoll(base + encodeURIComponent(props.taskId), true);
    // null follows the running card; a click pins the selection.
    const [picked, setPicked] = hooks.useState(null);
    const run = poll.data;
    const tasks = (run && run.tasks) || [];
    const current = tasks.filter(function (t) { return t.id === picked; })[0]
      || tasks.filter(function (t) { return t.status === "running"; })[0]
      || lastStarted(tasks);
    const back = h(UI.Button, { size: "sm", outlined: true, onClick: props.onBack }, "← All tasks");

    if (poll.error && !run) {
      return h("div", { className: "ht-panel" },
        h("div", { className: "ht-row" }, back),
        h(ErrorNote, { error: poll.error, what: "This task", onRetry: poll.reload }));
    }
    if (!run || !tasks.length) return h(Loading, { label: "Reading the task…" });

    const root = tasks[0];
    const done = tasks.filter(function (t) { return t.status === "done"; }).length;

    return h("div", { className: "ht-panel" },
      h("div", { className: "ht-panel-head" },
        h("div", { className: "ht-row" }, back, h("h3", null, root.title || root.id)),
        h("div", { className: "ht-row" },
          h(StatusChip, { status: root.status }),
          h(Muted, null, done + "/" + tasks.length + " done · board "),
          h(HostLink, { to: "/kanban", rootRef: props.rootRef, title: "Open the Kanban tab" },
            run.board),
          h(Muted, null, " · polling every 5s")
        )
      ),
      poll.error
        ? h(Note, { tone: "warn", title: "Last poll failed" }, describeError(poll.error, "This task"))
        : null,
      h("div", { className: "ht-run-split" },
        h("div", { className: "ht-table-wrap" },
          h("table", { className: "ht-table" },
            h("thead", null, h("tr", null,
              h("th", null, "#"),
              h("th", null, "card"),
              h("th", null, "status"),
              h("th", null, "time")
            )),
            h("tbody", null, tasks.map(function (task, i) {
              return h("tr", clickableRow(task.id, current && current.id === task.id,
                  function () { setPicked(task.id); }, "Show this card's log"),
                h("td", null, h(Muted, null, String(i + 1))),
                h("td", null,
                  h("code", { className: "ht-code" }, task.id),
                  h("span", { className: "ht-run-title" }, task.title || ""),
                  h("div", { className: "ht-run-meta" }, h(AssigneeChip, { assignee: task.assignee }))
                ),
                h("td", null, h(StatusChip, { status: task.status })),
                h("td", null, runTime(task))
              );
            }))
          )
        ),
        current ? h(TaskLogPane, {
          key: current.id, task: current,
          path: base + encodeURIComponent(current.id) + "/log",
        }) : null
      )
    );
  }

  function TaskLogPane(props) {
    const task = props.task;
    const poll = usePoll(props.path, task.status === "running");
    const log = poll.data;
    const preRef = hooks.useRef(null);
    // Follow the tail unless the reader scrolled up.
    const follow = hooks.useRef(true);
    hooks.useEffect(function () {
      const el = preRef.current;
      if (el && follow.current) el.scrollTop = el.scrollHeight;
    }, [log && log.content]);

    let body;
    if (poll.error && !log) {
      body = h(ErrorNote, { error: poll.error, what: "This log", onRetry: poll.reload });
    } else if (!log) {
      body = h(Loading, { label: "Reading the log…" });
    } else if (!log.exists) {
      body = h(Empty, { title: "No worker log yet." },
        "The card has not started, or its log was rotated away.");
    } else {
      body = h("pre", {
        ref: preRef, className: "ht-log",
        onScroll: function (e) {
          const el = e.currentTarget;
          follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        },
      }, log.content || "(empty)");
    }

    return h("div", { className: "ht-log-pane" },
      h("div", { className: "ht-panel-head" },
        h("div", null,
          h("div", { className: "ht-row" },
            h("code", { className: "ht-code" }, task.id),
            h(StatusChip, { status: task.status })),
          h("strong", { className: "ht-run-title" }, task.title || "")
        ),
        h("div", { className: "ht-row" },
          log && log.exists ? h(Muted, null, Math.ceil(log.size_bytes / 1024) + " KB") : null,
          h(UI.Button, { size: "sm", outlined: true, onClick: poll.reload }, "Refresh")
        )
      ),
      task.summary ? h("div", { className: "ht-kicker" }, "Summary") : null,
      task.summary ? h("pre", { className: "ht-summary" }, task.summary) : null,
      h("div", { className: "ht-kicker" },
        "Worker log" + (task.assignee ? " · " + task.assignee : "")),
      body,
      log && log.truncated
        ? h(Muted, null, "Showing the end of the log. Full log: ",
            h("code", { className: "ht-code" }, log.path))
        : null
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Drift panel — GET /teams/{slug}/drift + POST /teams/{slug}/sync
  // ═══════════════════════════════════════════════════════════════════════════

  function DriftTable(props) {
    if (!props.items || !props.items.length) {
      return h(Empty, { title: "No drift." }, "The record and the world agree.");
    }
    return h("div", { className: "ht-table-wrap" },
      h("table", { className: "ht-table" },
        h("thead", null, h("tr", null,
          h("th", null, "check"),
          h("th", null, "severity"),
          h("th", null, "subject"),
          h("th", null, "observed"),
          h("th", null, "desired"),
          h("th", null, "fix")
        )),
        h("tbody", null, props.items.map(function (item, i) {
          return h("tr", { key: item.check + ":" + item.subject + ":" + i },
            h("td", null, h("code", { className: "ht-code" }, item.check)),
            h("td", null, h(UI.Badge, {
              tone: item.severity === "error" ? "destructive"
                : (item.severity === "warn" ? "warning" : "secondary"),
            }, item.severity)),
            h("td", null, h("code", { className: "ht-code" }, item.subject)),
            h("td", null, h("span", { className: "ht-val" }, JSON.stringify(item.observed))),
            h("td", null, h("span", { className: "ht-val" }, JSON.stringify(item.desired))),
            h("td", null, item.auto_fixable
              ? h(UI.Badge, { tone: "success" }, "auto")
              : h("span", { className: "ht-hint" }, item.remedy || "manual"))
          );
        }))
      )
    );
  }

  function DriftPanel(props) {
    const slug = props.team.slug;
    const [drift, setDrift] = hooks.useState(null);
    const [report, setReport] = hooks.useState(null);
    const [loadError, setLoadError] = hooks.useState(null);   // string
    const [syncError, setSyncError] = hooks.useState(null);   // Error
    const [busy, setBusy] = hooks.useState(null);
    const [createMissing, setCreateMissing] = hooks.useState(false);
    const [reinstantiate, setReinstantiate] = hooks.useState(false);
    const [linkEnv, setLinkEnv] = hooks.useState(false);
    const busyGuard = useBusyGuard();

    hooks.useEffect(function () {
      let cancelled = false;
      apiGet("/teams/" + encodeURIComponent(slug) + "/drift").then(function (data) {
        if (cancelled) return;
        setDrift((data && data.drift) || []);
      }, function (err) {
        if (cancelled) return;
        setLoadError(describeError(err, "This team"));
      });
      return function () { cancelled = true; };
    }, [slug, props.refreshKey]);

    function sync(dry) {
      setBusy(dry ? "dry" : "apply");
      setSyncError(null);
      // The sync body carries the CLI's own flag names (post_team_sync).
      const payload = {
        create_missing: createMissing,
        reinstantiate: reinstantiate,
        link_env: linkEnv,
      };
      // CAS only on the applying run — the dry run writes nothing and is the
      // one non-GET still allowed when dashboard_writes is false (§11).
      if (!dry && props.revision !== null && props.revision !== undefined) {
        payload.expected_revision = props.revision;
      }
      const path = "/teams/" + encodeURIComponent(slug) + "/sync?dry_run=" + (dry ? "true" : "false");
      function done(data) {
        setBusy(null);
        setReport(data || null);
        setDrift((data && data.drift) || []);
        setLoadError(null);
        if (!dry) props.onChanged();
      }
      function fail(err) {
        setBusy(null);
        setSyncError(err);
        if (isReadOnly(err)) props.onReadOnly();
      }
      if (dry) {
        apiSend("POST", path, payload).then(done, fail);
        return;
      }
      busyGuard.run({
        send: function (onBusy) {
          return apiSend("POST", path, Object.assign({}, payload, { on_busy: onBusy }));
        },
        onDone: done,
        onError: fail,
        onCancel: function () { setBusy(null); },
      });
    }

    return h("div", { className: "ht-panel" },
      h("div", { className: "ht-panel-head" },
        h("h3", null, "Drift"),
        h("div", { className: "ht-row" },
          h(UI.Button, {
            size: "sm", outlined: true, disabled: busy !== null,
            onClick: function () { sync(true); },
          }, busy === "dry" ? "Checking…" : "Dry run"),
          h(UI.Button, {
            size: "sm", disabled: busy !== null || props.readOnly,
            onClick: function () { sync(false); },
          }, busy === "apply" ? "Applying…" : "Apply")
        )
      ),
      props.readOnly
        ? h(Note, { tone: "warn", title: "Read-only dashboard" },
            "Apply is disabled — the dry run still works, because it writes nothing (§11).")
        : null,
      h("div", { className: "ht-row ht-row-wrap" },
        h(CheckField, {
          label: "create missing board / workdir", checked: createMissing, onChange: setCreateMissing,
        }),
        h(CheckField, {
          label: "re-instantiate missing managed instances", checked: reinstantiate, onChange: setReinstantiate,
        }),
        h(CheckField, { label: "link .env", checked: linkEnv, onChange: setLinkEnv })
      ),
      loadError ? h(Note, { tone: "error", title: "Drift unavailable" }, loadError) : null,
      syncError ? h(ErrorNote, { error: syncError, what: "This team" }) : null,
      h(BusyNote, { guard: busyGuard }),
      report ? h("div", null,
        h(Note, { tone: report.dry_run ? "info" : "ok",
          title: report.dry_run ? "Dry run — nothing written" : "Applied" },
          busyOutcome(report) +
          (report.applied || []).length + " item(s) acted on · " +
          (report.profiles_updated || 0) + " profile config(s) changed · exit " +
          (report.exit_code || 0)),
        h(RestartBanner, { notice: report.restart_notice }),
        (report.applied || []).length ? h("div", { className: "ht-table-wrap" },
          h("table", { className: "ht-table" },
            h("thead", null, h("tr", null,
              h("th", null, "check"), h("th", null, "subject"),
              h("th", null, "applied"), h("th", null, "error")
            )),
            h("tbody", null, report.applied.map(function (item, i) {
              return h("tr", { key: i },
                h("td", null, h("code", { className: "ht-code" }, item.check)),
                h("td", null, h("code", { className: "ht-code" }, item.subject)),
                h("td", null, item.applied
                  ? h(UI.Badge, { tone: "success" }, "yes")
                  : h(UI.Badge, { tone: "destructive" }, "no")),
                h("td", null, item.error ? String(item.error) : h(Muted, null, "—"))
              );
            }))
          )
        ) : null
      ) : null,
      drift === null ? h(Loading, { label: "Diffing…" }) : h(DriftTable, { items: drift })
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Approvals — the cards parked on a human decision
  //
  // A card waits on a human iff status == "blocked" AND block_kind ==
  // "needs_input". approvals.py owns that predicate and the CLI calls the same
  // functions, so the two surfaces cannot drift. A decision posts a comment and
  // unblocks: neither surface ever completes the card — the assignee resumes,
  // acts on the note and completes it, which promotes the children.
  // ═══════════════════════════════════════════════════════════════════════════

  const DECISIONS = {
    approve: {
      label: "Approve", busyLabel: "Approving…", field: "note",
      prompt: "Your decision, in the words the assignee needs (\"A: OAuth application-only\").",
    },
    reject: {
      label: "Request changes", busyLabel: "Sending…", field: "reason",
      prompt: "What must change before this can be approved.",
    },
    // The stuck lane: no decision to record, so the note is the instruction
    // the assignee reads on the re-run. POST …/release comments + unblocks,
    // which also resets the retry budget the circuit breaker spent.
    release: {
      label: "Release", busyLabel: "Releasing…", field: "note",
      prompt: "What the assignee should do differently on the re-run.",
    },
  };

  function ArtifactView(props) {
    const att = props.attachment;
    const [data, setData] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);

    hooks.useEffect(function () {
      let cancelled = false;
      setData(null);
      setError(null);
      apiGet("/teams/" + encodeURIComponent(props.slug) + "/attachments/" +
        encodeURIComponent(att.id)).then(function (d) {
          if (!cancelled) setData(d || null);
        }, function (err) {
          if (!cancelled) setError(err);
        });
      return function () { cancelled = true; };
    }, [props.slug, att.id]);

    // The SDK ships no markdown renderer and the dashboard serves its own
    // assets — the artifact is read as text, never rendered.
    let body;
    if (error) {
      body = h(ErrorNote, { error: error, what: "This artifact" });
    } else if (!data) {
      body = h(Loading, { label: "Reading " + att.filename + "…" });
    } else if (data.text === null || data.text === undefined) {
      body = h(Note, { tone: "warn", title: "Not previewable" },
        (data.note || "This attachment is not text.") +
        (data.stored_path ? " " + data.stored_path : ""));
    } else {
      body = h("pre", { className: "ht-log" }, data.text || "(empty)");
    }

    return h("div", { className: "ht-log-pane" },
      h("div", { className: "ht-panel-head" },
        h("div", { className: "ht-row" },
          h("strong", null, att.filename),
          h(Muted, null, Math.ceil((att.size || 0) / 1024) + " KB")
        ),
        h(UI.Button, { size: "sm", outlined: true, onClick: props.onClose }, "Close")
      ),
      body,
      data && data.truncated
        ? h(Muted, null, "Showing the start of the file only — read the rest on disk.")
        : null
    );
  }

  function DecisionForm(props) {
    const spec = DECISIONS[props.action];
    const [text, setText] = hooks.useState("");
    const [park, setPark] = hooks.useState(false);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const id = "ht-decide-" + props.action + "-" + props.task.id;

    function submit() {
      setBusy(true);
      setError(null);
      // confirm == task_id is the same confirmation token the DELETEs use
      // (_confirm in plugin_api.py).
      const body = { confirm: props.task.id };
      body[spec.field] = text;
      if (props.action === "reject") body.park = park;
      apiSend("POST", "/teams/" + encodeURIComponent(props.slug) + "/tasks/" +
        encodeURIComponent(props.task.id) + "/" + props.action, body)
        .then(function (data) {
          setBusy(false);
          props.onDone(data || null);
        }, function (err) {
          setBusy(false);
          setError(err);
          if (isReadOnly(err)) props.onReadOnly();
        });
    }

    return h("div", { className: "ht-decide" },
      error ? h(ErrorNote, { error: error, what: "This card" }) : null,
      h(Field, { label: spec.field, htmlFor: id, hint: spec.prompt },
        h("textarea", {
          id: id, className: "ht-textarea", rows: 4, value: text,
          disabled: props.readOnly || busy,
          onChange: function (e) { setText(e.target.value); },
        })
      ),
      props.action === "reject" ? h(CheckField, {
        label: "park it — leave the card blocked",
        title: "Off: the card is unblocked and the assignee reworks it.",
        checked: park, disabled: props.readOnly || busy, onChange: setPark,
      }) : null,
      h("div", { className: "ht-row ht-row-end" },
        h(UI.Button, { size: "sm", outlined: true, disabled: busy, onClick: props.onCancel },
          "Cancel"),
        h(UI.Button, {
          size: "sm", destructive: props.action === "reject",
          disabled: props.readOnly || busy || !text.trim(), onClick: submit,
        }, busy ? spec.busyLabel : spec.label)
      )
    );
  }

  function ApprovalCard(props) {
    const row = props.row;
    const [action, setAction] = hooks.useState(null);
    const [openFile, setOpenFile] = hooks.useState(null);
    const attachments = row.attachments || [];
    // reason_kind is the lane: "needs_input" waits on a decision (approve /
    // request changes), "stuck" waits on nobody (release). Rows from an older
    // backend carry no reason_kind, which reads as the needs_input lane.
    const stuck = row.reason_kind === "stuck";

    return h("div", { className: "ht-approval" },
      h("div", { className: "ht-approval-head" },
        h("div", { className: "ht-row" },
          h("code", { className: "ht-code" }, row.id),
          stuck ? h("span", { className: "ht-approval-stuck" }, "STUCK") : null,
          h("span", { className: "ht-approval-wait" },
            (stuck ? "stuck " : "waiting ") + ageOf(row.blocked_at || row.created_at)),
          h(AssigneeChip, { assignee: row.assignee })
        ),
        h("strong", { className: "ht-run-title" }, row.title || "")
      ),
      stuck ? h("p", { className: "ht-hint" },
        (row.failure_error || row.block_reason ||
          "Blocked with nobody to answer — release it to put it back in the pool.") +
        (row.consecutive_failures
          ? "  (" + row.consecutive_failures + " consecutive failure(s))" : "")
      ) : (row.block_reason ? h("p", { className: "ht-hint" }, row.block_reason) : null),
      row.headline ? h("blockquote", { className: "ht-quote" }, row.headline) : null,
      attachments.length ? h("div", { className: "ht-row ht-approval-files" },
        h(Muted, null, "artifacts"),
        attachments.map(function (att) {
          const open = !!openFile && openFile.id === att.id;
          return h("button", {
            key: att.id,
            type: "button",
            className: cn("ht-chip ht-file", open && "ht-file-on"),
            disabled: !att.previewable,
            title: att.previewable
              ? "Read " + att.filename
              : att.filename + " is not previewable here",
            onClick: function () { setOpenFile(open ? null : att); },
          }, h("span", { className: "ht-chip-dot" }), att.filename);
        })
      ) : null,
      openFile ? h(ArtifactView, {
        key: openFile.id, slug: props.slug, attachment: openFile,
        onClose: function () { setOpenFile(null); },
      }) : null,
      action ? h(DecisionForm, {
        key: action, slug: props.slug, task: row, action: action,
        readOnly: props.readOnly, onReadOnly: props.onReadOnly,
        onCancel: function () { setAction(null); },
        onDone: function (data) { setAction(null); props.onDecided(data); },
      }) : h("div", { className: "ht-row ht-row-end" },
        stuck ? h(UI.Button, {
          size: "sm", disabled: props.readOnly,
          onClick: function () { setAction("release"); },
        }, "Release") : null,
        stuck ? null : h(UI.Button, {
          size: "sm", outlined: true, disabled: props.readOnly,
          onClick: function () { setAction("reject"); },
        }, "Request changes"),
        stuck ? null : h(UI.Button, {
          size: "sm", disabled: props.readOnly,
          onClick: function () { setAction("approve"); },
        }, "Approve")
      )
    );
  }

  // Silent when nothing waits: this is an alert surface, mounted above the
  // pill bar so a waiting or stuck card is visible whichever panel is open.
  function ApprovalsPanel(props) {
    const slug = props.team.slug;
    const poll = usePoll("/teams/" + encodeURIComponent(slug) + "/approvals", true,
      [props.refreshKey]);
    const rows = (poll.data && poll.data.approvals) || [];
    if (!poll.data || !rows.length) return null;
    const counts = (poll.data && poll.data.counts) || {};
    const lanes = [];
    if (counts.needs_input) lanes.push(counts.needs_input + " blocked on a decision");
    if (counts.stuck) lanes.push(counts.stuck + " stuck");

    return h("div", { className: "ht-panel ht-approvals" },
      h("div", { className: "ht-panel-head" },
        h("div", { className: "ht-row" },
          h("h3", null, "Waiting for you"),
          h(UI.Badge, { tone: "warning" }, String(rows.length))
        ),
        h(Muted, null, (lanes.join(" · ") || "blocked on a human decision") +
          " · polling every 5s")
      ),
      props.readOnly
        ? h(Note, { tone: "warn", title: "Read-only dashboard" }, READONLY_MESSAGE) : null,
      rows.map(function (row) {
        return h(ApprovalCard, {
          key: row.id, slug: slug, row: row,
          readOnly: props.readOnly, onReadOnly: props.onReadOnly,
          onDecided: function () { poll.reload(); props.onChanged(); },
        });
      })
    );
  }

  // header-right slot: the badge is on every dashboard page, so a card that
  // wants a decision is findable without knowing which team parked it.
  function ApprovalsBadge() {
    const poll = usePoll("/approvals", true);
    const ref = hooks.useRef(null);
    const count = poll.data ? Number(poll.data.count || 0) : 0;
    if (!count) return null;
    return h("span", { className: "ht-approvals-slot", ref: ref },
      h(HostLink, {
        to: "/synergia", rootRef: ref, className: "ht-approvals-badge",
        title: count + " card(s) blocked on your decision or stuck — open Synergia",
      },
        h("span", { className: "ht-approvals-dot" }),
        String(count) + " waiting"
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Team detail
  // ═══════════════════════════════════════════════════════════════════════════

  // Shared by TeamDetail ("Destroy team") and TeamList (card "Delete").
  function useTeamDestroyer(opts) {
    const [error, setError] = hooks.useState(null);
    const [purgeBoard, setPurgeBoard] = hooks.useState(false);
    const guard = useBusyGuard();

    // Same ref discipline as the roster's remover: never let a memoised
    // callback send a stale purge selection.
    const ref = hooks.useRef({});
    ref.current = { purgeBoard: purgeBoard, opts: opts };

    const destroyer = useConfirmDelete({
      onDelete: function (teamSlug) {
        const current = ref.current;
        // g5: DELETE /teams/{slug} refuses without {"confirm": "<slug>"}.
        const payload = {
          confirm: teamSlug,
          purge_board: current.purgeBoard,
        };
        const revision = current.opts.revision;
        if (revision !== null && revision !== undefined) {
          payload.expected_revision = revision;
        }
        setError(null);
        return guard.run({
          send: function (onBusy) {
            return apiSend("DELETE", "/teams/" + encodeURIComponent(teamSlug),
              Object.assign({}, payload, { on_busy: onBusy }));
          },
          onDone: function (data) {
            current.opts.onDestroyed(data || null, teamSlug);
          },
          onError: function (err) {
            setError(err);
            if (isReadOnly(err)) current.opts.onReadOnly();
          },
        });
      },
    });

    return {
      destroyer: destroyer,
      busy: guard,
      purgeBoard: purgeBoard,
      setPurgeBoard: setPurgeBoard,
      error: error,
      request: function (teamSlug) {
        setPurgeBoard(false);
        setError(null);
        destroyer.requestDelete(teamSlug);
      },
    };
  }

  // members === null means the roster is not loaded yet (list cards enrich lazily).
  function DestroyTeamDialog(props) {
    const state = props.state;
    const destroyer = state.destroyer;
    const managed = props.members
      ? props.members.filter(function (m) { return m.managed; })
          .map(function (m) { return m.profile; })
      : null;

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) destroyer.cancel(); },
    },
      h(UI.DialogContent, { className: "ht-dialog" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, "Destroy team " + props.slug + "?"),
          h(UI.DialogDescription, null,
            "The record and the profiles this team created are deleted — " +
            "every managed: false member is kept and reported.")
        ),
        h("div", { className: "ht-dialog-body ht-form" },
          h("p", { className: "ht-muted" }, managed === null
            ? "Profiles this team created are deleted; adopted members are kept."
            : (managed.length
              ? "Profiles deleted: " + managed.join(", ")
              : "No profiles are deleted — every member is adopted.")),
          h(CheckField, {
            label: "also delete the board directory (" + props.board + ")",
            checked: state.purgeBoard, onChange: state.setPurgeBoard,
          })
        ),
        h(UI.DialogFooter, null,
          h(UI.Button, { outlined: true, onClick: destroyer.cancel, disabled: destroyer.isDeleting }, "Cancel"),
          h(UI.Button, {
            destructive: true, disabled: destroyer.isDeleting, onClick: destroyer.confirm,
          }, destroyer.isDeleting ? "…" : "Destroy team")
        )
      )
    );
  }

  function TeamDetail(props) {
    const slug = props.slug;
    const [team, setTeam] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);
    const [panel, setPanel] = hooks.useState("roster");
    const [refreshKey, setRefreshKey] = hooks.useState(0);
    const [editingWorkdir, setEditingWorkdir] = hooks.useState(false);

    hooks.useEffect(function () {
      let cancelled = false;
      setError(null);
      apiGet("/teams/" + encodeURIComponent(slug)).then(function (data) {
        if (cancelled) return;
        setTeam((data && data.team) || null);
      }, function (err) {
        if (cancelled) return;
        setError(err);
      });
      return function () { cancelled = true; };
    }, [slug, refreshKey]);

    function reload() {
      setRefreshKey(function (n) { return n + 1; });
      props.onRegistryChanged();
    }

    const destroy = useTeamDestroyer({
      revision: props.revision,
      onDestroyed: props.onDestroyed,
      onReadOnly: props.onReadOnly,
    });

    if (error) {
      return h("div", { className: "ht-panel" },
        h(ErrorNote, { error: error, what: "This team", onRetry: reload }));
    }
    if (!team) return h(Loading, { label: "Loading team…" });

    const resolved = team._resolved || {};

    return h("div", { className: "ht-detail" },
      h("div", { className: "ht-detail-head" },
        h("div", null,
          h("div", { className: "ht-kicker" }, "team"),
          h("h2", null, team.display_name || team.slug),
          h("p", { className: "ht-muted" }, team.description || "")
        ),
        h("div", { className: "ht-row" },
          h(UI.Button, { size: "sm", outlined: true, onClick: reload }, "Reload"),
          h(UI.Button, {
            size: "sm", outlined: true, destructive: true, disabled: props.readOnly,
            onClick: function () { destroy.request(team.slug); },
          }, "Destroy team")
        )
      ),
      h("div", { className: "ht-facts" },
        h("div", null, h("span", { className: "ht-fact-label" }, "board"),
          h(HostLink, { to: "/kanban", rootRef: props.rootRef, title: "Open the Kanban tab" }, team.board),
          resolved.board_exists === false
            ? h(UI.Badge, { tone: "warning", className: "ht-inline-badge" }, "missing") : null),
        h("div", null, h("span", { className: "ht-fact-label" }, "workdir"),
          h("code", { className: "ht-code" }, team.workdir || "—"),
          resolved.workdir_exists === false
            ? h(UI.Badge, { tone: "warning", className: "ht-inline-badge" }, "missing") : null,
          resolved.workdir_is_git_repo
            ? h(UI.Badge, { tone: "outline", className: "ht-inline-badge" }, "git") : null,
          h(UI.Button, {
            size: "sm", outlined: true, className: "ht-inline-badge",
            disabled: props.readOnly,
            onClick: function () { setEditingWorkdir(function (v) { return !v; }); },
          }, "Change")),
        h("div", null, h("span", { className: "ht-fact-label" }, "policy"),
          h("code", { className: "ht-code" }, (team.policy && team.policy.mode) || "advise"),
          h("span", { className: "ht-fact-label" }, "workspace"),
          h("code", { className: "ht-code" }, team.workspace_policy || "scratch")),
        h("div", null, h("span", { className: "ht-fact-label" }, "revision"),
          h("code", { className: "ht-code" }, String(team.revision)),
          h("span", { className: "ht-fact-label" }, "managed instances"),
          h("code", { className: "ht-code" }, String(resolved.managed_profile_count || 0)))
      ),
      editingWorkdir ? h(WorkdirEditor, {
        team: team, revision: props.revision, readOnly: props.readOnly,
        onReadOnly: props.onReadOnly, onChanged: reload,
        onClose: function () { setEditingWorkdir(false); },
      }) : null,
      h(Counters, { counts: resolved.counts }),
      // Rendered outside the destroy dialog: useConfirmDelete closes it when
      // onDelete settles, so a refusal must survive that close.
      destroy.error ? h(ErrorNote, { error: destroy.error, what: "This team" }) : null,
      h(BusyNote, { guard: destroy.busy }),
      h(ApprovalsPanel, {
        team: team, refreshKey: refreshKey, readOnly: props.readOnly,
        onReadOnly: props.onReadOnly,
        onChanged: function () { setRefreshKey(function (n) { return n + 1; }); },
      }),
      h(PillBar, {
        value: panel,
        onChange: setPanel,
        items: [
          { value: "roster", label: "Roster", badge: (team.members || []).length },
          { value: "start", label: "Start work" },
          { value: "activity", label: "Activity" },
          { value: "drift", label: "Drift" },
        ],
      }),
      panel === "roster" ? h(RosterPanel, {
        team: team, resolved: resolved, revision: props.revision,
        readOnly: props.readOnly, rootRef: props.rootRef,
        onReadOnly: props.onReadOnly, onChanged: reload,
      }) : null,
      panel === "start" ? h(StartWorkPanel, {
        team: team, readOnly: props.readOnly, onReadOnly: props.onReadOnly,
        onCreated: function () { setRefreshKey(function (n) { return n + 1; }); },
      }) : null,
      panel === "activity" ? h(ActivityPanel, {
        team: team, refreshKey: refreshKey, rootRef: props.rootRef,
      }) : null,
      panel === "drift" ? h(DriftPanel, {
        team: team, revision: props.revision, refreshKey: refreshKey,
        readOnly: props.readOnly, onReadOnly: props.onReadOnly, onChanged: reload,
      }) : null,
      C.PluginSlot ? h(C.PluginSlot, { name: "team-panel" }) : null,
      destroy.destroyer.isOpen ? h(DestroyTeamDialog, {
        state: destroy, slug: team.slug, board: team.board,
        members: team.members || [],
      }) : null
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // New-team dialog — POST /teams (?dry_run=true → plan, else 202 → job)
  // ═══════════════════════════════════════════════════════════════════════════

  function NewTeamDialog(props) {
    const catalog = useCatalog();
    const modelOptions = useModelOptions();
    const [facets, setFacets] = hooks.useState({ q: "", pack: "", category: "" });
    const [form, setForm] = hooks.useState({
      name: "",
      description: "",
      board: "",
      workdir: "",
      workspace_policy: "worktree",
      profile_naming: "",
      policy: "advise",
      link_env: false,
    });
    // Every row carries a minted, stable `_rid`. Keying on blueprint+index
    // re-keys every row after a removed one, which remounts them and re-fires
    // their /skills fetch. payload() builds the body from named keys, so
    // `_rid` never reaches the wire.
    const rowSeq = hooks.useRef(0);
    const [roster, setRoster] = hooks.useState([]);
    const [plan, setPlan] = hooks.useState(null);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const jobs = useJobPoll(function (snapshot) {
      if (snapshot.state === "done") props.onCreated(snapshot.result || null);
    });

    // Functional updaters: two patches in one tick must not both read the
    // same render's state and drop the first write. Any edit clears the plan.
    function patch(next) {
      setForm(function (current) { return Object.assign({}, current, next); });
      setPlan(null);
    }

    function editRoster(fn) {
      setRoster(fn);
      setPlan(null);
    }

    function addEntry(entry) {
      rowSeq.current += 1;
      const rid = "r" + rowSeq.current;
      editRoster(function (current) {
        return current.concat([{
          _rid: rid,
          blueprint: entry.id,
          role: entry.suggested_role || "member",
          main: current.length === 0,
          skills: [],
        }]);
      });
    }

    // All three address the row by `_rid`, never by an index captured in an
    // earlier render. `p` may be a function of the current row (skill toggle).
    function patchMember(rid, p) {
      editRoster(function (current) {
        return current.map(function (m) {
          if (m._rid !== rid) return m;
          return Object.assign({}, m, typeof p === "function" ? p(m) : p);
        });
      });
    }

    function setMain(rid) {
      editRoster(function (current) {
        return current.map(function (m) {
          return Object.assign({}, m, { main: m._rid === rid });
        });
      });
    }

    function removeMember(rid) {
      editRoster(function (current) {
        const next = current.filter(function (m) { return m._rid !== rid; });
        // Removing the orchestrator promotes the first remaining row.
        if (next.length && !next.some(function (m) { return m.main; })) {
          next[0] = Object.assign({}, next[0], { main: true });
        }
        return next;
      });
    }

    const slug = slugify(form.name);
    // Same naming precedence as cli._create_impl, so the rendered names match
    // what the server stamps.
    const naming = form.profile_naming.trim() || props.defaultNaming || DEFAULT_PROFILE_NAMING;
    const mains = roster.filter(function (m) { return m.main; }).length;
    // model.name_conflict, client side: names compare by the slug they derive.
    const clash = slug ? (props.teams || []).filter(function (t) {
      return t.slug === slug || slugify(t.display_name || t.slug) === slug;
    })[0] || null : null;

    function payload() {
      // Only keys in plugin_api._CREATE_BODY_KEYS — an unknown key is a 400.
      const out = {
        name: form.name.trim(),
        workdir: form.workdir.trim(),
        workspace_policy: form.workspace_policy,
        policy: form.policy,
        link_env: form.link_env,
        roster: roster.map(function (m) {
          const row = { blueprint: m.blueprint, role: m.role || "member", main: !!m.main };
          if (m.skills && m.skills.length) row.skills = m.skills;
          if (m.model) row.model = m.model;
          if (m.provider) row.provider = m.provider;
          return row;
        }),
      };
      if (form.description.trim()) out.description = form.description.trim();
      if (form.board.trim()) out.board = form.board.trim();
      if (form.profile_naming.trim()) out.profile_naming = form.profile_naming.trim();
      if (props.revision !== null && props.revision !== undefined) {
        out.expected_revision = props.revision;
      }
      return out;
    }

    function preview() {
      setBusy(true);
      setError(null);
      // ?dry_run=true only — never both query and body (a disagreeing pair is
      // a deliberate 400, S19b F3).
      apiSend("POST", "/teams?dry_run=true", payload()).then(function (data) {
        setBusy(false);
        setPlan(data || null);
      }, function (err) {
        setBusy(false);
        setError(err);
        if (isReadOnly(err)) props.onReadOnly();
      });
    }

    function create() {
      setBusy(true);
      setError(null);
      apiSend("POST", "/teams", payload()).then(function (data) {
        setBusy(false);
        if (data && data.job_id) jobs.watch(data.job_id);
      }, function (err) {
        setBusy(false);
        setError(err);
        if (isReadOnly(err)) props.onReadOnly();
      });
    }

    const missing = [];
    if (!slug) missing.push("a team name with letters or digits");
    if (clash) missing.push("a name no other team uses");
    if (!form.workdir.trim()) missing.push("a folder (workdir)");
    if (!roster.length) missing.push("at least one profile");
    else if (mains !== 1) missing.push("one orchestrator");
    const ready = missing.length === 0;

    const result = (jobs.job && jobs.job.result) || {};
    const readiness = result.ready || null;
    const created = result.team || {};

    let nameHint = "Unique across teams. The slug, board and profile names derive from it.";
    if (clash) nameHint = "Taken by team " + clash.slug + " — team names are unique.";
    else if (slug) nameHint = "Slug " + slug + " — board, tenant and profile names use it.";
    else if (form.name.trim()) nameHint = "Needs letters or digits.";

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) props.onClose(); },
    },
      h(UI.DialogContent, { className: "ht-dialog ht-dialog-wide" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, "New team"),
          h(UI.DialogDescription, null,
            "Name the team, pick its profiles from the catalog and point it at a " +
            "folder. Create makes the board and folder, instantiates the profiles " +
            "and syncs them, so the team is ready for tasks. Preview first — the " +
            "dry run stops before anything is written.")
        ),
        h("div", { className: "ht-dialog-body" },
          jobs.job ? h("div", null,
            h(JobProgress, { job: jobs.job }),
            jobs.job.state === "done" ? h("div", null,
              h(Note, { tone: "ok", title: "Team created" },
                "Instantiated " + ((result.instantiated || []).join(", ") || "no new profiles") + "."),
              readiness ? (readiness.ready
                ? h(Note, { tone: "ok", title: "Ready for tasks" },
                    "Board " + created.board + " is live; new cards go to " +
                    created.orchestrator + ".")
                : h(Note, { tone: "warn", title: "Not ready for tasks yet" },
                    h("ul", { className: "ht-blocking" }, readiness.blocking.map(function (b, i) {
                      return h("li", { key: i },
                        h("code", { className: "ht-code" }, b.check), " " + b.subject + " — " + b.remedy);
                    })))) : null,
              result.warning
                ? h(Note, { tone: "warn", title: "Workspace fallback" }, result.warning)
                : null,
              h(RestartBanner, { notice: result.restart_notice })
            ) : null
          ) : h("div", { className: "ht-form" },
            h("div", { className: "ht-grid-2" },
              h(TextField, {
                id: "ht-new-name", label: "Team name *", value: form.name,
                onChange: function (v) { patch({ name: v }); },
                hint: nameHint,
              }),
              h(TextField, {
                id: "ht-new-workdir", label: "Folder (workdir) *", value: form.workdir,
                onChange: function (v) { patch({ workdir: v }); },
                hint: "Absolute path outside the Hermes root, e.g. /root/workspace/" +
                  (slug || "<slug>") + ". Created when missing.",
              }),
              h(TextField, {
                id: "ht-new-board", label: "Board", value: form.board,
                onChange: function (v) { patch({ board: v }); },
                hint: "Blank → the slug. Created when missing.",
              }),
              h(TextField, {
                id: "ht-new-desc", label: "Description", value: form.description,
                onChange: function (v) { patch({ description: v }); },
              })
            ),
            h("div", { className: "ht-grid-2" },
              h(Field, { label: "Workspace policy", htmlFor: "ht-new-policy" },
                h(UI.Select, {
                  id: "ht-new-policy", value: form.workspace_policy,
                  onValueChange: function (v) { patch({ workspace_policy: v }); },
                }, WORKSPACE_POLICIES.map(function (p) {
                  return h(UI.SelectOption, { key: p, value: p }, p);
                }))
              ),
              h(Field, { label: "Guard policy", htmlFor: "ht-new-guard" },
                h(UI.Select, {
                  id: "ht-new-guard", value: form.policy,
                  onValueChange: function (v) { patch({ policy: v }); },
                }, POLICY_MODES.map(function (p) {
                  return h(UI.SelectOption, { key: p, value: p }, p);
                }))
              ),
              h(TextField, {
                id: "ht-new-naming", label: "Profile naming", value: form.profile_naming,
                onChange: function (v) { patch({ profile_naming: v }); },
                hint: "Blank → settings, then " + DEFAULT_PROFILE_NAMING + ".",
              }),
              h(CheckField, { label: "link .env", checked: form.link_env,
                onChange: function (v) { patch({ link_env: v }); } })
            ),
            h(UI.Separator, { className: "ht-sep" }),
            h("div", { className: "ht-kicker" }, "roster"),
            h("div", { className: "ht-tm-list" },
              roster.length === 0
                ? h(Empty, { title: "No profiles yet." },
                    "Pick profiles from the catalog below. The first one is the orchestrator.")
                : roster.map(function (member) {
                    return h(RosterMemberRow, {
                      key: member._rid,
                      rid: member._rid,
                      member: member,
                      profileName: slug ? renderInstanceName(slug, member.blueprint, naming) : "",
                      entries: catalog.entries,
                      modelOptions: modelOptions,
                      onChange: function (p) { patchMember(member._rid, p); },
                      onMain: function () { setMain(member._rid); },
                      onRemove: function () { removeMember(member._rid); },
                    });
                  })
            ),
            h("div", { className: "ht-kicker" }, "catalog"),
            h(CatalogFacets, {
              entries: catalog.entries, facets: facets,
              onChange: function (p) { setFacets(Object.assign({}, facets, p)); },
            }),
            h(CatalogList, {
              entries: catalog.entries, error: catalog.error, facets: facets,
              selectedId: null, onSelect: addEntry,
            }),
            plan ? h("div", { className: "ht-plan" },
              h("div", { className: "ht-plan-head" },
                h("strong", null, "Dry run — nothing written"),
                h(UI.Badge, { tone: "secondary" },
                  ((plan.team && plan.team.members) || []).length + " members")
              ),
              h("div", { className: "ht-chips" },
                ((plan.team && plan.team.members) || []).map(function (m) {
                  return h(MemberChip, { key: m.profile, member: m, team: plan.team });
                })
              ),
              (plan.plans || []).map(function (p, i) {
                return h(PlanPreview, { key: i, plan: p });
              })
            ) : null,
            !plan ? h("p", { className: "ht-hint" }, ready
              ? "Run Dry run. Create team unlocks when the plan returns, and any edit clears the plan."
              : "Dry run needs " + missing.join(", ") + ".") : null,
            error ? h(ErrorNote, { error: error, what: "The registry" }) : null,
            h(JobErrorNote, { jobs: jobs })
          )
        ),
        h(UI.DialogFooter, null,
          jobs.job && (jobs.job.state === "done" || jobs.job.state === "error")
            ? h(UI.Button, { onClick: props.onClose }, "Close")
            : h(React.Fragment, null,
                h(UI.Button, { outlined: true, onClick: props.onClose, disabled: busy }, "Cancel"),
                h(UI.Button, {
                  outlined: true, disabled: !ready || busy || jobs.running, onClick: preview,
                }, busy && !plan ? "Planning…" : "Dry run"),
                h(UI.Button, {
                  disabled: !ready || busy || !plan || jobs.running, onClick: create,
                }, jobs.running ? "Creating…" : "Create team")
              )
        )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Team list
  // ═══════════════════════════════════════════════════════════════════════════

  function TeamCard(props) {
    const row = props.row;
    const detail = props.detail || {};
    const members = (detail.team && detail.team.members) || [];
    const drift = detail.drift || null;
    const ready = detail.ready || null;
    const errors = drift ? drift.filter(function (d) { return d.severity === "error"; }).length : 0;

    return h(UI.Card, { className: "ht-team-card" },
      h(UI.CardContent, { className: "ht-team-card-body" },
        h("div", { className: "ht-team-card-head" },
          h("button", {
            type: "button", className: "ht-team-name",
            onClick: function () { props.onOpen(row.slug); },
          }, (detail.team && detail.team.display_name) || row.display_name || row.slug),
          h("div", { className: "ht-row" },
            ready ? h(UI.Badge, { tone: ready.ready ? "success" : "destructive" },
              ready.ready ? "ready" : "not ready") : null,
            drift === null
              ? h(UI.Badge, { tone: "outline" }, "drift ?")
              : h(UI.Badge, { tone: drift.length === 0 ? "success" : (errors ? "destructive" : "warning") },
                  drift.length === 0 ? "in sync" : drift.length + " drift"))
        ),
        ready && !ready.ready ? h("ul", { className: "ht-blocking" },
          ready.blocking.map(function (b, i) {
            return h("li", { key: i }, h("code", { className: "ht-code" }, b.check), " " + b.subject);
          })) : null,
        h("div", { className: "ht-team-facts" },
          h("div", null,
            h("span", { className: "ht-fact-label" }, "board"),
            h(HostLink, { to: "/kanban", rootRef: props.rootRef, title: "Open the Kanban tab" }, row.board)),
          h("div", null,
            h("span", { className: "ht-fact-label" }, "workdir"),
            h("code", { className: "ht-code" }, row.workdir || "—"))
        ),
        h("div", { className: "ht-chips" },
          members.length
            ? members.map(function (m) {
                return h(MemberChip, { key: m.profile, member: m,
                  team: detail.team });
              })
            : h(Muted, null, row.members + " member(s)")
        ),
        h(Counters, { counts: row.counts }),
        h("div", { className: "ht-row ht-row-end" },
          h(UI.Button, {
            size: "sm", outlined: true, destructive: true, disabled: props.readOnly,
            onClick: function () { props.onDelete(row.slug); },
          }, "Delete"),
          h(UI.Button, { size: "sm", outlined: true, onClick: function () { props.onOpen(row.slug); } },
            "Open")
        )
      )
    );
  }

  function TeamList(props) {
    const [details, setDetails] = hooks.useState({});
    const destroy = useTeamDestroyer({
      revision: props.revision,
      onDestroyed: props.onDestroyed,
      onReadOnly: props.onReadOnly,
    });

    // The list payload carries counts but not roles or drift, so enrich each
    // row from GET /teams/{slug} + GET /teams/{slug}/drift. Sequential and
    // cancellable; a failure degrades the card, never the page.
    hooks.useEffect(function () {
      const rows = props.teams || [];
      if (!rows.length) return undefined;
      let cancelled = false;
      let chain = Promise.resolve();
      rows.forEach(function (row) {
        chain = chain.then(function () {
          if (cancelled) return undefined;
          return Promise.all([
            apiGet("/teams/" + encodeURIComponent(row.slug)).then(
              function (d) { return (d && d.team) || null; }, function () { return null; }),
            apiGet("/teams/" + encodeURIComponent(row.slug) + "/drift").then(
              function (d) { return d || {}; }, function () { return null; }),
          ]).then(function (pair) {
            if (cancelled) return;
            setDetails(function (current) {
              const next = Object.assign({}, current);
              next[row.slug] = {
                team: pair[0],
                drift: pair[1] ? (pair[1].drift || []) : null,
                ready: pair[1] ? (pair[1].ready || null) : null,
              };
              return next;
            });
          });
        });
      });
      return function () { cancelled = true; };
    }, [props.teams, props.refreshKey]);

    if (!props.teams) return h(Loading, { label: "Loading teams…" });
    if (!props.teams.length) {
      return h(Empty, { title: "No teams yet." },
        "New team: name it, pick its profiles from the catalog, point it at a folder.");
    }
    const pendingSlug = destroy.destroyer.pendingId;
    const pendingRow = props.teams.filter(function (r) { return r.slug === pendingSlug; })[0] || {};
    const pendingTeam = (details[pendingSlug] || {}).team;

    return h(React.Fragment, null,
      // Outside the dialog: useConfirmDelete closes it when onDelete settles.
      destroy.error ? h(ErrorNote, { error: destroy.error, what: "This team" }) : null,
      h(BusyNote, { guard: destroy.busy }),
      h("div", { className: "ht-team-grid" }, props.teams.map(function (row) {
        return h(TeamCard, {
          key: row.slug, row: row, detail: details[row.slug],
          readOnly: props.readOnly, onOpen: props.onOpen, onDelete: destroy.request,
          rootRef: props.rootRef,
        });
      })),
      destroy.destroyer.isOpen ? h(DestroyTeamDialog, {
        state: destroy, slug: pendingSlug,
        board: (pendingTeam && pendingTeam.board) || pendingRow.board,
        members: pendingTeam ? (pendingTeam.members || []) : null,
      }) : null
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Roster rows (New team): one catalog profile each — role, model, skills
  // ═══════════════════════════════════════════════════════════════════════════

  // Host route (hermes_cli/web_routers/models.py), not a plugin one: the same
  // provider/model list the dashboard's Models page offers.
  function useModelOptions() {
    const [state, setState] = hooks.useState({ data: null, error: null });
    hooks.useEffect(function () {
      let cancelled = false;
      SDK.fetchJSON("/api/model/options").then(function (data) {
        if (!cancelled) setState({ data: data || {}, error: null });
      }, function (err) {
        if (!cancelled) setState({ data: null, error: err });
      });
      return function () { cancelled = true; };
    }, []);
    return state;
  }

  function sameModel(choice, member) {
    return choice.model === member.model && choice.provider === (member.provider || "");
  }

  function modelChoices(data, member) {
    const out = [];
    ((data && data.providers) || []).forEach(function (row) {
      const slug = String(row.slug || "");
      (row.models || []).forEach(function (m) {
        out.push({ provider: slug, model: String(m), label: (row.name || slug) + " · " + m });
      });
    });
    // A saved pick stays selectable even when its provider is no longer listed.
    if (member.model && !out.some(function (c) { return sameModel(c, member); })) {
      out.unshift({
        provider: member.provider || "", model: member.model,
        label: (member.provider ? member.provider + " · " : "") + member.model + " (saved)",
      });
    }
    return out;
  }

  function ModelField(props) {
    const member = props.member;
    const id = "ht-tm-model-" + props.rid;
    if (props.options.error) {
      return h("div", { className: "ht-grid-2" },
        h(TextField, {
          id: id, label: "Model", value: member.model || "",
          onChange: function (v) { props.onChange({ model: v }); },
          hint: "Model list unavailable (" + errText(props.options.error) +
            "). Blank → the Hermes default model.",
        }),
        h(TextField, {
          id: "ht-tm-provider-" + props.rid, label: "Provider", value: member.provider || "",
          onChange: function (v) { props.onChange({ provider: v }); },
        })
      );
    }
    const data = props.options.data;
    const choices = modelChoices(data, member);
    const at = choices.findIndex(function (c) { return sameModel(c, member); });
    const fallback = data && data.model
      ? data.model + (data.provider ? " (" + data.provider + ")" : "")
      : "Hermes default";
    return h(Field, {
      label: "Model", htmlFor: id,
      hint: "Default → the Hermes model when the team is created.",
    },
      h(UI.Select, {
        id: id, value: at < 0 ? "" : String(at), disabled: !data,
        onValueChange: function (v) {
          const c = v === "" ? null : choices[Number(v)];
          props.onChange(c ? { model: c.model, provider: c.provider } : { model: "", provider: "" });
        },
      },
        h(UI.SelectOption, { value: "" }, data ? "Default — " + fallback : "Loading models…"),
        choices.map(function (c, i) {
          return h(UI.SelectOption, { key: i, value: String(i) }, c.label);
        })
      )
    );
  }

  function RosterMemberRow(props) {
    const member = props.member;
    const [skillRows, setSkillRows] = hooks.useState(null);
    const [skillError, setSkillError] = hooks.useState(null);
    const [openSkills, setOpenSkills] = hooks.useState(false);

    // A roster row is a blueprint, not a profile yet, so the skill universe
    // comes from an existing installation of that blueprint when there is one
    // (entry.installed_as). Otherwise skills stay free-form text.
    const entry = (props.entries || []).filter(function (e) { return e.id === member.blueprint; })[0];
    const source = entry && (entry.installed_as || [])[0];

    hooks.useEffect(function () {
      if (!openSkills || !source) return undefined;
      let cancelled = false;
      setSkillRows(null);
      setSkillError(null);
      apiGet("/skills?profile=" + encodeURIComponent(source)).then(function (data) {
        if (cancelled) return;
        setSkillRows((data && data.skills) || []);
      }, function (err) {
        if (cancelled) return;
        setSkillRows([]);
        setSkillError(describeError(err, "The skill list"));
      });
      return function () { cancelled = true; };
    }, [openSkills, source]);

    function toggleSkill(name) {
      // Read-modify-write against the row the reducer holds, not this render's
      // prop, so two quick toggles cannot drop one another.
      props.onChange(function (row) {
        const current = row.skills || [];
        const at = current.indexOf(name);
        const next = current.slice();
        if (at < 0) next.push(name); else next.splice(at, 1);
        return { skills: next };
      });
    }

    return h("div", { className: cn("ht-tm-row", member.main && "ht-tm-main") },
      h("div", { className: "ht-tm-head" },
        h("code", { className: "ht-code" }, member.blueprint),
        props.profileName ? h("span", { className: "ht-hint" }, "→ " + props.profileName) : null,
        h("label", { className: "ht-main-radio" },
          h("input", {
            type: "radio",
            name: "ht-roster-main",
            checked: !!member.main,
            onChange: function () { props.onMain(); },
          }),
          h("span", null, "orchestrator")
        ),
        h(UI.Button, {
          size: "sm", outlined: true, destructive: true, onClick: props.onRemove,
        }, "Remove")
      ),
      h("div", { className: "ht-grid-2" },
        h(TextField, {
          id: "ht-tm-role-" + props.rid, label: "Role", value: member.role || "",
          onChange: function (v) { props.onChange({ role: v }); },
        }),
        h(ModelField, {
          rid: props.rid, member: member, options: props.modelOptions,
          onChange: props.onChange,
        })
      ),
      h("div", { className: "ht-tm-skills" },
        h("div", { className: "ht-row" },
          h("span", { className: "ht-fact-label" }, "skills"),
          h("span", { className: "ht-hint" }, (member.skills || []).join(", ") || "none"),
          source
            ? h(UI.Button, {
                size: "sm", outlined: true,
                onClick: function () { setOpenSkills(!openSkills); },
              }, openSkills ? "Done" : "Choose")
            : null
        ),
        !source ? h(TextField, {
          id: "ht-tm-skills-" + props.rid,
          label: "Skills (comma separated)",
          value: (member.skills || []).join(", "),
          onChange: function (v) {
            props.onChange({
              skills: v.split(",").map(function (s) { return s.trim(); })
                .filter(function (s) { return s.length; }),
            });
          },
          hint: "No installed instance of this blueprint to read a skill list from.",
        }) : null,
        source && openSkills ? h(SkillPicker, {
          skills: skillRows, selected: member.skills || [], error: skillError, onToggle: toggleSkill,
        }) : null
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Profiles — the plugin's own catalog store: view, author,
  // import, hide, remove. The member pickers read plain GET /catalog, which
  // leaves hidden entries out; this screen asks for them explicitly.
  // ═══════════════════════════════════════════════════════════════════════════

  // catalog_store._SKILL_NAME_RE
  const SKILL_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

  function entriesWord(n) { return n + " entr" + (n === 1 ? "y" : "ies"); }

  function profileBadges(entry) {
    return h(React.Fragment, null,
      entry.local ? h(UI.Badge, { tone: "secondary" }, "plugin store") : null,
      entry.hidden ? h(UI.Badge, { tone: "warning" }, "hidden") : null);
  }

  function useCatalogAdmin(refreshKey) {
    const [entries, setEntries] = hooks.useState(null);
    const [revision, setRevision] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);
    hooks.useEffect(function () {
      let cancelled = false;
      apiGet("/catalog?include_hidden=true").then(function (data) {
        if (cancelled) return;
        setEntries((data && data.entries) || []);
        // CAS for every store write compares against catalog.yaml's OWN
        // revision (_catalog_gate), never the registry's.
        setRevision(data && typeof data.revision === "number" ? data.revision : null);
        setError(null);
      }, function (err) {
        if (!cancelled) setError(err);
      });
      return function () { cancelled = true; };
    }, [refreshKey]);
    return { entries: entries, revision: revision, error: error };
  }

  function ProfileDetail(props) {
    const entry = props.entry;
    const [detail, setDetail] = hooks.useState(null);
    const [error, setError] = hooks.useState(null);

    hooks.useEffect(function () {
      let cancelled = false;
      setDetail(null);
      setError(null);
      apiGet("/catalog/" + encodeURIComponent(entry.id)).then(function (data) {
        if (!cancelled) setDetail(data || {});
      }, function (err) {
        if (!cancelled) setError(err);
      });
      return function () { cancelled = true; };
    }, [entry.id, props.refreshKey]);

    const refs = (detail && detail.referenced_by) || { teams: [], recipes: [] };
    const skills = (detail && detail.skills) || [];

    return h(UI.Card, { className: "ht-profile-detail" },
      h(UI.CardContent, null,
        h("div", { className: "ht-team-card-head" },
          h("strong", null, entry.display_name || entry.id),
          h("div", { className: "ht-row" },
            h(UI.Badge, { tone: entry.local ? "secondary" : "outline" },
              entry.local ? "plugin store" : "external pack"),
            entry.hidden ? h(UI.Badge, { tone: "warning" }, "hidden") : null)
        ),
        h("code", { className: "ht-code" }, entry.id),
        h("p", { className: "ht-muted" }, entry.description || ""),
        h("dl", { className: "ht-kv" },
          h("dt", null, "pack"), h("dd", null, entry.pack || "—"),
          h("dt", null, "category"), h("dd", null, entry.category || "—"),
          h("dt", null, "suggested role"), h("dd", null, entry.suggested_role || "—"),
          h("dt", null, "version"), h("dd", null, entry.version || "—"),
          h("dt", null, "installed as"), h("dd", null, (entry.installed_as || []).join(", ") || "—"),
          h("dt", null, "team members"), h("dd", null, refs.teams.join(", ") || "—"),
          h("dt", null, "recipes"), h("dd", null, (refs.recipes || []).join(", ") || "—")
        ),
        error ? h(ErrorNote, { error: error, what: "The catalog" }) : null,
        detail === null && !error ? h(Loading, { label: "Loading SOUL.md…" }) : null,
        skills.length ? h(Field, { label: "Skills" },
          h("div", { className: "ht-chips" }, skills.map(function (skill) {
            return h("span", { key: skill.name, className: "ht-chip", title: skill.description || skill.name },
              h("span", { className: "ht-chip-dot" }), skill.name);
          }))) : null,
        detail && detail.soul
          ? h(Field, { label: "SOUL.md" }, h("pre", { className: "ht-soul" }, detail.soul))
          : (detail ? h("p", { className: "ht-hint" }, "No SOUL.md — instances run with the default persona.") : null),
        h("div", { className: "ht-row ht-row-end" },
          entry.local ? h(UI.Button, {
            size: "sm", outlined: true,
            disabled: props.readOnly || props.busy || !detail,
            onClick: function () { props.onEdit(entry, detail); },
          }, "Edit") : null,
          h(UI.Button, {
            size: "sm", outlined: true, disabled: props.readOnly || props.busy,
            onClick: function () { props.onToggleHidden(entry); },
          }, entry.hidden ? "Unhide" : "Hide"),
          entry.local ? h(UI.Button, {
            size: "sm", outlined: true, destructive: true,
            disabled: props.readOnly || props.busy,
            onClick: function () { props.onRemove(entry, refs); },
          }, "Remove") : null
        )
      )
    );
  }

  function ProfileDialog(props) {
    // Edit mode when props.entry is set; props.detail is its GET /catalog/{id}.
    const editing = !!props.entry;
    const detail = props.detail || {};
    const soulLocked = editing && !!detail.soul_truncated;
    const [initial] = hooks.useState(function () {
      const e = props.entry || {};
      return {
        id: e.id || "", display_name: e.display_name || "", category: e.category || "",
        description: e.description || "", soul: detail.soul || "",
      };
    });
    const [form, setForm] = hooks.useState(initial);
    // Rows carry a minted _rid (same reason as the New team roster): keying on the
    // index would remount every row after a removed one.
    const rowSeq = hooks.useRef(0);
    const [skills, setSkills] = hooks.useState(function () {
      return (detail.skills || []).map(function (s) {
        rowSeq.current += 1;
        return {
          _rid: "s" + rowSeq.current, name: s.dir || s.name, existing: true, removed: false,
          description: s.description || "", body: s.body || "",
          origDescription: s.description || "", origBody: s.body || "",
          locked: !!s.body_truncated, files: s.files || 0,
        };
      });
    });
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);

    function patch(next) { setForm(function (f) { return Object.assign({}, f, next); }); }
    function addSkill() {
      rowSeq.current += 1;
      const rid = "s" + rowSeq.current;
      setSkills(function (rows) { return rows.concat([{ _rid: rid, name: "", description: "", body: "" }]); });
    }
    function patchSkill(rid, next) {
      setSkills(function (rows) {
        return rows.map(function (row) { return row._rid === rid ? Object.assign({}, row, next) : row; });
      });
    }
    function dropSkill(rid) {
      // A stored skill is only marked, so the removal can be undone before saving.
      setSkills(function (rows) {
        return rows
          .filter(function (row) { return row._rid !== rid || row.existing; })
          .map(function (row) { return row._rid === rid ? Object.assign({}, row, { removed: !row.removed }) : row; });
      });
    }

    const stored = skills.filter(function (s) { return s.existing; }).map(function (s) { return s.name; });
    function skillHint(s) {
      if (s.existing) return null;
      if (!SKILL_NAME_RE.test(s.name)) return "Must match " + String(SKILL_NAME_RE);
      if (stored.indexOf(s.name) >= 0) return "This profile already has that skill — edit its row instead.";
      return null;
    }
    function skillPayload(s) { return { name: s.name, description: s.description, body: s.body }; }

    // Only what changed goes out: patch_catalog_entry() leaves absent keys alone,
    // and rewriting an untouched SKILL.md would reformat its frontmatter.
    function editBody() {
      const body = {};
      ["display_name", "category", "description"].forEach(function (k) {
        if (form[k] !== initial[k]) body[k] = form[k];
      });
      if (!soulLocked && form.soul !== initial.soul) body.soul = form.soul;
      const upserts = skills.filter(function (s) {
        if (!s.existing) return true;
        return !s.removed && !s.locked && (s.description !== s.origDescription || s.body !== s.origBody);
      });
      if (upserts.length) body.skills = upserts.map(skillPayload);
      const removed = skills.filter(function (s) { return s.existing && s.removed; })
        .map(function (s) { return s.name; });
      if (removed.length) body.remove_skills = removed;
      return body;
    }

    const changes = editing ? editBody() : null;
    const idValid = editing || PROFILE_NAME_RE.test(form.id);
    const skillsValid = skills.every(function (s) { return !skillHint(s); });
    const dirty = !editing || Object.keys(changes).length > 0;

    function save() {
      setBusy(true);
      setError(null);
      // Keys are exactly the ones post_catalog_entry() / patch_catalog_entry() read.
      const body = editing ? Object.assign({}, changes) : {
        id: form.id, display_name: form.display_name, category: form.category,
        description: form.description, soul: form.soul,
        skills: skills.map(skillPayload),
      };
      if (props.revision !== null && props.revision !== undefined) body.expected_revision = props.revision;
      const request = editing
        ? apiSend("PATCH", "/catalog/" + encodeURIComponent(props.entry.id), body)
        : apiSend("POST", "/catalog", body);
      request.then(function (data) {
        setBusy(false);
        props.onSaved(data || null);
      }, function (err) {
        setBusy(false);
        setError(err);
        if (isReadOnly(err)) props.onReadOnly();
      });
    }

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) props.onClose(); },
    },
      h(UI.DialogContent, { className: "ht-dialog ht-dialog-wide" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, editing ? "Edit profile" : "New profile"),
          h(UI.DialogDescription, null, editing
            ? "Changes apply to this blueprint only. Profiles already instantiated " +
              "from it keep their own SOUL.md and skills."
            : "Author a blueprint in the plugin's catalog store. It shows up in every " +
              "member picker; a team instantiates a real profile from it.")
        ),
        h("div", { className: "ht-dialog-body ht-form" },
          h("div", { className: "ht-grid-2" },
            h(TextField, {
              id: "ht-np-id", label: "Id", value: form.id, disabled: editing,
              onChange: function (v) { patch({ id: v.trim().toLowerCase() }); },
              hint: editing ? "Ids cannot change — to rename, add a new profile and remove this one."
                : idValid || !form.id ? "The blueprint id, e.g. my-reviewer."
                : "Must match " + String(PROFILE_NAME_RE),
            }),
            h(TextField, {
              id: "ht-np-name", label: "Display name", value: form.display_name,
              onChange: function (v) { patch({ display_name: v }); },
            }),
            h(TextField, {
              id: "ht-np-category", label: "Category", value: form.category,
              onChange: function (v) { patch({ category: v }); },
              hint: "Feeds the suggested role (qa → verifier, engineering → implementer).",
            }),
            h(TextField, {
              id: "ht-np-desc", label: "Description", value: form.description,
              onChange: function (v) { patch({ description: v }); },
              hint: "One line — the kanban decomposer routes on it.",
            })
          ),
          h(Field, {
            label: "SOUL.md", htmlFor: "ht-np-soul",
            hint: soulLocked
              ? "Too large to edit here — use hermes team profiles edit " + form.id + " --soul-file FILE."
              : "Role, responsibilities, working standard, boundaries, completion criteria.",
          },
            h("textarea", {
              id: "ht-np-soul", className: "ht-textarea", rows: 12, value: form.soul,
              readOnly: soulLocked,
              onChange: function (e) { patch({ soul: e.target.value }); },
            })
          ),
          h("div", { className: "ht-panel-head" },
            h("strong", null, "Skills"),
            h(UI.Button, { size: "sm", outlined: true, onClick: addSkill }, "Add skill")
          ),
          skills.map(function (s) {
            const frozen = s.existing && (s.locked || s.removed);
            return h("div", { key: s._rid, className: "ht-tm-row" },
              h("div", { className: "ht-grid-2" },
                h(TextField, {
                  id: "ht-np-sk-name-" + s._rid, label: "Name", value: s.name, disabled: s.existing,
                  onChange: function (v) { patchSkill(s._rid, { name: v.trim().toLowerCase() }); },
                  hint: skillHint(s),
                }),
                h(TextField, {
                  id: "ht-np-sk-desc-" + s._rid, label: "Description", value: s.description,
                  disabled: frozen,
                  onChange: function (v) { patchSkill(s._rid, { description: v }); },
                })
              ),
              h(Field, {
                label: "SKILL.md body", htmlFor: "ht-np-sk-body-" + s._rid,
                hint: s.removed ? "Removed when you save."
                  : s.locked ? "Too large to edit here — use hermes team profiles edit " + form.id + " --skill-dir DIR."
                  : s.files > 1 ? "Other files in this skill are kept." : null,
              },
                h("textarea", {
                  id: "ht-np-sk-body-" + s._rid, className: "ht-textarea", rows: 5, value: s.body,
                  readOnly: frozen,
                  onChange: function (e) { patchSkill(s._rid, { body: e.target.value }); },
                })
              ),
              h("div", { className: "ht-row ht-row-end" },
                h(UI.Button, {
                  size: "sm", outlined: true, destructive: !s.removed,
                  onClick: function () { dropSkill(s._rid); },
                }, s.removed ? "Keep skill" : "Remove skill"))
            );
          }),
          error ? h(ErrorNote, { error: error, what: "The catalog" }) : null
        ),
        h(UI.DialogFooter, null,
          h(UI.Button, { outlined: true, onClick: props.onClose, disabled: busy }, "Cancel"),
          h(UI.Button, {
            disabled: busy || !idValid || !skillsValid || !dirty,
            onClick: save,
          }, busy ? "Saving…" : "Save profile")
        )
      )
    );
  }

  function ImportPlan(props) {
    const plan = props.plan;
    return h("div", { className: "ht-plan" },
      h("div", { className: "ht-plan-head" },
        h("strong", null, "Dry run — " + entriesWord(plan.count) + " from " + plan.source),
        h(UI.Badge, { tone: "secondary" }, plan.kind)
      ),
      plan.recipes && plan.recipes.length
        ? h("p", { className: "ht-hint" }, "Recipes that would merge: " + plan.recipes.join(", "))
        : null,
      h("div", { className: "ht-table-wrap" },
        h("table", { className: "ht-table" },
          h("thead", null, h("tr", null,
            h("th", null, "id"), h("th", null, "pack"), h("th", null, "category"),
            h("th", null, "files"), h("th", null, "status"))),
          h("tbody", null, plan.entries.map(function (e) {
            const blocked = e.shadows_external || (e.exists && !props.replace);
            const status = e.shadows_external ? "refused — an external pack root has this id"
              : e.exists ? (props.replace ? "will replace" : "exists — tick replace")
              : "new";
            return h("tr", { key: e.id },
              h("td", null, h("code", { className: "ht-code" }, e.id)),
              h("td", null, e.pack),
              h("td", null, e.category || "—"),
              h("td", null, String(e.file_count)),
              h("td", { className: blocked ? "ht-bad" : null },
                status + ((e.warnings || []).length ? " · no SOUL.md" : "")));
          }))
        )
      )
    );
  }

  function ImportProfilesDialog(props) {
    const [source, setSource] = hooks.useState("");
    const [only, setOnly] = hooks.useState("");
    const [name, setName] = hooks.useState("");
    const [replace, setReplace] = hooks.useState(false);
    const [plan, setPlan] = hooks.useState(null);
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);
    const jobs = useJobPoll(null);

    // Any edit invalidates the dry run — Import stays disabled until a fresh one.
    function edit(setter) { return function (v) { setter(v); setPlan(null); }; }

    function body() {
      // Keys are exactly the ones post_catalog_import() reads.
      const out = { source: source.trim(), replace: replace };
      const ids = only.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      if (ids.length) out.only = ids;
      if (name.trim()) out.name = name.trim();
      if (props.revision !== null && props.revision !== undefined) out.expected_revision = props.revision;
      return out;
    }

    function fail(err) {
      setBusy(false);
      setError(err);
      if (isReadOnly(err)) props.onReadOnly();
    }

    function preview() {
      setBusy(true);
      setError(null);
      apiSend("POST", "/catalog/import?dry_run=true", body()).then(function (data) {
        setBusy(false);
        setPlan((data && data.plan) || null);
      }, fail);
    }

    function run() {
      setBusy(true);
      setError(null);
      apiSend("POST", "/catalog/import", body()).then(function (data) {
        setBusy(false);
        if (data && data.job_id) jobs.watch(data.job_id);
      }, fail);
    }

    const blocked = plan ? plan.entries.filter(function (e) {
      return e.shadows_external || (e.exists && !replace);
    }) : [];
    const finished = jobs.job && (jobs.job.state === "done" || jobs.job.state === "error");
    const result = jobs.job && jobs.job.state === "done" ? (jobs.job.result || {}) : null;

    return h(UI.Dialog, {
      open: true,
      onOpenChange: function (open) { if (!open) props.onClose(); },
    },
      h(UI.DialogContent, { className: "ht-dialog ht-dialog-wide" },
        h(UI.DialogHeader, null,
          h(UI.DialogTitle, null, "Import profiles"),
          h(UI.DialogDescription, null,
            "From a git URL (github.com/user/repo, https://…, git@…) or a directory on " +
            "the agent host. A pack repo (packs.json) imports every profile, or only the " +
            "ids you list. Dry run first — nothing is written until you import.")
        ),
        h("div", { className: "ht-dialog-body" },
          jobs.job ? h("div", null,
            h(JobProgress, { job: jobs.job }),
            result ? h(Note, { tone: "ok", title: "Imported " + entriesWord(result.count || 0) },
              ((result.recipes || []).length ? "Recipes merged: " + result.recipes.join(", ") + ". " : "") +
              ((result.warnings || []).length
                ? result.warnings.length + " warning(s): " + result.warnings.slice(0, 3).join("; ")
                : "")) : null
          ) : h("div", { className: "ht-form" },
            h(TextField, {
              id: "ht-import-source", label: "Source", value: source,
              placeholder: "https://github.com/Dadmin88/hermes-profile-packs",
              onChange: edit(setSource),
            }),
            h("div", { className: "ht-grid-2" },
              h(TextField, {
                id: "ht-import-only", label: "Only these ids", value: only,
                onChange: edit(setOnly),
                hint: "Pack repos: comma-separated ids. Blank imports every profile.",
              }),
              h(TextField, {
                id: "ht-import-name", label: "Import as", value: name,
                onChange: edit(setName),
                hint: "A single distribution only: store it under this id.",
              })
            ),
            h(CheckField, {
              label: "replace entries already in the store",
              checked: replace,
              onChange: setReplace,
            }),
            plan ? h(ImportPlan, { plan: plan, replace: replace }) : null,
            error ? h(ErrorNote, { error: error, what: "The catalog" }) : null,
            h(JobErrorNote, { jobs: jobs })
          )
        ),
        h(UI.DialogFooter, null,
          finished
            ? h(UI.Button, { onClick: props.onClose }, "Close")
            : h(React.Fragment, null,
                h(UI.Button, { outlined: true, onClick: props.onClose, disabled: busy }, "Cancel"),
                h(UI.Button, {
                  outlined: true,
                  disabled: !source.trim() || busy || jobs.running,
                  onClick: preview,
                }, busy && !plan ? "Staging…" : "Dry run"),
                h(UI.Button, {
                  disabled: !plan || !plan.count || blocked.length > 0 || busy || jobs.running,
                  onClick: run,
                }, jobs.running ? "Importing…" : "Import")
              )
        )
      )
    );
  }

  function ProfilesScreen(props) {
    const [refreshKey, setRefreshKey] = hooks.useState(0);
    const catalog = useCatalogAdmin(refreshKey);
    const [facets, setFacets] = hooks.useState({ q: "", pack: "", category: "" });
    const [showHidden, setShowHidden] = hooks.useState(false);
    const [selected, setSelected] = hooks.useState(null);    // entry id
    const [dialog, setDialog] = hooks.useState(null);        // null | "new" | "edit" | "import"
    const [editing, setEditing] = hooks.useState(null);      // {entry, detail} for "edit"
    const [busy, setBusy] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);

    const revisionRef = hooks.useRef(null);
    revisionRef.current = catalog.revision;
    const removeRecipes = hooks.useRef([]);

    function reload() { setRefreshKey(function (n) { return n + 1; }); }

    // The Profiles pill counts what the pickers offer, i.e. without hidden entries.
    hooks.useEffect(function () {
      if (catalog.entries && props.onCount) {
        props.onCount(catalog.entries.filter(function (e) { return !e.hidden; }).length);
      }
    }, [catalog.entries]);

    function refused(err) {
      setError(err);
      if (isReadOnly(err)) props.onReadOnly();
    }

    const deleter = useConfirmDelete({
      onDelete: function (id) {
        // g5: DELETE /catalog/{id} refuses without {"confirm": "<id>"}.
        setError(null);
        return apiSend("DELETE", "/catalog/" + encodeURIComponent(id), {
          confirm: id,
          expected_revision: revisionRef.current,
        }).then(function (data) {
          setSelected(null);
          reload();
          const refs = (data && data.team_refs) || [];
          props.onToast("Removed " + id + (refs.length ? " (still named by " + refs.join(", ") + ")" : "") + ".");
        }, refused);
      },
    });

    function toggleHidden(entry) {
      setBusy(true);
      setError(null);
      apiSend("POST", "/catalog/" + encodeURIComponent(entry.id) + "/visibility", {
        hidden: !entry.hidden,
        expected_revision: revisionRef.current,
      }).then(function () {
        setBusy(false);
        reload();
      }, function (err) {
        setBusy(false);
        refused(err);
      });
    }

    const all = catalog.entries;
    const visible = all === null ? null
      : all.filter(function (e) { return showHidden || !e.hidden || e.id === selected; });
    const selectedEntry = (all || []).filter(function (e) { return e.id === selected; })[0] || null;
    const count = function (pred) { return (all || []).filter(pred).length; };

    return h("div", { className: "ht-screen" },
      h("div", { className: "ht-panel-head" },
        h("h3", null, "Profiles"),
        h("div", { className: "ht-row" },
          h(UI.Button, {
            size: "sm", outlined: true, disabled: props.readOnly,
            onClick: function () { setDialog("import"); },
          }, "Import"),
          h(UI.Button, {
            size: "sm", disabled: props.readOnly,
            onClick: function () { setDialog("new"); },
          }, "New profile")
        )
      ),
      h("p", { className: "ht-hint" },
        "The blueprints every member picker offers. Entries in the plugin's store can be " +
        "edited or removed; entries from an external pack root are read-only but can be hidden. " +
        "Hidden entries stay resolvable for teams that already use them."),
      all ? h("p", { className: "ht-muted" },
        entriesWord(all.length) + " · " + count(function (e) { return e.local; }) +
        " in the plugin store · " + count(function (e) { return e.hidden; }) + " hidden") : null,
      error ? h(ErrorNote, { error: error, what: "The catalog" }) : null,
      catalog.error ? h(ErrorNote, { error: catalog.error, what: "The catalog", onRetry: reload }) : null,
      all !== null && all.length === 0
        ? h(Empty, { title: "The catalog is empty." },
            "Import a pack repo (e.g. https://github.com/Dadmin88/hermes-profile-packs) or author a profile.")
        : h("div", { className: "ht-profiles-layout" },
            h("div", { className: "ht-profiles-list" },
              h("div", { className: "ht-row ht-profiles-tools" },
                h(CatalogFacets, {
                  entries: all,
                  facets: facets,
                  onChange: function (next) { setFacets(Object.assign({}, facets, next)); },
                }),
                h(CheckField, { label: "show hidden", checked: showHidden, onChange: setShowHidden })
              ),
              h(CatalogList, {
                entries: visible,
                error: null,
                facets: facets,
                selectedId: selected,
                onSelect: function (entry) { setSelected(entry.id); },
                rowExtra: profileBadges,
              })
            ),
            selectedEntry ? h(ProfileDetail, {
              entry: selectedEntry,
              refreshKey: refreshKey,
              readOnly: props.readOnly,
              busy: busy,
              onToggleHidden: toggleHidden,
              onEdit: function (entry, detail) {
                setEditing({ entry: entry, detail: detail });
                setDialog("edit");
              },
              onRemove: function (entry, refs) {
                removeRecipes.current = refs.recipes || [];
                deleter.requestDelete(entry.id);
              },
            }) : null
          ),
      dialog === "new" || dialog === "edit" ? h(ProfileDialog, {
        entry: dialog === "edit" ? editing.entry : null,
        detail: dialog === "edit" ? editing.detail : null,
        revision: catalog.revision,
        onReadOnly: props.onReadOnly,
        onClose: function () { setDialog(null); },
        onSaved: function (result) {
          const id = (result && result.id) || "the profile";
          props.onToast(dialog === "edit" ? "Saved " + id + "." : "Added " + id + " to the catalog.");
          setDialog(null);
          reload();
          if (result && result.id) setSelected(result.id);
        },
      }) : null,
      dialog === "import" ? h(ImportProfilesDialog, {
        revision: catalog.revision,
        onReadOnly: props.onReadOnly,
        onClose: function () { setDialog(null); reload(); },
      }) : null,
      deleter.isOpen ? h(UI.ConfirmDialog, {
        open: true,
        destructive: true,
        title: "Remove " + deleter.pendingId + " from the catalog?",
        description: "Its stored copy is deleted" +
          (removeRecipes.current.length
            ? ", and so are the recipes that use it: " + removeRecipes.current.join(", ") : "") +
          ". Profiles already instantiated from it keep working.",
        confirmLabel: "Remove",
        loading: deleter.isDeleting,
        onCancel: deleter.cancel,
        onConfirm: deleter.confirm,
      }) : null
    );
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Root
  // ═══════════════════════════════════════════════════════════════════════════

  function TeamsPage() {
    const i18n = useI18n();
    const t = (i18n && i18n.t) || {};
    const rootRef = hooks.useRef(null);
    const toaster = useToast();

    const [screen, setScreen] = hooks.useState({ name: "teams", slug: null });
    const [teams, setTeams] = hooks.useState(null);
    const [revision, setRevision] = hooks.useState(null);
    const [teamsError, setTeamsError] = hooks.useState(null);
    const [defaultNaming, setDefaultNaming] = hooks.useState("");
    const [refreshKey, setRefreshKey] = hooks.useState(0);
    const [readOnly, setReadOnly] = hooks.useState(false);
    const [newTeam, setNewTeam] = hooks.useState(false);
    const [profileCount, setProfileCount] = hooks.useState(null);

    function reloadAll() { setRefreshKey(function (n) { return n + 1; }); }

    // Badge + naming default only — a failed read leaves them blank; the
    // Profiles screen reports its own errors.
    hooks.useEffect(function () {
      let cancelled = false;
      apiGet("/catalog").then(function (data) {
        if (cancelled) return;
        setProfileCount(data && typeof data.count === "number" ? data.count : null);
        setDefaultNaming((data && data.default_profile_naming) || "");
      }, function () {});
      return function () { cancelled = true; };
    }, [refreshKey]);

    hooks.useEffect(function () {
      let cancelled = false;
      apiGet("/teams").then(function (data) {
        if (cancelled) return;
        setTeams((data && data.teams) || []);
        // CAS gate compares against the REGISTRY revision (_cas_gate reads
        // storage.load().revision), not a team's own — so this is the value
        // every expected_revision below must carry.
        setRevision(data && typeof data.revision === "number" ? data.revision : null);
        setTeamsError(null);
      }, function (err) {
        if (cancelled) return;
        setTeamsError(err);
      });
      return function () { cancelled = true; };
    }, [refreshKey]);

    // Read-only capability probe (§11 / g7). plugin_api._mutation_guard
    // answers 405 DASHBOARD_READONLY on every non-GET except the dry-run sync
    // when dashboard_writes is false, and there is NO GET route that reports
    // the flag. So ask a write route a question that can never write:
    // POST /teams?dry_run=true with an empty body. Write-enabled → 400
    // (E_TEAM_INVALID "body must carry the team slug", checked after the
    // guard and before any work). Read-only → 405. Nothing is created either
    // way, and any other status simply leaves the banner off.
    hooks.useEffect(function () {
      let cancelled = false;
      apiSend("POST", "/teams?dry_run=true", {}).then(function () {
        if (!cancelled) setReadOnly(false);
      }, function (err) {
        if (cancelled) return;
        if (isReadOnly(err)) setReadOnly(true);
        else if (errStatus(err) === 400) setReadOnly(false);
      });
      return function () { cancelled = true; };
    }, []);

    function onReadOnly() {
      setReadOnly(true);
      toaster.showToast("This dashboard is read-only (dashboard_writes: false).", "error");
    }

    function openTeam(slug) { setScreen({ name: "detail", slug: slug }); }

    function onTeamDestroyed(data, slug) {
      toaster.showToast(
        "Destroyed " + ((data && data.destroyed) || slug) +
        ((data && data.skipped_purge && data.skipped_purge.length)
          ? " (kept adopted: " + data.skipped_purge.join(", ") + ")" : "") +
        (busyOutcome(data) ? ". " + busyOutcome(data).trim() : ""),
        "success");
      setScreen({ name: "teams", slug: null });
      reloadAll();
    }

    return h("div", { className: "ht-page", ref: rootRef },
      h(UI.Toast, { toast: toaster.toast }),
      h("section", { className: "ht-hero" },
        h("div", null,
          h("div", { className: "ht-kicker" }, tx(t, "hero.kicker", "Managed agent teams")),
          h("h1", null, tx(t, "hero.title", "Synergia")),
          h("p", null, tx(t, "hero.subtitle",
            "Rosters, boards and folders in one record. Everything here runs the " +
            "same code the hermes team CLI runs — no second implementation."))
        ),
        h("div", { className: "ht-row" },
          h(UI.Button, { outlined: true, onClick: reloadAll }, "Reload"),
          h(UI.Button, {
            disabled: readOnly,
            onClick: function () { setNewTeam(true); },
          }, "New team")
        )
      ),
      readOnly ? h(Note, { tone: "warn", title: "Read-only dashboard" }, READONLY_MESSAGE) : null,
      h(PillBar, {
        value: screen.name === "detail" ? "teams" : screen.name,
        onChange: function (value) { setScreen({ name: value, slug: null }); },
        items: [
          { value: "teams", label: "Teams", badge: teams ? teams.length : null },
          { value: "profiles", label: "Profiles", badge: profileCount },
        ],
      }),
      screen.name === "detail" ? h("div", { className: "ht-breadcrumb" },
        h("button", {
          type: "button", className: "ht-crumb",
          onClick: function () { setScreen({ name: "teams", slug: null }); },
        }, "← all teams"),
        h("code", { className: "ht-code" }, screen.slug)
      ) : null,
      teamsError && (screen.name === "teams" || screen.name === "detail")
        ? h(ErrorNote, { error: teamsError, what: "The registry", onRetry: reloadAll }) : null,
      screen.name === "teams" ? h(TeamList, {
        teams: teams, refreshKey: refreshKey, rootRef: rootRef, onOpen: openTeam,
        revision: revision, readOnly: readOnly, onReadOnly: onReadOnly,
        onDestroyed: onTeamDestroyed,
      }) : null,
      screen.name === "profiles" ? h(ProfilesScreen, {
        readOnly: readOnly,
        onReadOnly: onReadOnly,
        onCount: setProfileCount,
        onToast: function (message) { toaster.showToast(message, "success"); },
      }) : null,
      screen.name === "detail" ? h(TeamDetail, {
        slug: screen.slug,
        revision: revision,
        readOnly: readOnly,
        rootRef: rootRef,
        onReadOnly: onReadOnly,
        onRegistryChanged: reloadAll,
        onDestroyed: onTeamDestroyed,
      }) : null,
      newTeam ? h(NewTeamDialog, {
        teams: teams || [],
        defaultNaming: defaultNaming,
        revision: revision,
        onReadOnly: onReadOnly,
        onClose: function () { setNewTeam(false); reloadAll(); },
        onCreated: function (result) {
          toaster.showToast(
            "Team " + (((result || {}).team || {}).slug || "") + " created.", "success");
          reloadAll();
        },
      }) : null
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-agent-synergia", TeamsPage);
  // Older hosts have no slot registry — the tab still works without the badge.
  if (typeof window.__HERMES_PLUGINS__.registerSlot === "function") {
    window.__HERMES_PLUGINS__.registerSlot("hermes-agent-synergia", "header-right", ApprovalsBadge);
  }
})();
