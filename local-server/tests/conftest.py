from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DATA_DIR = pathlib.Path(tempfile.mkdtemp(prefix="axiom-test-data-"))
os.environ["AXIOM_DATA_DIR"] = str(_DATA_DIR)


def _wipe() -> None:
    for path in _DATA_DIR.rglob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def clean_data():
    _wipe()
    yield
    _wipe()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_dir():
    yield
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


class MockLLM:
    """Replaces axiom.planner's chat() with a canned reply or an exception."""

    def __init__(self, monkeypatch):
        import axiom.planner as planner

        self._monkeypatch = monkeypatch
        self._planner = planner
        self.calls: list[list[dict]] = []
        self.install('{"inputs": {}}')

    def install(self, reply):
        self.calls.clear()

        async def fake_chat(messages, model=None):
            self.calls.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply, "test-model"

        self._monkeypatch.setattr(self._planner, "chat", fake_chat)


@pytest.fixture
def mock_llm(monkeypatch):
    return MockLLM(monkeypatch)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Shared sample data
# --------------------------------------------------------------------------- #
def loc(strategy, value, alt=None, brittle=False, priority=50):
    from axiom.schemas import Locator

    return Locator(strategy=strategy, value=value, alt_value=alt, brittle=brittle, priority=priority)


def element(tag, locators, name=None, attrs=None, sensitive=False):
    from axiom.schemas import ElementMeta

    return ElementMeta(tag=tag, name=name, attrs=attrs or {}, locators=locators, sensitive=sensitive)


def event(seq, action_type, t, url, element_obj=None, **kw):
    from axiom.schemas import RecordingEvent

    origin = "https://www.linkedin.com" if "linkedin" in url else url.split("/")[0] + "//" + url.split("/")[2]
    return RecordingEvent(
        seq=seq, timestamp=t, action_type=action_type, url=url, origin=origin,
        element=element_obj, **kw,
    )


@pytest.fixture
def sample_recording():
    from axiom.schemas import Recording

    feed = "https://www.linkedin.com/feed/"
    composer_btn = element("button", [loc("testid", "share-start-post"), loc("role", "button", alt="Start a post")],
                           name="Start a post", attrs={"aria-label": "Start a post"})
    textarea = element("textarea", [loc("role", "textbox", alt="What do you want to talk about?"),
                                    loc("placeholder", "What do you want to talk about?")],
                       attrs={"placeholder": "What do you want to talk about?"})
    post_btn = element("button", [loc("role", "button", alt="Post"), loc("testid", "share-action")], name="Post")

    events = [
        event(0, "navigate", 1000.0, feed),
        event(1, "click", 1001.0, feed, composer_btn),
        event(2, "click", 1001.3, feed, composer_btn),  # duplicate, should be dropped
        event(3, "fill", 1003.0, feed, textarea, value_length=28, variable="post_text"),
        event(4, "click", 1006.0, feed, post_btn),
    ]
    return Recording(name="record create post", origin="https://www.linkedin.com", events=events)


@pytest.fixture
def sample_skill(sample_recording):
    from axiom.skills import build_skill

    return build_skill(sample_recording, "LinkedIn Create Post", "Create and publish a LinkedIn post")
