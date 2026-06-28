import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _block(name, tool_input=None, tool_id="toolu_test"):
    return SimpleNamespace(name=name, id=tool_id, input=tool_input or {})


def _read_events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _event_text(workspace):
    return (workspace / ".agent_events" / "events.jsonl").read_text()


def test_default_policy_denies_obviously_dangerous_bash(tmp_path):
    from coding_agent.security.policy import default_policy, evaluate_tool_use

    decision = evaluate_tool_use(
        _block("bash", {"command": "sudo reboot"}),
        policy=default_policy(),
        workspace=tmp_path,
    )

    assert decision.action == "deny"
    assert decision.rule == "sudo"
    assert "deny list" in decision.reason


def test_default_policy_asks_for_destructive_bash(tmp_path):
    from coding_agent.security.policy import default_policy, evaluate_tool_use

    decision = evaluate_tool_use(
        _block("bash", {"command": "rm build/output.txt"}),
        policy=default_policy(),
        workspace=tmp_path,
    )

    assert decision.action == "ask"
    assert decision.rule == "rm "


def test_default_policy_asks_for_indirect_shell_execution(tmp_path):
    from coding_agent.security.policy import evaluate_tool_use

    decision = evaluate_tool_use(
        _block(
            "bash",
            {
                "command": (
                    "python -c \"open('target','w').write('x')\""
                )
            },
        ),
        workspace=tmp_path,
    )

    assert decision.action == "ask"
    assert decision.rule == "bash_default_action"
    assert decision.reason == "Shell commands require explicit approval"


@pytest.mark.parametrize(
    ("tool", "tool_input", "rule"),
    [
        ("connect_mcp", {"name": "local-server"}, "mcp_process_start"),
        (
            "remove_worktree",
            {"name": "feature", "discard_changes": True},
            "worktree_removal",
        ),
    ],
)
def test_process_start_and_worktree_removal_require_approval(
    tool,
    tool_input,
    rule,
    tmp_path,
):
    from coding_agent.security.policy import evaluate_tool_use

    decision = evaluate_tool_use(
        _block(tool, tool_input),
        workspace=tmp_path,
    )

    assert decision.action == "ask"
    assert decision.rule == rule


def test_workspace_policy_can_explicitly_allow_unmatched_shell(tmp_path):
    from coding_agent.security.policy import evaluate_tool_use, load_policy

    (tmp_path / ".agent_policy.yaml").write_text(
        "bash:\n  default_action: allow\n",
        encoding="utf-8",
    )
    decision = evaluate_tool_use(
        _block("bash", {"command": "echo safe"}),
        policy=load_policy(tmp_path),
        workspace=tmp_path,
    )

    assert decision.action == "allow"


@pytest.mark.parametrize(
    "command",
    [
        r"type C:\Users\Public\outside.txt",
        r"Get-Content ..\outside.txt",
        r"cat $HOME/.ssh/config",
        r"cat /tmp/../etc/hosts",
    ],
)
def test_default_policy_asks_before_shell_access_outside_workspace(
    command,
    tmp_path,
):
    from coding_agent.security.policy import evaluate_tool_use

    decision = evaluate_tool_use(
        _block("bash", {"command": command}),
        workspace=tmp_path,
    )

    assert decision.action == "ask"


@pytest.mark.parametrize(
    "command",
    [
        r"powershell -NoProfile -Command Remove-Item -Recurse -Force C:\temp\victim",
        r"pwsh -Command Remove-Item -Recurse -Force C:\temp\victim",
        r"cmd /c rmdir /s /q C:\temp\victim",
        r"CMD /C DEL /F /Q C:\temp\victim.txt",
    ],
)
def test_default_policy_does_not_allow_windows_destructive_commands(
    command,
    tmp_path,
):
    from coding_agent.security.policy import evaluate_tool_use

    decision = evaluate_tool_use(
        _block("bash", {"command": command}),
        workspace=tmp_path,
    )

    assert decision.action in {"ask", "deny"}
    assert decision.reason == "Destructive-looking bash command"


def test_default_policy_denies_workspace_path_escape(tmp_path):
    from coding_agent.security.policy import default_policy, evaluate_tool_use

    decision = evaluate_tool_use(
        _block("write_file", {"path": "../outside.txt", "content": "x"}),
        policy=default_policy(),
        workspace=tmp_path,
    )

    assert decision.action == "deny"
    assert decision.rule == "workspace_path"
    assert "path escapes workspace" in decision.reason


def test_workspace_policy_extends_default_rules(tmp_path):
    from coding_agent.security.policy import evaluate_tool_use, load_policy

    (tmp_path / ".agent_policy.yaml").write_text(
        "bash:\n"
        "  extend_deny_patterns:\n"
        "    - custom-danger\n",
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)
    custom = evaluate_tool_use(
        _block("bash", {"command": "echo custom-danger"}),
        policy=policy,
        workspace=tmp_path,
    )
    default = evaluate_tool_use(
        _block("bash", {"command": "sudo ls"}),
        policy=policy,
        workspace=tmp_path,
    )

    assert custom.action == "deny"
    assert custom.rule == "custom-danger"
    assert default.action == "deny"
    assert default.rule == "sudo"


def test_permission_hook_deny_does_not_prompt_and_audits(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    def fail_input(*args, **kwargs):
        raise AssertionError("input should not be called for deny decisions")

    monkeypatch.setattr("builtins.input", fail_input)

    result = hooks.permission_hook(
        _block("bash", {"command": "sudo reboot"}, "toolu_deny"))

    assert result == "Permission denied: 'sudo' is on the deny list"
    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == ["permission_decision"]
    payload = events[0]["payload"]
    assert payload["action"] == "deny"
    assert payload["tool"] == "bash"
    assert payload["rule"] == "sudo"
    assert payload["tool_use_id"] == "toolu_deny"


def test_permission_decision_does_not_log_long_denied_command(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    command = "sudo " + ("x" * 1500)

    def fail_input(*args, **kwargs):
        raise AssertionError("input should not be called for deny decisions")

    monkeypatch.setattr("builtins.input", fail_input)

    result = hooks.permission_hook(
        _block("bash", {"command": command}, "toolu_long_deny"))

    assert result == "Permission denied: 'sudo' is on the deny list"
    assert command not in _event_text(tmp_path)

    events = _read_events(tmp_path)
    subject = events[0]["payload"]["subject"]
    assert subject["length"] == len(command)
    assert subject["truncated"] is True
    assert len(subject["preview"]) < subject["length"]


def test_permission_hook_ask_uses_confirmation_and_audits(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "n")

    result = hooks.permission_hook(
        _block("bash", {"command": "rm build/output.txt"}, "toolu_ask"))

    assert result == "Permission denied by user"
    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == [
        "permission_decision",
        "permission_decision",
    ]
    assert events[0]["payload"]["action"] == "ask"
    assert events[0]["payload"]["rule"] == "rm "
    assert events[1]["payload"]["action"] == "deny"
    assert events[1]["payload"]["source"] == "user_confirmation"


def test_permission_decision_ask_and_confirmation_do_not_log_long_command(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    command = "rm " + ("y" * 1500)

    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "n")

    result = hooks.permission_hook(
        _block("bash", {"command": command}, "toolu_long_ask"))

    assert result == "Permission denied by user"
    assert command not in _event_text(tmp_path)

    events = _read_events(tmp_path)
    assert [event["payload"]["action"] for event in events] == ["ask", "deny"]
    for event in events:
        subject = event["payload"]["subject"]
        assert subject["length"] == len(command)
        assert subject["truncated"] is True
        assert len(subject["preview"]) < subject["length"]


def test_permission_hook_allows_and_audits(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    result = hooks.permission_hook(
        _block("read_file", {"path": "README.md"}, "toolu_allow"))

    assert result is None
    events = _read_events(tmp_path)
    assert events[0]["type"] == "permission_decision"
    assert events[0]["payload"]["action"] == "allow"
    assert events[0]["payload"]["tool"] == "read_file"
