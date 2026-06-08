# LiveLLM Cloud — browser-agent architecture

Status: **design / in progress.** The `Browser` and `Controller` resources exist
and ship today. This document is the canonical design for the third resource —
**`BrowserAgent`** — the AI layer that drives a browser to complete a *task* as a
reviewable **trajectory** of steps. It spans five repos; this file is the source
of truth for the contract between them. Mirrors the style of
`cluster/docs/cell-architecture.md`.

## The problem this solves

We have a remote browser (`Browser`: patchright Chromium + NoVNC + CDP on :9222)
and a deterministic page-automation service (`Controller`: search/content/
interact/attribute parsing, plus a CDP registry over many browsers). What's
missing is the thing the product is named for: an **agent** that takes a
natural-language task, plans it, executes it in the browser, and lets a human
review and steer every step.

Hard requirements (from the product owner):

1. Before executing, the agent **splits the task into steps** and forms a
   **trajectory** (the plan, as data).
2. On each step the user reports **done / not-done / needs-reformation**, and may
   **manually rewrite** a step (before or after it runs).
3. After the run the user can **restart from a specific step**, and gets a saved
   **video** of the run plus the **trajectory JSON**.
4. **Webhooks** on step events, coupled to the tenant's existing alert
   **channels** (the user picks the channel).
5. Uses the **tenant's own AI integration** as the model — zero extra setup.

No off-the-shelf engine ships per-step human review, plan-before-execute, or
restart-from-step (verified survey: browser-use, Skyvern, Steel, Stagehand,
computer-use — none do). So the trajectory/checkpoint/review layer is **ours to
build**; the engine is just the body. We build it on **browser-use (MIT)** because
it already rides the **same patchright stack** the `Browser` image launches, so it
adds zero new stealth risk.

## What already exists (recap)

- **`Browser` CR** (`livellm.io/v1alpha1`, `livellm-browser-operator`): one pod =
  one browser. patchright Chromium, NoVNC (:6901), CDP proxy (:9222, fixed port,
  path rewritten across Chrome restarts). `status.wsUrl` is a deterministic
  Service address. Persistent profile PVC.
- **`Controller` CR**: FastAPI service (Python 3.9, patchright). Reads an
  operator-maintained ConfigMap registry (`<controller>-browsers`/`browsers.json`,
  hot-reloaded by mtime — **no Redis**, etcd/CR-status is the source of truth).
  Endpoints: `/search*`, `/content`, `/interact`, `/attribute`, `/browsers`,
  `/start_session`, `/end_session`. Multiplexes pages, autoscales browsers by
  `maxPagesPerBrowser`.
- Image build: `livellm-browser` repo, `uv`-based, one Dockerfile per component,
  CI in `.github/workflows/ci.yml`. Images `kamasalyamov/livellm-browser:<tag>`.

## The three design decisions

### A. The controller is optional
`BrowserAgentSpec.target` is a oneOf — `browserRef` (plain CDP to a Browser's
`status.wsUrl`), `controllerRef` (registry resolution **+** controller tools), or
`externalWsUrl` (BYO). Minimum viable tier is **Browser + BrowserAgent, no
controller**. The controller is an *accelerator*, never required.

### B. Stealth is connection-inherited, hardening is optional
The hardened binary + launch flags live on the **browser pod** (patchright
launch), so every CDP client inherits them — including the agent and any
BYO vanilla-Playwright client. The deeper client-side patches (`Runtime.enable`
suppression, `navigator.webdriver` init-scripts) are covered for the agent
because browser-use uses patchright. To cover *arbitrary* clients automatically
we can later make the in-pod CDP proxy **CDP-aware** and auto-inject
`Page.addScriptToEvaluateOnNewDocument` on every new target — but the 2026
anti-detect benchmark shows protocol-level patching adds ~no gain once the
binary/flags are right, so it's a low-priority enhancement, not part of v1.

### C. The controller is a tool server for the agent
When `controllerRef` is set, the agent registers the controller's deterministic
endpoints as **browser-use custom tools**, so the LLM offloads fast work instead
of reasoning over raw DOM:

| Controller endpoint | Agent tool | Replaces |
|---|---|---|
| `/search`,`/search_news`,`/search_images`,`/search_videos` | `web_search(query, kind)` | hand-parsing a SERP |
| `/search_hints` | `search_suggestions(query)` | — |
| `/content` | `read_page(url)` → clean text/html | scroll+extract loops |
| `/attribute` | `extract(selector, attr)` → bulk links/attrs | DOM walking |
| `/interact` | `bulk_interact(actions)` → click/fill/remove/login | many micro-steps |

No controller → tools absent → browser-use falls back to built-in DOM
extraction. Tiers degrade gracefully.

## Topology

```
  task (NL) ──▶ tenant-api ──┐  (owner-scoped; fetches tenant AI key; persists)
                             │
                   browser_agent DB (Postgres / CNPG, separate database)
                   tasks · trajectories · steps · verdicts · artifacts
                             │
            control channel  │  (cloud_gateways WS: step events ⇄ verdicts)
                             ▼
                       BrowserAgent pod  ── browser-use (patchright) ──┐
                             │  registers controller tools (optional)   │
                             ▼                                          ▼
              Controller (optional, registry + tools)            Browser pod (CDP :9222)
                             │                                    NoVNC :6901 (human takeover)
                             ▼
                    CDP ws_url ◀── plain or via registry
        artifacts: CDP Page.startScreencast → MP4  +  trajectory JSON  ─▶ MinIO
        webhooks: step/lifecycle events ─▶ tenant alert channels (chosen channel)
```

## The agent runtime (`agent/` image, the heart)

New third component in `livellm-browser`: `agent/`, its own `pyproject.toml` +
Dockerfile, image `kamasalyamov/livellm-browser:agent-X.Y.Z`. **Python ≥3.11**
(browser-use requirement — cannot reuse the 3.9 controller base).

Loop, in order:

1. **Plan.** A planner LLM pass turns the task into an ordered step list →
   persisted as the **trajectory** (this is *our* layer; browser-use is otherwise
   step-by-step ReAct).
2. **Connect.** `connect_over_cdp(ws_url)` via patchright (plain or registry-
   resolved). If `controllerRef`, register the controller tools (decision C).
3. **Execute, gated.** Drive browser-use step by step against the trajectory.
   Use `register_new_step_callback` to emit a **step event** and, when the step
   is marked `review`, block on `state.paused` until a **verdict** arrives over
   the control channel. `register_done_callback` closes the run.
4. **Checkpoint per step.** Snapshot browser state (URL + cookies +
   localStorage via CDP) into the step row — **do not** rely on browser-use
   `rerun_history()` (fragile on dynamic pages). Restart-from-step replays from
   the nearest checkpoint, re-seeding the agent.
5. **Record.** `Page.startScreencast` frames → encode MP4 (page-scoped, works even
   if a browser hosts multiple pages). On completion, MP4 + trajectory JSON → MinIO.
6. **Notify.** Emit lifecycle/step events to the chosen alert channel (webhook).

The model is built from the **tenant's `/integrations` AI provider** (provider +
key fetched per-task from tenant-api, owner-scoped, held only for the run; prefer
a vision-capable model).

## State model — separate `browser_agent` database

Tasks/trajectories/steps are high-churn, mutable, human-interactive application
data — they do **not** belong in etcd/CRs and should not bloat tenant-api's
schema. They live in a **separate `browser_agent` database** on the **same CNPG
operator** (own goose migrations + sqlc), independently migratable/scalable; a
dedicated physical cluster only when write volume demands it (maps onto per-cell
DBs later). Multitenancy is **row-scoped** (`tenant_id` + app-level scoping),
not DB-per-tenant.

```
tasks         (id, tenant_id, browser_agent_ref, prompt, mode, status, created_at)
trajectories  (id, task_id, version, plan_json, created_at)   -- versioned (reformation)
steps         (id, trajectory_id, idx, intent, action_json, status,   -- pending|running|review|done|skipped|failed
               checkpoint_json, screenshot_ref, started_at, ended_at)
verdicts      (id, step_id, kind, note, edited_action_json, actor, at) -- done|not_done|reform|rewrite
artifacts     (id, task_id, kind, object_ref, bytes, created_at)       -- video|trajectory|screenshot
```

CR status (etcd) stays the source of truth for **infra**; the DB owns **dynamic
run state** — same split as `Tenant` CR vs the `users` table.

## Control protocol (per-step review · restart · pause)

Reuses the `cloud_gateways` streaming proxy + HMAC token mint (same as VNC/agent
streams). tenant-api mints a short-lived token; the agent pod holds a WS to the
gateway. Messages:

- agent → UI: `step.started`, `step.review` (blocks), `step.done`, `run.done`,
  `plan.ready`.
- UI → agent: `verdict{done|not_done|reform|rewrite, note?, action?}`,
  `pause`, `resume`, `restart_from{step_idx}`.

`reform` re-runs the planner from the current step → new **trajectory version**.
`rewrite` swaps a single step's action. `restart_from` replays to the chosen
step's checkpoint, then resumes live.

## API surface (tenant-api)

`POST /v1/browsers/{id}/act {prompt, mode}` → create task + plan (the screenshot's
"Drive via API"). Plus: `GET /v1/tasks/{id}` (trajectory+steps), `POST
/v1/tasks/{id}/steps/{idx}/verdict`, `POST /v1/tasks/{id}/restart {from}`,
`GET /v1/tasks/{id}/artifacts`. All owner-scoped.

## CRD sketch (`BrowserAgent`, `livellm.io/v1alpha1`, short `ba`)

```
spec:
  target:                      # oneOf
    browserRef: <name>         #   plain CDP to that Browser
    controllerRef: <name>      #   registry + controller tools
    externalWsUrl: <wss://…>   #   BYO
  model:
    integrationRef: <name>     # tenant /integrations provider (default: tenant default)
  recording: { enabled: true } # CDP screencast → MinIO
  running: true                # pause/resume the runtime (PVC retained)
status:
  phase: Pending|Creating|Running|Failed|Stopped
  controlUrl: <gateway ws>     # where the UI connects for step review
  activeTaskId: <uuid>
```

## Phasing

```
P1  agent/ runtime image      browser-use over CDP; planner→trajectory; step
                              gating + checkpoints; controller tools; screencast.
P2  BrowserAgent CRD+operator deploy runtime, wire target, control token, status.
P3  tenant-api                browser_agent DB (goose+sqlc); task/verdict/restart
                              endpoints; integration-key fetch; webhook→channels.
P4  charts/tenant + tenant-ui render BrowserAgent workload; the review UI
                              (trajectory, per-step verdict buttons, video, restart).
```

## Open seams

- CDP-aware stealth proxy (decision B) — deferred; revisit only if BYO
  vanilla-Playwright clients need automatic hardening.
- Multiplex vs dedicate: set controller `maxPagesPerBrowser: 1` for agent
  sessions to get a dedicated browser (clean NoVNC takeover) while still using
  the registry/autoscaler; raise for density on cheap tasks.
- Endpoints start in tenant-api with schema isolation so they can split into a
  dedicated agent service later without a data migration.
