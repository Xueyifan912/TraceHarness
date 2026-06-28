import json
import os
from pathlib import Path

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def test_create_session_record_in_workspace(tmp_path):
    from coding_agent.runtime.session import create_session

    session = create_session(tmp_path)

    assert session.path.parent == tmp_path / ".agent_sessions"
    assert session.path.exists()
    data = json.loads(session.path.read_text(encoding="utf-8"))
    assert data["session_id"] == session.session_id
    assert data["workspace_path"] == str(tmp_path.resolve())
    assert data["message_count"] == 0
    assert data["messages"] == []


def test_save_and_load_session_snapshot(tmp_path):
    from coding_agent.runtime.session import (
        create_session,
        load_latest_session,
        load_session,
        save_session_snapshot,
    )

    session = create_session(tmp_path)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    assert save_session_snapshot(session, messages) is True

    loaded = load_session(session.session_id, tmp_path)
    latest = load_latest_session(tmp_path)
    assert loaded == latest
    assert loaded["session_id"] == session.session_id
    assert loaded["message_count"] == 2
    assert loaded["last_user_prompt_preview"] == {
        "preview": "hello",
        "length": 5,
        "truncated": False,
    }
    assert loaded["messages"] == messages
    assert loaded["display_messages"] == messages
    assert loaded["display_message_count"] == 2


def test_generated_session_id_can_load(tmp_path):
    from coding_agent.runtime.session import create_session, load_session

    session = create_session(tmp_path)

    loaded = load_session(session.session_id, tmp_path)

    assert loaded is not None
    assert loaded["session_id"] == session.session_id


def test_load_session_rejects_path_traversal(tmp_path):
    from coding_agent.runtime.session import load_session

    assert load_session("../outside", tmp_path) is None
    assert load_session("..\\outside", tmp_path) is None


def test_load_session_rejects_absolute_path(tmp_path):
    from coding_agent.runtime.session import load_session

    assert load_session(str(tmp_path / "outside"), tmp_path) is None


def test_load_session_rejects_path_separators(tmp_path):
    from coding_agent.runtime.session import load_session

    assert load_session("nested/session", tmp_path) is None
    assert load_session("nested\\session", tmp_path) is None


def test_load_session_rejects_tampered_internal_session_id(tmp_path):
    from coding_agent.runtime.session import create_session, load_session

    record = create_session(tmp_path)
    payload = json.loads(record.path.read_text(encoding="utf-8"))
    payload["session_id"] = "../outside"
    record.path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_session(record.session_id, tmp_path) is None
    assert not (tmp_path.parent / "outside.json").exists()


def test_scan_recent_sessions_reports_corrupt_and_mismatched_records(tmp_path):
    from coding_agent.runtime.session import (
        create_session,
        scan_recent_sessions,
    )

    valid = create_session(tmp_path)
    session_dir = tmp_path / ".agent_sessions"
    (session_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    (session_dir / "mismatch.json").write_text(
        json.dumps({"session_id": "different"}),
        encoding="utf-8",
    )

    sessions, warnings = scan_recent_sessions(tmp_path)

    assert [item["session_id"] for item in sessions] == [valid.session_id]
    assert len(warnings) == 2
    assert any("broken.json" in warning for warning in warnings)
    assert any("mismatch.json" in warning for warning in warnings)


def test_safe_manual_session_id_can_load(tmp_path):
    from coding_agent.runtime.session import (
        SessionRecord,
        load_session,
        save_session_snapshot,
    )

    record = SessionRecord(
        session_id="manual-001",
        created_at="2026-01-01T00:00:00.000Z",
        workspace_path=str(tmp_path.resolve()),
        path=tmp_path / ".agent_sessions" / "manual-001.json",
    )

    assert save_session_snapshot(
        record, [{"role": "user", "content": "manual"}]
    ) is True

    loaded = load_session("manual-001", tmp_path)
    assert loaded is not None
    assert loaded["session_id"] == "manual-001"
    assert loaded["message_count"] == 1


def test_list_recent_sessions(tmp_path):
    from coding_agent.runtime.session import (
        create_session,
        list_recent_sessions,
        save_session_snapshot,
    )

    session = create_session(tmp_path)
    save_session_snapshot(session, [{"role": "user", "content": "one"}])

    recent = list_recent_sessions(tmp_path)

    assert len(recent) == 1
    assert recent[0]["session_id"] == session.session_id
    assert recent[0]["message_count"] == 1
    assert Path(recent[0]["path"]).exists()


def test_large_tool_result_is_not_fully_duplicated(tmp_path):
    from coding_agent.runtime.session import (
        create_session,
        load_session,
        save_session_snapshot,
    )

    session = create_session(tmp_path)
    large_output = "z" * 5000
    messages = [
        {"role": "user", "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_large",
                "content": large_output,
            }
        ]}
    ]

    assert save_session_snapshot(session, messages) is True

    raw_snapshot = session.path.read_text(encoding="utf-8")
    assert large_output not in raw_snapshot

    loaded = load_session(session.session_id, tmp_path)
    content = loaded["messages"][0]["content"][0]["content"]
    assert isinstance(content, str)
    assert "Tool result truncated in saved session" in content
    assert "original length: 5000 chars" in content
    assert len(content) < 5000


def test_load_session_normalizes_legacy_tool_result_preview(tmp_path):
    from coding_agent.runtime.session import load_session

    session_dir = tmp_path / ".agent_sessions"
    session_dir.mkdir()
    payload = {
        "snapshot_version": 1,
        "session_id": "legacy-preview",
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
        "workspace_path": str(tmp_path.resolve()),
        "message_count": 1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_legacy",
                        "content": {
                            "preview": "legacy output preview",
                            "length": 2118,
                            "truncated": True,
                        },
                    }
                ],
            }
        ],
    }
    (session_dir / "legacy-preview.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    loaded = load_session("legacy-preview", tmp_path)
    content = loaded["messages"][0]["content"][0]["content"]

    assert isinstance(content, str)
    assert "Tool result truncated in saved session" in content
    assert "original length: 2118 chars" in content
    assert "legacy output preview" in content


def test_session_write_failure_does_not_crash(monkeypatch, tmp_path):
    from coding_agent.runtime import session as session_mod

    record = session_mod.SessionRecord(
        session_id="session_failure",
        created_at="2026-01-01T00:00:00.000Z",
        workspace_path=str(tmp_path),
        path=tmp_path / ".agent_sessions" / "session_failure.json",
    )

    def failing_write(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(session_mod, "_write_json", failing_write)

    assert session_mod.save_session_snapshot(
        record, [{"role": "user", "content": "hello"}]
    ) is False


def test_archive_session_moves_snapshot_out_of_active_listing(tmp_path):
    from coding_agent.runtime.session import (
        archive_session,
        create_session,
        list_recent_sessions,
        load_session,
    )

    session = create_session(tmp_path)

    archived = archive_session(session.session_id, tmp_path)

    assert archived == (
        tmp_path / ".agent_sessions" / "archive" /
        f"{session.session_id}.json"
    )
    assert archived.exists()
    assert not session.path.exists()
    assert load_session(session.session_id, tmp_path) is None
    assert list_recent_sessions(tmp_path) == []
