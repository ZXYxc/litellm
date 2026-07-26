from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from prisma.errors import PrismaError
from pydantic import BaseModel, ConfigDict, Field

from litellm.llms.chatgpt.id_token_verifier import OAuthIdentityVerificationError
from litellm.llms.chatgpt.oauth_account_service import (
    ChatGPTOAuthAccountStatus,
    OAuthTransientError,
)
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.chatgpt_oauth.runtime import (
    ChatGPTOAuthRuntime,
    get_chatgpt_oauth_runtime,
    initialize_chatgpt_oauth_runtime,
)

router = APIRouter(prefix="/chatgpt/oauth", tags=["ChatGPT OAuth accounts"])


class StartDeviceFlowRequest(BaseModel):
    credential_name: str = Field(min_length=1, max_length=128)
    model_config = ConfigDict(extra="forbid")


class DeviceFlowStartedResponse(BaseModel):
    flow_id: str
    credential_id: str
    verification_url: str
    user_code: str
    interval_seconds: int
    expires_at: datetime


class DeviceFlowPollResponseModel(BaseModel):
    status: str
    credential_id: str | None = None
    credential_name: str | None = None
    email: str | None = None


class AccountStatusRequest(BaseModel):
    status: Literal["active", "disabled"]
    model_config = ConfigDict(extra="forbid")


class AccountResponse(BaseModel):
    credential_id: str
    credential_name: str
    email: str
    status: ChatGPTOAuthAccountStatus
    expires_at: datetime
    token_version: int
    refresh_backoff_until: datetime | None = None


class AccountListResponse(BaseModel):
    accounts: list[AccountResponse]


class AccountStatusResponse(BaseModel):
    credential_id: str
    status: Literal["active", "disabled"]


def _require_proxy_admin(user: UserAPIKeyAuth) -> str:
    if user.user_role not in (LitellmUserRoles.PROXY_ADMIN, LitellmUserRoles.PROXY_ADMIN.value):
        raise HTTPException(status_code=403, detail="Proxy Admin role required")
    return str(user.user_id or "proxy-admin")


async def _runtime() -> ChatGPTOAuthRuntime:
    runtime = get_chatgpt_oauth_runtime()
    if runtime is not None:
        return runtime
    from litellm.proxy.proxy_server import llm_router, prisma_client

    if prisma_client is None:
        raise HTTPException(status_code=503, detail="Database is not connected")
    runtime = await initialize_chatgpt_oauth_runtime(prisma_client, llm_router)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="LITELLM_SALT_KEY is required for ChatGPT database OAuth",
        )
    return runtime


@router.post("/device/start", response_model=DeviceFlowStartedResponse)
async def start_device_flow(
    request: StartDeviceFlowRequest,
    user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> DeviceFlowStartedResponse:
    admin_id = _require_proxy_admin(user)
    runtime = await _runtime()
    started = await runtime.device_flow_service.start(request.credential_name, admin_id)
    return DeviceFlowStartedResponse(**started.__dict__)


@router.post("/device/{flow_id}/poll", response_model=DeviceFlowPollResponseModel)
async def poll_device_flow(
    flow_id: str,
    user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> DeviceFlowPollResponseModel:
    admin_id = _require_proxy_admin(user)
    runtime = await _runtime()
    try:
        result = await runtime.device_flow_service.poll(flow_id, admin_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Device authorization not found")
    except TimeoutError:
        raise HTTPException(status_code=410, detail="Device authorization expired")
    except (OAuthIdentityVerificationError, OAuthTransientError, PrismaError, ValueError):
        raise HTTPException(status_code=400, detail="Device authorization failed")
    return DeviceFlowPollResponseModel(**result.__dict__)


@router.get("/accounts", response_model=AccountListResponse)
async def list_accounts(
    user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> AccountListResponse:
    _require_proxy_admin(user)
    runtime = await _runtime()
    return AccountListResponse(accounts=await runtime.repository.list_admin_accounts())


@router.patch("/accounts/{credential_id}/status", response_model=AccountStatusResponse)
async def update_account_status(
    credential_id: str,
    request: AccountStatusRequest,
    user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> AccountStatusResponse:
    admin_id = _require_proxy_admin(user)
    runtime = await _runtime()
    updated = await runtime.repository.set_status(
        credential_id,
        ChatGPTOAuthAccountStatus(request.status),
        admin_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="ChatGPT OAuth account not found")
    return AccountStatusResponse(credential_id=credential_id, status=request.status)
