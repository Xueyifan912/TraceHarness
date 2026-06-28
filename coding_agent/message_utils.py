INTERNAL_MESSAGE_KEY = "_internal"
_LEGACY_INTERNAL_PREFIXES = (
    "[Compacted]\n",
    "[Reactive compact]\n",
    "[snipped ",
)
_LEGACY_INTERNAL_MESSAGES = {
    "[Compacted. Continue with summarized context.]",
    "<reminder>Update your todos.</reminder>",
    "Continue from the previous response. Do not repeat completed work.",
}


def internal_user_message(content) -> dict:
    return {
        "role": "user",
        "content": content,
        INTERNAL_MESSAGE_KEY: True,
    }


def is_internal_message(message) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get(INTERNAL_MESSAGE_KEY) is True:
        return True
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    return (
        stripped in _LEGACY_INTERNAL_MESSAGES
        or stripped.startswith("[Scheduled] ")
        or any(content.startswith(prefix) for prefix in _LEGACY_INTERNAL_PREFIXES)
    )


def is_user_visible_message(message) -> bool:
    if not isinstance(message, dict) or is_internal_message(message):
        return False
    role = message.get("role")
    content = message.get("content")
    if isinstance(content, str):
        return role in {"user", "assistant"} and bool(content.strip())
    if not isinstance(content, list):
        return False
    if role == "user":
        return False
    if role != "assistant":
        return False
    # Assistant turns that contain a tool request are intermediate reasoning /
    # progress turns.  They belong in the runtime transcript and inspector, not
    # in the user-facing chat history.
    if has_tool_use(content):
        return False
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "text" and str(block.get("text") or "").strip():
                return True
            if block_type not in {"tool_use", "tool_result"}:
                return True
        elif getattr(block, "type", None) == "text":
            if str(getattr(block, "text", "") or "").strip():
                return True
    return False


def public_chat_messages(messages: list) -> list:
    return [
        message
        for message in messages
        if is_user_visible_message(message)
    ]


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def has_tool_use(content) -> bool:
    # Do not rely on stop_reason alone; the concrete tool_use block is the
    # continuation signal used by the loop.
    if not isinstance(content, list):
        return False
    return any(
        (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        ) == "tool_use"
        for block in content
    )
