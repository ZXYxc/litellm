"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

import ChatGPTOAuthAccounts from "./ChatGPTOAuthAccounts";

export default function ChatGPTOAuthAccountsPage() {
  const { accessToken } = useAuthorized();
  return <ChatGPTOAuthAccounts accessToken={accessToken} />;
}
