from ..config import terminal_print


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            terminal_print(content)
            continue
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            block_text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if block_type == "text" and block_text:
                terminal_print(block_text)
