from typing import Any, Dict

import pytest

from litellm.proxy.hooks.chatgpt_oauth_credentials import (
    ChatGPTOAuthCredentialHook,
    ResolvedChatGPTOAuthCredential,
)


class FakeCredentialResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, credential_id: str) -> ResolvedChatGPTOAuthCredential:
        self.calls += 1
        return ResolvedChatGPTOAuthCredential(
            credential_id=credential_id,
            access_token="database-access-token",
            account_id="database-account-id",
        )


@pytest.mark.asyncio
async def test_hook_ignores_requests_without_database_credential() -> None:
    resolver = FakeCredentialResolver()
    hook = ChatGPTOAuthCredentialHook(resolver)
    request: Dict[str, Any] = {"model": "chatgpt/gpt-5"}

    result = await hook.async_pre_call_deployment_hook(request, None)

    assert result == request
    assert resolver.calls == 0


@pytest.mark.asyncio
async def test_hook_resolves_selected_deployment_credential() -> None:
    resolver = FakeCredentialResolver()
    hook = ChatGPTOAuthCredentialHook(resolver)
    request: Dict[str, Any] = {
        "model": "chatgpt/gpt-5",
        "chatgpt_oauth_credential_id": "credential-a",
    }

    result = await hook.async_pre_call_deployment_hook(request, None)

    assert result is not None
    assert result["api_key"] == "database-access-token"
    assert result["chatgpt_account_id"] == "database-account-id"
    assert result["chatgpt_oauth_resolved"] is True
    assert resolver.calls == 1


@pytest.mark.asyncio
async def test_hook_overwrites_untrusted_identity_values() -> None:
    resolver = FakeCredentialResolver()
    hook = ChatGPTOAuthCredentialHook(resolver)
    request: Dict[str, Any] = {
        "chatgpt_oauth_credential_id": "credential-a",
        "api_key": "attacker-token",
        "chatgpt_account_id": "attacker-account",
        "headers": {
            "Authorization": "Bearer attacker-token",
            "ChatGPT-Account-Id": "attacker-account",
        },
    }

    result = await hook.async_pre_call_deployment_hook(request, None)

    assert result is not None
    assert result["api_key"] == "database-access-token"
    assert result["chatgpt_account_id"] == "database-account-id"
    assert "Authorization" not in result["headers"]
    assert "ChatGPT-Account-Id" not in result["headers"]
