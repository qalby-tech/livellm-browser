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

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from browser_use import Agent, BrowserProfile, BrowserSession, ChatOpenAI, Tools
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import UserMessage
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion
from pydantic import ValidationError

from livellm_agent.config import Settings
from livellm_agent.repair import repair_output
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


# ── LLM output repair (GLM & friends) ────────────────────────────────────────
# Z.ai's GLM endpoint ignores `response_format: json_schema` — it replies with
# prose ("Looking at the browser state…") or a bare `{"done": {...}}` object,
# which browser-use's model_validate_json turns into a per-step AgentOutput
# ValidationError. RepairingChatOpenAI makes that survivable at the LLM level.

def _wants_raw_output(provider: str, model: str) -> bool:
    """Providers/models known to ignore or reject provider-side json_schema —
    put the schema in the prompt instead and repair the reply locally."""
    return provider == "zai-coding-plan" or model.lower().startswith("glm")


# Substrings identifying a structured-output PARSE failure (ChatOpenAI wraps
# pydantic's ValidationError into a ModelProviderError with its message).
_PARSE_ERROR_MARKERS = ("validation error", "invalid json", "failed to parse structured output")

_SCHEMA_NUDGE = (
    "Respond with a SINGLE JSON object only — no prose, no markdown fences. "
    "It must validate against this JSON schema; in particular `action` is a "
    "LIST of action objects (e.g. {{\"action\": [{{\"done\": {{...}}}}]}}):\n{schema}"
)


@dataclass
class RepairingChatOpenAI(ChatOpenAI):
    """ChatOpenAI that survives models without reliable structured output.

    With raw_output=True the provider-side `response_format: json_schema` is
    skipped entirely (the schema goes into the prompt); either way, a parse
    failure gets ONE corrective retry — the schema plus the parse error are
    appended as a user message — and the reply is repaired locally (prose
    stripped, `{"done": ...}` coerced to `{"action": [{"done": ...}]}`) before
    validation. Only if that retry also fails does the error reach browser-use's
    step loop, which feeds it back to the model as a recoverable step failure.
    """

    raw_output: bool = False

    async def ainvoke(self, messages, output_format=None, **kwargs):  # type: ignore[override]
        if output_format is None:
            return await super().ainvoke(messages, output_format=None, **kwargs)

        parse_error: str
        if self.raw_output:
            try:
                return await self._ask_raw(messages, output_format, note=None, **kwargs)
            except (ValidationError, ValueError) as e:
                parse_error = str(e)
        else:
            try:
                return await super().ainvoke(messages, output_format=output_format, **kwargs)
            except ModelProviderError as e:
                msg = str(e)
                if not any(m in msg.lower() for m in _PARSE_ERROR_MARKERS):
                    raise
                parse_error = msg

        logger.warning("model output unparseable (%.200s); retrying once with a corrective nudge", parse_error)
        return await self._ask_raw(messages, output_format, note=parse_error[:500], **kwargs)

    async def _ask_raw(self, messages, output_format, note: Optional[str], **kwargs):
        """Plain-text ask with the schema in the prompt, then local repair."""
        schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(output_format))
        prompt = _SCHEMA_NUDGE.format(schema=schema)
        if note:
            prompt = f"Your previous reply could not be parsed ({note}).\n{prompt}"
        raw = await super().ainvoke(list(messages) + [UserMessage(content=prompt)], output_format=None, **kwargs)
        parsed = repair_output(raw.completion, output_format)
        return ChatInvokeCompletion(completion=parsed, usage=raw.usage, stop_reason=raw.stop_reason)


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
    # openrouter, etc.) RepairingChatOpenAI adds prompt-schema + output repair
    # for models that don't honor provider-side structured output.
    base = s.model_base_url or OPENAI_COMPAT_BASE.get(provider)
    model = s.model_name or OPENAI_COMPAT_DEFAULT_MODEL.get(provider, "gpt-4o")
    return RepairingChatOpenAI(
        model=model,
        api_key=s.model_api_key,
        base_url=base,
        raw_output=_wants_raw_output(provider, model),
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


# ── ask-human ────────────────────────────────────────────────────────────────
# The runner detects this prefix in a sub-goal's final output and parks the
# task as `paused` with the question in trajectory.result; a `rewrite` verdict
# with a note answers it (see runner._ask_human).
NEED_HUMAN_PREFIX = "NEED_HUMAN:"

_NEED_HUMAN_NUDGE = (
    "If you reach a point where you need information only the human operator "
    "can provide (login credentials, a 2FA/verification code, a payment "
    "confirmation, or a choice you must not guess), do NOT invent it: call "
    "`done` with success=false and text starting exactly with 'NEED_HUMAN: ' "
    "followed by one clear question for the human."
)


async def _mirror_control(agent: Agent, control) -> None:
    """Mirror UI pause/resume/cancel onto the RUNNING browser-use agent, so
    pause takes effect at the next action (browser-use checks state.paused
    between actions and steps) instead of the next sub-goal boundary, and a
    cancel also unblocks a paused agent (agent.stop() sets the pause event)."""
    paused = False
    try:
        while True:
            if control.cancelled.is_set():
                agent.stop()
                return
            want = control.paused.is_set()
            if want and not paused:
                agent.pause()
                paused = True
            elif not want and paused:
                agent.resume()
                paused = False
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        pass


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
    control=None,
) -> SubgoalResult:
    """Execute one trajectory sub-goal as a browser-use run on the shared session.

    `control` (a ControlChannel) is optional: when given, its pause/cancel flags
    are mirrored onto the running agent mid-sub-goal (pause at the next action,
    cancel within seconds) instead of only at sub-goal boundaries.
    """
    agent = Agent(
        task=intent,
        llm=llm,
        browser_session=session,
        tools=tools,
        enable_planning=False,                       # our layer plans; keep sub-goals focused
        use_vision="auto",                            # don't force screenshots on non-vision models
        use_judge=False,                              # GLM/weak models return invalid JSON for the judge
        use_thinking=False,                           # ditto the thinking schema; keep the action loop simple
        register_should_stop_callback=should_stop,    # async cancel/restart (clean stop)
        extend_system_message=_NEED_HUMAN_NUDGE,      # ask-human escape hatch (see runner)
    )
    bridge = asyncio.create_task(_mirror_control(agent, control)) if control is not None else None
    try:
        history = await agent.run(max_steps=SUBGOAL_MAX_STEPS)
    finally:
        if bridge:
            bridge.cancel()

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
