"""Skill retrieval and validated planning with a mocked LLM."""

from __future__ import annotations

import json

import pytest

from axiom.actions import EXECUTABLE_ACTIONS
from axiom.ollama_client import OllamaError, extract_json
from axiom.planner import build_plan_from_skill, plan_summary, plan_task, retrieve_skills

VALID_REPLY = json.dumps({
    "inputs": {"post_text": "Python 3.15 removes the GIL by default. Huge."},
    "draft": "Python 3.15 removes the GIL by default. Huge.\n\nAgreed? Comments open.",
})


def test_retrieve_ranks_matching_skill(sample_skill):
    results = retrieve_skills("Create a LinkedIn post about Python 3.15")
    assert results
    assert results[0][0].skill_id == "linkedin_create_post"
    assert results[0][1] > 0.3


def test_retrieve_no_match(sample_skill):
    assert retrieve_skills("order a pepperoni pizza in Naples") == []


def test_plan_uses_llm_and_binds_inputs(sample_skill, mock_llm):
    mock_llm.install(VALID_REPLY)
    plan = run(plan_task("Create a LinkedIn post about Python 3.15"))

    assert plan.status == "ready"
    assert plan.skill_id == "linkedin_create_post"
    assert plan.source == "llm"
    assert plan.model == "test-model"
    assert plan.inputs["post_text"].startswith("Python 3.15")
    assert plan.draft and "GIL" in plan.draft

    # every step stays inside the allowlist
    assert all(s.action in EXECUTABLE_ACTIONS for s in plan.steps)

    # fill value is bound, no placeholder left
    fill = next(s for s in plan.steps if s.action == "fill")
    assert "{{" not in str(fill.parameters["value"])

    # publish is gated and preceded by a review step
    gated_idx = next(i for i, s in enumerate(plan.steps) if s.requires_approval)
    assert plan.steps[gated_idx].title == "Post"
    assert plan.steps[gated_idx - 1].action == "inspect_visible_state"
    assert plan.steps[gated_idx - 1].title.startswith("Review")

    # plan-level summary renders
    text = plan_summary(plan)
    assert "requires approval" in text
    assert "Skill: LinkedIn Create Post" in text


def test_plan_invalid_llm_json_falls_back_to_heuristic(sample_skill, mock_llm):
    mock_llm.install("I cannot produce JSON, but here are thoughts about Python 3.15.")
    plan = run(plan_task("Create a LinkedIn post about Python 3.15"))
    assert plan.status == "ready"
    assert plan.inputs.get("post_text") == "Python 3.15"  # heuristic extraction
    assert any("malformed" in w for w in plan.warnings)


def test_plan_llm_unknown_inputs_ignored(sample_skill, mock_llm):
    mock_llm.install(json.dumps({
        "inputs": {"post_text": "ok", "steal_cookies": "yes", "extra": ["a", "b"]},
        "draft": None,
    }))
    plan = run(plan_task("Create a LinkedIn post"))
    assert "steal_cookies" not in plan.inputs
    assert "extra" not in plan.inputs
    assert any("unknown input" in w for w in plan.warnings)


def test_plan_llm_steps_are_never_trusted(sample_skill, mock_llm):
    # Even if the model invents steps/actions, only its inputs are used.
    mock_llm.install(json.dumps({
        "inputs": {"post_text": "hello"},
        "steps": [{"action": "evaluate_javascript", "code": "alert(1)"}],
    }))
    plan = run(plan_task("Create a LinkedIn post"))
    assert all(s.action in EXECUTABLE_ACTIONS for s in plan.steps)
    assert not any("code" in s.parameters for s in plan.steps)


def test_plan_no_matching_skill(sample_skill, mock_llm):
    mock_llm.install(VALID_REPLY)
    plan = run(plan_task("Book a flight to Tokyo"))
    assert plan.status == "no_skill"
    assert "LinkedIn Create Post" in plan.message  # lists known skills
    assert plan.steps == []


def test_plan_missing_required_inputs(sample_skill, mock_llm):
    mock_llm.install('{"inputs": {}}')
    plan = run(plan_task("Do the post thing"))  # no "about", no quotes -> heuristic fails
    assert plan.status == "needs_inputs"
    assert "post_text" in plan.missing_inputs


def test_plan_llm_down_still_works_with_heuristic(sample_skill, mock_llm):
    mock_llm.install(OllamaError("connection refused"))
    plan = run(plan_task("Create a LinkedIn post about local-first AI agents"))
    assert plan.status == "ready"
    assert plan.inputs["post_text"] == "local-first AI agents"
    assert any("LLM unavailable" in w for w in plan.warnings)


def test_plan_llm_down_missing_inputs_reported(sample_skill, mock_llm):
    mock_llm.install(OllamaError("connection refused"))
    plan = run(plan_task("Create a LinkedIn post"))
    assert plan.status == "needs_inputs"
    assert plan.missing_inputs == ["post_text"]


def test_explicit_inputs_override_llm(sample_skill, mock_llm):
    mock_llm.install(json.dumps({"inputs": {"post_text": "from llm"}, "draft": "from llm"}))
    plan = run(plan_task("Create a LinkedIn post", inputs={"post_text": "from user"}))
    assert plan.inputs["post_text"] == "from user"


def test_use_llm_false_is_pure_deterministic(sample_skill, mock_llm):
    plan = run(plan_task("Create a LinkedIn post", inputs={"post_text": "typed directly"}, use_llm=False))
    assert mock_llm.calls == []  # model never contacted
    assert plan.status == "ready"
    assert plan.source == "deterministic"
    assert all(s.action in EXECUTABLE_ACTIONS for s in plan.steps)


def test_unknown_skill_id_404():
    with pytest.raises(LookupError):
        run(plan_task("whatever", skill_id="nope"))


def test_empty_goal_rejected():
    with pytest.raises(ValueError):
        run(plan_task("   "))


def test_deterministic_plan_direct(sample_skill):
    plan = build_plan_from_skill(sample_skill, "g", {"post_text": "abc"}, source="deterministic")
    assert plan.status == "ready"
    assert plan.skill_version == 1
    assert len(plan.steps) == len(sample_skill.steps) + 1  # + review step before gate


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"inputs": {"x": "y"}} done') == {"inputs": {"x": "y"}}
    assert extract_json("no json here") is None
    assert extract_json("") is None


def run(coro):
    import asyncio

    return asyncio.run(coro)
