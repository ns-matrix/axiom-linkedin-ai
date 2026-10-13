"""API surface: routes, schemas, error handling, MCP tools."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_module

from conftest import element, event, loc


@pytest.fixture
def client(monkeypatch):
    async def fake_ollama_get(path):
        if path.endswith("/api/tags"):
            return {"models": [{"name": "test-model"}]}
        raise HTTPException(503, "not available in tests")

    monkeypatch.setattr(app_module, "ollama_get", fake_ollama_get)
    return TestClient(app_module.app)


def sample_events():
    feed = "https://www.linkedin.com/feed/"
    composer_btn = element("button", [loc("role", "button", alt="Start a post")], name="Start a post")
    textarea = element("textarea", [loc("placeholder", "What do you want to talk about?")],
                       attrs={"placeholder": "What do you want to talk about?"})
    post_btn = element("button", [loc("role", "button", alt="Post")], name="Post")
    return [
        event(0, "navigate", 1000.0, feed),
        event(1, "click", 1001.0, feed, composer_btn),
        event(2, "fill", 1003.0, feed, textarea, value_length=20, variable="post_text"),
        event(3, "click", 1006.0, feed, post_btn),
    ]


def serialize(events):
    return [json.loads(e.model_dump_json()) for e in events]


def create_skill_via_api(client, name="LinkedIn Create Post"):
    resp = client.post("/skills", json={"name": name, "description": "Create a post",
                                        "events": serialize(sample_events())})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
def test_health_and_models(client):
    data = client.get("/health").json()
    assert data["ok"] is True
    assert data["service"] == "axiom-local-bridge"
    assert data["models"] == ["test-model"]
    assert data["skills"] == 0

    assert client.get("/models").json() == {"models": ["test-model"]}


def test_chat_requires_message(client):
    assert client.post("/chat", json={"message": "   "}).status_code == 400


def test_recording_crud(client):
    resp = client.post("/recordings", json={"name": "demo", "origin": "https://www.linkedin.com",
                                            "events": serialize(sample_events())})
    assert resp.status_code == 200
    rec_id = resp.json()["recording_id"]

    listed = client.get("/recordings").json()["recordings"]
    assert listed[0]["events"] == 4

    # skill from stored recording
    skill = client.post("/skills", json={"name": "LinkedIn Create Post",
                                         "recording_id": rec_id}).json()
    assert skill["version"] == 1

    # recreate bumps version
    skill2 = client.post("/skills", json={"name": "LinkedIn Create Post",
                                          "recording_id": rec_id}).json()
    assert skill2["version"] == 2

    assert client.delete(f"/recordings/{rec_id}").status_code == 200
    assert client.delete(f"/recordings/{rec_id}").status_code == 404


def test_skill_crud_and_validation(client):
    skill = create_skill_via_api(client)
    assert skill["skill_id"] == "linkedin_create_post"
    assert [i["name"] for i in skill["input_schema"]] == ["post_text"]
    assert any(s["requires_approval"] for s in skill["steps"])

    # list / get
    assert len(client.get("/skills").json()["skills"]) == 1
    assert client.get("/skills/linkedin_create_post").status_code == 200
    assert client.get("/skills/nope").status_code == 404

    # patch bumps version
    patched = client.patch("/skills/linkedin_create_post", json={"name": "LI Post"}).json()
    assert patched["name"] == "LI Post"
    assert patched["version"] == 2
    assert client.patch("/skills/linkedin_create_post", json={}).status_code == 400
    assert client.patch("/skills/nope", json={"name": "x"}).status_code == 404

    assert client.delete("/skills/linkedin_create_post").status_code == 200
    assert client.delete("/skills/linkedin_create_post").status_code == 404


def test_create_skill_error_paths(client):
    assert client.post("/skills", json={"name": "x"}).status_code == 400
    assert client.post("/skills", json={"name": "x", "recording_id": "missing"}).status_code == 404

    bad = [json.loads(event(0, "fill", 1.0, "https://example.com/",
                            element("input", [], sensitive=True)).model_dump_json())]
    resp = client.post("/skills", json={"name": "x", "events": bad})
    assert resp.status_code == 400
    assert "sensitive" in resp.json()["detail"]


def test_tasks_plan_and_legacy_plan(client, mock_llm):
    create_skill_via_api(client)
    mock_llm.install(json.dumps({"inputs": {"post_text": "Python 3.15 is here"},
                                 "draft": "Python 3.15 is here\n\nThoughts?"}))

    resp = client.post("/tasks/plan", json={"goal": "Create a LinkedIn post about Python 3.15"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["plan"]["skill_id"] == "linkedin_create_post"
    assert all(s["action"] in ("navigate", "click", "fill", "select", "scroll",
                               "wait_for", "inspect_visible_state", "request_approval")
               for s in body["plan"]["steps"])

    # legacy endpoint shape preserved
    legacy = client.post("/plan", json={"goal": "Create a LinkedIn post about Python 3.15"}).json()
    assert "reply" in legacy and "plan" in legacy and legacy["done"] is True

    # error paths
    assert client.post("/tasks/plan", json={"goal": "   "}).status_code == 400
    assert client.post("/tasks/plan", json={"goal": "x", "skill_id": "nope"}).status_code == 404


def test_dry_run(client, mock_llm):
    create_skill_via_api(client)
    mock_llm.install('{"inputs": {}}')
    resp = client.post("/skills/linkedin_create_post/dry-run",
                       json={"inputs": {"post_text": "hello"}, "goal": "dry"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert all(t["would_execute"] for t in body["trace"])
    assert client.post("/skills/nope/dry-run", json={}).status_code == 404

    missing = client.post("/skills/linkedin_create_post/dry-run", json={"inputs": {}})
    assert missing.json()["missing_inputs"] == ["post_text"]


def test_session_lifecycle_via_api(client, mock_llm):
    create_skill_via_api(client)
    mock_llm.install(json.dumps({"inputs": {"post_text": "Draft body"}, "draft": "Draft body"}))
    plan = client.post("/tasks/plan", json={"goal": "Create a LinkedIn post"}).json()["plan"]

    created = client.post("/sessions", json={"plan": plan, "tab_id": 7})
    assert created.status_code == 200
    sid = created.json()["session_id"]
    assert created.json()["state"] == "awaiting_approval"

    # gated before any action
    wait = client.post(f"/sessions/{sid}/claim").json()
    assert wait["reason"] == "awaiting_plan_approval"

    assert client.post(f"/sessions/{sid}/approve").status_code == 200
    assert client.post(f"/sessions/{sid}/approve").status_code == 409  # no double approve

    claimed = client.post(f"/sessions/{sid}/claim").json()
    assert claimed["status"] == "claimed"
    step = claimed["step"]

    # wrong token rejected
    bad = client.post(f"/sessions/{sid}/steps/{step['step_id']}/result",
                      json={"token": "bogus", "status": "succeeded", "observed": {}})
    assert bad.status_code == 409

    # invalid status code
    bad_status = client.post(f"/sessions/{sid}/steps/{step['step_id']}/result",
                             json={"token": step and claimed["token"], "status": "exploded"})
    assert bad_status.status_code == 400

    ok = client.post(f"/sessions/{sid}/steps/{step['step_id']}/result",
                     json={"token": claimed["token"], "status": "succeeded",
                           "observed": {"url": "https://www.linkedin.com/feed/"}})
    assert ok.status_code == 200

    # drive to the publish gate and exercise approvals
    while True:
        nxt = client.post(f"/sessions/{sid}/claim").json()
        if nxt["status"] != "claimed":
            break
        observed = {"visible": True, "value_present": True, "value_length": 10, "ok": True}
        client.post(f"/sessions/{sid}/steps/{nxt['step']['step_id']}/result",
                    json={"token": nxt["token"], "status": "succeeded", "observed": observed})
    assert nxt["reason"] == "awaiting_step_approval"

    session = client.get(f"/sessions/{sid}").json()
    assert session["pending_approval"]["type"] == "step"
    gate = session["pending_approval"]["step_id"]

    # bad decision rejected
    assert client.post(f"/sessions/{sid}/approvals/{gate}",
                       json={"decision": "maybe"}).status_code == 400

    approved = client.post(f"/sessions/{sid}/approvals/{gate}", json={"decision": "approve"})
    assert approved.status_code == 200

    final = client.post(f"/sessions/{sid}/claim").json()
    assert final["status"] == "claimed"
    client.post(f"/sessions/{sid}/steps/{final['step']['step_id']}/result",
                json={"token": final["token"], "status": "succeeded", "observed": {"ok": True}})
    assert client.get(f"/sessions/{sid}").json()["state"] == "completed"

    history = client.get("/sessions").json()["history"]
    assert history[0]["session_id"] == sid
    assert history[0]["state"] == "completed"


def test_session_errors(client, mock_llm):
    create_skill_via_api(client)
    mock_llm.install(json.dumps({"inputs": {"post_text": "x"}, "draft": "x"}))
    plan = client.post("/tasks/plan", json={"goal": "Create a LinkedIn post"}).json()["plan"]

    sid = client.post("/sessions", json={"plan": plan}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/start").status_code == 409  # not approved yet
    assert client.post(f"/sessions/{sid}/pause").status_code == 409
    client.post(f"/sessions/{sid}/cancel")
    assert client.post(f"/sessions/{sid}/cancel").status_code == 200  # idempotent
    assert client.post(f"/sessions/{sid}/approve").status_code == 409

    assert client.get("/sessions/ses_missing").status_code == 404
    assert client.post("/sessions/ses_missing/claim").status_code == 404


def test_session_rejects_plan_with_unready_status(client):
    create_skill_via_api(client)
    plan = client.post("/tasks/plan", json={"goal": "Create a LinkedIn post"}).json()["plan"]
    plan["status"] = "needs_inputs"
    resp = client.post("/sessions", json={"plan": plan})
    assert resp.status_code == 400
    assert "not executable" in resp.json()["detail"]


def test_mcp_endpoint(client, mock_llm):
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).json()
    assert init["result"]["serverInfo"]["name"] == "axiom-linkedin-local"

    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"list_skills", "get_skill", "plan_browser_task", "start_supervised_session",
            "get_session_status", "approve_session", "cancel_session",
            "execution_history", "ollama_status", "draft_linkedin_content"} <= names
    assert names == {t["name"] for t in client.get("/mcp/tools").json()["tools"]}

    create_skill_via_api(client)

    def call(tool, arguments=None, call_id=99):
        return client.post("/mcp", json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                                         "params": {"name": tool, "arguments": arguments or {}}}).json()

    listed = json.loads(call("list_skills")["result"]["content"][0]["text"])
    assert listed["skills"][0]["skill_id"] == "linkedin_create_post"

    unknown = call("no_such_tool")
    assert unknown["result"]["isError"] is True

    missing = call("get_skill", {"skill_id": "nope"})
    assert missing["result"]["isError"] is True

    # plan + supervised session through MCP, same gating rules
    mock_llm.install(json.dumps({"inputs": {"post_text": "via mcp"}, "draft": "via mcp"}))
    planned = call("plan_browser_task", {"goal": "Create a LinkedIn post"})
    plan_body = json.loads(planned["result"]["content"][0]["text"])
    assert plan_body["status"] == "ready"

    mock_llm.install(json.dumps({"inputs": {"post_text": "via mcp"}, "draft": "via mcp"}))
    started = json.loads(call("start_supervised_session",
                              {"goal": "Create a LinkedIn post"})["result"]["content"][0]["text"])
    sid = started["session_id"]
    assert started["state"] == "awaiting_approval"

    status = json.loads(call("get_session_status", {"session_id": sid})["result"]["content"][0]["text"])
    assert status["state"] == "awaiting_approval"
    assert status["pending_approval"]["type"] == "plan"

    approved = json.loads(call("approve_session", {"session_id": sid, "scope": "plan"})
                          ["result"]["content"][0]["text"])
    assert approved["state"] == "ready"

    history = json.loads(call("execution_history")["result"]["content"][0]["text"])
    assert history["history"][0]["session_id"] == sid


def test_mcp_method_errors(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "weird/method"})
    assert resp.status_code == 400
