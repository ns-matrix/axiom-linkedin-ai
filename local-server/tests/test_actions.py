"""Action allowlisting and validation rules."""

from __future__ import annotations

from axiom.actions import (
    ACTION_SPECS,
    is_consequential,
    required_risk,
    step_action_payload,
    validate_step,
    validate_steps,
)
from axiom.schemas import EXECUTABLE_ACTIONS, Locator, PlanStep


def step(action="navigate", params=None, target=None, title="", requires_approval=False, timeout=20):
    return PlanStep(
        step_id="st_1",
        action=action,  # type: ignore[arg-type]
        title=title,
        parameters=params if params is not None else {},
        target=target,
        requires_approval=requires_approval,
        timeout=timeout,
    )


def test_allowlist_is_exactly_the_supported_set():
    assert set(EXECUTABLE_ACTIONS) == set(ACTION_SPECS)
    assert EXECUTABLE_ACTIONS == (
        "navigate", "click", "fill", "select", "scroll",
        "wait_for", "inspect_visible_state", "request_approval",
    )


def test_rejects_unknown_action():
    bad = PlanStep.model_construct(step_id="st_1", action="evaluate_javascript", parameters={}, timeout=20)
    assert any("not in the allowlist" in e for e in validate_step(bad))


def test_rejects_code_execution_parameters():
    bad = step(action="click", target=[Locator(strategy="role", value="button", alt_value="Go")],
               params={"js": "document.body.remove()"})
    assert any("forbidden" in e for e in validate_step(bad))
    for key in ("shell", "code", "eval", "command"):
        s = step(action="navigate", params={"url": "https://x.com", key: "rm -rf /"})
        assert any("forbidden" in e for e in validate_step(s))


def test_rejects_non_http_navigation():
    errors = validate_step(step(action="navigate", params={"url": "javascript:alert(1)"}))
    assert any("http(s)" in e for e in errors)


def test_requires_target_for_click():
    errors = validate_step(step(action="click"))
    assert any("target" in e for e in errors)


def test_requires_value_for_fill():
    target = [Locator(strategy="role", value="textbox", alt_value="About")]
    assert any("value" in e for e in validate_step(step(action="fill", target=target, params={})))
    assert validate_step(step(action="fill", target=target, params={"value": "hello"})) == []


def test_rejects_unknown_parameters():
    errors = validate_step(step(action="navigate", params={"url": "https://x.com", "surprise": 1}))
    assert any("does not accept" in e for e in errors)


def test_timeout_bounds():
    assert validate_step(step(action="scroll", timeout=500))
    assert validate_step(step(action="scroll", timeout=0))


def test_consequential_detection():
    publish = step(action="click", title="Post", target=[Locator(strategy="role", value="button", alt_value="Post")])
    assert is_consequential(publish)
    assert required_risk(publish) == "high"

    scroll = step(action="scroll", params={"y": 300})
    assert not is_consequential(scroll)
    assert required_risk(scroll) == "low"

    gated = step(action="click", requires_approval=True)
    assert is_consequential(gated)


def test_request_approval_is_always_consequential():
    s = step(action="request_approval", params={"prompt": "Publish now?"})
    assert is_consequential(s)


def test_validate_steps_reports_duplicate_ids():
    a = step(action="scroll", params={"y": 1})
    b = step(action="scroll", params={"y": 2})
    b.step_id = "st_1"
    errors = validate_steps([a, b])
    assert any("duplicate step_id" in e for e in errors)


def test_step_action_payload_shape():
    s = step(action="fill", target=[Locator(strategy="placeholder", value="Type here")],
             params={"value": "hello"})
    payload = step_action_payload(s)
    assert payload["action"] == "fill"
    assert payload["parameters"]["value"] == "hello"
    assert payload["target"][0]["strategy"] == "placeholder"
    assert payload["requires_approval"] is False
    assert "token" not in payload
