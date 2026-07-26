from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from litellm.llms.chatgpt.oauth_http_client import ChatGPTOAuthHTTPClient


class DeviceFlowRepository(Protocol):
    async def create(self, **kwargs: Any) -> None: ...

    async def get_for_admin(
        self,
        flow_id: str,
        admin_id: str,
    ) -> dict[str, Any] | None: ...

    async def claim(self, flow_id: str, admin_id: str) -> bool: ...

    async def complete(self, flow_id: str, admin_id: str) -> None: ...

    async def release(self, flow_id: str, admin_id: str) -> None: ...


class AccountCreator(Protocol):
    async def create_account(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class DeviceFlowStarted:
    flow_id: str
    credential_id: str
    verification_url: str
    user_code: str
    interval_seconds: int
    expires_at: datetime


@dataclass(frozen=True)
class DeviceFlowPollResponse:
    status: str
    credential_id: str | None = None
    credential_name: str | None = None
    email: str | None = None


class ChatGPTDeviceFlowService:
    def __init__(
        self,
        oauth_client: ChatGPTOAuthHTTPClient,
        flow_repository: DeviceFlowRepository,
        account_repository: AccountCreator,
        clock: Callable[[], datetime],
    ) -> None:
        self._oauth_client = oauth_client
        self._flow_repository = flow_repository
        self._account_repository = account_repository
        self._clock = clock

    async def start(self, credential_name: str, admin_id: str) -> DeviceFlowStarted:
        flow_id = str(uuid4())
        credential_id = str(uuid4())
        authorization = await self._oauth_client.request_device_authorization()
        expires_at = self._clock() + timedelta(seconds=authorization.expires_in_seconds)
        await self._flow_repository.create(
            flow_id=flow_id,
            credential_id=credential_id,
            admin_id=admin_id,
            expires_at=expires_at,
            record={
                "credential_name": credential_name,
                "device_auth_id": authorization.device_auth_id,
                "user_code": authorization.user_code,
            },
        )
        return DeviceFlowStarted(
            flow_id=flow_id,
            credential_id=credential_id,
            verification_url=authorization.verification_url,
            user_code=authorization.user_code,
            interval_seconds=authorization.interval_seconds,
            expires_at=expires_at,
        )

    async def poll(self, flow_id: str, admin_id: str) -> DeviceFlowPollResponse:
        flow = await self._flow_repository.get_for_admin(flow_id, admin_id)
        if flow is None:
            raise KeyError(f"ChatGPT device flow not found: {flow_id}")
        if flow["expires_at"] <= self._clock():
            raise TimeoutError("ChatGPT device authorization expired")
        record = flow["record"]
        poll_result = await self._oauth_client.poll_device_authorization(
            record["device_auth_id"],
            record["user_code"],
        )
        if poll_result.is_pending:
            return DeviceFlowPollResponse(status="pending")
        if not await self._flow_repository.claim(flow_id, admin_id):
            return DeviceFlowPollResponse(status="processing")
        try:
            if poll_result.authorization_code is None or poll_result.code_verifier is None:
                raise ValueError("Device authorization result is incomplete")
            authorization = await self._oauth_client.exchange_device_authorization(
                poll_result.authorization_code,
                poll_result.code_verifier,
            )
            await self._account_repository.create_account(
                credential_id=flow["credential_id"],
                credential_name=record["credential_name"],
                email=authorization.identity.email,
                account_id=authorization.identity.account_id,
                tokens=authorization.tokens,
                user_id=admin_id,
            )
            await self._flow_repository.complete(flow_id, admin_id)
        except Exception:
            await self._flow_repository.release(flow_id, admin_id)
            raise
        return DeviceFlowPollResponse(
            status="complete",
            credential_id=flow["credential_id"],
            credential_name=record["credential_name"],
            email=authorization.identity.email,
        )
