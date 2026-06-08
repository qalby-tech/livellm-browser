"""browser-use engine glue — the ONLY module that binds to browser-use's API.

Pinned to browser-use==0.12.9 (raw CDP via cdp-use; `Chat*` LLM classes;
`Tools()` registry; per-step `register_should_stop_callback`). Everything
version-sensitive is isolated here so a future bump is a one-file change.

Architecture (see docs/browser-agent-architecture.md): our trajectory layer
sits ABOVE browser-use. The planner decomposes a task into ordered sub-goals;
each sub-goal is ONE browser-use run on a shared BrowserSession (so browser
state carries across steps); human review gates between sub-goals. We disable
browser-use's internal planning (`enable_planning=False`) because the trajectory
is ours.
"""

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from browser_use import Agent, BrowserProfile, BrowserSession, Tools

from livellm_agent.config import Settings
from livellm_agent.tools import ControllerTools

logger = logging.getLogger(__name__)

# Per-sub-goal step cap — a sub-goal is a focused goal ("open X", "extract Y"),
# not a whole task, so it should converge well within this.
SUBGOAL_MAX_STEPS = 15

# Base URLs for known OpenAI-compatible providers (anything not anthropic/google
# is driven through browser-use's ChatOpenAI). An explicit AGENT_MODEL_BASE_URL
# overrides these.
OPENAI_COMPAT_BASE = {
    "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
    "openrouter": "https://openrouter.ai/api/v1",
}
OPENAI_COMPAT_DEFAULT_MODEL = {
    "openai": "gpt-4o",
    "zai-coding-plan": "glm-4.6",
    "openrouter": "openai/gpt-4o",
}


class ModelNotConfigured(Exception):
    """Raised when the tenant has no resolvable AI integration. Never fall back
    to a platform key — surfaced to the caller as `model_not_configured`."""


def build_llm(s: Settings):
    """Build the browser-use chat model from the tenant's integration.

    REQUIRED — there is no implicit default (product decision). Raises
    ModelNotConfigured if the provider or key is missing.
    """
    if not s.model_provider or not s.model_api_key:
        raise ModelNotConfigured("no AI integration configured for this tenant")

    provider = s.model_provider.lower()
    if provider == "anthropic":
        from browser_use import ChatAnthropic
        return ChatAnthropic(
            model=s.model_name or "claude-sonnet-4-6",
            api_key=s.model_api_key,
            base_url=s.model_base_url,
        )
    if provider == "google":
        from browser_use import ChatGoogle
        return ChatGoogle(model=s.model_name or "gemini-2.5-pro", api_key=s.model_api_key)

    # Everything else is treated as an OpenAI-compatible chat API. Known
    # providers get their base URL from OPENAI_COMPAT_BASE; an explicit
    # AGENT_MODEL_BASE_URL always wins. (zai-coding-plan = Z.ai GLM coding plan,
    # openrouter, etc.)
    from browser_use import ChatOpenAI
    base = s.model_base_url or OPENAI_COMPAT_BASE.get(provider)
    return ChatOpenAI(
        model=s.model_name or OPENAI_COMPAT_DEFAULT_MODEL.get(provider, "gpt-4o"),
        api_key=s.model_api_key,
        base_url=base,
    )


def build_session(s: Settings) -> BrowserSession:
    """Connect to the EXISTING Browser over CDP (does not launch Chromium).

    decision A: plain CDP to a Browser's status.wsUrl, or registry-resolved
    when targeting a Controller (the operator injects the resolved ws URL).
    """
    if not s.cdp_ws_url:
        raise RuntimeError("no cdp_ws_url configured (operator must resolve the target)")
    # Remote attach to a Chrome we don't own: keep_alive so teardown never tries
    # to kill it; is_local False so no local-process assumptions fire. (NB: the
    # browser must be EXCLUSIVE to this agent — a second CDP client creating/
    # destroying targets steals browser-use's page focus and breaks state reads.)
    profile = BrowserProfile(keep_alive=True, is_local=False)
    return BrowserSession(cdp_url=s.cdp_ws_url, browser_profile=profile)


def build_controller_tools(s: Settings) -> tuple[Optional[Tools], Optional[ControllerTools]]:
    """decision C: register the Controller's deterministic endpoints as
    browser-use tools. Returns (Tools registry, client-to-close) or (None, None)
    when no controller is configured (browser-use falls back to built-in DOM).
    """
    if not s.uses_controller:
        return None, None

    client = ControllerTools(base_url=s.controller_url, browser_id=s.browser_id)
    tools = Tools()

    @tools.action(description="Structured web search (Google) with result links, snippets, wiki and AI-overview panels. kind: web|news|images|videos.")
    async def web_search(query: str, kind: str = "web", count: int = 5) -> str:
        import json
        return json.dumps(await client.web_search(query, kind=kind, count=count))  # type: ignore[arg-type]

    @tools.action(description="Get Google autocomplete suggestions for a query.")
    async def search_suggestions(query: str) -> str:
        import json
        return json.dumps(await client.search_suggestions(query))

    @tools.action(description="Fetch a URL and return its clean readable text (auto-scrolled). Use this to READ a page fast instead of navigating step by step.")
    async def read_page(url: str) -> str:
        return await client.read_page(url)

    @tools.action(description="Extract all hyperlink URLs from a page fast (returns the href of every <a>).")
    async def extract_links(url: str) -> str:
        import json
        res = await client.extract(url, [{"name": "links", "selector": "a", "attribute": "href"}])
        return json.dumps(res)

    logger.info("registered controller tools (web_search, search_suggestions, read_page, extract_links)")
    return tools, client


@dataclass
class SubgoalResult:
    success: bool
    output: Optional[str]
    url: Optional[str]
    screenshot_b64: Optional[str]
    error: Optional[str]  # last browser-use error when the sub-goal failed
    raw: dict  # history.model_dump(), for the trajectory artifact


async def run_subgoal(
    session: BrowserSession,
    llm,
    tools: Optional[Tools],
    intent: str,
    should_stop: Callable[[], Awaitable[bool]],
) -> SubgoalResult:
    """Execute one trajectory sub-goal as a browser-use run on the shared session."""
    agent = Agent(
        task=intent,
        llm=llm,
        browser_session=session,
        tools=tools,
        enable_planning=False,                       # our layer plans; keep sub-goals focused
        use_vision="auto",                            # don't force screenshots on non-vision models
        use_judge=False,                              # GLM/weak models return invalid JSON for the judge
        use_thinking=False,                           # ditto the thinking schema; keep the action loop simple
        register_should_stop_callback=should_stop,    # async pause/cancel (clean stop)
    )
    history = await agent.run(max_steps=SUBGOAL_MAX_STEPS)

    screenshots = history.screenshots() or []
    urls = history.urls() or []
    errs: list[str] = []
    try:
        errs = [str(e) for e in (history.errors() or []) if e]
    except Exception:
        pass
    return SubgoalResult(
        success=bool(history.is_successful()),
        output=history.final_result(),
        url=urls[-1] if urls else None,
        screenshot_b64=screenshots[-1] if screenshots else None,
        error=errs[-1] if errs else None,
        raw=history.model_dump(),
    )
