"""Startup reliability and safety/privacy helpers."""

from __future__ import annotations

from axiom import preflight
from axiom.sessions import _redact
from axiom.schemas import sanitize_url


def test_reuse_healthy_instance(monkeypatch):
    monkeypatch.setattr(preflight, "probe_axiom", lambda port, timeout=1.5: {"version": "0.3.0"})
    assert preflight.run_preflight(8765) == "reuse"
    # --force never kills, it only attempts to bind
    assert preflight.run_preflight(8765, force=True) == "proceed"


def test_blocked_foreign_process(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "probe_axiom", lambda port, timeout=1.5: None)
    monkeypatch.setattr(preflight, "probe_port", lambda port, timeout=0.6: True)
    monkeypatch.setattr(preflight, "port_owner", lambda port: "PID 4242: other-app.exe")
    assert preflight.run_preflight(8765) == "blocked"
    out = capsys.readouterr().out
    assert "another process" in out
    assert "other-app.exe" in out
    assert "AXIOM_PORT" in out
    assert "not terminate unrelated processes" in out


def test_proceed_when_port_free(monkeypatch):
    monkeypatch.setattr(preflight, "probe_axiom", lambda port, timeout=1.5: None)
    monkeypatch.setattr(preflight, "probe_port", lambda port, timeout=0.6: False)
    assert preflight.run_preflight(8765) == "proceed"


def test_conflict_report_is_actionable():
    report = preflight.conflict_report(8765)
    assert "8765" in report
    assert "http://127.0.0.1:8765/health" in report
    assert "set AXIOM_PORT" in report


def test_parse_args():
    assert preflight.parse_args(["--force"]) is True
    assert preflight.parse_args(["-f"]) is True
    assert preflight.parse_args([]) is False


def test_probe_axiom_requires_axiom_service(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({"service": "other-app"}))
    assert preflight.probe_axiom(8765) is None

    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: FakeResponse({"service": "axiom-local-bridge", "version": "0.3.0"}))
    assert preflight.probe_axiom(8765)["version"] == "0.3.0"


def test_redact_truncates_long_messages():
    long = "x" * 5000
    out = _redact(long)
    assert len(out) < 300
    assert out.endswith("…[truncated]")


def test_redact_strips_url_credentials():
    out = _redact("failed to reach https://user:secret123@internal.example/path")
    assert "secret123" not in out
    assert "internal.example" in out


def test_sanitize_url_privacy():
    assert sanitize_url("https://site.com/p?session=abc#frag") == "https://site.com/p"
    assert sanitize_url("file:///etc/passwd") == ""
    assert sanitize_url("") == ""
