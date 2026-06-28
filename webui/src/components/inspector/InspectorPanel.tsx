import {
  Activity,
  AlertTriangle,
  Clock3,
  Database,
  Layers,
  Radio,
  RefreshCw,
  Send,
  Server,
  Users,
  Wrench
} from "lucide-react";
import type { ReactNode } from "react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { apiClient } from "../../api/client";
import {
  ApiError,
  type Approval,
  type AuditEvent,
  type McpStatusResponse,
  type MemoryResponse,
  type RunSummary,
  type SessionSummary,
  type SseConnectionStatus,
  type TaskItem,
  type TeamStatusResponse,
  type TimelineItem,
  type ToolMetadata,
  type WorktreeItem
} from "../../api/types";
import { ApprovalPanel } from "../approval/ApprovalPanel";

type InspectorTab = "run" | "timeline" | "events" | "team" | "mcp" | "tools" | "memory";

interface InspectorPanelProps {
  session: SessionSummary | null;
  currentRun: RunSummary | null;
  isRunning: boolean;
  timelineItems: TimelineItem[];
  events: AuditEvent[];
  timelineNotice: string | null;
  isLoadingTimeline?: boolean;
  approvals?: Approval[];
  pendingApproval?: Approval | null;
  lastResolvedApproval?: Approval | null;
  approvalsLoading?: boolean;
  approvalsError?: string | null;
  sseStatus?: SseConnectionStatus;
  sseError?: string | null;
  sseLastSeenAt?: string | null;
  onOpenApproval?: (approval: Approval) => void;
  onRefreshSessionActivity?: (sessionId: string) => Promise<void> | void;
}

interface TeamPanelData {
  team: TeamStatusResponse | null;
  tasks: TaskItem[];
  worktrees: WorktreeItem[];
}

const MEMORY_APPEND_MAX_LENGTH = 20480;

const tabs: Array<{ id: InspectorTab; label: string; icon: typeof Activity }> = [
  { id: "run", label: "Run", icon: Activity },
  { id: "timeline", label: "Timeline", icon: Clock3 },
  { id: "events", label: "Events", icon: Radio },
  { id: "team", label: "Team", icon: Users },
  { id: "mcp", label: "MCP", icon: Layers },
  { id: "tools", label: "Tools", icon: Wrench },
  { id: "memory", label: "Memory", icon: Database }
];

const eventTypeOptions = [
  { value: "", label: "全部事件" },
  { value: "tool_call_started", label: "工具开始" },
  { value: "tool_call_ended", label: "工具结束" },
  { value: "llm_call_started", label: "LLM 开始" },
  { value: "llm_call_ended", label: "LLM 结束" },
  { value: "permission_decision", label: "权限" },
  { value: "memory_append", label: "Memory" },
  { value: "mcp_connect", label: "MCP" }
];

function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "暂无";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(date);
}

function readableJson(value: unknown): string {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function compactValue(value: unknown): string {
  if (value === undefined || value === null || value === "") {
    return "暂无";
  }
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => compactValue(item)).join(", ") : "无";
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.text_preview === "string") {
      return record.text_preview;
    }
    if (typeof record.preview === "string") {
      return record.preview;
    }
    if (record.type === "text" && typeof record.text === "string") {
      return record.text;
    }
    if (record.type === "tool_use") {
      return `tool_use ${String(record.name ?? record.tool_use_id ?? "")}`.trim();
    }
    if (record.type === "tool_result") {
      return `tool_result ${String(record.tool_use_id ?? "")}`.trim();
    }
    return readableJson(value);
  }
  return String(value);
}

function formatBytes(value: number | null | undefined): string {
  if (!value) {
    return "0 B";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatError(error: unknown): string {
  if (error instanceof ApiError) {
    const errors = error.details.errors;
    if (Array.isArray(errors) && errors.length > 0) {
      const first = errors[0] as Record<string, unknown>;
      const loc = Array.isArray(first.loc) ? first.loc.join(".") : "";
      const msg = typeof first.msg === "string" ? first.msg : readableJson(first);
      return `${error.message}（${error.code}：${loc ? `${loc} ` : ""}${msg}）`;
    }
    return `${error.message}（${error.code}）`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}

function timelineStatusLabel(status: string | undefined): string {
  const labels: Record<string, string> = {
    running: "运行中",
    completed: "完成",
    failed: "失败",
    error: "错误",
    denied: "已拒绝",
    allowed: "已允许"
  };
  return labels[status ?? ""] ?? status ?? "未知";
}

function sseStatusLabel(status: SseConnectionStatus | undefined): string {
  const labels: Record<SseConnectionStatus, string> = {
    idle: "未连接",
    connecting: "连接中",
    connected: "已连接",
    disconnected: "已断开",
    completed: "已结束",
    failed: "运行失败",
    cancelled: "运行已取消",
    error: "SSE 连接异常"
  };
  return labels[status ?? "idle"];
}

function groupMcpToolsByServer(tools: ToolMetadata[]): Array<{ server: string; tools: ToolMetadata[] }> {
  const groups = new Map<string, ToolMetadata[]>();
  tools
    .filter((tool) => tool.source === "mcp")
    .forEach((tool) => {
      const server = tool.server || "未知 server";
      groups.set(server, [...(groups.get(server) ?? []), tool]);
    });
  return Array.from(groups.entries()).map(([server, groupedTools]) => ({
    server,
    tools: groupedTools
  }));
}

function toolDescription(tool: ToolMetadata): string {
  return tool.description || "暂无描述";
}

export function InspectorPanel({
  session,
  currentRun,
  isRunning,
  timelineItems,
  events,
  timelineNotice,
  isLoadingTimeline = false,
  approvals = [],
  pendingApproval = null,
  lastResolvedApproval = null,
  approvalsLoading = false,
  approvalsError = null,
  sseStatus = "idle",
  sseError = null,
  sseLastSeenAt = null,
  onOpenApproval,
  onRefreshSessionActivity
}: InspectorPanelProps) {
  const [activeTab, setActiveTab] = useState<InspectorTab>("run");
  const [teamData, setTeamData] = useState<TeamPanelData>({ team: null, tasks: [], worktrees: [] });
  const [teamLoading, setTeamLoading] = useState(false);
  const [teamError, setTeamError] = useState<string | null>(null);
  const [mcpStatus, setMcpStatus] = useState<McpStatusResponse | null>(null);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [mcpError, setMcpError] = useState<string | null>(null);
  const [mcpConnectingName, setMcpConnectingName] = useState<string | null>(null);
  const [mcpConnectMessage, setMcpConnectMessage] = useState<string | null>(null);
  const [tools, setTools] = useState<ToolMetadata[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [memory, setMemory] = useState<MemoryResponse | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [memoryAppendText, setMemoryAppendText] = useState("");
  const [memoryAppending, setMemoryAppending] = useState(false);
  const [memoryAppendMessage, setMemoryAppendMessage] = useState<string | null>(null);
  const [eventFilter, setEventFilter] = useState("");

  const loadTeamData = useCallback(async () => {
    setTeamLoading(true);
    setTeamError(null);
    const [teamResult, tasksResult, worktreesResult] = await Promise.allSettled([
      apiClient.getTeamStatus(),
      apiClient.getTasks(),
      apiClient.getWorktrees()
    ]);

    const errors: string[] = [];
    const team = teamResult.status === "fulfilled" ? teamResult.value : null;
    if (teamResult.status === "rejected") {
      errors.push(`Team：${formatError(teamResult.reason)}`);
    } else if (team?.warnings?.length) {
      errors.push(...team.warnings);
    }
    const tasks = tasksResult.status === "fulfilled" ? tasksResult.value.tasks : team?.tasks ?? [];
    if (tasksResult.status === "rejected") {
      errors.push(`Tasks：${formatError(tasksResult.reason)}`);
    } else if (tasksResult.value.warnings?.length) {
      errors.push(...tasksResult.value.warnings);
    }
    const worktrees = worktreesResult.status === "fulfilled" ? worktreesResult.value.worktrees : team?.worktrees ?? [];
    if (worktreesResult.status === "rejected") {
      errors.push(`Worktrees：${formatError(worktreesResult.reason)}`);
    }

    setTeamData({ team, tasks, worktrees });
    setTeamError(errors.length ? errors.join("；") : null);
    setTeamLoading(false);
  }, []);

  const loadMcpStatus = useCallback(async () => {
    setMcpLoading(true);
    setMcpError(null);
    try {
      setMcpStatus(await apiClient.getMcpStatus());
    } catch (error) {
      setMcpStatus(null);
      setMcpError(formatError(error));
    } finally {
      setMcpLoading(false);
    }
  }, []);

  const loadTools = useCallback(async () => {
    setToolsLoading(true);
    setToolsError(null);
    try {
      const response = await apiClient.getTools();
      setTools(response.tools);
    } catch (error) {
      setTools([]);
      setToolsError(formatError(error));
    } finally {
      setToolsLoading(false);
    }
  }, []);

  const refreshMcpAndTools = useCallback(async () => {
    await Promise.allSettled([loadMcpStatus(), loadTools()]);
  }, [loadMcpStatus, loadTools]);

  const loadMemory = useCallback(async () => {
    setMemoryLoading(true);
    setMemoryError(null);
    try {
      setMemory(await apiClient.getMemory());
    } catch (error) {
      setMemory(null);
      setMemoryError(formatError(error));
    } finally {
      setMemoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTeamData();
    void refreshMcpAndTools();
    void loadMemory();
  }, [loadMemory, refreshMcpAndTools, loadTeamData]);

  async function handleMcpConnect(name: string) {
    const serverName = name.trim();
    if (!serverName || mcpConnectingName) {
      return;
    }
    setMcpConnectingName(serverName);
    setMcpError(null);
    setMcpConnectMessage(null);
    try {
      const response = await apiClient.connectMcpServer(serverName);
      setMcpConnectMessage(response.message || `${serverName} 已连接`);
      await refreshMcpAndTools();
    } catch (error) {
      setMcpError(formatError(error));
    } finally {
      setMcpConnectingName(null);
    }
  }

  async function handleMemoryAppend(event: FormEvent) {
    event.preventDefault();
    const content = memoryAppendText.trim();
    if (!content || content.length > MEMORY_APPEND_MAX_LENGTH || memoryAppending) {
      return;
    }
    setMemoryAppending(true);
    setMemoryError(null);
    setMemoryAppendMessage(null);
    try {
      const response = await apiClient.appendMemory(content, session?.session_id);
      if (!response.ok) {
        throw new Error(response.message || "Memory append failed");
      }
      setMemory(response.memory);
      setMemoryAppendText("");
      setMemoryAppendMessage(response.message || `已追加 ${response.length} 个字符`);
      await loadMemory();
      if (session?.session_id) {
        await onRefreshSessionActivity?.(session.session_id);
      }
    } catch (error) {
      setMemoryAppendMessage(null);
      setMemoryError(formatError(error));
    } finally {
      setMemoryAppending(false);
    }
  }

  const filteredEvents = useMemo(
    () => events.filter((event) => !eventFilter || event.type === eventFilter),
    [eventFilter, events]
  );

  return (
    <div className="inspector">
      <div className="inspector-header">
        <div>
          <div className="product-label">Inspector</div>
          <h2>运行观察</h2>
        </div>
        <span className={`status-pill ${isRunning ? "running" : "idle"}`}>{isRunning ? "运行中" : "空闲"}</span>
      </div>

      <div className="inspector-tabs" role="tablist" aria-label="Inspector tabs">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={activeTab === tab.id ? "active" : ""}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              role="tab"
              aria-selected={activeTab === tab.id}
            >
              <Icon size={15} />
              {tab.label}
            </button>
          );
        })}
      </div>

      <div className="inspector-content">
        {activeTab === "run" ? (
          <RunPanel
            session={session}
            currentRun={currentRun}
            isRunning={isRunning}
            approvals={approvals}
            pendingApproval={pendingApproval}
            lastResolvedApproval={lastResolvedApproval}
            approvalsLoading={approvalsLoading}
            approvalsError={approvalsError}
            sseStatus={sseStatus}
            sseError={sseError}
            sseLastSeenAt={sseLastSeenAt}
            onOpenApproval={onOpenApproval}
          />
        ) : null}

        {activeTab === "timeline" ? (
          <TimelinePanel
            session={session}
            items={timelineItems}
            notice={timelineNotice}
            isLoading={isLoadingTimeline}
          />
        ) : null}

        {activeTab === "events" ? (
          <EventsPanel
            session={session}
            events={filteredEvents}
            totalCount={events.length}
            notice={timelineNotice}
            isLoading={isLoadingTimeline}
            eventFilter={eventFilter}
            onEventFilterChange={setEventFilter}
          />
        ) : null}

        {activeTab === "team" ? (
          <TeamPanel
            data={teamData}
            isLoading={teamLoading}
            error={teamError}
            onRefresh={loadTeamData}
          />
        ) : null}

        {activeTab === "mcp" ? (
          <McpPanel
            status={mcpStatus}
            isLoading={mcpLoading}
            error={mcpError}
            connectMessage={mcpConnectMessage}
            connectingName={mcpConnectingName}
            onConnect={handleMcpConnect}
            onRefresh={refreshMcpAndTools}
          />
        ) : null}

        {activeTab === "tools" ? (
          <ToolsPanel
            tools={tools}
            isLoading={toolsLoading}
            error={toolsError}
            onRefresh={loadTools}
          />
        ) : null}

        {activeTab === "memory" ? (
          <MemoryPanel
            memory={memory}
            isLoading={memoryLoading}
            error={memoryError}
            appendText={memoryAppendText}
            appendMessage={memoryAppendMessage}
            isAppending={memoryAppending}
            onAppendTextChange={setMemoryAppendText}
            onAppend={handleMemoryAppend}
            onRefresh={loadMemory}
            maxLength={MEMORY_APPEND_MAX_LENGTH}
          />
        ) : null}
      </div>
    </div>
  );
}

function RunPanel({
  session,
  currentRun,
  isRunning,
  approvals,
  pendingApproval,
  lastResolvedApproval,
  approvalsLoading,
  approvalsError,
  sseStatus,
  sseError,
  sseLastSeenAt,
  onOpenApproval
}: {
  session: SessionSummary | null;
  currentRun: RunSummary | null;
  isRunning: boolean;
  approvals: Approval[];
  pendingApproval: Approval | null;
  lastResolvedApproval: Approval | null;
  approvalsLoading: boolean;
  approvalsError: string | null;
  sseStatus: SseConnectionStatus;
  sseError: string | null;
  sseLastSeenAt: string | null;
  onOpenApproval?: (approval: Approval) => void;
}) {
  return (
    <div className="status-tab">
      <div className="kv-list">
        <div>
          <span>Session</span>
          <strong>{session?.session_id ?? "未选择"}</strong>
        </div>
        <div>
          <span>状态</span>
          <strong>{isRunning ? "running" : session?.status ?? "idle"}</strong>
        </div>
        <div>
          <span>消息数</span>
          <strong>{session?.message_count ?? 0}</strong>
        </div>
        <div>
          <span>Run ID</span>
          <strong>{currentRun?.run_id ?? session?.active_run_id ?? "暂无"}</strong>
        </div>
        <div>
          <span>Pending Approval</span>
          <strong>{currentRun?.pending_approval_id ?? pendingApproval?.approval_id ?? "暂无"}</strong>
        </div>
        <div>
          <span>开始</span>
          <strong>{formatTime(currentRun?.started_at)}</strong>
        </div>
        <div>
          <span>结束</span>
          <strong>{formatTime(currentRun?.ended_at)}</strong>
        </div>
        {currentRun?.error ? <div className="danger-text">{currentRun.error}</div> : null}
      </div>
      <Section title="SSE 事件流">
        <div className="kv-list">
          <div>
            <span>连接状态</span>
            <strong>{sseStatusLabel(sseStatus)}</strong>
          </div>
          <div>
            <span>最后事件</span>
            <strong>{formatTime(sseLastSeenAt)}</strong>
          </div>
        </div>
        {sseError ? <ErrorPanel text={sseError} tone="warning" /> : null}
      </Section>
      <ApprovalPanel
        approvals={approvals}
        pendingApproval={pendingApproval}
        lastResolvedApproval={lastResolvedApproval}
        isLoading={approvalsLoading}
        error={approvalsError}
        onOpenApproval={onOpenApproval}
      />
    </div>
  );
}

function TimelinePanel({
  session,
  items,
  notice,
  isLoading
}: {
  session: SessionSummary | null;
  items: TimelineItem[];
  notice: string | null;
  isLoading: boolean;
}) {
  if (!session) {
    return <EmptyPanel text="请选择会话后查看 timeline。" />;
  }
  return (
    <div className="timeline-list">
      {notice ? <NoticePanel text={notice} /> : null}
      {isLoading ? <LoadingPanel text="正在加载 timeline..." /> : null}
      {!isLoading && items.length === 0 ? <EmptyPanel text="暂无 timeline。运行一次会话后会显示工具与模型活动。" /> : null}
      {!isLoading
        ? items.map((item) => (
            <div className="timeline-item" key={item.id}>
              <div className="timeline-item-top">
                <strong>{item.title || item.type}</strong>
                <span className={`status-pill ${item.status ?? "idle"}`}>{timelineStatusLabel(item.status)}</span>
              </div>
              <div className="muted-line">
                {formatTime(item.started_at || item.timestamp)} - {formatTime(item.ended_at)}
              </div>
              {item.tool_use_id ? <MetricLine label="Tool Use ID" value={item.tool_use_id} /> : null}
              {item.output_preview ? (
                <div className="preview-block">
                  <span>输出预览</span>
                  <pre>{item.output_preview}</pre>
                </div>
              ) : null}
              {item.input_preview ? (
                <div className="preview-block">
                  <span>输入预览</span>
                  <pre>{readableJson(item.input_preview)}</pre>
                </div>
              ) : null}
              <details className="raw-details">
                <summary>查看原始 JSON</summary>
                <pre>{readableJson(item)}</pre>
              </details>
            </div>
          ))
        : null}
    </div>
  );
}

function EventsPanel({
  session,
  events,
  totalCount,
  notice,
  isLoading,
  eventFilter,
  onEventFilterChange
}: {
  session: SessionSummary | null;
  events: AuditEvent[];
  totalCount: number;
  notice: string | null;
  isLoading: boolean;
  eventFilter: string;
  onEventFilterChange: (value: string) => void;
}) {
  if (!session) {
    return <EmptyPanel text="请选择会话后查看 audit events。" />;
  }
  return (
    <div className="timeline-list">
      {notice ? <NoticePanel text={notice} /> : null}
      <div className="inspector-toolbar">
        <label>
          事件类型
          <select value={eventFilter} onChange={(event) => onEventFilterChange(event.target.value)}>
            {eventTypeOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <span className="muted-line">显示 {events.length} / {totalCount}</span>
      </div>
      {isLoading ? <LoadingPanel text="正在加载 audit events..." /> : null}
      {!isLoading && events.length === 0 ? <EmptyPanel text="暂无匹配事件。" /> : null}
      {!isLoading
        ? events.map((event) => (
            <div className="event-item" key={event.event_id}>
              <div className="timeline-item-top">
                <strong>{event.type}</strong>
                <span>{formatTime(event.ts)}</span>
              </div>
              <div className="event-grid">
                <MetricLine label="来源" value={event.source ?? "暂无"} />
                <MetricLine label="Run" value={event.run_id ?? "暂无"} />
              </div>
              <PayloadSummary payload={event.payload} />
              <details className="raw-details">
                <summary>查看原始 JSON</summary>
                <pre>{readableJson(event)}</pre>
              </details>
            </div>
          ))
        : null}
    </div>
  );
}

function TeamPanel({
  data,
  isLoading,
  error,
  onRefresh
}: {
  data: TeamPanelData;
  isLoading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const team = data.team;
  const activeTeammates = team?.active_teammates ?? [];
  const pendingRequests = team?.pending_requests ?? [];

  return (
    <div className="status-tab">
      <PanelHeader title="Team / Tasks / Worktrees" onRefresh={onRefresh} isLoading={isLoading} />
      {error ? <ErrorPanel text={error} /> : null}
      {isLoading ? <LoadingPanel text="正在加载 team 状态..." /> : null}

      <Section title="活跃 teammate" count={activeTeammates.length}>
        {activeTeammates.length === 0 ? (
          <EmptyPanel text="当前没有活跃 teammate。" />
        ) : (
          activeTeammates.map((item, index) => (
            <StructuredCard key={String(item.name ?? index)} title={String(item.name ?? `teammate-${index + 1}`)}>
              <MetricLine label="角色" value={item.role} />
              <MetricLine label="状态" value={item.status} />
              <MetricLine label="任务" value={item.task_id} />
              <MetricLine label="Worktree" value={item.worktree ?? item.worktree_path} />
              <details className="raw-details">
                <summary>查看原始 JSON</summary>
                <pre>{readableJson(item)}</pre>
              </details>
            </StructuredCard>
          ))
        )}
      </Section>

      <Section title="待处理请求" count={pendingRequests.length}>
        {pendingRequests.length === 0 ? (
          <EmptyPanel text="暂无 pending request。" />
        ) : (
          pendingRequests.map((item, index) => (
            <StructuredCard key={String(item.request_id ?? index)} title={String(item.request_id ?? `request-${index + 1}`)}>
              <MetricLine label="类型" value={item.type} />
              <MetricLine label="发送方" value={item.sender} />
              <MetricLine label="目标" value={item.target} />
              <MetricLine label="状态" value={item.status} />
              <details className="raw-details">
                <summary>查看原始 JSON</summary>
                <pre>{readableJson(item)}</pre>
              </details>
            </StructuredCard>
          ))
        )}
      </Section>

      <Section title="Tasks" count={data.tasks.length}>
        {data.tasks.length === 0 ? (
          <EmptyPanel text="暂无任务。" />
        ) : (
          data.tasks.map((task) => (
            <StructuredCard key={task.id} title={task.subject || task.id}>
              <MetricLine label="ID" value={task.id} />
              <MetricLine label="状态" value={task.status} />
              <MetricLine label="Owner" value={task.owner} />
              <MetricLine label="Worktree" value={task.worktree} />
              <MetricLine label="Blocked By" value={task.blockedBy} />
              {task.description ? <p className="card-description">{task.description}</p> : null}
            </StructuredCard>
          ))
        )}
      </Section>

      <Section title="Worktrees" count={data.worktrees.length}>
        {data.worktrees.length === 0 ? (
          <EmptyPanel text="暂无 worktree。" />
        ) : (
          data.worktrees.map((worktree) => (
            <StructuredCard key={worktree.name} title={worktree.name}>
              <MetricLine label="Branch" value={worktree.branch} />
              <MetricLine label="Task ID" value={worktree.task_id} />
              <MetricLine label="Path" value={worktree.path} />
            </StructuredCard>
          ))
        )}
      </Section>

      {team?.raw_text ? (
        <details className="raw-details raw-text-details">
          <summary>调试：raw team_status 文本</summary>
          <pre>{team.raw_text}</pre>
        </details>
      ) : null}
    </div>
  );
}

function McpPanel({
  status,
  isLoading,
  error,
  connectMessage,
  connectingName,
  onConnect,
  onRefresh
}: {
  status: McpStatusResponse | null;
  isLoading: boolean;
  error: string | null;
  connectMessage: string | null;
  connectingName: string | null;
  onConnect: (name: string) => void;
  onRefresh: () => void;
}) {
  const [selectedConnectName, setSelectedConnectName] = useState("");
  const { connectedNames, connectCandidates } = useMemo(() => {
    const connected = new Set(status?.connected_servers.map((server) => server.name) ?? []);
    const candidates = Array.from(
      new Set([
        ...(status?.mock_servers ?? []),
        ...(status?.configured_servers.map((server) => server.name) ?? [])
      ])
    ).filter((name) => !connected.has(name));
    return {
      connectedNames: connected,
      connectCandidates: candidates
    };
  }, [status]);
  const selectedName =
    selectedConnectName && connectCandidates.includes(selectedConnectName)
      ? selectedConnectName
      : connectCandidates[0] ?? "";

  function handleConnectSubmit(event: FormEvent) {
    event.preventDefault();
    if (selectedName) {
      onConnect(selectedName);
    }
  }

  return (
    <div className="status-tab">
      <PanelHeader title="MCP 状态" onRefresh={onRefresh} isLoading={isLoading} />
      {error ? <ErrorPanel text={error} /> : null}
      {connectMessage ? <NoticePanel text={connectMessage} /> : null}
      {isLoading ? <LoadingPanel text="正在加载 MCP 状态..." /> : null}
      {!isLoading && !status ? <EmptyPanel text="暂无 MCP 状态。" /> : null}
      {status ? (
        <>
          <Section title="连接 MCP server" count={connectCandidates.length}>
            <NoticePanel text="只提交 server name；configured 表示配置存在，不代表当前已经 connected。前端不会传 env value。" />
            {connectCandidates.length === 0 ? (
              <EmptyPanel text="当前没有可连接的 mock/configured server。" />
            ) : (
              <>
                <form className="connect-form" onSubmit={handleConnectSubmit}>
                  <label htmlFor="mcp-connect-name">Server</label>
                  <select
                    id="mcp-connect-name"
                    value={selectedName}
                    onChange={(event) => setSelectedConnectName(event.target.value)}
                    disabled={Boolean(connectingName)}
                  >
                    {connectCandidates.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                  <button className="connect-button" type="submit" disabled={!selectedName || Boolean(connectingName)}>
                    {connectingName === selectedName ? <RefreshCw size={15} className="spin" /> : null}
                    {connectingName === selectedName ? "连接中" : "连接"}
                  </button>
                </form>
                <div className="connect-list">
                  {connectCandidates.map((name) => (
                    <div className="connect-row" key={name}>
                      <span>{name}</span>
                      <McpConnectAction
                        name={name}
                        connected={connectedNames.has(name)}
                        connectingName={connectingName}
                        onConnect={onConnect}
                      />
                    </div>
                  ))}
                </div>
              </>
            )}
          </Section>

          <Section title="Mock Servers" count={status.mock_servers.length}>
            {status.mock_servers.length === 0 ? (
              <EmptyPanel text="没有内置 mock server。" />
            ) : (
              <div className="chip-row">
                {status.mock_servers.map((server) => (
                  <span className="data-chip" key={server}>{server}</span>
                ))}
              </div>
            )}
          </Section>

          <Section title="Configured Servers" count={status.configured_servers.length}>
            <NoticePanel text="configured 表示配置存在，不代表当前进程已经 connected。" />
            {status.configured_servers.length === 0 ? (
              <EmptyPanel text="暂无 configured MCP server。" />
            ) : (
              status.configured_servers.map((server) => (
                <StructuredCard key={server.name} title={server.name}>
                  <MetricLine label="Transport" value={server.transport} />
                  <MetricLine label="Command" value={server.command} />
                  <MetricLine label="Args" value={server.args} />
                  <MetricLine label="Env Keys" value={server.env_keys} />
                </StructuredCard>
              ))
            )}
          </Section>

          <Section title="Connected Servers" count={status.connected_servers.length}>
            {status.connected_servers.length === 0 ? (
              <EmptyPanel text="当前没有 connected MCP server。" />
            ) : (
              status.connected_servers.map((server) => (
                <StructuredCard key={server.name} title={server.name}>
                  <MetricLine label="Transport" value={server.transport} />
                  <MetricLine label="工具数" value={server.tool_count} />
                  <span className="status-pill completed">已连接</span>
                  {server.tools.length === 0 ? (
                    <EmptyPanel text="该 server 暂无 discovered tools。" />
                  ) : (
                    <div className="tool-list">
                      {server.tools.map((tool) => (
                        <div className="tool-row" key={tool.name}>
                          <Wrench size={14} />
                          <div>
                            <strong>{tool.name}</strong>
                            <span>{tool.description || tool.raw_name || "暂无描述"}</span>
                            <details className="raw-details">
                              <summary>查看 input_schema JSON</summary>
                              <pre>{readableJson(tool.input_schema ?? {})}</pre>
                            </details>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </StructuredCard>
              ))
            )}
          </Section>

          <Section title="MCP Errors" count={status.errors.length}>
            {status.errors.length === 0 ? (
              <EmptyPanel text="暂无 MCP 错误。" />
            ) : (
              status.errors.map((item, index) => (
                <StructuredCard key={`${item.type}-${index}`} title={item.type}>
                  <MetricLine label="时间" value={formatTime(item.ts)} />
                  <MetricLine label="Server" value={item.server} />
                  <MetricLine label="Tool" value={item.tool ?? item.prefixed_name} />
                  <MetricLine label="Message" value={item.message} />
                  <details className="raw-details">
                    <summary>查看原始 JSON</summary>
                    <pre>{readableJson(item)}</pre>
                  </details>
                </StructuredCard>
              ))
            )}
          </Section>
        </>
      ) : null}
    </div>
  );
}

function McpConnectAction({
  name,
  connected,
  connectingName,
  onConnect
}: {
  name: string;
  connected: boolean;
  connectingName: string | null;
  onConnect: (name: string) => void;
}) {
  if (connected) {
    return <span className="status-pill completed">已连接</span>;
  }
  const isConnecting = connectingName === name;
  return (
    <button className="connect-button" type="button" onClick={() => onConnect(name)} disabled={Boolean(connectingName)}>
      {isConnecting ? <RefreshCw size={15} className="spin" /> : null}
      {isConnecting ? "连接中" : "连接"}
    </button>
  );
}

function ToolsPanel({
  tools,
  isLoading,
  error,
  onRefresh
}: {
  tools: ToolMetadata[];
  isLoading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const builtinTools = tools.filter((tool) => tool.source !== "mcp");
  const mcpGroups = groupMcpToolsByServer(tools);

  return (
    <div className="status-tab">
      <PanelHeader title="Tools" onRefresh={onRefresh} isLoading={isLoading} />
      <NoticePanel text="这里只展示工具 metadata，不提供工具执行入口。" />
      {error ? <ErrorPanel text={error} /> : null}
      {isLoading ? <LoadingPanel text="正在加载 tools..." /> : null}
      {!isLoading && tools.length === 0 ? <EmptyPanel text="暂无工具 metadata。" /> : null}

      {!isLoading && tools.length > 0 ? (
        <>
          <Section title="Builtin Tools" count={builtinTools.length}>
            {builtinTools.length === 0 ? (
              <EmptyPanel text="暂无 builtin tools。" />
            ) : (
              builtinTools.map((tool) => <ToolMetadataCard key={tool.name} tool={tool} />)
            )}
          </Section>

          <Section title="MCP Tools" count={tools.length - builtinTools.length}>
            {mcpGroups.length === 0 ? (
              <EmptyPanel text="暂无 MCP tools。连接 MCP server 后会在这里显示。" />
            ) : (
              mcpGroups.map((group) => (
                <StructuredCard key={group.server} title={group.server}>
                  <MetricLine label="Server" value={group.server} />
                  <MetricLine label="工具数" value={group.tools.length} />
                  <div className="tool-list">
                    {group.tools.map((tool) => <ToolMetadataCard key={tool.name} tool={tool} />)}
                  </div>
                </StructuredCard>
              ))
            )}
          </Section>
        </>
      ) : null}
    </div>
  );
}

function ToolMetadataCard({ tool }: { tool: ToolMetadata }) {
  return (
    <article className="structured-card">
      <div className="structured-card-title">
        <Wrench size={15} />
        <strong>{tool.name}</strong>
      </div>
      <div className="structured-card-body">
        <MetricLine label="Source" value={tool.source} />
        <MetricLine label="Server" value={tool.server ?? "无"} />
        <p className="card-description">{toolDescription(tool)}</p>
        <details className="raw-details">
          <summary>查看 input_schema JSON</summary>
          <pre>{readableJson(tool.input_schema ?? {})}</pre>
        </details>
      </div>
    </article>
  );
}

function MemoryPanel({
  memory,
  isLoading,
  error,
  appendText,
  appendMessage,
  isAppending,
  onAppendTextChange,
  onAppend,
  onRefresh,
  maxLength
}: {
  memory: MemoryResponse | null;
  isLoading: boolean;
  error: string | null;
  appendText: string;
  appendMessage: string | null;
  isAppending: boolean;
  onAppendTextChange: (value: string) => void;
  onAppend: (event: FormEvent) => void;
  onRefresh: () => void;
  maxLength: number;
}) {
  return (
    <div className="status-tab">
      <PanelHeader title="Memory" onRefresh={onRefresh} isLoading={isLoading} />
      {error ? <ErrorPanel text={error} /> : null}
      {appendMessage ? <NoticePanel text={appendMessage} /> : null}
      {isLoading ? <LoadingPanel text="正在读取 memory..." /> : null}
      {!isLoading && !memory ? <EmptyPanel text="暂无 memory 响应。" /> : null}
      {memory ? (
        <>
          <div className="kv-list">
            <div>
              <span>Path</span>
              <strong>{memory.path}</strong>
            </div>
            <div>
              <span>状态</span>
              <strong>{memory.exists ? "已存在" : "尚未创建"}</strong>
            </div>
            <div>
              <span>文件大小</span>
              <strong>{formatBytes(memory.size_bytes)}</strong>
            </div>
            <div>
              <span>返回字符数</span>
              <strong>{memory.length} chars</strong>
            </div>
            <div>
              <span>更新时间</span>
              <strong>{formatTime(memory.updated_at)}</strong>
            </div>
            <div>
              <span>读取上限</span>
              <strong>{memory.limit} chars</strong>
            </div>
            <div>
              <span>Truncated</span>
              <strong>{memory.truncated ? "是" : "否"}</strong>
            </div>
          </div>
          {memory.truncated ? (
            <ErrorPanel text={`内容已按读取上限截断，仅显示前 ${memory.limit} 个字符。`} tone="warning" />
          ) : null}
          <Section title="Memory 内容">
            {memory.content ? <pre className="memory-content">{memory.content}</pre> : <EmptyPanel text="Memory 当前为空。" />}
          </Section>
        </>
      ) : null}

      <form className="memory-form" onSubmit={onAppend}>
        <label htmlFor="memory-append">追加 memory</label>
        <textarea
          id="memory-append"
          value={appendText}
          onChange={(event) => onAppendTextChange(event.target.value)}
          placeholder="输入要追加到 .memory/MEMORY.md 的内容"
          rows={4}
          disabled={isAppending}
          maxLength={maxLength}
        />
        <div className="textarea-meta">
          <span>{appendText.length} / {maxLength}</span>
          <span>{Math.max(0, maxLength - appendText.length)} 字可用</span>
        </div>
        <button className="send-button" type="submit" disabled={isAppending || appendText.trim().length === 0 || appendText.length > maxLength}>
          {isAppending ? <RefreshCw size={16} className="spin" /> : <Send size={16} />}
          追加
        </button>
      </form>
    </div>
  );
}

function PayloadSummary({ payload }: { payload: Record<string, unknown> }) {
  const summaryPayload =
    Array.isArray(payload.content) && typeof payload.text_preview === "string"
      ? { ...payload, content: payload.text_preview }
      : payload;
  const keys = Object.keys(summaryPayload).slice(0, 4);
  if (keys.length === 0) {
    return <EmptyPanel text="该事件没有 payload。" />;
  }
  return (
    <div className="payload-summary">
      {keys.map((key) => (
        <MetricLine key={key} label={key} value={summaryPayload[key]} />
      ))}
    </div>
  );
}

function PanelHeader({
  title,
  onRefresh,
  isLoading
}: {
  title: string;
  onRefresh: () => void;
  isLoading: boolean;
}) {
  return (
    <div className="panel-header">
      <h3>{title}</h3>
      <button className="icon-button" type="button" onClick={onRefresh} disabled={isLoading} title="刷新">
        <RefreshCw size={16} className={isLoading ? "spin" : ""} />
      </button>
    </div>
  );
}

function Section({
  title,
  count,
  children
}: {
  title: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className="status-section">
      <div className="section-heading">
        <h4>{title}</h4>
        {typeof count === "number" ? <span className="count-pill">{count}</span> : null}
      </div>
      {children}
    </section>
  );
}

function StructuredCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <article className="structured-card">
      <div className="structured-card-title">
        <Server size={15} />
        <strong>{title}</strong>
      </div>
      <div className="structured-card-body">{children}</div>
    </article>
  );
}

function MetricLine({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="metric-line">
      <span>{label}</span>
      <strong>{compactValue(value)}</strong>
    </div>
  );
}

function EmptyPanel({ text }: { text: string }) {
  return <div className="empty-panel">{text}</div>;
}

function NoticePanel({ text }: { text: string }) {
  return <div className="notice-panel">{text}</div>;
}

function LoadingPanel({ text }: { text: string }) {
  return (
    <div className="empty-panel">
      <RefreshCw size={16} className="spin" />
      {text}
    </div>
  );
}

function ErrorPanel({ text, tone = "danger" }: { text: string; tone?: "danger" | "warning" }) {
  return (
    <div className={`error-panel ${tone}`}>
      <AlertTriangle size={16} />
      <span>{text}</span>
    </div>
  );
}
