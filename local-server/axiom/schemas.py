"""Data models for recordings, skills, plans and supervised sessions."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

RiskLevel = Literal["low", "medium", "high"]
FailurePolicy = Literal["abort", "continue", "retry_safe"]

#: Actions the extension is allowed to execute. Anything else is rejected.
EXECUTABLE_ACTIONS = (
    "navigate",
    "click",
    "fill",
    "select",
    "scroll",
    "wait_for",
    "inspect_visible_state",
    "request_approval",
)

#: Action types the recorder may capture. `submit` is normalized to a click
#: step during skill creation.
RECORDING_ACTION_TYPES = (
    "navigate",
    "click",
    "fill",
    "select",
    "scroll",
    "submit",
    "wait",
)

StepStatus = Literal[
    "pending",
    "running",
    "awaiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "verification_inconclusive",
]

SessionState = Literal[
    "created",
    "awaiting_approval",
    "ready",
    "running",
    "paused",
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
    "verification_inconclusive",
]

TERMINAL_STATES = {
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
    "verification_inconclusive",
}


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_url(url: str) -> str:
    """Strip query strings and fragments; only http/https origins survive."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class Locator(BaseModel):
    """An ordered, redundant way to find an element on a page."""

    strategy: Literal["testid", "role", "label", "placeholder", "name", "text", "css", "url"]
    value: str
    alt_value: str | None = None
    priority: int = 50
    brittle: bool = False
    note: str | None = None

    @field_validator("value")
    @classmethod
    def _limit(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("locator value is required")
        if len(v) > 300:
            raise ValueError("locator value too long (max 300 chars)")
        return v


class ElementMeta(BaseModel):
    tag: str = "unknown"
    role: str | None = None
    name: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)
    locators: list[Locator] = Field(default_factory=list)
    sensitive: bool = False


class RecordingEvent(BaseModel):
    """A single normalized event captured by the recorder."""

    event_id: str = Field(default_factory=lambda: uid("evt"))
    session_id: str = ""
    seq: int = 0
    timestamp: float = Field(default_factory=time.time)
    action_type: Literal["navigate", "click", "fill", "select", "scroll", "submit", "wait"]
    origin: str = ""
    url: str = ""
    element: ElementMeta | None = None
    value: str | None = None
    value_length: int | None = None
    variable: str | None = None
    expected_outcome: str | None = None
    source: Literal["recorder", "imported", "demo"] = "recorder"
    confidence: float = 1.0
    requires_approval: bool = False
    consent_text_recorded: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return sanitize_url(v)

    @field_validator("origin")
    @classmethod
    def _strip_origin(cls, v: str) -> str:
        return sanitize_url(v)


class Recording(BaseModel):
    recording_id: str = Field(default_factory=lambda: uid("rec"))
    name: str = "Untitled recording"
    origin: str = ""
    created_at: str = Field(default_factory=now_iso)
    status: Literal["open", "stopped", "saved", "deleted"] = "stopped"
    events: list[RecordingEvent] = Field(default_factory=list)


class SkillInput(BaseModel):
    name: str
    type: Literal["string", "text", "number", "boolean", "path", "url"] = "string"
    required: bool = True
    description: str = ""
    default: Any = None
    example: str | None = None
    secret: bool = False


class VerificationSpec(BaseModel):
    method: Literal[
        "element_present",
        "element_visible",
        "value_present",
        "url_match",
        "text_present",
        "none",
    ] = "element_present"
    target: list[Locator] | None = None
    expect: str | None = None
    timeout: int = 8


class SkillStep(BaseModel):
    step_id: str
    title: str
    action: Literal[
        "navigate",
        "click",
        "fill",
        "select",
        "scroll",
        "wait_for",
        "inspect_visible_state",
        "request_approval",
    ]
    target: list[Locator] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    risk_level: RiskLevel = "low"
    requires_approval: bool = False
    timeout: int = 20
    failure_policy: FailurePolicy = "abort"
    variable: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    skill_id: str
    name: str
    description: str = ""
    version: int = 1
    source_recording_id: str | None = None
    input_schema: list[SkillInput] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    verification_rules: list[VerificationSpec] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    approval_requirements: list[str] = Field(default_factory=list)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class PlanStep(BaseModel):
    step_id: str
    action: Literal[
        "navigate",
        "click",
        "fill",
        "select",
        "scroll",
        "wait_for",
        "inspect_visible_state",
        "request_approval",
    ]
    title: str = ""
    target: list[Locator] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    verification_method: Literal[
        "element_present",
        "element_visible",
        "value_present",
        "url_match",
        "text_present",
        "none",
    ] = "none"
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    risk_level: RiskLevel = "low"
    requires_approval: bool = False
    timeout: int = 20
    failure_policy: FailurePolicy = "abort"


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uid("plan"))
    goal: str
    skill_id: str | None = None
    skill_name: str | None = None
    skill_version: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    draft: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)
    source: Literal["llm", "deterministic", "deterministic_fallback"] = "deterministic"
    status: Literal["no_skill", "needs_inputs", "ready", "approved", "rejected"] = "ready"
    missing_inputs: list[str] = Field(default_factory=list)
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    created_at: str = Field(default_factory=now_iso)


class StepRuntime(BaseModel):
    step: PlanStep
    index: int = 0
    status: StepStatus = "pending"
    attempts: int = 0
    approved: bool = False
    claim_token: str | None = None
    claim_issued_at: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: uid("ses"))
    plan: Plan
    state: SessionState = "created"
    steps: list[StepRuntime] = Field(default_factory=list)
    current_index: int = 0
    pending_approval: dict[str, Any] | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    execution_tab_id: int | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
