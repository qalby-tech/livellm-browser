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
  UI ──▶ tenant-api ──┐  (owner-scoped; the UI talks ONLY to tenant-api)
                      │   POST /browsers/{id}/act · /tasks/{id} · /verdict · /restart · /control
            HTTP      │   browser_agent_tasks (Postgres; trajectory snapshots as JSONB)
            control   │   ▲ POST /v1/internal/browser-agent/events  (agent → tenant-api)
                      ▼   │  (persist snapshot + fan out webhook to chosen channel)
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
4. **Checkpoint per step (implemented).** Snapshot url + cookies + localStorage
   via raw CDP (`get_or_create_cdp_session()` → `session_id`) into the step row.
   Restart-from-step *k* restores step *k-1*'s snapshot (cookies → navigate →
   localStorage → reload) so *k* re-runs from the same state — **not** via
   browser-use `rerun_history()` (fragile on dynamic pages).
5. **Record.** `Page.startScreencast` frames → encode MP4 (page-scoped, works even
   if a browser hosts multiple pages). On completion, MP4 + trajectory JSON → MinIO.
6. **Notify.** Emit lifecycle/step events to the chosen alert channel (webhook).

The model is built from the **tenant's `/integrations` AI provider** (provider +
key fetched per-task from tenant-api, owner-scoped, held only for the run; prefer
a vision-capable model). **There is no implicit default model** — if the tenant
has no resolvable AI integration the task is refused with a clear error
(`model_not_configured`), never billed to a platform key.

## State model — `browser_agent_tasks` (one row per task, JSONB trajectory)

The agent streams trajectory **snapshots** (the whole plan + per-step status +
verdicts), so persistence is ~one write per step *event*, not a row per step.
That collapses the original normalized design into **one table** whose
`trajectory` JSONB holds the current snapshot:

```
browser_agent_tasks (id, tenant_name, browser_id, prompt, mode, status,
                     channel_id → alert_channels,        -- webhook fan-out target
                     trajectory JSONB,                   -- {version, plan:[{idx,intent,status,checkpoint,...}]}
                     video_ref, trajectory_ref, created_at, updated_at)
```

Because write volume is now low (row-per-task, not row-per-step), it lives in
**tenant-api's existing DB** (goose migration `0005` + sqlc), prefixed and
logically separable. A dedicated `browser_agent` database/cluster is deferred
until volume justifies the second pool (see Open seams) — the original
separate-DB rationale (per-step row churn) no longer applies. Multitenancy is
**row-scoped** (`tenant_name`). CR status (etcd) stays the source of truth for
**infra**; the DB owns **dynamic run state** — same split as `Tenant` CR vs `users`.

## Control protocol (HTTP, tenant-api-centric)

The UI talks only to tenant-api (platform convention; `cloud_gateways` is for
media streaming — the deferred recorder/live-view — not control). So control is
plain HTTP, no gateway WS:

- **OUTBOUND (agent → tenant-api):** the runtime POSTs trajectory **snapshots**
  to a per-task `callback_url` = `POST /v1/internal/browser-agent/events`
  (in-cluster trust, like the Grafana webhook). Event types `plan.ready` /
  `step.event` / `run.done`; each carries the full current trajectory + status.
  tenant-api persists the snapshot and fans out a webhook to the task's chosen
  alert channel.
- **INBOUND (UI → tenant-api → agent):** `POST /tasks/{id}/verdict`,
  `/tasks/{id}/control {pause|resume|cancel}`, `/tasks/{id}/restart {from}`.
  tenant-api forwards to the agent pod's `/verdict`, `/control`, `/restart`,
  which feed the same per-step Future / pause Event the runner awaits.

`reform` re-runs the planner from the current step → new **trajectory version**.
`rewrite` swaps a single step's intent. `restart_from` restores the prior
step's checkpoint, then resumes live.

## API surface (tenant-api) — implemented

All owner-scoped under `/v1/tenants/{name}`:
- `POST /browsers/{id}/act {prompt, mode, channelId?}` → create task + drive the
  agent (the screenshot's "Drive via API"). 202 + the task.
- `GET /browsers/{id}/tasks` → recent tasks for a browser.
- `GET /tasks/{taskId}` → task + trajectory snapshot (steps + verdicts inline).
- `POST /tasks/{taskId}/verdict {stepIdx, kind, note?, action?}` → forwarded to agent.
- `POST /tasks/{taskId}/restart {from}` · `POST /tasks/{taskId}/control {op}`.
- internal: `POST /v1/internal/browser-agent/events` (agent → tenant-api snapshot ingestion).

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
                              gating + checkpoints; controller tools; MP4
                              recorder (recording.py: CDP screencast → spill
                              frames to /tmp → ffmpeg concat → MinIO,
                              video_ref on run.done). DONE.
P2  BrowserAgent CRD+operator deploy runtime, wire target, control token, status.
                              DONE (livellm-browser-operator: api + reconciler +
                              rbac + CRD; resolves target from Browser/Controller
                              status; Deployment+Service on :8800).
P3  tenant-api                browser_agent_tasks (goose 0005 + sqlc); act/get/list/
                              verdict/restart/control endpoints; internal events
                              ingestion + webhook→channels; agent HTTP control plane
                              (replaced the gateway WS); api-rbac browseragents
                              get/list. DONE. (model-key wiring → P4 chart.)
P4  charts/tenant + tenant-ui DONE. chart renders the BrowserAgent workload
                              (model key ← tenant provider Secret); operator
                              TenantSpec gains browser-agent; tenant-ui Agent tab
                              (trajectory + verdict buttons + restart + webhooks
                              + inline recording playback via the tenant-api
                              presign redirect).
```

## Open seams

- CDP-aware stealth proxy (decision B) — deferred; revisit only if BYO
  vanilla-Playwright clients need automatic hardening.
- Multiplex vs dedicate: set controller `maxPagesPerBrowser: 1` for agent
  sessions to get a dedicated browser (clean NoVNC takeover) while still using
  the registry/autoscaler; raise for density on cheap tasks.
- Endpoints start in tenant-api with schema isolation so they can split into a
  dedicated agent service later without a data migration.
