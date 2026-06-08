"""Plan-before-execute — turns a task prompt into an ordered list of sub-goal
intents (the trajectory). This is OUR layer; browser-use's internal planning is
disabled. We call the provider SDK directly for a structured plan (stable API),
rather than browser-use's chat abstraction.

Each returned intent is a concrete browser sub-goal, e.g.
"search 'managed vector db pricing'", "open pinecone.io/pricing",
"extract starter / standard / enterprise tiers" — matching the product's
step list. Falls back to a single step (the whole task) for providers without a
planner path, so a task always runs.
"""

import json
import logging
import re
from typing import Optional

from livellm_agent.config import Settings

logger = logging.getLogger(__name__)

_SYS = (
    "You are a planner for a web-browsing agent. Decompose the user's task into "
    "an ordered list of concrete, individually-verifiable browser sub-goals. "
    "Each sub-goal is one navigation/search/extraction action a human could "
    "review (e.g. \"search 'X'\", \"open example.com/pricing\", \"extract the "
    "pricing tiers\"). Keep it minimal — 2 to 8 steps. "
    'Respond ONLY with JSON: {"steps": ["...", "..."]}.'
)


def _parse_steps(text: str) -> list[str]:
    """Pull the steps array out of a model response, tolerantly."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    steps = obj.get("steps") if isinstance(obj, dict) else obj
    if not isinstance(steps, list):
        return []
    return [str(s).strip() for s in steps if str(s).strip()]


async def plan(s: Settings, prompt: str) -> list[str]:
    provider = (s.model_provider or "").lower()
    # base URLs for known OpenAI-compatible providers (mirror engine.py)
    openai_base = {
        "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    openai_default_model = {"openai": "gpt-4o", "zai-coding-plan": "glm-4.6", "openrouter": "openai/gpt-4o"}

    steps: list[str] = []
    try:
        if provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=s.model_api_key, base_url=s.model_base_url)
            msg = await client.messages.create(
                model=s.model_name or "claude-sonnet-4-6",
                max_tokens=1024,
                system=_SYS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            steps = _parse_steps(text)
        elif provider != "google":  # openai / openai-compatible / zai / openrouter / …
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=s.model_api_key, base_url=s.model_base_url or openai_base.get(provider))
            resp = await client.chat.completions.create(
                model=s.model_name or openai_default_model.get(provider, "gpt-4o"),
                messages=[
                    {"role": "system", "content": _SYS},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            steps = _parse_steps(resp.choices[0].message.content or "")
    except Exception as e:  # planning is best-effort; degrade to single-step
        logger.warning("planner failed (%s); falling back to single-step", e)

    if not steps:
        logger.info("planner produced no steps; running the task as one sub-goal")
        steps = [prompt]
    return steps
