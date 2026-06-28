export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export interface PreviewText {
  preview: string;
  length: number;
  truncated: boolean;
}

export interface HealthResponse {
  ok: boolean;
  app: string;
  workspace_path: string;
  version: string;
}

export interface SessionSummary {
  session_id: string;
  created_at: string | null;
  updated_at: string | null;
  workspace_path: string | null;
  message_count: number;
  last_user_prompt_preview: PreviewText | null;
  status: RunStatus | string;
  active_run_id: string | null;
}

export interface TextContentBlock {
  type: "text";
  text: string;
}

export interface GenericContentBlock {
  type?: string;
  [key: string]: unknown;
}

export type MessageContent =
  | string
  | Array<TextContentBlock | GenericContentBlock>
  | Record<string, unknown>
  | null;

export interface ChatMessage {
  role: "user" | "assistant" | "system" | string;
  content: MessageContent;
  [key: string]: unknown;
}

export interface SessionCreateResponse {
  session: SessionSummary;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
}

export interface SessionDetailResponse {
  session: SessionSummary;
  messages: ChatMessage[];
  display_messages?: ChatMessage[];
}

export interface SessionArchiveResponse {
  ok: boolean;
  session_id: string;
  archived: boolean;
}

export interface RunSummary {
  run_id: string;
  session_id: string;
  status: RunStatus | string;
  started_at: string;
  ended_at: string | null;
  error: string | null;
  pending_approval_id: string | null;
}

export interface TimelineItem {
  id: string;
  type:
    | "llm_call"
    | "tool_call"
    | "permission"
    | "mcp"
    | "memory"
    | "session"
    | "team_message"
    | "final_stop"
    | "error"
    | string;
  title: string;
  status?: string;
  started_at?: string;
  ended_at?: string;
  timestamp?: string;
  tool_use_id?: string;
  input_preview?: unknown;
  output_preview?: string;
  [key: string]: unknown;
}

export interface ChatTurnResponse {
  run: RunSummary;
  session: SessionSummary;
  messages: ChatMessage[];
  timeline: TimelineItem[];
}

export interface RunStartResponse {
  run: RunSummary;
  session: SessionSummary;
}

export interface RunDetailResponse {
  run: RunSummary;
}

export type RunEventType =
  | "run_started"
  | "run_status"
  | "user_message"
  | "assistant_message"
  | "llm_call_started"
  | "llm_call_ended"
  | "llm_call_failed"
  | "tool_call_started"
  | "tool_call_ended"
  | "permission_decision"
  | "approval_requested"
  | "approval_resolved"
  | "final_stop"
  | "run_completed"
  | "run_failed"
  | "run_cancelled"
  | "run_cancel_requested"
  | "stream_gap"
  | "heartbeat"
  | string;

export interface RunEvent {
  event_id: string;
  ts: string | null;
  type: RunEventType;
  session_id: string | null;
  run_id: string | null;
  source?: string | null;
  payload: Record<string, unknown>;
  _sse_event?: string;
}

export type SseConnectionStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "disconnected"
  | "completed"
  | "failed"
  | "cancelled"
  | "error";

export interface AuditEvent {
  event_id: string;
  ts: string | null;
  type: string;
  session_id: string | null;
  run_id: string | null;
  source: string | null;
  payload: Record<string, unknown>;
}

export interface EventListResponse {
  events: AuditEvent[];
  next_cursor: string | null;
  warnings: string[];
}

export interface TimelineResponse {
  items: TimelineItem[];
  warnings: string[];
}

export interface TaskItem {
  id: string;
  subject: string;
  description: string;
  status: string;
  owner: string | null;
  blockedBy: string[];
  worktree: string | null;
}

export interface WorktreeItem {
  name: string;
  path: string;
  branch: string;
  task_id: string;
}

export interface TeamStatusResponse {
  active_teammates: Array<Record<string, unknown>>;
  pending_requests: Array<Record<string, unknown>>;
  tasks: TaskItem[];
  worktrees: WorktreeItem[];
  raw_text: string;
}

export interface TasksResponse {
  tasks: TaskItem[];
}

export interface WorktreesResponse {
  worktrees: WorktreeItem[];
}

export interface McpConfiguredServer {
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  env_keys?: string[];
  configured?: boolean;
  [key: string]: unknown;
}

export interface McpConnectedTool {
  name: string;
  raw_name?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
}

export interface McpConnectedServer {
  name: string;
  transport: string;
  tool_count: number;
  tools: McpConnectedTool[];
  [key: string]: unknown;
}

export interface McpErrorItem {
  type: string;
  ts?: string | null;
  server?: string | null;
  message?: string | null;
  prefixed_name?: string | null;
  tool?: string | null;
  [key: string]: unknown;
}

export interface McpStatusResponse {
  mock_servers: string[];
  configured_servers: McpConfiguredServer[];
  connected_servers: McpConnectedServer[];
  errors: McpErrorItem[];
}

export interface McpConnectResponse {
  ok: boolean;
  message: string;
  server: McpConnectedServer;
}

export interface ToolMetadata {
  name: string;
  description?: string | null;
  source: "builtin" | "mcp" | string;
  server: string | null;
  input_schema: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface ToolsResponse {
  tools: ToolMetadata[];
}

export interface MemoryResponse {
  path: string;
  exists: boolean;
  length: number;
  size_bytes: number;
  updated_at: string | null;
  content: string;
  truncated: boolean;
  limit: number;
}

export interface MemoryAppendResponse {
  ok: boolean;
  message: string;
  length: number;
  max_length: number;
  memory: MemoryResponse;
}

export type ApprovalDecision = "allow" | "deny";

export type ApprovalStatus =
  | "pending"
  | "allowed"
  | "denied"
  | "expired"
  | "cancelled"
  | string;

export interface Approval {
  approval_id: string;
  session_id: string;
  run_id: string;
  tool_name: string;
  tool_use_id: string | null;
  input_preview: PreviewText | Record<string, unknown>;
  reason: string;
  rule: string;
  created_at: string;
  expires_at: string;
  timeout_seconds: number;
  status: ApprovalStatus;
  decision: ApprovalDecision | string | null;
  message: string | null;
  resolved_at: string | null;
}

export interface ApprovalListResponse {
  approvals: Approval[];
}

export interface ApprovalDetailResponse {
  approval: Approval;
}

export interface ApprovalResolveResponse {
  approval: Approval;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;

  constructor(message: string, code: string, status: number, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}
