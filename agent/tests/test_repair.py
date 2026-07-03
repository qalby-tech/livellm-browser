"""Unit tests for livellm_agent.repair — the GLM output-repair path.

Shapes mirror browser-use 0.12.9's AgentOutput (extra='forbid', `action` is a
required list of single-key action objects) and the two failure modes seen in
real zai-coding-plan trajectories: prose instead of JSON, and a bare
`{"done": {...}}` object instead of the `{"action": [...]}` envelope.
"""

from typing import Optional

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from livellm_agent.repair import coerce_shape, extract_json, repair_output


class FakeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: Optional[dict] = None
    click: Optional[dict] = None


class FakeAgentOutput(BaseModel):
    """Mirrors browser-use AgentOutput: forbid extras, action is a list."""

    model_config = ConfigDict(extra="forbid")
    thinking: Optional[str] = None
    evaluation_previous_goal: Optional[str] = None
    memory: Optional[str] = None
    next_goal: Optional[str] = None
    action: list[FakeAction]


# ── extract_json ─────────────────────────────────────────────────────────────

def test_extract_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_prose_around():
    text = 'Looking at the browser state, here is my action: {"action": [{"done": {"text": "hi"}}]} — done.'
    assert extract_json(text) == {"action": [{"done": {"text": "hi"}}]}


def test_extract_json_from_markdown_fence():
    text = 'Sure!\n```json\n{"action": [{"done": {"text": "x"}}]}\n```'
    assert extract_json(text) == {"action": [{"done": {"text": "x"}}]}


def test_extract_json_handles_braces_in_strings():
    text = 'note {"memory": "set {a} to }b{", "action": []} tail'
    assert extract_json(text) == {"memory": "set {a} to }b{", "action": []}


def test_extract_json_none_for_pure_prose():
    assert extract_json("Looking at the browser state, the site is working properly.") is None
    assert extract_json("") is None


# ── coerce_shape / repair_output ─────────────────────────────────────────────

def test_repair_valid_envelope_passthrough():
    out = repair_output('{"next_goal": "g", "action": [{"done": {"text": "ok"}}]}', FakeAgentOutput)
    assert out.action[0].done == {"text": "ok"}


def test_repair_bare_done_object():
    # the real GLM failure: {"done": {...}} instead of {"action": [{"done": {...}}]}
    out = repair_output('{"done": {"text": "no results found", "success": false}}', FakeAgentOutput)
    assert out.action == [FakeAction(done={"text": "no results found", "success": False})]


def test_repair_action_dict_instead_of_list():
    out = repair_output('{"action": {"click": {"index": 3}}}', FakeAgentOutput)
    assert out.action == [FakeAction(click={"index": 3})]


def test_repair_flattens_current_state_and_drops_extras():
    text = (
        '{"current_state": {"memory": "m", "next_goal": "n", "bogus": 1},'
        ' "confidence": 0.9, "action": [{"done": {"text": "t"}}]}'
    )
    out = repair_output(text, FakeAgentOutput)
    assert out.memory == "m" and out.next_goal == "n"
    assert out.action[0].done == {"text": "t"}


def test_repair_prose_wrapped_bare_action():
    text = 'I will finish now.\n```json\n{"done": {"text": "answer", "success": true}}\n```'
    out = repair_output(text, FakeAgentOutput)
    assert out.action[0].done["success"] is True


def test_repair_raises_valueerror_on_pure_prose():
    with pytest.raises(ValueError):
        repair_output("Looking at the browser state, the site is working properly.", FakeAgentOutput)


def test_repair_raises_validation_error_when_unfixable():
    with pytest.raises(ValidationError):
        repair_output('{"memory": "no actions here at all"}', FakeAgentOutput)


def test_coerce_shape_non_dict_returns_none():
    assert coerce_shape([1, 2], FakeAgentOutput) is None
