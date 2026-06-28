import type { ReactNode } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { useState } from "react";

interface AppShellProps {
  sidebar: ReactNode;
  chat: ReactNode;
  composer: ReactNode;
  inspector: ReactNode;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  title: string;
  subtitle: string;
  error: string | null;
  children?: ReactNode;
}

export function AppShell({
  sidebar,
  chat,
  composer,
  inspector,
  inspectorOpen,
  onToggleInspector,
  title,
  subtitle,
  error,
  children
}: AppShellProps) {
  const ToggleIcon = inspectorOpen ? PanelRightClose : PanelRightOpen;
  const [mobilePane, setMobilePane] = useState<"sessions" | "chat" | "inspector">("chat");

  function toggleInspector() {
    onToggleInspector();
    setMobilePane(inspectorOpen ? "chat" : "inspector");
  }

  function showInspector() {
    if (!inspectorOpen) {
      onToggleInspector();
    }
    setMobilePane("inspector");
  }

  return (
    <div className={`app-shell ${inspectorOpen ? "inspector-open" : "inspector-closed"} mobile-${mobilePane}`}>
      <nav className="mobile-pane-nav" aria-label="移动端工作区切换">
        <button type="button" aria-pressed={mobilePane === "sessions"} onClick={() => setMobilePane("sessions")}>
          会话
        </button>
        <button type="button" aria-pressed={mobilePane === "chat"} onClick={() => setMobilePane("chat")}>
          对话
        </button>
        <button type="button" aria-pressed={mobilePane === "inspector"} onClick={showInspector}>
          检查器
        </button>
      </nav>
      <aside className="sidebar-column">{sidebar}</aside>

      <main className="chat-column">
        <header className="chat-header">
          <div className="chat-title-group">
            <h1>{title}</h1>
            <p>{subtitle}</p>
          </div>
          <button className="icon-button" type="button" onClick={toggleInspector} title="切换右侧检查器">
            <ToggleIcon size={18} />
          </button>
        </header>

        <div className="chat-error-slot">
          {error ? <div className="error-banner">{error}</div> : null}
        </div>

        <section className="chat-body">{chat}</section>
        <section className="composer-region">{composer}</section>
      </main>

      {inspectorOpen ? <aside className="inspector-column">{inspector}</aside> : null}
      {children}
    </div>
  );
}
