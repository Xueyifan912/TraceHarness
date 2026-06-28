import type { ChatMessage, GenericContentBlock, TextContentBlock } from "../../api/types";

interface MessageBubbleProps {
  message: ChatMessage;
}

type ActivityPart = {
  label: string;
  detail: string;
  kind: "tool_use" | "tool_result" | "raw";
};

function roleLabel(role: string, activities: ActivityPart[]): string {
  if (activities.some((activity) => activity.kind === "tool_result")) {
    return "工具结果";
  }
  if (activities.some((activity) => activity.kind === "tool_use") || role === "tool") {
    return "工具";
  }
  if (role === "user") {
    return "你";
  }
  if (role === "assistant") {
    return "助手";
  }
  if (role === "system") {
    return "系统";
  }
  return role;
}

function isTextBlock(block: TextContentBlock | GenericContentBlock): block is TextContentBlock {
  return block.type === "text" && typeof block.text === "string";
}

function stringifyBlock(block: unknown): string {
  if (typeof block === "string") {
    return block;
  }
  try {
    return JSON.stringify(block, null, 2);
  } catch {
    return String(block);
  }
}

function previewValue(value: unknown): unknown {
  if (value && typeof value === "object" && "preview" in value) {
    return (value as { preview?: unknown }).preview;
  }
  return value;
}

function activityFromBlock(block: GenericContentBlock | Record<string, unknown>): ActivityPart {
  const blockType = typeof block.type === "string" ? block.type : "内容块";
  if (blockType === "tool_use") {
    const toolName = typeof block.name === "string" && block.name ? block.name : "工具";
    return {
      label: `工具调用：${toolName}`,
      detail: stringifyBlock(block.input ?? block),
      kind: "tool_use"
    };
  }

  if (blockType === "tool_result") {
    return {
      label: "工具结果",
      detail: stringifyBlock(previewValue(block.content ?? block.output ?? block.result ?? block)),
      kind: "tool_result"
    };
  }

  return {
    label: blockType,
    detail: stringifyBlock(block),
    kind: "raw"
  };
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const role = message.role || "assistant";
  const content = message.content;
  const textParts: string[] = [];
  const activityParts: ActivityPart[] = [];

  if (typeof content === "string") {
    textParts.push(content);
  } else if (Array.isArray(content)) {
    content.forEach((block) => {
      if (isTextBlock(block)) {
        textParts.push(block.text);
        return;
      }

      activityParts.push(activityFromBlock(block));
    });
  } else if (content !== undefined && content !== null) {
    activityParts.push(activityFromBlock(content));
  }

  return (
    <article className={`message-bubble ${role}`}>
      <div className="message-role">{roleLabel(role, activityParts)}</div>
      <div className="message-content">
        {textParts.length > 0 ? (
          textParts.map((part, index) => (
            <p key={`${role}-text-${index}`} className="message-text">
              {part}
            </p>
          ))
        ) : activityParts.length === 0 ? (
          <p className="message-text muted-text">没有可显示的文本内容</p>
        ) : null}

        {activityParts.map((part, index) => (
          <details key={`${part.label}-${index}`} className="activity-details">
            <summary>{part.label}</summary>
            <pre>{part.detail}</pre>
          </details>
        ))}
      </div>
    </article>
  );
}
