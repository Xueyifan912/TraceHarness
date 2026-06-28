import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";


function textarea(): HTMLTextAreaElement {
  return screen.getByPlaceholderText(
    "输入你的任务或问题，Enter 发送，Shift+Enter 换行"
  );
}


describe("Composer", () => {
  it("keeps the draft when sending fails", async () => {
    const onSend = vi.fn().mockResolvedValue(false);
    render(
      <Composer
        disabled={false}
        isRunning={false}
        onSend={onSend}
      />
    );
    fireEvent.change(textarea(), { target: { value: "保留这条草稿" } });

    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("保留这条草稿"));
    expect(textarea()).toHaveValue("保留这条草稿");
  });

  it("clears the draft only after a successful send", async () => {
    const onSend = vi.fn().mockResolvedValue(true);
    render(
      <Composer
        disabled={false}
        isRunning={false}
        onSend={onSend}
      />
    );
    fireEvent.change(textarea(), { target: { value: "发送成功" } });

    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(textarea()).toHaveValue(""));
  });

  it("does not submit Enter while an IME composition is active", async () => {
    const onSend = vi.fn().mockResolvedValue(true);
    render(
      <Composer
        disabled={false}
        isRunning={false}
        onSend={onSend}
      />
    );
    fireEvent.change(textarea(), { target: { value: "中文输入" } });
    fireEvent.compositionStart(textarea());

    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.compositionEnd(textarea());
    fireEvent.keyDown(textarea(), { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
  });
});
