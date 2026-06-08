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
build**; the engine is just the body. We build it on **browser-use (MIT)**,
pinned to **0.12.9** (raw CDP via `cdp-use`; its own `Chat*` LLM classes;
`Tools()` registry). It connects to the already-running, patchright-launched
`Browser`, so it inherits the launch-layer stealth (see decision B for the
client-layer nuance).

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

### B. Stealth is launch-inherited; client-layer hardening lives in the proxy
The hardened binary + launch flags live on the **browser pod** (patchright
launch), so every CDP client inherits them — the agent, and any BYO client.
This is the layer that actually moves the needle.

> **Pin note (browser-use 0.12.9):** modern browser-use **dropped Playwright/
> Patchright** — it talks raw CDP via `cdp-use`. So the agent no longer inherits
> patchright's *client-side* patches (`Runtime.enable` suppression,
> `navigator.webdriver` init-scripts); it only inherits the launch-layer
> hardening. The client layer therefore belongs in the **in-pod CDP proxy**:
> make it CDP-aware and auto-inject `Page.addScriptToEvaluateOnNewDocument` on
> every new target, covering *all* clients uniformly. The 2026 anti-detect
> benchmark shows this client-layer gives ~no gain once the binary/flags are
> right, so it stays a **deferred enhancement** (not v1) — but it's now the
> single home for client-layer stealth rather than something the engine carries.

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
                       BrowserAgent pod  ── browser-use (raw CDP) ────┐
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

1. **Plan.** A planner LLM pass (provider SDK, structured) turns the task into
   an ordered list of **sub-goal** intents → persisted as the **trajectory**.
   This is *our* layer; browser-use's internal planning is disabled
   (`enable_planning=False`).
2. **Connect.** `BrowserSession(cdp_url=...)` → `Agent(browser_session=...)`
   (plain or registry-resolved). If `controllerRef`, register the controller
   endpoints as `Tools()` (decision C).
3. **Execute, gated — one browser-use run per sub-goal.** Each trajectory step
   is a single `agent.run()` on the **shared** `BrowserSession` (state carries
   across steps). Human review gates cleanly *between* sub-goals — we emit a
   `step.event(review)` and block on the verdict over the control channel.
   Mid-step pause/cancel uses the async `register_should_stop_callback`. (We do
   **not** drive browser-use's internal step loop: in 0.12.9 `pause()` raises
   `InterruptedError`, so the clean seam is between runs, and a "sub-goal" is the
   reviewable unit the product shows — "search X", "open Y", "extract Z".)
4. **Checkpoint per step.** Record the step's resulting URL (and, as an
   enhancement, cookies/localStorage via CDP) into the step row. Restart-from-
   step replays the trajectory from step *k* on the same session — **not** via
   browser-use `rerun_history()` (fragile on dynamic pages).
5. **Record.** `Page.startScreencast` frames → encode MP4 (page-scoped, works even
   if a browser hosts multiple pages). On completion, MP4 + trajectory JSON → MinIO.
6. **Notify.** Emit lifecycle/step events to the chosen alert channel (webhook).

The model is built from the **tenant's `/integrations` AI provider** (provider +
key fetched per-task from tenant-api, owner-scoped, held only for the run; prefer
a vision-capable model). **There is no implicit default model** — if the tenant
has no resolvable AI integration the task is refused with a clear error
(`model_not_configured`), never billed to a platform key.

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
    integrationRef: <name>     # tenant /integrations provider; REQUIRED — no implicit default, task refused if unresolved
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
