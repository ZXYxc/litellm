import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChatGPTOAuthAccounts from "./ChatGPTOAuthAccounts";

const account = {
  credential_id: "credential-1",
  credential_name: "Primary",
  email: "admin@example.com",
  status: "active",
  expires_at: "2026-07-26T12:00:00Z",
  token_version: 3,
};

describe("ChatGPTOAuthAccounts", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows account identity and never renders stored OAuth tokens", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ accounts: [{ ...account, access_token: "secret-access" }] }), { status: 200 }),
    );

    render(<ChatGPTOAuthAccounts accessToken="admin-token" />);

    expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    expect(screen.queryByText("secret-access")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
  });

  it("starts device login and polls until the account is ready", async () => {
    const startedFlow = {
      flow_id: "flow-1",
      credential_id: "credential-1",
      verification_url: "https://auth.openai.com/codex/device",
      user_code: "ABCD-EFGH",
      interval_seconds: 1,
      expires_at: "2026-07-26T12:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ accounts: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(startedFlow), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ status: "complete", credential_id: "credential-1", email: "admin@example.com" }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ accounts: [account] }), { status: 200 }));

    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ChatGPTOAuthAccounts accessToken="admin-token" pollDelayMs={10} />);
    await screen.findByText("暂无已登录账号");
    fireEvent.change(screen.getByLabelText("账号名称"), { target: { value: "Primary" } });
    fireEvent.click(screen.getByRole("button", { name: "开始登录" }));

    expect(await screen.findByText("ABCD-EFGH")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
  });
});
