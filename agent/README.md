# livellm-agent

The **browser-agent** runtime — the AI layer of the browser stack. It takes a
natural-language task, **plans it into a trajectory** of steps, executes those
steps in a `Browser` over CDP, and exposes every step for **human review**
(done / not-done / reform / rewrite) with **restart-from-step**, **video**, and
**webhooks**.

This is the third image in the repo, alongside `browser/` and `controller/`:
`kamasalyamov/livellm-browser:agent-X.Y.Z`. **Python ≥3.11** (browser-use
requirement) — it is *not* a fork of the 3.9 controller.

See the full design — CRD, DB schema, control protocol, phasing — in
[`docs/browser-agent-architecture.md`](../docs/browser-agent-architecture.md).

## How it connects

The agent does **not** launch Chromium. It connects OUT over CDP to a `Browser`
pod (`connect_over_cdp`, patchright — so it inherits the pod's hardened
binary/flags). The target is one of:

- a `Browser`'s deterministic `status.wsUrl` (plain),
- a `Controller` registry entry (registry + the controller's deterministic
  search/content/interact/attribute endpoints registered as agent **tools**),
- a BYO external CDP ws URL.

## Layout

```
main.py                 FastAPI entrypoint (health + internal /act)
livellm_agent/
  config.py             env-driven settings (CDP target, controller, model, gateway, minio)
  models.py             Task / Trajectory / Step / Verdict + control events (matches the DB + protocol)
  tools.py              Controller tool client — decision C (web_search / read_page / extract)
  engine.py             browser-use glue (plan → execute → step gating)        [P1 cont.]
  recorder.py           CDP Page.startScreencast → MP4                          [P1 cont.]
  checkpoint.py         per-step CDP state snapshot/restore (restart-from-step) [P1 cont.]
  control.py            cloud_gateways WS client (events ⇄ verdicts)            [P1 cont.]
  artifacts.py          MinIO upload (video + trajectory JSON)                  [P1 cont.]
  runner.py             orchestrates a task end to end                          [P1 cont.]
```
