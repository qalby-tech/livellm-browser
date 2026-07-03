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
from typing import Any, Optional

from livellm_agent.config import Settings
from livellm_agent.repair import extract_json

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


_REPLAN_SYS = (
    "You are a recovery planner for a web-browsing agent. One step of the plan "
    "FAILED; the steps after it have NOT run. Using the task goal, what already "
    "succeeded, and why the step failed, decide how to proceed. Respond ONLY "
    "with a single JSON object, in exactly ONE of these two shapes:\n"
    '1. {"steps": ["...", "..."]} — a revised remainder of the plan, replacing '
    "the failed step and everything after it, when the goal is still achievable "
    "autonomously. Each step is one concrete, individually-verifiable browser "
    "sub-goal. Do not repeat steps that already succeeded, and never include a "
    "step that depends on the failed step having succeeded.\n"
    '2. {"need_human": "<one clear question for the user>"} — when the blocker '
    "needs information or a decision only the human has: a delivery address or "
    "delivery time slot, login credentials or a 2FA code, a payment "
    "confirmation, or a choice between options the agent must not guess."
)


def _parse_replan(text: str) -> Optional[dict[str, Any]]:
    """Parse a replan_or_ask reply into {"steps": [...]} or {"need_human": "..."}.

    Defensive: tolerates prose/fences around the JSON (via repair.extract_json).
    Returns None when neither shape can be recovered.
    """
    obj = extract_json(text or "")
    if not isinstance(obj, dict):
        return None
    q = obj.get("need_human")
    if isinstance(q, str) and q.strip():
        return {"need_human": q.strip()}
    steps = obj.get("steps")
    if isinstance(steps, list):
        cleaned = [str(x).strip() for x in steps if str(x).strip()]
        if cleaned:
            return {"steps": cleaned}
    return None


def _plan_state(steps, failed_step) -> str:
    """Render the plan with per-step status, plus the failed step's evidence."""
    lines: list[str] = []
    for st in steps:
        aj = st.action_json or {}
        line = f"{st.idx + 1}. [{st.status.value}] {st.intent}"
        if st.idx == failed_step.idx:
            line += "   <-- FAILED"
            out = aj.get("output")
            err = aj.get("error")
            if out:
                line += f"\n   agent output: {str(out)[:1500]}"
            if err:
                line += f"\n   error: {str(err)[:1500]}"
        elif aj.get("output") and st.status.value == "done":
            line += f"\n   output: {str(aj['output'])[:300]}"
        lines.append(line)
    return "\n".join(lines)


async def replan_or_ask(s: Settings, prompt: str, steps, failed_step) -> Optional[dict[str, Any]]:
    """After a step fails in auto mode: ask the LLM to either revise the
    remainder of the plan or escalate to the human.

    Returns {"steps": [...]} (revised remainder, from the failed step onward),
    {"need_human": "<question>"} , or None when the call/parse fails (the
    caller then escalates to the human itself).
    """
    provider = (s.model_provider or "").lower()
    openai_base = {
        "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    openai_default_model = {"openai": "gpt-4o", "zai-coding-plan": "glm-4.6", "openrouter": "openai/gpt-4o"}

    user = (
        f"TASK:\n{prompt}\n\n"
        f"PLAN (step {failed_step.idx + 1} failed; later steps have not run):\n"
        f"{_plan_state(steps, failed_step)}\n\n"
        "Answer with the JSON object."
    )
    try:
        if provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=s.model_api_key, base_url=s.model_base_url)
            msg = await client.messages.create(
                model=s.model_name or "claude-sonnet-4-6",
                max_tokens=1024,
                system=_REPLAN_SYS,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            return _parse_replan(text)
        if provider != "google":  # openai / openai-compatible / zai / openrouter / …
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=s.model_api_key, base_url=s.model_base_url or openai_base.get(provider))
            resp = await client.chat.completions.create(
                model=s.model_name or openai_default_model.get(provider, "gpt-4o"),
                messages=[
                    {"role": "system", "content": _REPLAN_SYS},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
            return _parse_replan(resp.choices[0].message.content or "")
    except Exception as e:  # best-effort; the runner escalates to the human
        logger.warning("replan_or_ask failed: %s", e)
    return None


_SYNTH_SYS = (
    "You are a research assistant. A browser agent executed a plan and collected "
    "notes from web pages. Using ONLY those notes, write a clear, well-structured, "
    "complete answer to the user's task — the actual deliverable, not a summary of "
    "what you did. Use markdown (headings, bullet points, steps) where it helps. "
    "If the notes are insufficient for part of the task, say so briefly."
)


async def synthesize(s: Settings, prompt: str, notes: str) -> str:
    """Turn the collected step notes into a final answer to the task prompt.
    Best-effort: returns '' if the provider call fails."""
    provider = (s.model_provider or "").lower()
    user = f"TASK:\n{prompt}\n\nNOTES collected by the agent:\n{notes}\n\nWrite the answer to the TASK."
    openai_base = {
        "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    openai_default_model = {"openai": "gpt-4o", "zai-coding-plan": "glm-4.6", "openrouter": "openai/gpt-4o"}
    try:
        if provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=s.model_api_key, base_url=s.model_base_url)
            msg = await client.messages.create(
                model=s.model_name or "claude-sonnet-4-6",
                max_tokens=2048,
                system=_SYNTH_SYS,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        if provider != "google":
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=s.model_api_key, base_url=s.model_base_url or openai_base.get(provider))
            resp = await client.chat.completions.create(
                model=s.model_name or openai_default_model.get(provider, "gpt-4o"),
                messages=[{"role": "system", "content": _SYNTH_SYS}, {"role": "user", "content": user}],
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("synthesize failed: %s", e)
    return ""
