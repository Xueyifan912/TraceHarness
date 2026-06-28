import { AlertTriangle, CheckCircle2, Clock3, ShieldAlert, XCircle } from "lucide-react";
import type { Approval } from "../../api/types";

interface ApprovalPanelProps {
  approvals: Approval[];
  pendingApproval: Approval | null;
  lastResolvedApproval: Approval | null;
  isLoading: boolean;
  error: string | null;
  onOpenApproval?: (approval: Approval) => void;
}

export function approvalStatusLabel(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    pending: "待审批",
    allowed: "已允许",
    denied: "已拒绝",
    expired: "已超时",
    cancelled: "已取消"
  };
  return labels[status ?? ""] ?? status ?? "未知";
}

export function approvalDecisionLabel(decision: string | null | undefined): string {
  if (decision === "allow") {
    return "允许";
  }
  if (decision === "deny") {
    return "拒绝";
  }
  return "暂无";
}

export function formatApprovalTime(value: string | null | undefined): string {
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

export function approvalInputPreview(approval: Approval): string {
  const preview = approval.input_preview;
  if (preview && typeof preview === "object" && "preview" in preview) {
    return String(preview.preview ?? "");
  }
  try {
    return JSON.stringify(preview, null, 2);
  } catch {
    return String(preview ?? "");
  }
}

export function approvalRemainingText(approval: Approval): string {
  const expiresAt = new Date(approval.expires_at).getTime();
  if (Number.isNaN(expiresAt)) {
    return `到期时间：${approval.expires_at || "暂无"}`;
  }
  const remainingMs = expiresAt - Date.now();
  if (remainingMs <= 0) {
    return "剩余时间：已到期";
  }
  const remainingSeconds = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  return `剩余时间：${minutes}分${seconds.toString().padStart(2, "0")}秒`;
}

function statusIcon(status: string) {
  if (status === "allowed") {
    return <CheckCircle2 size={16} />;
  }
  if (status === "denied" || status === "expired" || status === "cancelled") {
    return <XCircle size={16} />;
  }
  if (status === "pending") {
    return <ShieldAlert size={16} />;
  }
  return <Clock3 size={16} />;
}

export function ApprovalPanel({
  approvals,
  pendingApproval,
  lastResolvedApproval,
  isLoading,
  error,
  onOpenApproval
}: ApprovalPanelProps) {
  const visibleApproval = pendingApproval ?? lastResolvedApproval;

  return (
    <section className="approval-panel">
      <div className="approval-panel-header">
        <div>
          <h3>权限审批</h3>
          <p>本地工具权限审批状态</p>
        </div>
        <span className={`status-pill ${pendingApproval ? "waiting_approval" : "idle"}`}>
          {pendingApproval ? "待审批" : "无待审批"}
        </span>
      </div>

      {error ? (
        <div className="approval-error">
          <AlertTriangle size={16} />
          <span>{error}</span>
        </div>
      ) : null}

      {isLoading && approvals.length === 0 ? <div className="empty-panel">正在检查 pending approvals...</div> : null}

      {!visibleApproval && !isLoading ? (
        <div className="empty-panel">当前没有待审批操作。</div>
      ) : null}

      {visibleApproval ? (
        <article className={`approval-card ${visibleApproval.status}`}>
          <div className="approval-card-title">
            {statusIcon(visibleApproval.status)}
            <strong>{visibleApproval.tool_name}</strong>
            <span>{approvalStatusLabel(visibleApproval.status)}</span>
          </div>
          <div className="approval-fields">
            <ApprovalField label="Approval ID" value={visibleApproval.approval_id} />
            <ApprovalField label="Run ID" value={visibleApproval.run_id} />
            <ApprovalField label="Tool Use ID" value={visibleApproval.tool_use_id || "暂无"} />
            <ApprovalField label="规则" value={visibleApproval.rule || "暂无"} />
            <ApprovalField label="原因" value={visibleApproval.reason || "暂无"} />
            <ApprovalField label="决策" value={approvalDecisionLabel(visibleApproval.decision)} />
            <ApprovalField label="到期" value={formatApprovalTime(visibleApproval.expires_at)} />
          </div>
          {visibleApproval.status === "pending" ? (
            <button className="approval-open-button" type="button" onClick={() => onOpenApproval?.(visibleApproval)}>
              打开审批
            </button>
          ) : null}
        </article>
      ) : null}
    </section>
  );
}

function ApprovalField({ label, value }: { label: string; value: string }) {
  return (
    <div className="approval-field">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
