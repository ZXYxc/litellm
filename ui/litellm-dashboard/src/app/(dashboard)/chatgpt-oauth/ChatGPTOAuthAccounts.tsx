"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCallback, useEffect, useState } from "react";

import { chatgptOAuthApi } from "./chatgptOAuthApi";

interface ChatGPTOAuthAccount {
  credential_id: string;
  credential_name: string;
  email: string;
  status: "active" | "disabled" | "reauth_required";
  expires_at: string;
  token_version: number;
  refresh_backoff_until?: string | null;
}

interface DeviceFlow {
  flow_id: string;
  verification_url: string;
  user_code: string;
  interval_seconds: number;
  expires_at: string;
}

interface DevicePollResult {
  status: "pending" | "processing" | "complete";
}

interface ChatGPTOAuthAccountsProps {
  accessToken: string | null;
  pollDelayMs?: number;
}

const statusLabel: Record<ChatGPTOAuthAccount["status"], string> = {
  active: "正常",
  disabled: "已停用",
  reauth_required: "需要重新登录",
};

export default function ChatGPTOAuthAccounts({ accessToken, pollDelayMs }: ChatGPTOAuthAccountsProps) {
  const [accounts, setAccounts] = useState<ChatGPTOAuthAccount[]>([]);
  const [credentialName, setCredentialName] = useState("");
  const [flow, setFlow] = useState<DeviceFlow | null>(null);
  const [loading, setLoading] = useState(Boolean(accessToken));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAccounts = useCallback(async () => {
    if (!accessToken) return;
    const response = await chatgptOAuthApi.get<{ accounts: ChatGPTOAuthAccount[] }>(
      "/chatgpt/oauth/accounts",
      accessToken,
    );
    setAccounts(response.accounts);
  }, [accessToken]);

  useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    queueMicrotask(() => {
      loadAccounts()
        .catch((cause: unknown) => {
          if (!cancelled) setError(cause instanceof Error ? cause.message : "加载账号失败");
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    });
    return () => {
      cancelled = true;
    };
  }, [accessToken, loadAccounts]);

  useEffect(() => {
    if (!flow || !accessToken) return;
    let cancelled = false;
    const delay = pollDelayMs ?? flow.interval_seconds * 1000;
    const poll = async (): Promise<void> => {
      if (cancelled) return;
      try {
        const result = await chatgptOAuthApi.post<DevicePollResult>(
          `/chatgpt/oauth/device/${flow.flow_id}/poll`,
          accessToken,
        );
        if (result.status === "complete") {
          await loadAccounts();
          if (!cancelled) {
            setFlow(null);
            setCredentialName("");
          }
          return;
        }
      } catch (cause: unknown) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "登录状态检查失败");
        return;
      }
      if (!cancelled) window.setTimeout(poll, delay);
    };
    window.setTimeout(poll, delay);
    return () => {
      cancelled = true;
    };
  }, [accessToken, flow, loadAccounts, pollDelayMs]);

  const startLogin = async () => {
    if (!accessToken || !credentialName.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const started = await chatgptOAuthApi.post<DeviceFlow>("/chatgpt/oauth/device/start", accessToken, {
        credential_name: credentialName.trim(),
      });
      setFlow(started);
      window.open(started.verification_url, "_blank", "noopener,noreferrer");
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "启动登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  const setAccountStatus = async (account: ChatGPTOAuthAccount) => {
    if (!accessToken) return;
    const nextStatus = account.status === "disabled" ? "active" : "disabled";
    setError(null);
    try {
      await chatgptOAuthApi.patch(`/chatgpt/oauth/accounts/${account.credential_id}/status`, accessToken, {
        status: nextStatus,
      });
      await loadAccounts();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "更新账号失败");
    }
  };

  if (!accessToken) return null;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 px-6 py-8">
      <div>
        <h1 className="text-2xl font-semibold">ChatGPT 登录账号</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          通过 OpenAI 设备授权登录。系统不会要求或保存密码、验证码和浏览器 Cookie。
        </p>
      </div>

      <section className="rounded-lg border bg-card p-5">
        <h2 className="font-medium">添加账号</h2>
        <div className="mt-4 flex max-w-xl gap-3">
          <Input
            aria-label="账号名称"
            placeholder="例如：主账号"
            value={credentialName}
            onChange={(event) => setCredentialName(event.target.value)}
          />
          <Button onClick={startLogin} disabled={submitting || !credentialName.trim()}>
            {submitting ? "启动中" : "开始登录"}
          </Button>
        </div>
        {flow ? (
          <div className="mt-5 rounded-md border bg-muted/30 p-4">
            <p className="text-sm">请在 OpenAI 页面输入以下授权码，完成后本页会自动更新：</p>
            <p className="my-3 font-mono text-2xl font-semibold tracking-widest">{flow.user_code}</p>
            <a
              className="inline-flex h-9 items-center justify-center rounded-md border bg-background px-4 text-sm font-medium shadow-xs hover:bg-accent"
              href={flow.verification_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              打开 OpenAI 授权页面
            </a>
          </div>
        ) : null}
        {error ? <p className="mt-4 text-sm text-destructive">{error}</p> : null}
      </section>

      <section className="overflow-hidden rounded-lg border bg-card">
        <div className="border-b px-5 py-4">
          <h2 className="font-medium">已登录账号</h2>
        </div>
        {loading ? <p className="p-5 text-sm text-muted-foreground">加载中</p> : null}
        {!loading && accounts.length === 0 ? <p className="p-5 text-sm text-muted-foreground">暂无已登录账号</p> : null}
        {accounts.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b bg-muted/30 text-muted-foreground">
                <tr>
                  <th className="px-5 py-3 font-medium">名称</th>
                  <th className="px-5 py-3 font-medium">邮箱</th>
                  <th className="px-5 py-3 font-medium">状态</th>
                  <th className="px-5 py-3 font-medium">Token 到期时间</th>
                  <th className="px-5 py-3 font-medium">版本</th>
                  <th className="px-5 py-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.credential_id} className="border-b last:border-0">
                    <td className="px-5 py-4 font-medium">{account.credential_name}</td>
                    <td className="px-5 py-4">{account.email}</td>
                    <td className="px-5 py-4">
                      <Badge variant={account.status === "active" ? "default" : "secondary"}>
                        {statusLabel[account.status]}
                      </Badge>
                    </td>
                    <td className="px-5 py-4">{new Date(account.expires_at).toLocaleString()}</td>
                    <td className="px-5 py-4">{account.token_version}</td>
                    <td className="px-5 py-4">
                      <Button variant="outline" size="sm" onClick={() => setAccountStatus(account)}>
                        {account.status === "disabled" ? "启用" : "停用"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
