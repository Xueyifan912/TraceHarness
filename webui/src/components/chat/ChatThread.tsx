import { Loader2, MessageSquare } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ChatMessage, SessionSummary } from "../../api/types";
import { MessageBubble } from "./MessageBubble";

interface ChatThreadProps {
  session: SessionSummary | null;
  messages: ChatMessage[];
  isLoading: boolean;
  isRunning: boolean;
}

function isContentBlock(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object");
}

function isTextBlock(value: unknown): boolean {
  return isContentBlock(value) && value.type === "text" && typeof value.text === "string" && value.text.trim().length > 0;
}

function isToolActivityBlock(value: unknown): boolean {
  return isContentBlock(value) && (value.type === "tool_use" || value.type === "tool_result");
}

function isInternalMessage(message: ChatMessage): boolean {
  if (message._internal === true) {
    return true;
  }
  if (message.role !== "user" || typeof message.content !== "string") {
    return false;
  }
  const content = message.content.trim();
  return (
    content.startsWith("[Compacted]") ||
    content.startsWith("[Reactive compact]") ||
    content.startsWith("[snipped ") ||
    content.startsWith("[Scheduled] ") ||
    content === "[Compacted. Continue with summarized context.]" ||
    content === "<reminder>Update your todos.</reminder>" ||
    content === "Continue from the previous response. Do not repeat completed work."
  );
}

function chatDisplayMessage(message: ChatMessage): ChatMessage | null {
  if (isInternalMessage(message)) {
    return null;
  }
  const content = message.content;
  if (typeof content === "string") {
    return content.trim().length > 0 ? message : null;
  }
  if (Array.isArray(content)) {
    if (
      message.role === "assistant" &&
      content.some((block) => isContentBlock(block) && block.type === "tool_use")
    ) {
      return null;
    }
    const textBlocks = content.filter(isTextBlock);
    if (textBlocks.length > 0) {
      return { ...message, content: textBlocks };
    }
    const nonToolBlocks = content.filter((block) => !isToolActivityBlock(block));
    if (nonToolBlocks.length > 0 && message.role !== "user") {
      return { ...message, content: nonToolBlocks };
    }
    return null;
  }
  if (isToolActivityBlock(content)) {
    return null;
  }
  return content === undefined || content === null ? null : message;
}

export function ChatThread({ session, messages, isLoading, isRunning }: ChatThreadProps) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const lastSessionRef = useRef<string | null>(null);
  const [visibleLimit, setVisibleLimit] = useState(200);
  const allVisibleMessages = messages
    .map(chatDisplayMessage)
    .filter((message): message is ChatMessage => message !== null);
  const hiddenMessageCount = Math.max(0, allVisibleMessages.length - visibleLimit);
  const visibleMessages = allVisibleMessages.slice(hiddenMessageCount);

  useEffect(() => {
    setVisibleLimit(200);
  }, [session?.session_id]);

  useEffect(() => {
    const end = endRef.current;
    const scrollContainer = end?.closest(".chat-body") as HTMLElement | null;
    if (!end || !scrollContainer) {
      return;
    }
    const sessionId = session?.session_id ?? null;
    const sessionChanged = lastSessionRef.current !== sessionId;
    lastSessionRef.current = sessionId;
    const distanceFromBottom =
      scrollContainer.scrollHeight -
      scrollContainer.scrollTop -
      scrollContainer.clientHeight;
    if (sessionChanged || distanceFromBottom < 200) {
      end.scrollIntoView({ block: "end" });
    }
  }, [session?.session_id, visibleMessages.length, isRunning]);

  if (!session) {
    return (
      <div className="thread-empty">
        <MessageSquare size={22} />
        <h2>选择或新建一个会话</h2>
        <p>左侧会话列表用于管理本地 session，中间区域展示对话结果。</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="thread-empty">
        <Loader2 size={22} className="spin" />
        <h2>正在加载会话</h2>
      </div>
    );
  }

  return (
    <div className="thread">
      {visibleMessages.length === 0 ? (
        <div className="thread-empty inline">
          <MessageSquare size={20} />
          <h2>当前会话没有消息</h2>
          <p>在下方输入中文需求，发送后会等待同步 API 返回并刷新会话。</p>
        </div>
      ) : (
        <>
          {hiddenMessageCount > 0 ? (
            <button
              className="load-history-button"
              type="button"
              onClick={() => setVisibleLimit((current) => current + 200)}
            >
              加载更早的 {Math.min(hiddenMessageCount, 200)} 条消息
            </button>
          ) : null}
          {visibleMessages.map((message, index) => (
            <MessageBubble
              key={`${message.role}-${hiddenMessageCount + index}`}
              message={message}
            />
          ))}
        </>
      )}

      {isRunning ? (
        <div className="running-row">
          <Loader2 size={16} className="spin" />
          <span>Agent 正在运行，等待后端同步返回...</span>
        </div>
      ) : null}
      <div ref={endRef} />
    </div>
  );
}
