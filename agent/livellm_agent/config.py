from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Agent settings from environment variables.

    The operator injects these from the BrowserAgent CR (target, model
    integration, gateway, artifact store). Everything is optional so the image
    boots a health server even before a task is assigned.
    """

    # ── CDP target (decision A: controller is optional) ──────────────────
    # Exactly one of these is set by the operator from spec.target:
    #   - cdp_ws_url     plain CDP to a Browser's status.wsUrl (or BYO external)
    #   - controller_url + browser_id   resolve + tools via a Controller
    cdp_ws_url: Optional[str] = None
    controller_url: Optional[str] = None        # e.g. http://<controller>.<ns>:8000/parser
    browser_id: Optional[str] = None            # registry id within the controller
    cdp_headers_json: Optional[str] = None       # auth headers for BYO CDP (JSON object)

    # ── model: the tenant's own AI integration (decision: zero extra setup) ──
    # The operator/tenant-api resolves the tenant's /integrations provider and
    # injects a provider id + key here; the agent never stores it long-term.
    model_provider: Optional[str] = None         # openai | anthropic | ...
    # Workspace-owner guidance folded into planning + sub-goal prompts
    # (chart delivers it as AGENT_INSTRUCTIONS from browser.aiAgent).
    instructions: Optional[str] = None
    model_name: Optional[str] = None             # vision-capable preferred
    model_api_key: Optional[str] = None
    model_base_url: Optional[str] = None          # optional override / proxy
    # Vision override (AGENT_MODEL_VISION=true/false). Unset → inferred: models
    # driven with raw_output (zai/GLM text endpoints) get no screenshots, the
    # rest keep browser-use's "auto" behavior. See engine.resolve_use_vision.
    model_vision: Optional[bool] = None
    # Per-call LLM timeout (seconds) for browser-use's action loop. The library
    # default is 75s, which slow models (GLM et al.) routinely blow through.
    llm_timeout: int = 180

    # ── control channel (cloud_gateways) ─────────────────────────────────
    control_url: Optional[str] = None             # wss://<gateway>/agent/... (events ⇄ verdicts)
    control_token: Optional[str] = None           # short-lived HMAC, minted by tenant-api

    # ── artifacts (MinIO) ─────────────────────────────────────────────────
    artifact_endpoint: Optional[str] = None
    artifact_bucket: str = "browser-agent"
    artifact_access_key: Optional[str] = None
    artifact_secret_key: Optional[str] = None

    # ── recording ──────────────────────────────────────────────────────────
    recording_enabled: bool = True
    recording_fps: int = 12

    # ── identity / scoping ───────────────────────────────────────────────
    tenant_id: Optional[str] = None
    browser_agent_ref: Optional[str] = None

    # ── server ───────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8800

    model_config = {"env_prefix": "AGENT_", "case_sensitive": False}

    @property
    def uses_controller(self) -> bool:
        return bool(self.controller_url and self.browser_id)


settings = Settings()
