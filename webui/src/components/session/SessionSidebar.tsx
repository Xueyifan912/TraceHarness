import { Archive, Plus, RefreshCw, Circle, MessageSquare } from "lucide-react";
import type { HealthResponse, SessionSummary } from "../../api/types";

interface SessionSidebarProps {
  sessions: SessionSummary[];
  selectedSessionId: string | null;
  health: HealthResponse | null;
  isLoading: boolean;
  isCreating: boolean;
  isArchiving: boolean;
  onCreate: () => void;
  onRefresh: () => void;
  onSelect: (sessionId: string) => void;
  onArchive: (sessionId: string) => void;
}

function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "暂无时间";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "空闲",
    running: "运行中",
    queued: "排队",
    waiting_approval: "待审批",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消"
  };
  return labels[status] ?? status;
}

export function SessionSidebar({
  sessions,
  selectedSessionId,
  health,
  isLoading,
  isCreating,
  isArchiving,
  onCreate,
  onRefresh,
  onSelect,
  onArchive
}: SessionSidebarProps) {
  const current = sessions.find((item) => item.session_id === selectedSessionId);

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <div>
          <div className="product-label">本地工作台</div>
          <h2>会话</h2>
        </div>
        <button className="icon-button" type="button" onClick={onRefresh} disabled={isLoading} title="刷新会话">
          <RefreshCw size={17} className={isLoading ? "spin" : ""} />
        </button>
      </div>

      <button className="primary-action" type="button" onClick={onCreate} disabled={isCreating}>
        <Plus size={17} />
        {isCreating ? "正在新建" : "新建会话"}
      </button>

      <div className="status-panel">
        <div className="status-row">
          <Circle size={10} className={health?.ok ? "status-dot ok" : "status-dot muted"} />
          <span>{health?.ok ? "后端已连接" : "等待后端"}</span>
        </div>
        <div className="muted-line">{current ? `当前：${statusLabel(current.status)}` : "当前：未选择会话"}</div>
      </div>

      {current ? (
        <button
          className="secondary-action"
          type="button"
          disabled={isArchiving || ["running", "queued", "waiting_approval"].includes(current.status)}
          onClick={() => onArchive(current.session_id)}
        >
          <Archive size={16} />
          {isArchiving ? "正在归档" : "归档当前会话"}
        </button>
      ) : null}

      <div className="session-list" aria-label="会话列表">
        {sessions.length === 0 ? (
          <div className="empty-panel compact">
            <MessageSquare size={18} />
            <span>还没有会话</span>
          </div>
        ) : (
          sessions.map((session) => {
            const active = session.session_id === selectedSessionId;
            const preview = session.last_user_prompt_preview?.preview || "新会话";
            return (
              <button
                key={session.session_id}
                className={`session-item ${active ? "active" : ""}`}
                type="button"
                onClick={() => onSelect(session.session_id)}
              >
                <div className="session-item-top">
                  <span className="session-title">{preview}</span>
                  <span className={`status-pill ${session.status}`}>{statusLabel(session.status)}</span>
                </div>
                <div className="session-meta">
                  <span>{formatTime(session.updated_at || session.created_at)}</span>
                  <span>{session.message_count} 条消息</span>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
