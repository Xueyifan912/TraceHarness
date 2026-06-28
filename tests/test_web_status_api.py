import json
import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


def _client(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(workspace=tmp_path)
    return TestClient(create_app(service=service))


def test_tasks_and_worktrees_status_are_structured(tmp_path):
    client = _client(tmp_path)
    tasks_dir = tmp_path / ".tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task_1.json").write_text(
        json.dumps({
            "id": "task_1",
            "subject": "Implement API",
            "description": "Status endpoint",
            "status": "pending",
            "owner": None,
            "blockedBy": ["task_0"],
            "worktree": "wt-one",
        }),
        encoding="utf-8",
    )
    worktree_dir = tmp_path / ".worktrees" / "wt-one"
    worktree_dir.mkdir(parents=True)

    tasks = client.get("/api/tasks")
    assert tasks.status_code == 200
    assert tasks.json()["tasks"] == [{
        "id": "task_1",
        "subject": "Implement API",
        "description": "Status endpoint",
        "status": "pending",
        "owner": None,
        "blockedBy": ["task_0"],
        "worktree": "wt-one",
    }]

    worktrees = client.get("/api/worktrees")
    assert worktrees.status_code == 200
    assert worktrees.json()["worktrees"] == [{
        "name": "wt-one",
        "path": str(worktree_dir),
        "branch": "wt/wt-one",
        "task_id": "task_1",
    }]


def test_task_status_reports_corrupt_records(tmp_path):
    client = _client(tmp_path)
    tasks_dir = tmp_path / ".tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task_broken.json").write_text(
        "{not-json",
        encoding="utf-8",
    )

    tasks = client.get("/api/tasks")
    team = client.get("/api/team/status")

    assert tasks.status_code == 200
    assert tasks.json()["tasks"] == []
    assert "task_broken.json" in tasks.json()["warnings"][0]
    assert team.json()["warnings"] == tasks.json()["warnings"]


def test_team_status_prioritizes_structured_state(monkeypatch, tmp_path):
    from coding_agent import teams as teams_mod

    client = _client(tmp_path)
    original_teammates = dict(teams_mod.active_teammates)
    original_pending = dict(teams_mod.pending_requests)
    teams_mod.active_teammates.clear()
    teams_mod.pending_requests.clear()
    try:
        teams_mod.active_teammates["worker-1"] = {
            "role": "implementer",
            "status": "working",
            "task_id": "task_1",
            "worktree": "wt-one",
            "worktree_path": str(tmp_path / ".worktrees" / "wt-one"),
            "started_at": 123.0,
        }
        request = teams_mod.ProtocolState(
            request_id="req_001",
            type="plan_approval",
            sender="worker-1",
            target="lead",
            status="pending",
            payload="Plan",
            created_at=456.0,
        )
        teams_mod.pending_requests["req_001"] = request

        response = client.get("/api/team/status")
    finally:
        teams_mod.active_teammates.clear()
        teams_mod.active_teammates.update(original_teammates)
        teams_mod.pending_requests.clear()
        teams_mod.pending_requests.update(original_pending)

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_teammates"][0]["name"] == "worker-1"
    assert payload["active_teammates"][0]["role"] == "implementer"
    assert payload["pending_requests"][0]["request_id"] == "req_001"
    assert payload["pending_requests"][0]["type"] == "plan_approval"
    assert isinstance(payload["tasks"], list)
    assert isinstance(payload["worktrees"], list)
    assert isinstance(payload["raw_text"], str)


def test_mcp_status_splits_mock_configured_connected_and_errors(tmp_path):
    from coding_agent.mcp import client as mcp_client_mod
    from coding_agent.runtime.events import log_event

    client = _client(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({
            "servers": {
                "local-tools": {
                    "command": "python",
                    "args": ["server.py"],
                    "env": {"TOKEN": "secret"},
                }
            }
        }),
        encoding="utf-8",
    )
    workspace_clients = mcp_client_mod.current_mcp_clients(tmp_path)
    original_clients = dict(workspace_clients)
    workspace_clients.clear()
    try:
        workspace_clients["docs"] = mcp_client_mod.MOCK_SERVERS["docs"]()
        log_event(
            "mcp_connect",
            {"server": "broken", "ok": False, "error": "failed to start"},
            workspace=tmp_path,
        )

        response = client.get("/api/mcp/status")
    finally:
        workspace_clients.clear()
        workspace_clients.update(original_clients)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mock_servers"] == ["deploy", "docs"]
    assert payload["configured_servers"] == [{
        "name": "local-tools",
        "transport": "stdio",
        "command": "python",
        "args": ["server.py"],
        "env_keys": ["TOKEN"],
        "configured": True,
    }]
    assert payload["connected_servers"][0]["name"] == "docs"
    assert payload["connected_servers"][0]["transport"] == "mock"
    assert payload["connected_servers"][0]["tool_count"] == 2
    assert payload["connected_servers"][0]["tools"][0]["name"].startswith(
        "mcp__docs__"
    )
    assert payload["errors"] == [{
        "type": "mcp_connect",
        "ts": payload["errors"][0]["ts"],
        "server": "broken",
        "message": "failed to start",
    }]


def test_memory_api_uses_fixed_workspace_file_and_truncates(tmp_path):
    client = _client(tmp_path)
    outside = tmp_path / "outside.md"

    appended = client.post(
        "/api/memory/append",
        json={"content": "remember this"},
    )
    assert appended.status_code == 200
    payload = appended.json()
    assert payload["ok"] is True
    assert payload["length"] == len("remember this")
    assert payload["memory"]["path"] == str(tmp_path / ".memory" / "MEMORY.md")
    assert not outside.exists()

    memory = client.get("/api/memory")
    assert memory.status_code == 200
    assert memory.json()["exists"] is True
    assert "- remember this" in memory.json()["content"]

    rejected_path = client.post(
        "/api/memory/append",
        json={"content": "bad path", "path": str(outside)},
    )
    assert rejected_path.status_code == 422
    assert rejected_path.json()["error"]["code"] == "validation_error"
    assert not outside.exists()

    memory_path = tmp_path / ".memory" / "MEMORY.md"
    memory_path.write_text("x" * (50 * 1024 + 10), encoding="utf-8")
    large = client.get("/api/memory")
    assert large.status_code == 200
    assert large.json()["truncated"] is True
    assert len(large.json()["content"]) == large.json()["limit"]
    assert large.json()["length"] == large.json()["limit"]
    assert large.json()["size_bytes"] > large.json()["limit"]
