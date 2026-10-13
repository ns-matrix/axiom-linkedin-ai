"""Stage A/B: recording normalization and skill creation/versioning."""

from __future__ import annotations

import pytest

from axiom.schemas import Locator, Recording, sanitize_url
from axiom.skills import build_skill, delete_skill, get_skill, list_skills, update_skill

from conftest import element, event, loc


def test_sanitize_url_strips_query_and_fragment():
    assert sanitize_url("https://x.com/a?token=1#frag") == "https://x.com/a"
    assert sanitize_url("javascript:alert(1)") == ""
    assert sanitize_url("chrome://extensions") == ""


def test_build_skill_from_recording(sample_recording):
    skill = build_skill(sample_recording, "LinkedIn Create Post", "Create and publish a LinkedIn post")

    assert skill.skill_id == "linkedin_create_post"
    assert skill.version == 1
    assert skill.source_recording_id == sample_recording.recording_id

    actions = [s.action for s in skill.steps]
    assert actions[0] == "navigate"
    assert "fill" in actions
    assert actions.count("click") >= 2  # duplicate click was dropped

    # variableized input
    assert [i.name for i in skill.input_schema] == ["post_text"]
    fill = next(s for s in skill.steps if s.action == "fill")
    assert fill.parameters["value"] == "{{post_text}}"
    assert fill.verification.method == "value_present"

    # the publish click must be gated
    publish = next(s for s in skill.steps if s.title == "Post")
    assert publish.requires_approval is True
    assert publish.risk_level == "high"
    assert skill.risk_level == "high"
    assert skill.approval_requirements

    assert "Skill 'LinkedIn Create Post'" in skill.summary
    assert get_skill("linkedin_create_post") is not None
    assert any(s.skill_id == "linkedin_create_post" for s in list_skills())


def test_versioning_on_recreate(sample_recording):
    first = build_skill(sample_recording, "LinkedIn Create Post")
    second = build_skill(sample_recording, "LinkedIn Create Post")
    assert first.version == 1
    assert second.version == 2
    assert get_skill("linkedin_create_post").version == 2


def test_update_skill_bumps_version_and_renames(sample_recording):
    build_skill(sample_recording, "LinkedIn Create Post")
    updated = update_skill("linkedin_create_post", {"name": "LI Post"})
    assert updated.name == "LI Post"
    assert updated.version == 2
    assert update_skill("does_not_exist", {"name": "x"}) is None


def test_password_element_refused():
    pw = element("input", [loc("css", "#pw")], attrs={"type": "password"}, sensitive=True)
    recording = Recording(events=[event(0, "fill", 1.0, "https://example.com/", pw)])
    with pytest.raises(ValueError, match="sensitive"):
        build_skill(recording, "Bad skill")


def test_duplicate_events_are_deduped(sample_recording):
    skill = build_skill(sample_recording, "LinkedIn Create Post")
    click_start = [s for s in skill.steps if s.title == "Start a post"]
    assert len(click_start) == 1


def test_brittle_locators_detected(sample_recording):
    skill = build_skill(sample_recording, "LinkedIn Create Post")
    # sample has clean locators: no brittle warnings
    assert not any("brittle locator(s)" in w for w in skill.warnings)

    brittle_btn = element("button", [loc("css", "#ember123 > div:nth-child(2)")], name="Save")
    recording = Recording(origin="https://example.com", events=[
        event(0, "navigate", 1.0, "https://example.com/"),
        event(1, "click", 2.0, "https://example.com/", brittle_btn),
    ])
    skill2 = build_skill(recording, "Brittle flow")
    assert any("brittle" in w for w in skill2.warnings)
    assert any(l.brittle for s in skill2.steps for l in (s.target or []))


def test_unlocatable_steps_dropped_with_warning():
    naked_click = element("div", [], name="Mystery")
    recording = Recording(origin="https://example.com", events=[
        event(0, "navigate", 1.0, "https://example.com/"),
        event(1, "click", 2.0, "https://example.com/", naked_click),
    ])
    skill = build_skill(recording, "Partial flow")
    assert all(s.action != "click" for s in skill.steps)
    assert any("without a usable locator" in w for w in skill.warnings)


def test_empty_recording_rejected():
    with pytest.raises(ValueError, match="no events"):
        build_skill(Recording(), "Empty")


def test_delete_skill(sample_recording):
    build_skill(sample_recording, "LinkedIn Create Post")
    assert delete_skill("linkedin_create_post") is True
    assert get_skill("linkedin_create_post") is None
    assert delete_skill("linkedin_create_post") is False


def test_navigate_step_strips_query(sample_recording):
    recording = Recording(origin="https://example.com", events=[
        event(0, "navigate", 1.0, "https://example.com/page?sid=secret123#top"),
    ])
    skill = build_skill(recording, "Nav only")
    assert skill.steps[0].parameters["url"] == "https://example.com/page"


def test_locator_priority_prefers_stable_strategies(sample_recording):
    skill = build_skill(sample_recording, "LinkedIn Create Post")
    fill = next(s for s in skill.steps if s.action == "fill")
    strategies = [l.strategy for l in fill.target]
    assert strategies[0] in ("role", "placeholder")  # role/placeholder before css
