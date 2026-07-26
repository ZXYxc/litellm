from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from litellm.llms.chatgpt.oauth_account_service import ChatGPTOAuthTokens
from litellm.llms.chatgpt.oauth_http_client import (
    DeviceAuthorization,
    DeviceAuthorizationPollResult,
    OAuthAuthorizationResult,
    VerifiedChatGPTIdentity,
)
from litellm.proxy.chatgpt_oauth.device_flow_service import ChatGPTDeviceFlowService


class FakeFlowRepository:
    def __init__(self) -> None:
        self.flow: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> None:
        self.flow = kwargs

    async def get_for_admin(self, flow_id: str, admin_id: str) -> dict[str, Any] | None:
        return self.flow

    async def claim(self, flow_id: str, admin_id: str) -> bool:
        return True

    async def complete(self, flow_id: str, admin_id: str) -> None:
        return None

    async def release(self, flow_id: str, admin_id: str) -> None:
        return None


class FakeOAuthClient:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def request_device_authorization(self) -> DeviceAuthorization:
        return DeviceAuthorization("device-a", "ABCD-EFGH", "https://auth.test/device", 5, 900)

    async def poll_device_authorization(self, device_auth_id: str, user_code: str) -> DeviceAuthorizationPollResult:
        return DeviceAuthorizationPollResult(
            is_pending=False,
            authorization_code="authorization-a",
            code_verifier="verifier-a",
        )

    async def exchange_device_authorization(
        self,
        authorization_code: str,
        code_verifier: str,
    ) -> OAuthAuthorizationResult:
        return OAuthAuthorizationResult(
            tokens=ChatGPTOAuthTokens(
                "access-a",
                "refresh-a",
                "id-a",
                self.now + timedelta(hours=1),
            ),
            identity=VerifiedChatGPTIdentity(
                "alice@example.com",
                "account-a",
                "subject-a",
            ),
        )


class FakeAccountRepository:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None

    async def create_account(self, **kwargs: Any) -> Any:
        self.created = kwargs
        return None


@pytest.mark.asyncio
async def test_device_flow_creates_account_from_verified_identity() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    flow_repository = FakeFlowRepository()
    account_repository = FakeAccountRepository()
    service = ChatGPTDeviceFlowService(
        oauth_client=FakeOAuthClient(now),
        flow_repository=flow_repository,
        account_repository=account_repository,
        clock=lambda: now,
    )

    started = await service.start("primary-account", "admin-a")
    completed = await service.poll(started.flow_id, "admin-a")

    assert started.user_code == "ABCD-EFGH"
    assert completed.status == "complete"
    assert completed.email == "alice@example.com"
    assert account_repository.created is not None
    assert account_repository.created["email"] == "alice@example.com"
    assert account_repository.created["account_id"] == "account-a"
    assert "password" not in str(flow_repository.flow).casefold()
