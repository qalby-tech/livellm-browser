"""Tests for the auto-mode failure-policy parsing (planner._parse_replan)."""

import json

from livellm_agent.models import Step, StepStatus
from livellm_agent.planner import _parse_replan, _plan_state


# ── steps shape ──────────────────────────────────────────────────────────────

def test_parse_steps_shape():
    out = _parse_replan('{"steps": ["ask for address", "add item to cart"]}')
    assert out == {"steps": ["ask for address", "add item to cart"]}


def test_parse_steps_strips_and_drops_empties():
    out = _parse_replan('{"steps": ["  open site  ", "", "   ", "extract"]}')
    assert out == {"steps": ["open site", "extract"]}


def test_parse_steps_coerces_non_strings():
    out = _parse_replan('{"steps": [1, "two"]}')
    assert out == {"steps": ["1", "two"]}


def test_parse_empty_steps_is_none():
    assert _parse_replan('{"steps": []}') is None


# ── need_human shape ─────────────────────────────────────────────────────────

def test_parse_need_human_shape():
    out = _parse_replan('{"need_human": "What is your delivery address?"}')
    assert out == {"need_human": "What is your delivery address?"}


def test_parse_need_human_wins_over_steps():
    out = _parse_replan('{"need_human": "Which time slot?", "steps": ["x"]}')
    assert out == {"need_human": "Which time slot?"}


def test_parse_blank_need_human_falls_back_to_steps():
    out = _parse_replan('{"need_human": "  ", "steps": ["retry checkout"]}')
    assert out == {"steps": ["retry checkout"]}


# ── prose / fences / garbage ─────────────────────────────────────────────────

def test_parse_tolerates_prose_and_fences():
    text = 'Sure, here is my decision:\n```json\n{"need_human": "Login code?"}\n```\nHope that helps.'
    assert _parse_replan(text) == {"need_human": "Login code?"}


def test_parse_tolerates_embedded_json():
    text = 'I think the plan should change. {"steps": ["open cart", "checkout"]} That is all.'
    assert _parse_replan(text) == {"steps": ["open cart", "checkout"]}


def test_parse_garbage_is_none():
    assert _parse_replan("no json here at all") is None
    assert _parse_replan("") is None
    assert _parse_replan(None) is None
    assert _parse_replan("[1, 2, 3]") is None  # not an object
    assert _parse_replan('{"answer": 42}') is None  # neither shape
    assert _parse_replan('{"need_human": 5}') is None  # wrong types
    assert _parse_replan('{"steps": "not a list"}') is None


# ── prompt state rendering ───────────────────────────────────────────────────

def test_plan_state_marks_failure_and_evidence():
    steps = [
        Step(idx=0, intent="open vkusvill.ru", status=StepStatus.done,
             action_json={"output": "opened the homepage", "success": True}),
        Step(idx=1, intent="add milk to cart", status=StepStatus.failed,
             action_json={"output": "a delivery-address modal blocked add-to-cart",
                          "success": False, "error": "reached step limit"}),
        Step(idx=2, intent="checkout", status=StepStatus.pending),
    ]
    txt = _plan_state(steps, steps[1])
    assert "1. [done] open vkusvill.ru" in txt
    assert "opened the homepage" in txt
    assert "2. [failed] add milk to cart" in txt and "FAILED" in txt
    assert "delivery-address modal" in txt
    assert "reached step limit" in txt
    assert "3. [pending] checkout" in txt
