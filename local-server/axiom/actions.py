"""Allowlisted browser action schema and validation.

The LLM never emits executable code. Every plan step must map onto one of the
actions below with the expected parameters; anything else is rejected before
it can reach the extension.
"""

from __future__ import annotations

from typing import Any

from .schemas import EXECUTABLE_ACTIONS, PlanStep, RiskLevel, SkillStep

# Which parameters each action accepts. `required` must be present,
# `optional` may be present; unknown parameters are rejected.
ACTION_SPECS: dict[str, dict[str, Any]] = {
    "navigate": {
        "required": ("url",),
        "optional": ("timeout",),
        "idempotent": True,
        "consequential": False,
    },
    "click": {
        "required": ("target",),
        "optional": (),
        "idempotent": False,
        "consequential": False,
    },
    "fill": {
        "required": ("target", "value"),
        "optional": ("clear",),
        "idempotent": True,
        "consequential": False,
    },
    "select": {
        "required": ("target", "value"),
        "optional": (),
        "idempotent": True,
        "consequential": False,
    },
    "scroll": {
        "required": (),
        "optional": ("target", "x", "y"),
        "idempotent": True,
        "consequential": False,
    },
    "wait_for": {
        "required": (),
        "optional": ("target", "timeout", "state", "url"),
        "idempotent": True,
        "consequential": False,
    },
    "inspect_visible_state": {
        "required": (),
        "optional": ("checks",),
        "idempotent": True,
        "consequential": False,
    },
    "request_approval": {
        "required": ("prompt",),
        "optional": ("payload",),
        "idempotent": True,
        "consequential": True,
    },
}

# Labels that mark a browser action as consequential when they *are* the
# button text (LinkedIn's publish button is literally "Post").
CONSEQUENTIAL_LABELS = {
    "post", "publish", "submit", "send", "share", "invite", "connect",
    "delete", "remove", "purchase", "buy", "checkout", "apply", "endorse",
    "confirm", "pay", "easy apply", "send message", "place order",
    "publish now", "post now", "send now",
}

# Phrases that mark an action consequential anywhere in its label/target.
CONSEQUENTIAL_PHRASES = (
    "publish", "submit", "send message", "delete", "purchase", "checkout",
    "withdraw", "confirm order", "place order", "invite", "connect with",
    "endos", "pay ", "share now", "apply now", "post comment", "post reply",
    "send ", "remove ", "apply", "buy now",
)

#: Parameters that never appear in an allowlisted action. Their presence means
#: someone tried to smuggle code execution into a plan.
FORBIDDEN_KEYS = {
    "js",
    "javascript",
    "code",
    "script",
    "eval",
    "shell",
    "cmd",
    "command",
    "exec",
    "expression",
    "html",
    "innerhtml",
    "python",
    "py",
}

MAX_TARGET_LEN = 300
MAX_VALUE_LEN = 20000


def _target_text(target: Any) -> str:
    """Human readable label for a target (list of locators, str or dict)."""
    if isinstance(target, str):
        return target
    if isinstance(target, list):
        parts = []
        for item in target:
            if isinstance(item, dict):
                piece = f"{item.get('strategy')}={item.get('value')}"
                if item.get("alt_value"):
                    piece += f" ({item['alt_value']})"
            elif hasattr(item, "strategy"):
                piece = f"{item.strategy}={item.value}"
                if item.alt_value:
                    piece += f" ({item.alt_value})"
            else:
                piece = str(item)
            parts.append(piece)
        return "; ".join(parts)
    if isinstance(target, dict):
        return f"{target.get('strategy')}={target.get('value')}"
    return str(target)


def _label_consequential(text: str) -> bool:
    norm = " ".join((text or "").lower().split())
    if not norm:
        return False
    if norm in CONSEQUENTIAL_LABELS:
        return True
    return any(phrase in norm for phrase in CONSEQUENTIAL_PHRASES)


def is_consequential(step: PlanStep | SkillStep) -> bool:
    """True when a step needs its own approval directly before execution.

    Checks the step flag, its title and its target labels only. Content values
    (e.g. the text a user is typing) never trigger this — the *action* is what
    needs gating, not what the user wrote.
    """
    if getattr(step, "requires_approval", False):
        return True
    if step.action == "request_approval":
        return True
    if _label_consequential(step.title or ""):
        return True
    return _label_consequential(_target_text(step.target))


def required_risk(step: PlanStep | SkillStep) -> RiskLevel:
    if is_consequential(step):
        return "high"
    if step.action in ("fill", "select"):
        return "medium"
    return "low"


def validate_step(step: PlanStep | SkillStep) -> list[str]:
    """Return a list of problems; empty list means the step is executable."""
    errors: list[str] = []
    action = getattr(step, "action", None)
    if action not in EXECUTABLE_ACTIONS:
        return [f"action '{action}' is not in the allowlist"]

    spec = ACTION_SPECS[action]
    params = dict(step.parameters or {})

    lowered_keys = {str(k).lower() for k in params}
    bad = lowered_keys & FORBIDDEN_KEYS
    if bad:
        errors.append(f"forbidden parameter(s) {sorted(bad)}: code execution is never allowed")

    for key in spec["required"]:
        if key == "target":
            if not step.target and not params.get("target"):
                errors.append(f"'{action}' requires a target locator")
        elif key not in params or params.get(key) in (None, ""):
            errors.append(f"'{action}' requires parameter '{key}'")

    allowed = set(spec["required"]) | set(spec["optional"])
    for key in params:
        if str(key).lower() in FORBIDDEN_KEYS:
            continue
        if key not in allowed:
            errors.append(f"'{action}' does not accept parameter '{key}'")

    url = params.get("url")
    if action == "navigate" and isinstance(url, str):
        if not url.startswith(("http://", "https://")):
            errors.append(f"navigate url must be http(s), got: {url[:60]!r}")
        elif len(url) > 2000:
            errors.append("navigate url too long")

    value = params.get("value")
    if isinstance(value, str) and len(value) > MAX_VALUE_LEN:
        errors.append("value too long")
    if isinstance(value, (dict, list)):
        errors.append("value must be a string or scalar, not a nested object")

    if step.target:
        for loc in step.target if isinstance(step.target, list) else [step.target]:
            text = _target_text(loc)
            if len(text) > MAX_TARGET_LEN:
                errors.append("target locator too long")
            if any(ch in text for ch in ("\n", "\r", "\x00")):
                errors.append("target locator contains control characters")

    if step.timeout is not None and not (1 <= int(step.timeout) <= 120):
        errors.append("timeout must be between 1 and 120 seconds")

    return errors


def validate_steps(steps: list[PlanStep]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for i, step in enumerate(steps, start=1):
        for err in validate_step(step):
            errors.append(f"step {i} ({step.step_id}): {err}")
        if step.step_id in seen:
            errors.append(f"step {i}: duplicate step_id '{step.step_id}'")
        seen.add(step.step_id)
    return errors


def step_action_payload(step: PlanStep) -> dict[str, Any]:
    """Serialize a validated step into the message the extension executes."""
    payload: dict[str, Any] = {
        "step_id": step.step_id,
        "action": step.action,
        "parameters": dict(step.parameters or {}),
        "timeout": step.timeout,
        "verification": step.verification.model_dump(mode="json"),
        "expected_outcome": step.expected_outcome,
        "risk_level": step.risk_level,
        "requires_approval": bool(step.requires_approval) or is_consequential(step),
    }
    if step.target:
        payload["target"] = [loc.model_dump(mode="json") for loc in step.target]
    return payload
