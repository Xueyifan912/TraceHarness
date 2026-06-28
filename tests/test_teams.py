import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def clear_team_state():
    from coding_agent import teams as teams_mod

    teams_mod.active_teammates.clear()
    teams_mod.pending_requests.clear()
    yield
    teams_mod.active_teammates.clear()
    teams_mod.pending_requests.clear()


def _read_events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _configure_team_dirs(monkeypatch, tmp_path):
    from coding_agent import teams as teams_mod
    from coding_agent.task_system import tasks as tasks_mod
    from coding_agent.task_system import worktrees as worktrees_mod

    tasks_dir = tmp_path / ".tasks"
    worktrees_dir = tmp_path / ".worktrees"
    tasks_dir.mkdir()
    worktrees_dir.mkdir()
    monkeypatch.setattr(tasks_mod, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(worktrees_mod, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(teams_mod, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(teams_mod, "WORKTREES_DIR", worktrees_dir)
    return teams_mod, tasks_mod, worktrees_mod, tasks_dir, worktrees_dir


class FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, from_agent, to_agent, content,
             msg_type="message", metadata=None):
        self.sent.append({
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "metadata": metadata or {},
        })

    def read_inbox(self, agent):
        return []


def test_team_status_includes_teammate_task_and_worktree_context(
        monkeypatch, tmp_path):
    teams_mod, tasks_mod, _, _, worktrees_dir = _configure_team_dirs(
        monkeypatch, tmp_path)
    worktree_path = worktrees_dir / "demo-wt"
    worktree_path.mkdir()
    task = tasks_mod.Task(
        id="task_demo",
        subject="Build demo",
        description="",
        status="in_progress",
        owner="alice",
        blockedBy=[],
        worktree="demo-wt",
    )
    tasks_mod.save_task(task)
    teams_mod.active_teammates["alice"] = {
        "role": "implementer",
        "status": "working",
        "task_id": "task_demo",
        "worktree": "demo-wt",
        "worktree_path": str(worktree_path),
    }
    teams_mod.pending_requests["req_000001"] = teams_mod.ProtocolState(
        request_id="req_000001",
        type="plan_approval",
        sender="alice",
        target="lead",
        status="pending",
        payload="Plan",
    )

    status = teams_mod.team_status()

    assert "Active teammates:" in status
    assert "alice | role=implementer | status=working" in status
    assert "task=task_demo | worktree=demo-wt" in status
    assert f"path={worktree_path}" in status
    assert "req_000001 | type=plan_approval" in status
    assert ("task_demo: Build demo | status=in_progress | owner=alice "
            "| worktree=demo-wt") in status
    assert f"demo-wt: path={worktree_path} | branch=wt/demo-wt" in status
    assert "task_id=task_demo" in status


def test_team_state_snapshots_are_stable_copies():
    from coding_agent import teams as teams_mod

    teams_mod.active_teammates["alice"] = {
        "role": "implementer",
        "status": "running",
    }
    teams_mod.pending_requests["req_snapshot"] = teams_mod.ProtocolState(
        request_id="req_snapshot",
        type="plan_approval",
        sender="alice",
        target="lead",
        status="pending",
        payload="Plan",
    )

    teammate_snapshot = teams_mod.active_teammates_snapshot()
    request_snapshot = teams_mod.pending_requests_snapshot()
    teams_mod.active_teammates["alice"]["status"] = "completed"
    teams_mod.pending_requests["req_snapshot"].status = "approved"

    assert teammate_snapshot["alice"]["status"] == "running"
    assert request_snapshot["req_snapshot"].status == "pending"


def test_request_ids_are_unique_under_concurrency():
    from coding_agent import teams as teams_mod

    results = []
    result_lock = threading.Lock()

    def generate():
        generated = [teams_mod.new_request_id() for _ in range(250)]
        with result_lock:
            results.extend(generated)

    threads = [threading.Thread(target=generate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2000
    assert len(set(results)) == len(results)
    assert all(
        request_id.startswith("req_") and len(request_id) == 36
        for request_id in results
    )


def test_review_plan_rejects_duplicate_resolution(monkeypatch):
    from coding_agent import teams as teams_mod

    fake_bus = FakeBus()
    monkeypatch.setattr(teams_mod, "BUS", fake_bus)
    result = teams_mod._teammate_submit_plan("alice", "Plan")
    request_id = result.removeprefix("Plan submitted (").removesuffix(")")

    assert teams_mod.run_review_plan(request_id, True) == "Plan approved"
    assert (
        teams_mod.run_review_plan(request_id, False)
        == f"Request {request_id} is already approved"
    )
    responses = [
        message for message in fake_bus.sent
        if message["type"] == "plan_approval_response"
    ]
    assert len(responses) == 1


def test_protocol_response_cannot_reverse_a_resolved_request():
    from coding_agent import teams as teams_mod

    request_id = "req_idempotent"
    teams_mod.pending_requests[request_id] = teams_mod.ProtocolState(
        request_id=request_id,
        type="plan_approval",
        sender="alice",
        target="lead",
        status="pending",
        payload="Plan",
    )

    teams_mod.match_response(
        "plan_approval_response",
        request_id,
        True,
    )
    teams_mod.match_response(
        "plan_approval_response",
        request_id,
        False,
    )

    assert teams_mod.pending_requests[request_id].status == "approved"


def test_message_bus_send_and_read_emit_audit_events(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import teams as teams_mod

    mailbox_dir = tmp_path / ".mailboxes"
    mailbox_dir.mkdir()
    monkeypatch.setattr(teams_mod, "MAILBOX_DIR", mailbox_dir)
    bus = teams_mod.MessageBus()

    bus.send("lead", "alice", "hello from lead", "message",
             {"request_id": "req_000001"})
    messages = bus.read_inbox("alice")

    assert messages[0]["content"] == "hello from lead"
    events = _read_events(tmp_path)
    event_types = [event["type"] for event in events]
    assert "teammate_message_sent" in event_types
    assert "teammate_messages_read" in event_types
    sent = next(event for event in events
                if event["type"] == "teammate_message_sent")
    assert sent["payload"]["from"] == "lead"
    assert sent["payload"]["to"] == "alice"
    assert sent["payload"]["request_id"] == "req_000001"
    assert sent["payload"]["content"] == {
        "preview": "hello from lead",
        "length": len("hello from lead"),
        "truncated": False,
    }


def test_message_bus_preserves_valid_lines_when_one_line_is_corrupt(
    monkeypatch,
    tmp_path,
):
    from coding_agent import teams as teams_mod

    mailbox_dir = tmp_path / ".mailboxes"
    mailbox_dir.mkdir()
    monkeypatch.setattr(teams_mod, "MAILBOX_DIR", mailbox_dir)
    bus = teams_mod.MessageBus()
    bus.send("lead", "alice", "first")
    inbox = mailbox_dir / "alice.jsonl"
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")
    bus.send("lead", "alice", "second")

    messages = bus.read_inbox("alice")

    assert [item["content"] for item in messages] == ["first", "second"]
    corrupt_files = list(mailbox_dir.glob("alice.corrupt-*.jsonl"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{not-json}\n"


def test_teammate_final_handoff_includes_context(monkeypatch, tmp_path):
    from coding_agent.providers.base import TextBlock, ToolUseBlock

    teams_mod, tasks_mod, _, _, worktrees_dir = _configure_team_dirs(
        monkeypatch, tmp_path)
    worktree_path = worktrees_dir / "demo-wt"
    worktree_path.mkdir()
    task = tasks_mod.Task(
        id="task_demo",
        subject="Build demo",
        description="",
        status="pending",
        owner=None,
        blockedBy=[],
        worktree="demo-wt",
    )
    tasks_mod.save_task(task)

    class FakeProvider:
        name = "fake-provider"

        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    content=[ToolUseBlock(
                        id="call_claim",
                        name="claim_task",
                        input={"task_id": "task_demo"},
                    )],
                    stop_reason="tool_use",
                )
            return SimpleNamespace(
                content=[TextBlock("Implemented the demo task.")],
                stop_reason="end_turn",
            )

    fake_bus = FakeBus()
    provider = FakeProvider()
    teammate_name = "alice"

    monkeypatch.setattr(teams_mod, "BUS", fake_bus)
    monkeypatch.setattr(teams_mod, "get_model_provider", lambda: provider)
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    monkeypatch.setattr(teams_mod, "IDLE_POLL_INTERVAL", 1)

    try:
        result = teams_mod.spawn_teammate_thread(
            teammate_name, "implementer", "claim the demo task")
        deadline = time.time() + 2
        while time.time() < deadline and teammate_name in teams_mod.active_teammates:
            time.sleep(0.01)

        assert result == "Teammate 'alice' spawned as implementer"
        assert teammate_name not in teams_mod.active_teammates
    finally:
        teams_mod.active_teammates.pop(teammate_name, None)

    result_messages = [msg for msg in fake_bus.sent if msg["type"] == "result"]
    assert result_messages
    content = result_messages[-1]["content"]
    assert "Teammate: alice" in content
    assert "Role: implementer" in content
    assert "Task: task_demo - Build demo [in_progress] owner=alice" in content
    assert f"Worktree: demo-wt at {worktree_path}" in content
    assert "Summary:\nImplemented the demo task." in content
    assert result_messages[-1]["metadata"] == {
        "role": "implementer",
        "task_id": "task_demo",
        "worktree": "demo-wt",
    }


def test_teammate_read_handler_forwards_limit_and_offset(monkeypatch):
    from coding_agent.providers.base import TextBlock, ToolUseBlock
    from coding_agent import teams as teams_mod

    captured = {}

    def fake_read(path, limit=None, offset=0, cwd=None):
        captured.update({
            "path": path,
            "limit": limit,
            "offset": offset,
            "cwd": cwd,
        })
        return "selected lines"

    class FakeProvider:
        name = "fake-provider"

        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    content=[ToolUseBlock(
                        id="call_read",
                        name="read_file",
                        input={
                            "path": "notes.txt",
                            "limit": 5,
                            "offset": 7,
                        },
                    )],
                    stop_reason="tool_use",
                )
            return SimpleNamespace(
                content=[TextBlock("done")],
                stop_reason="end_turn",
            )

    monkeypatch.setattr(teams_mod, "BUS", FakeBus())
    monkeypatch.setattr(teams_mod, "run_read", fake_read)
    monkeypatch.setattr(
        teams_mod,
        "get_model_provider",
        lambda: FakeProvider(),
    )
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    monkeypatch.setattr(teams_mod, "IDLE_POLL_INTERVAL", 1)

    teams_mod.spawn_teammate_thread("reader", "reviewer", "read notes")
    deadline = time.time() + 2
    while time.time() < deadline and "reader" in teams_mod.active_teammates:
        time.sleep(0.01)

    assert "reader" not in teams_mod.active_teammates
    assert captured == {
        "path": "notes.txt",
        "limit": 5,
        "offset": 7,
        "cwd": None,
    }


def test_worktree_name_validation_still_blocks_path_escape():
    from coding_agent.task_system.worktrees import (
        create_worktree,
        validate_worktree_name,
    )

    assert validate_worktree_name("manual-001") is None
    assert "Invalid worktree name" in validate_worktree_name("../outside")
    assert "Invalid worktree name" in validate_worktree_name(r"..\outside")
    assert create_worktree("../outside").startswith("Error: Invalid worktree name")
