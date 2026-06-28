import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def test_background_results_are_scoped_to_event_context(tmp_path):
    from coding_agent import background as bg
    from coding_agent.runtime.events import event_context

    bg.background_tasks.clear()
    bg.background_results.clear()
    bg._bg_counter = 0
    block = SimpleNamespace(
        id="toolu_bg",
        name="bash",
        input={"command": "pytest"},
    )

    with event_context(
        session_id="session_a",
        run_id="run_a",
        source="web",
        workspace=tmp_path,
    ):
        bg_id = bg.start_background_task(block, {"bash": lambda command: "done a"})

    deadline = time.time() + 2
    while time.time() < deadline:
        if bg.background_tasks[bg_id]["status"] == "completed":
            break
        time.sleep(0.01)
    assert bg.background_tasks[bg_id]["status"] == "completed"
    assert bg.background_tasks[bg_id]["session_id"] == "session_a"
    assert bg.background_tasks[bg_id]["run_id"] == "run_a"
    assert bg.background_tasks[bg_id]["workspace"] == str(tmp_path)

    with event_context(
        session_id="session_b",
        run_id="run_b",
        source="web",
        workspace=tmp_path,
    ):
        assert bg.collect_background_results() == []

    with event_context(
        session_id="session_a",
        run_id="run_b",
        source="web",
        workspace=tmp_path,
    ):
        notes = bg.collect_background_results()
    assert notes == []

    with event_context(
        session_id="session_a",
        run_id="run_a",
        source="web",
        workspace=tmp_path,
    ):
        notes = bg.collect_background_results()
    assert len(notes) == 1
    assert "<task_id>bg_0001</task_id>" in notes[0]
    assert "done a" in notes[0]

    assert bg.collect_background_results() == []

    with event_context(
        session_id="session_a",
        run_id="run_a",
        source="web",
        workspace=tmp_path,
    ):
        assert bg.collect_background_results() == []


def test_background_failure_is_not_delivered_to_next_run(tmp_path):
    from coding_agent import background as bg
    from coding_agent.runtime.events import event_context

    bg.background_tasks.clear()
    bg.background_results.clear()
    block = SimpleNamespace(
        id="toolu_bg_failed",
        name="bash",
        input={"command": "pytest"},
    )

    def fail(command):
        raise RuntimeError("background failed")

    with event_context(
        session_id="session_a",
        run_id="run_a",
        source="web",
        workspace=tmp_path,
    ):
        bg_id = bg.start_background_task(block, {"bash": fail})

    deadline = time.time() + 2
    while time.time() < deadline:
        if bg.background_tasks[bg_id]["status"] == "failed":
            break
        time.sleep(0.01)

    with event_context(
        session_id="session_a",
        run_id="run_b",
        source="web",
        workspace=tmp_path,
    ):
        notes = bg.collect_background_results()
    assert notes == []

    with event_context(
        session_id="session_a",
        run_id="run_a",
        source="web",
        workspace=tmp_path,
    ):
        notes = bg.collect_background_results()
    assert len(notes) == 1
    assert "<status>failed</status>" in notes[0]
    assert "background failed" in notes[0]


def test_background_results_without_context_still_collect_for_cli():
    from coding_agent import background as bg

    bg.background_tasks.clear()
    bg.background_results.clear()
    bg._bg_counter = 0
    block = SimpleNamespace(
        id="toolu_bg_cli",
        name="bash",
        input={"command": "pytest"},
    )

    bg_id = bg.start_background_task(block, {"bash": lambda command: "done cli"})

    deadline = time.time() + 2
    while time.time() < deadline:
        if bg.background_tasks[bg_id]["status"] == "completed":
            break
        time.sleep(0.01)
    assert bg.background_tasks[bg_id]["status"] == "completed"
    assert bg.background_tasks[bg_id]["context"] == {}

    notes = bg.collect_background_results()
    assert len(notes) == 1
    assert "<task_id>bg_0001</task_id>" in notes[0]
    assert "done cli" in notes[0]


def test_remove_worktree_refuses_when_git_log_status_unverifiable(
        monkeypatch, tmp_path):
    from coding_agent.task_system import worktrees as worktrees_mod

    worktrees_dir = tmp_path / ".worktrees"
    path = worktrees_dir / "demo"
    path.mkdir(parents=True)
    monkeypatch.setattr(worktrees_mod, "WORKDIR", tmp_path)
    monkeypatch.setattr(worktrees_mod, "WORKTREES_DIR", worktrees_dir)
    calls = []

    def fake_run(args, cwd, capture_output, text, timeout):
        calls.append((args, cwd))
        if args == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["git", "log", "@{push}..HEAD", "--oneline"]:
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr="fatal: no upstream configured",
            )
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(worktrees_mod.subprocess, "run", fake_run)

    result = worktrees_mod.remove_worktree("demo", discard_changes=False)

    assert result.startswith("Cannot verify worktree status")
    assert "discard_changes=true" in result
    assert path.exists()
    assert all(call[0] != ["git", "worktree", "remove", str(path), "--force"]
               for call in calls)


def test_worktree_change_count_falls_back_to_workspace_head_without_upstream(
        monkeypatch, tmp_path):
    from coding_agent.task_system import worktrees as worktrees_mod

    worktree = tmp_path / ".worktrees" / "demo"
    worktree.mkdir(parents=True)
    base_commit = "a" * 40

    def fake_run(args, cwd, capture_output, text, timeout):
        if args == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["git", "log", "@{push}..HEAD", "--oneline"]:
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr="fatal: no upstream configured",
            )
        if args == ["git", "rev-parse", "HEAD"]:
            assert Path(cwd).resolve() == tmp_path.resolve()
            return SimpleNamespace(
                returncode=0,
                stdout=f"{base_commit}\n",
                stderr="",
            )
        if args == [
            "git", "log", f"{base_commit}..HEAD", "--oneline"
        ]:
            assert Path(cwd).resolve() == worktree.resolve()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(worktrees_mod, "WORKDIR", tmp_path)
    monkeypatch.setattr(worktrees_mod.subprocess, "run", fake_run)

    verified, files, commits, reason = (
        worktrees_mod._count_worktree_changes(worktree)
    )

    assert (verified, files, commits, reason) == (True, 0, 0, "")


def test_task_id_path_hardening_rejects_path_escape(monkeypatch, tmp_path):
    from coding_agent.task_system import tasks as tasks_mod
    from coding_agent.tools import registry as registry_mod

    tasks_dir = tmp_path / ".tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(tasks_mod, "TASKS_DIR", tasks_dir)

    task = tasks_mod.Task(
        id="task_safe-1.2",
        subject="Safe task",
        description="",
        status="pending",
        owner=None,
        blockedBy=[],
    )
    tasks_mod.save_task(task)
    assert tasks_mod.load_task("task_safe-1.2").subject == "Safe task"

    for invalid in ("../outside", r"..\outside", str(tmp_path / "x"), "bad/id"):
        with pytest.raises(ValueError):
            tasks_mod.load_task(invalid)

    result = registry_mod.run_get_task("../outside")
    assert result.startswith("Error: task ../outside not found")


def test_corrupt_task_isolated_from_task_listing(tmp_path):
    from coding_agent.runtime.execution import execution_context
    from coding_agent.task_system.tasks import (
        create_task,
        list_tasks,
        load_task,
    )

    with execution_context(workspace=tmp_path, source="web"):
        valid = create_task("valid")
        corrupt = tmp_path / ".tasks" / "task_broken.json"
        corrupt.write_text("{not valid json", encoding="utf-8")

        assert [task.id for task in list_tasks()] == [valid.id]
        with pytest.raises(ValueError, match="Corrupt task file"):
            load_task("task_broken")

    event_path = tmp_path / ".agent_events" / "events.jsonl"
    events = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    corrupt_events = [
        event for event in events
        if event["type"] == "task_file_corrupt"
    ]
    assert len(corrupt_events) == 2
    assert all(
        event["payload"]["file"] == "task_broken.json"
        for event in corrupt_events
    )


def test_concurrent_task_claim_has_one_winner(tmp_path):
    import threading

    from coding_agent.runtime.execution import execution_context
    from coding_agent.task_system.tasks import claim_task, create_task, load_task

    with execution_context(workspace=tmp_path, source="web"):
        task = create_task("race")

    barrier = threading.Barrier(2)
    results = []

    def worker(owner):
        with execution_context(workspace=tmp_path, source="web"):
            barrier.wait(timeout=2)
            results.append(claim_task(task.id, owner))

    first = threading.Thread(target=worker, args=("alice",))
    second = threading.Thread(target=worker, args=("bob",))
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert sum(result.startswith("Claimed") for result in results) == 1
    with execution_context(workspace=tmp_path, source="web"):
        assert load_task(task.id).owner in {"alice", "bob"}


def test_cron_queue_is_scoped_to_session(tmp_path):
    from coding_agent import cron_scheduler as cron
    from coding_agent.runtime.execution import execution_context

    with execution_context(
        workspace=tmp_path,
        session_id="session_a",
        source="web",
    ):
        _, _, queue, _ = cron._workspace_state()
        queue.clear()
        queue.append(cron.CronJob(
            "cron_a",
            "* * * * *",
            "for a",
            False,
            False,
            session_id="session_a",
        ))

    with execution_context(
        workspace=tmp_path,
        session_id="session_b",
        source="web",
    ):
        assert cron.consume_cron_queue() == []

    with execution_context(
        workspace=tmp_path,
        session_id="session_a",
        source="web",
    ):
        assert [job.id for job in cron.consume_cron_queue()] == ["cron_a"]


def test_message_bus_validates_mailbox_names_and_preserves_messages(
        monkeypatch, tmp_path):
    from coding_agent import teams as teams_mod

    monkeypatch.chdir(tmp_path)
    mailbox_dir = tmp_path / ".mailboxes"
    mailbox_dir.mkdir()
    monkeypatch.setattr(teams_mod, "MAILBOX_DIR", mailbox_dir)
    bus = teams_mod.MessageBus()

    with pytest.raises(ValueError):
        bus.send("lead", "../alice", "bad")
    with pytest.raises(ValueError):
        bus.send("../lead", "alice", "bad")
    with pytest.raises(ValueError):
        bus.read_inbox(r"..\alice")

    bus.send("lead", "alice", "first")
    bus.send("lead", "alice", "second")
    messages = bus.read_inbox("alice")
    assert [message["content"] for message in messages] == ["first", "second"]

    bus.send("lead", "alice", "third")
    messages = bus.read_inbox("alice")
    assert [message["content"] for message in messages] == ["third"]


def test_basic_file_tools_use_utf8_explicitly(monkeypatch, tmp_path):
    from coding_agent.tools import basic as basic_mod

    monkeypatch.setattr(basic_mod, "WORKDIR", tmp_path)

    assert basic_mod.run_write("notes/utf8.txt", "中文内容") == (
        "Wrote 4 bytes to notes/utf8.txt"
    )
    raw = (tmp_path / "notes" / "utf8.txt").read_bytes()
    assert raw == "中文内容".encode("utf-8")

    assert basic_mod.run_read("notes/utf8.txt") == "中文内容"
    assert basic_mod.run_edit("notes/utf8.txt", "内容", "文本") == (
        "Edited notes/utf8.txt"
    )
    assert basic_mod.run_read("notes/utf8.txt") == "中文文本"
def test_registry_send_message_invalid_agent_returns_tool_result():
    from coding_agent.tools import registry as registry_mod

    result = registry_mod.run_send_message("../alice", "bad")

    assert result.startswith("Error: Invalid agent name")


def _run_teammate_tool_for_result(monkeypatch, tmp_path, tool_name, tool_input,
                                  bus=None):
    from coding_agent import teams as teams_mod
    from coding_agent.providers.base import TextBlock, ToolUseBlock
    from coding_agent.task_system import tasks as tasks_mod

    tasks_dir = tmp_path / ".tasks"
    tasks_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(tasks_mod, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(teams_mod, "TASKS_DIR", tasks_dir)
    if bus is None:
        bus = SimpleNamespace(
            sent=[],
            send=lambda *args, **kwargs: bus.sent.append((args, kwargs)),
            read_inbox=lambda agent: [],
        )
    monkeypatch.setattr(teams_mod, "BUS", bus)
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    monkeypatch.setattr(teams_mod, "IDLE_POLL_INTERVAL", 1)

    class FakeProvider:
        name = "fake-provider"

        def __init__(self):
            self.calls = 0
            self.tool_result = None

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    content=[ToolUseBlock(
                        id="call_invalid",
                        name=tool_name,
                        input=tool_input,
                    )],
                    stop_reason="tool_use",
                )
            for message in reversed(kwargs["messages"]):
                content = message.get("content")
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict) and first.get("type") == "tool_result":
                        self.tool_result = first.get("content")
                        break
            return SimpleNamespace(
                content=[TextBlock("done")],
                stop_reason="end_turn",
            )

    provider = FakeProvider()
    monkeypatch.setattr(teams_mod, "get_model_provider", lambda: provider)
    teams_mod.active_teammates.clear()
    try:
        result = teams_mod.spawn_teammate_thread(
            "alice", "implementer", "run invalid tool")
        deadline = time.time() + 2
        while time.time() < deadline and "alice" in teams_mod.active_teammates:
            time.sleep(0.01)
    finally:
        teams_mod.active_teammates.pop("alice", None)

    assert result == "Teammate 'alice' spawned as implementer"
    assert provider.tool_result is not None
    return provider.tool_result


def test_teammate_invalid_send_message_returns_tool_result(monkeypatch, tmp_path):
    from coding_agent import teams as teams_mod

    mailbox_dir = tmp_path / ".mailboxes"
    mailbox_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(teams_mod, "MAILBOX_DIR", mailbox_dir)
    bus = teams_mod.MessageBus()

    output = _run_teammate_tool_for_result(
        monkeypatch,
        tmp_path,
        "send_message",
        {"to": "../lead", "content": "bad"},
        bus=bus,
    )

    assert output.startswith("Error: Invalid agent name")


@pytest.mark.parametrize("tool_name", ["claim_task", "complete_task"])
def test_teammate_invalid_task_id_returns_tool_result(
        monkeypatch, tmp_path, tool_name):
    output = _run_teammate_tool_for_result(
        monkeypatch,
        tmp_path,
        tool_name,
        {"task_id": "../outside"},
    )

    assert output.startswith("Error: Invalid task_id")
