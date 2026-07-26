import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from litellm.llms.chatgpt.oauth_account_service import (
    OAuthInvalidGrantError,
    OAuthTransientError,
)
from litellm.llms.chatgpt.oauth_http_client import (
    ChatGPTOAuthHTTPClient,
    VerifiedChatGPTIdentity,
)


def make_jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class FakeIdentityVerifier:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def verify(
        self,
        id_token: str,
        expected_nonce: str | None = None,
    ) -> VerifiedChatGPTIdentity:
        self.tokens.append(id_token)
        return VerifiedChatGPTIdentity(
            email="alice@example.com",
            account_id="account-a",
            subject="subject-a",
        )


@pytest.mark.asyncio
async def test_refresh_validates_identity_and_returns_rotated_tokens() -> None:
    access_token = make_jwt({"exp": 1785060000})
    id_token = make_jwt({"sub": "subject-a"})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["refresh_token"] == "refresh-old"
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "refresh-new",
                "id_token": id_token,
            },
        )

    verifier = FakeIdentityVerifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth_client = ChatGPTOAuthHTTPClient(client, verifier)
        result = await oauth_client.refresh("refresh-old")

    assert result.access_token == access_token
    assert result.refresh_token == "refresh-new"
    assert result.expires_at == datetime.fromtimestamp(1785060000, tz=timezone.utc)
    assert verifier.tokens == [id_token]


@pytest.mark.asyncio
async def test_refresh_maps_invalid_grant_without_exposing_refresh_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    verifier = FakeIdentityVerifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth_client = ChatGPTOAuthHTTPClient(client, verifier)
        with pytest.raises(OAuthInvalidGrantError) as error:
            await oauth_client.refresh("refresh-secret-value")

    assert "refresh-secret-value" not in str(error.value)


@pytest.mark.asyncio
async def test_refresh_maps_server_failure_to_transient_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "temporarily_unavailable"})

    verifier = FakeIdentityVerifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth_client = ChatGPTOAuthHTTPClient(client, verifier)
        with pytest.raises(OAuthTransientError, match="temporarily unavailable"):
            await oauth_client.refresh("refresh-secret-value")


@pytest.mark.asyncio
async def test_starts_and_polls_device_authorization() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device-code":
            assert json.loads(request.content) == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "device-auth-a",
                    "user_code": "ABCD-EFGH",
                    "interval": 3,
                    "expires_in": 900,
                },
            )
        return httpx.Response(403, json={"error": "authorization_pending"})

    verifier = FakeIdentityVerifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth_client = ChatGPTOAuthHTTPClient(
            client,
            verifier,
            device_code_url="https://auth.openai.test/device-code",
            device_token_url="https://auth.openai.test/device-token",
        )
        authorization = await oauth_client.request_device_authorization()
        poll_result = await oauth_client.poll_device_authorization(
            authorization.device_auth_id,
            authorization.user_code,
        )

    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.interval_seconds == 3
    assert poll_result.is_pending is True


@pytest.mark.asyncio
async def test_exchanges_device_authorization_and_validates_identity() -> None:
    access_token = make_jwt({"exp": 1785060000})
    id_token = make_jwt({"sub": "subject-a"})

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "refresh-new",
                "id_token": id_token,
            },
        )

    verifier = FakeIdentityVerifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth_client = ChatGPTOAuthHTTPClient(client, verifier)
        result = await oauth_client.exchange_device_authorization(
            authorization_code="authorization-code-a",
            code_verifier="code-verifier-a",
        )

    assert result.tokens.refresh_token == "refresh-new"
    assert result.identity.account_id == "account-a"
    assert verifier.tokens == [id_token]
