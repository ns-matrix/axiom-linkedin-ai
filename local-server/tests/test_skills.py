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


def test_keystroke_fills_collapse_and_unlabeled_clicks_gated():
    """Real-world shape: slow per-keystroke typing + an unlabeled 'Post' span."""
    from axiom.actions import validate_step

    compose = "https://www.linkedin.com/sharing/compose"
    editor = element("div", [loc("css",
        "body > div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > div:nth-of-type(1)",
        brittle=True, priority=70)])
    post_span = element("span", [loc("css",
        "body > div:nth-of-type(1) > footer:nth-of-type(2) > button:nth-of-type(2) > span:nth-of-type(1)",
        brittle=True, priority=70)])

    events = [event(0, "navigate", 1000.0, compose)]
    t = 1001.0
    for i in range(12):
        t += 0.2 + (i % 3) * 0.45  # gaps up to 1.1s: slower than the dedupe window
        events.append(event(1 + i, "fill", t, compose, editor,
                            value_length=12 - i, variable="text_value"))
    events.append(event(13, "click", t + 2.0, compose, post_span))

    skill = build_skill(Recording(name="Create A post",
                                  origin="https://www.linkedin.com", events=events),
                        "Create A post")

    fills = [s for s in skill.steps if s.action == "fill"]
    assert len(fills) == 1, "keystroke runs must collapse into one fill step"
    assert fills[0].parameters["value"] == "{{text_value}}"

    clicks = [s for s in skill.steps if s.action == "click"]
    assert len(clicks) == 1
    assert clicks[0].requires_approval is True, "unlabeled click could be Post"
    assert clicks[0].risk_level == "high"
    assert clicks[0].title != "Click element"
    assert "button" in clicks[0].title, "title should hint at the structural parent"

    assert all(validate_step(s) == [] for s in skill.steps)
    assert any("no accessible label" in w for w in skill.warnings)
    assert any("brittle" in w for w in skill.warnings)


def test_unlabeled_fields_get_distinct_variables():
    field_a = element("div", [loc("css", "body > div:nth-of-type(1) > div:nth-of-type(2)")])
    field_b = element("input", [loc("css", "body > input:nth-of-type(17)")])
    events = [
        event(0, "fill", 1.0, "https://example.com/", field_a,
              value_length=3, variable="text_value"),
        event(1, "fill", 2.0, "https://example.com/", field_b,
              value_length=5, variable="text_value"),
    ]
    skill = build_skill(Recording(origin="https://example.com", events=events), "Two fields")

    assert [i.name for i in skill.input_schema] == ["text_value", "text_value_2"]
    values = [s.parameters["value"] for s in skill.steps if s.action == "fill"]
    assert values == ["{{text_value}}", "{{text_value_2}}"]
