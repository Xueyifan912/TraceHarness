import { AlertTriangle, Check, ShieldAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { Approval, ApprovalDecision } from "../../api/types";
import {
  approvalInputPreview,
  approvalRemainingText,
  approvalStatusLabel,
  formatApprovalTime
} from "./ApprovalPanel";

interface ApprovalModalProps {
  approval: Approval | null;
  isSubmitting: boolean;
  error: string | null;
  onResolve: (approval: Approval, decision: ApprovalDecision, message: string) => Promise<void> | void;
  onDismiss: () => void;
}

export function ApprovalModal({
  approval,
  isSubmitting,
  error,
  onResolve,
  onDismiss
}: ApprovalModalProps) {
  const [message, setMessage] = useState("");
  const [decision, setDecision] = useState<ApprovalDecision | null>(null);

  useEffect(() => {
    setMessage("");
    setDecision(null);
  }, [approval?.approval_id]);

  if (!approval) {
    return null;
  }

  const canSubmit = approval.status === "pending" && !isSubmitting;

  async function submit(nextDecision: ApprovalDecision) {
    if (!approval || !canSubmit) {
      return;
    }
    setDecision(nextDecision);
    try {
      await onResolve(approval, nextDecision, message.trim());
    } catch {
      // The approval hook owns the visible error state; keep the modal open.
    }
  }

  return (
    <div className="approval-modal-backdrop" role="presentation">
      <section className="approval-modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <header className="approval-modal-header">
          <div>
            <div className="product-label">Permission Approval</div>
            <h2 id="approval-title">
              <ShieldAlert size={20} />
              本地工具权限审批
            </h2>
          </div>
          <button className="icon-button" type="button" onClick={onDismiss} title="暂时收起审批窗口">
            <X size={17} />
          </button>
        </header>

        <div className="approval-modal-body">
          <div className="notice-panel">
            工具请求命中了 ask 策略。请确认是否允许本次本地工具调用继续执行。
          </div>

          {error ? (
            <div className="approval-error">
              <AlertTriangle size={16} />
              <span>{error}</span>
            </div>
          ) : null}

          <div className="approval-summary-grid">
            <ApprovalModalField label="状态" value={approvalStatusLabel(approval.status)} />
            <ApprovalModalField label="工具" value={approval.tool_name} />
            <ApprovalModalField label="Tool Use ID" value={approval.tool_use_id || "暂无"} />
            <ApprovalModalField label="Run ID" value={approval.run_id} />
            <ApprovalModalField label="规则" value={approval.rule || "暂无"} />
            <ApprovalModalField label="原因" value={approval.reason || "暂无"} />
            <ApprovalModalField label="创建时间" value={formatApprovalTime(approval.created_at)} />
            <ApprovalModalField label="到期时间" value={`${formatApprovalTime(approval.expires_at)} · ${approvalRemainingText(approval)}`} />
          </div>

          <div className="approval-preview">
            <span>输入预览</span>
            <pre>{approvalInputPreview(approval) || "(empty)"}</pre>
          </div>

          <label className="approval-message-label" htmlFor="approval-message">
            拒绝说明，可选
          </label>
          <textarea
            id="approval-message"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="例如：该操作不符合当前任务范围"
            rows={3}
            maxLength={2000}
            disabled={isSubmitting || approval.status !== "pending"}
          />
        </div>

        <footer className="approval-modal-actions">
          <button
            className="approval-deny-button"
            type="button"
            disabled={!canSubmit}
            onClick={() => void submit("deny")}
          >
            {isSubmitting && decision === "deny" ? <span className="small-spinner" /> : <X size={17} />}
            拒绝
          </button>
          <button
            className="approval-allow-button"
            type="button"
            disabled={!canSubmit}
            onClick={() => void submit("allow")}
          >
            {isSubmitting && decision === "allow" ? <span className="small-spinner" /> : <Check size={17} />}
            允许
          </button>
        </footer>
      </section>
    </div>
  );
}

function ApprovalModalField({ label, value }: { label: string; value: string }) {
  return (
    <div className="approval-modal-field">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
