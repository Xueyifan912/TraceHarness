import os
import json

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _read_events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_memory_append_creates_memory_file(tmp_path):
    from coding_agent.memory.store import append_memory, memory_path

    result = append_memory("User prefers concise summaries.", workspace=tmp_path)

    path = memory_path(tmp_path)
    assert result == "Appended memory (31 chars)"
    assert path == tmp_path.resolve() / ".memory" / "MEMORY.md"
    assert path.exists()
    assert "User prefers concise summaries." in path.read_text(encoding="utf-8")


def test_memory_append_audit_event_records_length_only(tmp_path):
    from coding_agent.memory.store import append_memory

    memory_text = "private persistent fact"

    result = append_memory(memory_text, workspace=tmp_path)

    assert result == f"Appended memory ({len(memory_text)} chars)"
    events = _read_events(tmp_path)
    memory_events = [event for event in events if event["type"] == "memory_append"]
    assert len(memory_events) == 1
    payload = memory_events[0]["payload"]
    assert payload["content_length"] == len(memory_text)
    assert "content" not in payload
    assert memory_text not in (tmp_path / ".agent_events" / "events.jsonl").read_text()


def test_memory_read_empty_state(tmp_path):
    from coding_agent.memory.store import read_memory_for_tool

    assert read_memory_for_tool(tmp_path) == "(memory empty)"


def test_memory_read_after_append(tmp_path):
    from coding_agent.memory.store import append_memory, read_memory_for_tool

    append_memory("Project uses pytest.", workspace=tmp_path)

    assert "Project uses pytest." in read_memory_for_tool(tmp_path)


def test_memory_append_rejects_empty_content(tmp_path):
    from coding_agent.memory.store import append_memory, memory_path

    result = append_memory("   ", workspace=tmp_path)

    assert result == "Error: memory content is empty"
    assert not memory_path(tmp_path).exists()


def test_memory_tools_do_not_accept_arbitrary_paths():
    from coding_agent.tools.registry import assemble_tool_pool

    tools, handlers = assemble_tool_pool()
    by_name = {tool["name"]: tool for tool in tools}

    assert "memory_read" in by_name
    assert "memory_append" in by_name
    assert by_name["memory_read"]["input_schema"]["properties"] == {}
    assert set(by_name["memory_append"]["input_schema"]["properties"]) == {"content"}
    assert "path" not in by_name["memory_append"]["input_schema"]["properties"]
    assert "memory_read" in handlers
    assert "memory_append" in handlers


def test_update_context_and_system_prompt_inject_memory(monkeypatch, tmp_path):
    from coding_agent.memory import context as memory_context
    from coding_agent.memory.store import append_memory

    append_memory("Persistent project fact.", workspace=tmp_path)
    monkeypatch.setattr(memory_context, "WORKDIR", tmp_path)

    context = memory_context.update_context({}, [])
    prompt = memory_context.assemble_system_prompt(context)

    assert "Persistent project fact." in context["memories"]
    assert "Relevant memories:" in prompt
    assert "Persistent project fact." in prompt


def test_large_memory_read_response_is_bounded(tmp_path):
    from coding_agent.memory.store import append_memory, read_memory_for_tool

    large_memory = "m" * 6000
    append_memory(large_memory, workspace=tmp_path)

    output = read_memory_for_tool(tmp_path)

    assert large_memory not in output
    assert output.startswith("[Memory preview:")
    assert "truncated" in output
    assert len(output) < len(large_memory)


def test_skill_manifest_is_read_as_utf8(monkeypatch, tmp_path):
    from coding_agent.memory import skills as skills_mod

    skills_dir = tmp_path / ".skills"
    manifest_dir = skills_dir / "chinese"
    manifest_dir.mkdir(parents=True)
    manifest_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: 中文技能\n"
        "description: 处理中文路径\n"
        "---\n"
        "# 内容\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", skills_dir)

    skills_mod.scan_skills()

    assert skills_mod.SKILL_REGISTRY["中文技能"]["description"] == "处理中文路径"
    assert "# 内容" in skills_mod.load_skill("中文技能")
