"""Stage B: normalize raw recording events into a versioned, reusable skill.

The pipeline validates the event sequence, drops duplicates, groups events
into logical steps, identifies inputs/variables and expected outcomes,
detects brittle locators, and produces a human-readable summary.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .actions import is_consequential, required_risk, validate_step
from .schemas import (
    ElementMeta,
    Locator,
    Recording,
    RecordingEvent,
    RiskLevel,
    Skill,
    SkillInput,
    SkillStep,
    VerificationSpec,
    now_iso,
    uid,
)
from . import store

GENERIC_TOKENS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "is", "this", "that", "click", "input", "page",
}

#: CSS patterns that usually break between sessions.
BRITTLE_CSS_PATTERNS = (
    re.compile(r":nth-child\("),
    re.compile(r"#ember\d+", re.I),
    re.compile(r"react-aria", re.I),
    re.compile(r":r[0-9a-z]{4,}", re.I),
    re.compile(r"radix", re.I),
    re.compile(r"chakra", re.I),
    re.compile(r"\d{5,}"),  # generated numeric ids
    re.compile(r"\[[^\]]*style=", re.I),
)

_SUCCESS_HINTS = (
    "success", "published", "posted", "saved", "done", "complete",
    "confirmation", "toast", "notify", "flash__alert",
)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "skill"


def _locator_score(loc: Locator) -> tuple[int, int]:
    order = {"testid": 0, "role": 1, "label": 2, "placeholder": 3, "name": 4, "text": 5, "url": 6, "css": 7}
    return (1 if loc.brittle else 0, order.get(loc.strategy, 9))


def best_locators(meta: ElementMeta | None, limit: int = 4) -> list[Locator] | None:
    if not meta or not meta.locators:
        return None
    locs = sorted(meta.locators, key=_locator_score)[:limit]
    return locs or None


def _is_brittle(loc: Locator) -> bool:
    if loc.brittle:
        return True
    if loc.strategy != "css":
        return False
    return any(p.search(loc.value) for p in BRITTLE_CSS_PATTERNS)


def _access_name(meta: ElementMeta | None) -> str:
    if not meta:
        return ""
    return (meta.name or "").strip()


def _var_name(meta: ElementMeta | None, existing: set[str]) -> str:
    base_source = ""
    if meta:
        for key in ("name", "placeholder", "aria-label", "label", "id"):
            val = (meta.attrs.get(key) or "").strip()
            if val and not val.startswith(("http://", "https://")):
                base_source = val
                break
        if not base_source:
            base_source = meta.name or ""
    base = slugify(base_source) if base_source else "text_value"
    if not base or base == "text_value":
        base = "text_value"
    base = base[:40].strip("_") or "text_value"
    name = base
    n = 2
    while name in existing:
        name = f"{base}_{n}"
        n += 1
    existing.add(name)
    return name


def _event_label(event: RecordingEvent) -> str:
    meta = event.element
    if not meta:
        return ""
    return _access_name(meta) or meta.attrs.get("placeholder", "") or meta.attrs.get("title", "") or ""


def _event_risk(event: RecordingEvent) -> RiskLevel:
    if event.action_type == "submit":
        return "high"
    if event.action_type in ("fill", "select"):
        return "medium"
    label = _event_label(event)
    locs = best_locators(event.element)
    synthetic = SkillStep(step_id="risk_probe", title=label, action="click", target=locs, parameters={})
    if is_consequential(synthetic):
        return "high"
    return "low"


def _dedupe(events: list[RecordingEvent]) -> list[RecordingEvent]:
    kept: list[RecordingEvent] = []
    for event in events:
        if kept:
            prev = kept[-1]
            same_target = _locator_key(prev) == _locator_key(event)
            close_in_time = abs(event.timestamp - prev.timestamp) < 0.75
            if same_target and event.action_type == prev.action_type and close_in_time:
                # keep the richer of the two (e.g. later fill with text)
                if event.action_type == "fill" and (event.value or event.value_length):
                    kept[-1] = event
                continue
            if event.action_type == "scroll" and prev.action_type == "scroll" and close_in_time:
                kept[-1] = event
                continue
        kept.append(event)
    return kept


def _locator_key(event: RecordingEvent) -> str:
    locs = best_locators(event.element, limit=1)
    if locs:
        return f"{locs[0].strategy}:{locs[0].value}"
    if event.element:
        return f"{event.element.tag}:{_access_name(event.element)}"
    return event.url


def _expected_for_click(events: list[RecordingEvent], index: int) -> tuple[str, VerificationSpec]:
    """Heuristic: a click usually makes the *next* interaction's element appear."""
    if index + 1 < len(events):
        nxt = events[index + 1]
        if nxt.action_type in ("fill", "select", "click", "submit") and nxt.element and nxt.element.locators:
            locs = best_locators(nxt.element) or []
            if locs:
                verb = {"fill": "editable", "select": "selectable"}.get(nxt.action_type, "visible")
                return (
                    f"Element '{_access_name(nxt.element) or locs[0].value}' becomes {verb}",
                    VerificationSpec(method="element_visible", target=locs, timeout=8),
                )
        if nxt.url and nxt.url != events[index].url:
            return (
                f"Navigated to {nxt.url}",
                VerificationSpec(method="url_match", expect=nxt.url, timeout=10),
            )
    label = _event_label(events[index])
    if label:
        return (f"Action on '{label}' accepted by the page", VerificationSpec(method="none"))
    return ("Click accepted by the page", VerificationSpec(method="none"))


def _group_steps(events: list[RecordingEvent]) -> list[SkillStep]:
    steps: list[SkillStep] = []
    var_pool: set[str] = set()
    i = 0
    while i < len(events):
        event = events[i]

        if event.action_type == "wait":
            i += 1
            continue

        if event.action_type == "scroll":
            group = [event]
            j = i + 1
            while j < len(events) and events[j].action_type == "scroll" and events[j].timestamp - events[j - 1].timestamp < 1.5:
                group.append(events[j])
                j += 1
            steps.append(
                SkillStep(
                    step_id=uid("st"),
                    title="Scroll to reveal content",
                    action="scroll",
                    parameters={"y": int(group[-1].meta.get("scrollY", group[-1].meta.get("y", 600)) or 600)},
                    expected_outcome="Relevant content is scrolled into view",
                    verification=VerificationSpec(method="none"),
                    risk_level="low",
                    timeout=10,
                    failure_policy="retry_safe",
                    source_event_ids=[e.event_id for e in group],
                )
            )
            i = j
            continue

        if event.action_type == "navigate":
            steps.append(
                SkillStep(
                    step_id=uid("st"),
                    title=f"Open {event.url or event.origin}",
                    action="navigate",
                    parameters={"url": event.url or event.origin},
                    expected_outcome=f"Page {event.url or event.origin} is loaded",
                    verification=VerificationSpec(method="url_match", expect=event.url or event.origin, timeout=15),
                    risk_level="low",
                    timeout=20,
                    failure_policy="retry_safe",
                    source_event_ids=[event.event_id],
                )
            )
            i += 1
            continue

        if event.action_type in ("fill", "select"):
            meta = event.element
            var = event.variable or _var_name(meta, var_pool)
            locs = best_locators(meta)
            value = event.value if event.consent_text_recorded else None
            length = event.value_length or (len(event.value) if event.value else 0)
            if event.action_type == "fill":
                params: dict[str, Any] = {"value": f"{{{{{var}}}}}"}
                verification = VerificationSpec(method="value_present", target=locs, timeout=6)
                expected = f"Field for '{var}' contains the provided text (length checked, content not logged)"
            else:
                params = {"value": event.value if (event.consent_text_recorded and event.value) else f"{{{{{var}}}}}"}
                verification = VerificationSpec(method="element_present", target=locs, timeout=6)
                expected = f"Control for '{var}' is set"
            steps.append(
                SkillStep(
                    step_id=uid("st"),
                    title=f"Enter {var.replace('_', ' ')}",
                    action="fill" if event.action_type == "fill" else "select",
                    target=locs,
                    parameters=params,
                    expected_outcome=expected,
                    verification=verification,
                    risk_level="medium" if event.action_type == "fill" else "low",
                    timeout=15,
                    failure_policy="retry_safe",
                    variable=var,
                    source_event_ids=[event.event_id],
                )
            )
            # remember metadata for input schema assembly
            event.meta.setdefault(
                "_var_hint",
                {"name": var, "label": _access_name(meta),
                 "length": length, "example": value if event.consent_text_recorded else None},
            )
            i += 1
            continue

        if event.action_type in ("click", "submit"):
            label = _event_label(event) or ("Submit form" if event.action_type == "submit" else "Click element")
            locs = best_locators(event.element)
            expected, verification = _expected_for_click(events, i)
            risk = _event_risk(event)
            consequential = is_consequential(
                SkillStep(step_id="x", title=label, action="click", target=locs, parameters={}, requires_approval=(risk == "high"))
            )
            steps.append(
                SkillStep(
                    step_id=uid("st"),
                    title=label[:80],
                    action="click",
                    target=locs,
                    parameters={},
                    expected_outcome=expected,
                    verification=verification,
                    risk_level=risk,
                    requires_approval=consequential or risk == "high",
                    timeout=15,
                    failure_policy="abort",
                    source_event_ids=[event.event_id],
                )
            )
            i += 1
            continue

        i += 1
    return steps


def _max_risk(steps: list[SkillStep]) -> RiskLevel:
    levels = {"low": 0, "medium": 1, "high": 2}
    if not steps:
        return "low"
    return max((s.risk_level for s in steps), key=lambda r: levels.get(r, 0))  # type: ignore[return-value]


def build_skill(
    recording: Recording,
    name: str,
    description: str = "",
    skill_id: str | None = None,
) -> Skill:
    """Stage B pipeline: recording -> validated, parameterized skill."""
    warnings: list[str] = []
    raw = sorted(recording.events, key=lambda e: (e.seq, e.timestamp))
    if not raw:
        raise ValueError("recording has no events")

    for event in raw:
        if event.action_type not in ("navigate", "click", "fill", "select", "scroll", "submit", "wait"):
            raise ValueError(f"unsupported action type in recording: {event.action_type}")
        if event.element and event.element.sensitive:
            raise ValueError("recording contains a sensitive element (password field); refusing to save")

    # Fill events carry a variable hint the recorder may have proposed.
    used_vars: set[str] = set()
    for event in raw:
        if event.action_type in ("fill", "select") and not event.variable:
            event.variable = _var_name(event.element, used_vars)

    events = _dedupe(raw)
    dropped = len(raw) - len(events)
    if dropped:
        warnings.append(f"Removed {dropped} duplicate/no-op event(s).")

    # Infer waits from long gaps between events.
    with_waits: list[RecordingEvent] = []
    for idx, event in enumerate(events):
        if idx > 0 and event.timestamp - events[idx - 1].timestamp > 3.0:
            with_waits.append(
                RecordingEvent(
                    session_id=event.session_id,
                    action_type="wait",
                    timestamp=event.timestamp - 1.0,
                    url=event.url,
                    origin=event.origin,
                    meta={"inferred": True, "gap_seconds": round(event.timestamp - events[idx - 1].timestamp, 1)},
                )
            )
        with_waits.append(event)
    events = with_waits

    steps = _group_steps(events)

    # Steps that must target an element but never captured one cannot be replayed.
    unlocatable = [s for s in steps if s.action in ("click", "fill", "select") and not s.target]
    if unlocatable:
        warnings.append(
            "Dropped " + str(len(unlocatable)) + " step(s) without a usable locator: "
            + "; ".join(s.title for s in unlocatable[:3])
        )
        steps = [s for s in steps if s.action not in ("click", "fill", "select") or s.target]
    if not steps:
        raise ValueError("recording produced no executable steps (no usable locators found)")

    # Brittle locator audit.
    brittle_count = 0
    for step in steps:
        if step.target:
            for loc in step.target:
                if _is_brittle(loc):
                    loc.brittle = True
                    brittle_count += 1
            if all(l.brittle for l in step.target):
                warnings.append(f"Step '{step.title}' only has brittle locators; re-record if it breaks.")
    if brittle_count:
        warnings.append(f"{brittle_count} brittle locator(s) detected (generated ids / nth-child).")

    # Fill steps whose value is a variable placeholder become required inputs.
    inputs: list[SkillInput] = []
    seen_vars: set[str] = set()
    for step in steps:
        if step.action in ("fill", "select") and step.variable and step.variable not in seen_vars:
            seen_vars.add(step.variable)
            example = None
            for event in events:
                if event.variable == step.variable and event.consent_text_recorded and event.value:
                    example = event.value[:500]
                    break
            inputs.append(
                SkillInput(
                    name=step.variable,
                    type="text",
                    required=True,
                    description=f"Value for '{step.variable.replace('_', ' ')}'.",
                    example=example,
                )
            )
            # Bind the parameter to the variable placeholder.
            step.parameters["value"] = f"{{{{{step.variable}}}}}"

    # Route fill/select values through inputs; drop recorded secrets defensively.
    for step in steps:
        for key in list(step.parameters):
            if key.lower() in ("password", "token", "cookie", "secret"):
                del step.parameters[key]
                warnings.append(f"Removed sensitive parameter '{key}' from step '{step.title}'.")

    approval_reqs = [
        f"{s.step_id}: {s.title}" for s in steps if s.requires_approval or is_consequential(s)
    ]
    if approval_reqs:
        warnings.append("These steps need explicit approval every run: " + "; ".join(approval_reqs))

    origin = recording.origin or (raw[0].origin if raw else "")
    preconditions = [
        f"The page on {origin or 'the target site'} is open in the active tab",
        "The user is signed in and the intended account is correct",
        "The user has reviewed and approved this plan for this run",
    ]
    if "linkedin" in origin.lower():
        preconditions.insert(0, "LinkedIn is open and the user is authenticated")

    # Validation pass: every step must survive the allowlist check.
    step_errors: list[str] = []
    for step in steps:
        step_errors.extend(f"{step.title}: {e}" for e in validate_step(step))
    if step_errors:
        warnings.extend(step_errors[:5])

    expected_outcomes = [s.expected_outcome for s in steps if s.expected_outcome]
    verification_rules = [s.verification for s in steps if s.verification.method != "none"]

    summary_lines = [f"Skill '{name}' ({len(steps)} steps, risk: {_max_risk(steps)}):"]
    for n, step in enumerate(steps, 1):
        gate = " [approval required]" if step.requires_approval else ""
        summary_lines.append(f"  {n}. {step.title} — {step.action}{gate}")
    summary = "\n".join(summary_lines)

    sid = skill_id or slugify(name)
    existing = store.load_model("skills", sid, Skill)

    skill = Skill(
        skill_id=sid,
        name=name.strip() or "Untitled skill",
        description=description.strip() or summary.splitlines()[0],
        version=(existing.version + 1) if existing else 1,
        source_recording_id=recording.recording_id,
        input_schema=inputs,
        preconditions=preconditions,
        steps=steps,
        expected_outcomes=expected_outcomes,
        verification_rules=verification_rules,
        risk_level=_max_risk(steps),
        approval_requirements=approval_reqs,
        summary=summary,
        warnings=warnings,
        created_at=existing.created_at if existing else now_iso(),
        updated_at=now_iso(),
    )
    if existing:
        store.archive_model("skills", sid, existing.version)
    store.save_model("skills", skill)
    return skill


def get_skill(skill_id: str) -> Skill | None:
    return store.load_model("skills", skill_id, Skill)


def list_skills() -> list[Skill]:
    return sorted(store.list_models("skills", Skill), key=lambda s: s.name.lower())


def delete_skill(skill_id: str) -> bool:
    return store.delete_model("skills", skill_id)


def update_skill(skill_id: str, changes: dict[str, Any]) -> Skill | None:
    """Rename / edit a skill. Any change bumps the version."""
    skill = get_skill(skill_id)
    if skill is None:
        return None
    allowed = {"name", "description", "input_schema", "steps", "preconditions", "summary", "warnings"}
    changed = False
    for key, value in changes.items():
        if key not in allowed or value is None:
            continue
        if key == "input_schema":
            skill.input_schema = [SkillInput.model_validate(v) for v in value]
        elif key == "steps":
            skill.steps = [SkillStep.model_validate(v) for v in value]
        elif key == "preconditions":
            skill.preconditions = [str(v) for v in value]
        else:
            setattr(skill, key, value)
        changed = True
    if not changed:
        return skill
    store.archive_model("skills", skill_id, skill.version)
    skill.version += 1
    skill.updated_at = now_iso()
    store.save_model("skills", skill)
    return skill
