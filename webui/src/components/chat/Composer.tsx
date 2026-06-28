import { Send, Loader2, Square } from "lucide-react";
import { FormEvent, KeyboardEvent, useState } from "react";

interface ComposerProps {
  disabled: boolean;
  isRunning: boolean;
  disabledReason?: string | null;
  onSend: (content: string) => Promise<boolean> | boolean;
  onCancel?: () => Promise<void> | void;
  isCancelling?: boolean;
}

export function Composer({
  disabled,
  isRunning,
  disabledReason,
  onSend,
  onCancel,
  isCancelling = false
}: ComposerProps) {
  const [content, setContent] = useState("");
  const [isComposing, setIsComposing] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const value = content.trim();
    if (!value || disabled || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    try {
      const sent = await onSend(value);
      if (sent) {
        setContent("");
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !isComposing) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer-input-wrap">
        <textarea
          value={content}
          onChange={(event) => setContent(event.target.value)}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => setIsComposing(true)}
          onCompositionEnd={() => setIsComposing(false)}
          placeholder="输入你的任务或问题，Enter 发送，Shift+Enter 换行"
          rows={3}
          disabled={disabled || isSubmitting}
        />
        {disabled && disabledReason ? <div className="composer-disabled-reason">{disabledReason}</div> : null}
      </div>
      <div className="composer-actions">
        {isRunning && onCancel ? (
          <button
            className="cancel-run-button"
            type="button"
            disabled={isCancelling}
            onClick={() => void onCancel()}
          >
            {isCancelling ? <Loader2 size={17} className="spin" /> : <Square size={15} />}
            停止
          </button>
        ) : null}
        <button className="send-button" type="submit" disabled={disabled || isSubmitting || content.trim().length === 0}>
          {isRunning || isSubmitting ? <Loader2 size={17} className="spin" /> : <Send size={17} />}
          发送
        </button>
      </div>
    </form>
  );
}
