import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";


describe("AppShell mobile navigation", () => {
  it("switches between the three workspace panes", () => {
    const onToggleInspector = vi.fn();
    const { container } = render(
      <AppShell
        sidebar={<div>会话内容</div>}
        chat={<div>对话内容</div>}
        composer={<div>输入区</div>}
        inspector={<div>检查器内容</div>}
        inspectorOpen
        onToggleInspector={onToggleInspector}
        title="标题"
        subtitle="副标题"
        error={null}
      />
    );
    const shell = container.firstElementChild;

    fireEvent.click(screen.getByRole("button", { name: "会话" }));
    expect(shell).toHaveClass("mobile-sessions");

    fireEvent.click(screen.getByRole("button", { name: "检查器" }));
    expect(shell).toHaveClass("mobile-inspector");

    fireEvent.click(screen.getByRole("button", { name: "对话" }));
    expect(shell).toHaveClass("mobile-chat");
    expect(onToggleInspector).not.toHaveBeenCalled();
  });
});
