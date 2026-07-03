"""Output repair for chat models without reliable structured output (GLM et al.).

Some OpenAI-compatible providers (Z.ai's GLM coding endpoint among them) ignore
`response_format: json_schema` and reply with prose around the JSON, or with a
bare action object like `{"done": {...}}` instead of the AgentOutput envelope
`{"action": [{"done": {...}}]}`. This module extracts the first JSON object
from such a reply and coerces the common shape deviations before validating
against the expected Pydantic model.

Pure module: pydantic-only, no browser-use imports, so it is unit-testable
without the engine's dependencies.
"""

import json
import re
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object out of a possibly-prose model reply."""
    if not text:
        return None
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # balanced-brace scan from each '{' (string-aware), first parseable wins
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def coerce_shape(obj: Any, model: Type[BaseModel]) -> Optional[dict]:
    """Coerce common AgentOutput deviations into the expected envelope.

    Handles: legacy `{"current_state": {...}, "action": [...]}` nesting,
    a dict `action` instead of a list, bare top-level action objects
    (`{"done": {...}}`), and extra keys (the model forbids them).
    """
    if not isinstance(obj, dict):
        return None
    out = dict(obj)
    fields = set(model.model_fields)

    # legacy nesting: flatten current_state into the top level
    state = out.pop("current_state", None)
    if isinstance(state, dict):
        for k, v in state.items():
            if k in fields:
                out.setdefault(k, v)

    action = out.get("action")
    if isinstance(action, dict):
        out["action"] = [action]
    elif not isinstance(action, list):
        out.pop("action", None)
        # bare action(s) at the top level, e.g. {"done": {...}} — every unknown
        # dict-valued key is treated as one action
        moved = [k for k in out if k not in fields and isinstance(out[k], dict)]
        if moved:
            out["action"] = [{k: out.pop(k)} for k in moved]

    # the schema forbids extras: drop anything it doesn't know
    return {k: v for k, v in out.items() if k in fields}


def repair_output(text: str, model: Type[T]) -> T:
    """Parse `text` into `model`, repairing prose wrapping and shape drift.

    Raises ValueError when no JSON object can be found, or pydantic's
    ValidationError when the (coerced) object still doesn't validate.
    """
    obj = extract_json(text)
    if obj is None:
        raise ValueError(f"no JSON object found in model output: {text[:200]!r}")
    try:
        return model.model_validate(obj)
    except ValidationError:
        coerced = coerce_shape(obj, model)
        if coerced is None:
            raise
        return model.model_validate(coerced)
