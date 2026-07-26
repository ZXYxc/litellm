from dataclasses import dataclass
from typing import Any, Protocol

from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.chatgpt.oauth_account_service import ChatGPTOAuthAccountService
from litellm.types.utils import CallTypes


@dataclass(frozen=True)
class ResolvedChatGPTOAuthCredential:
    credential_id: str
    access_token: str
    account_id: str


class ChatGPTOAuthCredentialResolverProtocol(Protocol):
    async def resolve(self, credential_id: str) -> ResolvedChatGPTOAuthCredential: ...


class ChatGPTOAuthIdentityReader(Protocol):
    async def get_account_id(self, credential_id: str) -> str: ...


class ChatGPTOAuthCredentialResolver:
    def __init__(
        self,
        account_service: ChatGPTOAuthAccountService,
        identity_reader: ChatGPTOAuthIdentityReader,
    ) -> None:
        self._account_service = account_service
        self._identity_reader = identity_reader

    async def resolve(self, credential_id: str) -> ResolvedChatGPTOAuthCredential:
        access_token = await self._account_service.get_valid_access_token(
            credential_id,
            refresh_ahead_seconds=60,
        )
        account_id = await self._identity_reader.get_account_id(credential_id)
        return ResolvedChatGPTOAuthCredential(
            credential_id=credential_id,
            access_token=access_token,
            account_id=account_id,
        )


class ChatGPTOAuthCredentialHook(CustomLogger):
    def __init__(self, resolver: ChatGPTOAuthCredentialResolverProtocol) -> None:
        self._resolver = resolver

    async def async_pre_call_deployment_hook(
        self,
        kwargs: dict[str, Any],
        call_type: CallTypes | None,
    ) -> dict | None:
        credential_id = kwargs.get("chatgpt_oauth_credential_id")
        if not isinstance(credential_id, str) or not credential_id:
            return kwargs

        credential = await self._resolver.resolve(credential_id)
        sanitized_headers = {
            key: value
            for key, value in dict(kwargs.get("headers") or {}).items()
            if key.casefold() not in {"authorization", "chatgpt-account-id"}
        }
        return {
            **kwargs,
            "api_key": credential.access_token,
            "chatgpt_account_id": credential.account_id,
            "chatgpt_oauth_resolved": True,
            "headers": sanitized_headers,
        }
