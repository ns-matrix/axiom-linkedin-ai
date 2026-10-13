"""Stage C + D: skill retrieval and structured task planning.

Separation of concerns:
  * The LLM only adapts *inputs* (extracting values / drafting content) for a
    chosen skill. It never emits executable steps.
  * Steps come from the stored skill template and are validated against the
    action allowlist before a plan can exist.
  * The plan is a proposal; approval and execution live in sessions.py.
"""

from __future__ import annotations

import re
from typing import Any

from . import skills as skill_store
from .actions import is_consequential, validate_steps
from .ollama_client import OllamaError, chat, extract_json
from .schemas import (
    Locator,
    Plan,
    PlanStep,
    Skill,
    VerificationSpec,
    uid,
)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "is", "are", "be", "me", "my", "please", "can",
    "you", "this", "that", "it", "its", "about", "new", "make", "create",
    "do", "get", "i", "we", "us", "our", "your", "want", "need", "task",
}

MAX_TEXT = 20000


def _tokens(text: str) -> set[str]:
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in raw if t and t not in STOPWORDS and len(t) > 1}


def _skill_tokens(skill: Skill) -> set[str]:
    parts = [skill.name.replace("_", " "), skill.description, " ".join(s.title for s in skill.steps)]
    parts.extend(skill.name.split("_"))
    return _tokens(" ".join(parts))


def retrieve_skills(goal: str, top_k: int = 3) -> list[tuple[Skill, float]]:
    """Deterministic keyword retrieval over saved skills (local, no network)."""
    goal_tokens = _tokens(goal)
    if not goal_tokens:
        return []
    scored: list[tuple[Skill, float]] = []
    for skill in skill_store.list_skills():
        skill_tokens = _skill_tokens(skill)
        if not skill_tokens:
            continue
        overlap = goal_tokens & skill_tokens
        score = len(overlap) / max(len(goal_tokens), 1)
        slug_tokens = set(skill.skill_id.split("_"))
        if slug_tokens and slug_tokens <= _tokens(goal.replace("_", " ")) | goal_tokens:
            score += 0.25
        if overlap:
            scored.append((skill, round(min(score, 1.0), 3)))
    scored.sort(key=lambda item: (-item[1], item[0].name))
    return scored[:top_k]


def _heuristic_inputs(goal: str, skill: Skill) -> dict[str, Any]:
    """Fallback value extraction when the LLM is unavailable or invalid."""
    inputs: dict[str, Any] = {}
    text_inputs = [i for i in skill.input_schema if i.required and i.name not in inputs]
    if not text_inputs:
        return inputs
    quoted = re.findall(r"[\"“']([^\"”']{3,})[\"”']", goal)
    about = re.search(r"\babout\b\s+(.+)$", goal.strip(), re.I)
    candidate = quoted[0] if quoted else (about.group(1).strip() if about else "")
    candidate = candidate.rstrip(".! ")
    if candidate:
        inputs[text_inputs[0].name] = candidate[:MAX_TEXT]
    return inputs


def _sanitize_llm_inputs(raw: Any, skill: Skill) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    inputs: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return inputs, ["LLM inputs were not a JSON object"]
    allowed = {i.name for i in skill.input_schema}
    for key, value in raw.items():
        if key not in allowed:
            errors.append(f"LLM produced unknown input '{key}' (ignored)")
            continue
        if isinstance(value, (dict, list)):
            errors.append(f"input '{key}' must be a scalar (ignored)")
            continue
        if isinstance(value, str) and len(value) > MAX_TEXT:
            errors.append(f"input '{key}' too long (ignored)")
            continue
        if not isinstance(value, (str, int, float, bool)):
            errors.append(f"input '{key}' has unsupported type (ignored)")
            continue
        inputs[key] = value
    return inputs, errors


async def _llm_extract(goal: str, skill: Skill, model: str | None) -> tuple[dict[str, Any], str | None, str | None, list[str]]:
    """Ask the local model to fill skill inputs. Returns (inputs, draft, model, warnings)."""
    schema = [
        {"name": i.name, "type": i.type, "required": i.required, "description": i.description}
        for i in skill.input_schema
    ]
    prompt = (
        "You extract inputs for a recorded browser workflow. You never design actions "
        "or code; steps are fixed by the skill.\n"
        f"Skill: {skill.name}\nDescription: {skill.description}\n"
        f"Inputs: {schema}\n"
        f"User goal: {goal}\n"
        "Return ONLY JSON: {\"inputs\": {<input_name>: <value>, ...}, \"draft\": \"<full draft text if the "
        "goal requires creating content and a text input exists, else null>\"}. "
        f"String values max {MAX_TEXT} characters. Omit inputs you cannot infer."
    )
    try:
        content, used_model = await chat(
            [{"role": "system", "content": "Respond with strict JSON only."}, {"role": "user", "content": prompt}],
            model=model,
        )
    except OllamaError as exc:
        return {}, None, None, [f"LLM unavailable, using heuristic inputs: {exc}"]

    parsed = extract_json(content)
    if not isinstance(parsed, dict):
        return {}, None, used_model, ["LLM returned malformed JSON; plan built deterministically"]

    inputs, errors = _sanitize_llm_inputs(parsed.get("inputs"), skill)
    warnings = list(errors)
    draft = parsed.get("draft")
    if draft is not None and (not isinstance(draft, str) or len(draft) > MAX_TEXT):
        warnings.append("LLM draft rejected (bad type or too long)")
        draft = None
    return inputs, draft if isinstance(draft, str) else None, used_model, warnings


def _resolve_parameters(skill_step_value: Any, inputs: dict[str, Any], missing: list[str]) -> Any:
    """Replace {{variable}} placeholders with approved input values."""

    def repl_str(text: str) -> str:
        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in inputs:
                return str(inputs[name])
            if name not in missing:
                missing.append(name)
            return ""

        return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", sub, text)

    if isinstance(skill_step_value, str):
        return repl_str(skill_step_value)
    if isinstance(skill_step_value, dict):
        return {k: _resolve_parameters(v, inputs, missing) for k, v in skill_step_value.items()}
    if isinstance(skill_step_value, list):
        return [_resolve_parameters(v, inputs, missing) for v in skill_step_value]
    return skill_step_value


def _to_plan_step(step, inputs: dict[str, Any], missing: list[str]) -> PlanStep:
    parameters = _resolve_parameters(dict(step.parameters or {}), inputs, missing)
    if step.action == "fill" and "value" in parameters and not str(parameters["value"]).strip():
        if step.variable and step.variable not in missing:
            missing.append(step.variable)
    target = None
    if step.target:
        target = [Locator.model_validate(loc.model_dump()) for loc in step.target]
    return PlanStep(
        step_id=step.step_id,
        action=step.action,  # type: ignore[arg-type]
        title=step.title,
        target=target,
        parameters=parameters,
        preconditions=list(step.preconditions),
        expected_outcome=step.expected_outcome,
        verification_method=step.verification.method,  # type: ignore[arg-type]
        verification=VerificationSpec.model_validate(step.verification.model_dump()),
        risk_level=step.risk_level,
        requires_approval=bool(step.requires_approval) or is_consequential(step),
        timeout=step.timeout,
        failure_policy=step.failure_policy,
    )


def build_plan_from_skill(
    skill: Skill,
    goal: str,
    inputs: dict[str, Any],
    draft: str | None = None,
    source: str = "deterministic",
    model: str | None = None,
    warnings: list[str] | None = None,
) -> Plan:
    warnings = list(warnings or [])
    missing: list[str] = []
    plan_steps: list[PlanStep] = []
    saw_fill = False
    last_fill_targets: list[Locator] | None = None
    draft_review_inserted = False

    for step in skill.steps:
        plan_step = _to_plan_step(step, inputs, missing)
        needs_draft_review = (
            plan_step.requires_approval
            and saw_fill
            and not draft_review_inserted
            and last_fill_targets
        )
        if needs_draft_review:
            plan_steps.append(
                PlanStep(
                    step_id=f"{plan_step.step_id}_review",
                    action="inspect_visible_state",
                    title="Review the prepared content before the gated action",
                    parameters={"checks": [{"target": [loc.model_dump() for loc in last_fill_targets], "state": "visible"}]},
                    preconditions=["The prepared content is visible on screen"],
                    expected_outcome="The user can see and confirm the content that will be submitted",
                    verification_method="element_visible",
                    verification=VerificationSpec(method="element_visible", target=last_fill_targets, timeout=8),
                    risk_level="low",
                    requires_approval=False,
                    timeout=15,
                    failure_policy="abort",
                )
            )
            draft_review_inserted = True
        plan_steps.append(plan_step)
        if plan_step.action in ("fill", "select"):
            saw_fill = True
            if plan_step.target:
                last_fill_targets = plan_step.target

    # Required inputs that stayed empty.
    for spec in skill.input_schema:
        if spec.required and spec.name not in inputs and spec.default in (None, ""):
            if spec.name not in missing:
                missing.append(spec.name)

    unique_missing = [m for m in dict.fromkeys(missing)]
    errors = validate_steps(plan_steps)
    if errors:
        warnings.extend(errors)
        status = "needs_inputs"
    elif unique_missing:
        status = "needs_inputs"
    else:
        status = "ready"

    resolved_draft = draft
    if resolved_draft is None:
        for name, value in inputs.items():
            if isinstance(value, str) and len(value) > 40 and name in {s.variable for s in skill.steps if s.variable}:
                resolved_draft = value
                break

    return Plan(
        goal=goal,
        skill_id=skill.skill_id,
        skill_name=skill.name,
        skill_version=skill.version,
        inputs=inputs,
        draft=resolved_draft,
        steps=plan_steps,
        source=source,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        missing_inputs=unique_missing,
        message="" if status == "ready" else f"Missing required inputs: {', '.join(unique_missing)}",
        warnings=warnings,
        model=model,
    )


async def plan_task(
    goal: str,
    skill_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    model: str | None = None,
    use_llm: bool = True,
) -> Plan:
    """Select a skill, adapt it to the request, and produce a validated plan."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal is required")

    supplied = dict(inputs or {})
    warnings: list[str] = []
    skill: Skill | None = None

    if skill_id:
        skill = skill_store.get_skill(skill_id)
        if skill is None:
            raise LookupError(f"skill '{skill_id}' not found")
    else:
        candidates = retrieve_skills(goal)
        if not candidates:
            known = ", ".join(s.name for s in skill_store.list_skills()) or "none"
            return Plan(
                goal=goal,
                status="no_skill",
                message=f"No saved skill matches this request. Saved skills: {known}.",
                warnings=warnings,
            )
        skill, score = candidates[0]
        warnings.append(f"Selected skill '{skill.name}' (match {score:.2f})")

    llm_inputs: dict[str, Any] = {}
    draft: str | None = None
    used_model: str | None = None

    needs_llm = use_llm and any(i.required for i in skill.input_schema)
    if needs_llm:
        try:
            llm_inputs, draft, used_model, llm_warnings = await _llm_extract(goal, skill, model)
            warnings.extend(llm_warnings)
        except Exception as exc:  # defensive: never let planning 500 on LLM quirks
            llm_inputs, draft, used_model = {}, None, None
            warnings.append(f"LLM extraction failed: {exc}")
        if not llm_inputs and not draft:
            llm_inputs = _heuristic_inputs(goal, skill)
            if llm_inputs:
                warnings.append("Inputs filled by local heuristic (LLM produced nothing usable).")

    merged = dict(llm_inputs)
    merged.update(supplied)  # explicit user/API inputs always win

    if draft is None:
        for value in merged.values():
            if isinstance(value, str) and len(value) > 60:
                draft = value
                break

    return build_plan_from_skill(
        skill,
        goal,
        merged,
        draft=draft,
        source="llm" if (llm_inputs or draft) and used_model else "deterministic",
        model=used_model,
        warnings=warnings,
    )


def plan_summary(plan: Plan) -> str:
    """Human-readable plan text used by chat / MCP responses."""
    if plan.status == "no_skill":
        return plan.message
    lines = [
        f"Plan for: {plan.goal}",
        f"Skill: {plan.skill_name} v{plan.skill_version} ({plan.source})",
        f"Status: {plan.status}",
    ]
    if plan.missing_inputs:
        lines.append("Missing inputs: " + ", ".join(plan.missing_inputs))
    if plan.draft:
        lines.append("Draft:\n" + plan.draft)
    lines.append("Steps:")
    for i, step in enumerate(plan.steps, 1):
        gate = " [requires approval]" if step.requires_approval else ""
        lines.append(f"  {i}. [{step.action}] {step.title or step.step_id} — verify: {step.verification_method}{gate}")
    for warning in plan.warnings:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)
