import os
import queue
import threading

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


class FakeLoop:
    def __init__(self, text="api response", started=None, release=None):
        self.text = text
        self.started = started
        self.release = release

    def run(self, messages, context):
        if self.started:
            self.started.set()
        if self.release:
            assert self.release.wait(timeout=5)
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": self.text}],
        })


def test_web_api_health_sessions_and_message(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(
        workspace=tmp_path,
        loop_factory=lambda: FakeLoop("api ok"),
    )
    client = TestClient(create_app(service=service))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["workspace_path"] == str(tmp_path.resolve())

    created = client.post("/api/sessions", json={})
    assert created.status_code == 200
    session_id = created.json()["session"]["session_id"]

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["session_id"] == session_id

    detail = client.get(f"/api/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == []

    turn = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "hello"},
    )
    assert turn.status_code == 200
    payload = turn.json()
    assert payload["run"]["status"] == "completed"
    assert payload["session"]["message_count"] == 2
    assert payload["messages"][0]["content"][0]["text"] == "api ok"


def test_web_api_running_session_returns_409(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    started = threading.Event()
    release = threading.Event()
    service = AgentService(
        workspace=tmp_path,
        loop_factory=lambda: FakeLoop("done", started=started, release=release),
    )
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    results = queue.Queue()

    def first_request():
        results.put(client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "first"},
        ))

    thread = threading.Thread(target=first_request)
    thread.start()
    assert started.wait(timeout=5)

    conflict = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "second"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "session_running"
    assert conflict.json()["error"]["details"]["active_run_id"]

    release.set()
    thread.join(timeout=5)
    first = results.get_nowait()
    assert first.status_code == 200


def test_web_api_unexpected_error_returns_stable_json(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    class FailingLoop:
        def run(self, messages, context):
            raise RuntimeError("boom")

    service = AgentService(
        workspace=tmp_path,
        loop_factory=FailingLoop,
    )
    client = TestClient(create_app(service=service), raise_server_exceptions=False)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "explode"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal error.",
            "details": {"error_type": "RuntimeError"},
        }
    }


def test_web_api_validation_error_returns_stable_json(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(
        workspace=tmp_path,
        loop_factory=lambda: FakeLoop("unused"),
    )
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["message"] == "Request validation failed."
    assert payload["error"]["details"]["errors"]


def test_web_api_archives_idle_session_and_rejects_active_session(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    started = threading.Event()
    release = threading.Event()
    service = AgentService(
        workspace=tmp_path,
        loop_factory=lambda: FakeLoop(
            "done",
            started=started,
            release=release,
        ),
    )
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "hold"},
    ).json()["run"]
    assert started.wait(timeout=5)

    blocked = client.post(f"/api/sessions/{session_id}/archive")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "session_running"

    release.set()
    deadline = threading.Event()
    for _ in range(500):
        detail = client.get(
            f"/api/sessions/{session_id}/runs/{run['run_id']}"
        ).json()["run"]
        if detail["status"] == "completed":
            break
        deadline.wait(0.01)
    assert detail["status"] == "completed"

    archived = client.post(f"/api/sessions/{session_id}/archive")
    assert archived.status_code == 200
    assert archived.json() == {
        "ok": True,
        "session_id": session_id,
        "archived": True,
    }
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert client.get("/api/sessions").json()["sessions"] == []
