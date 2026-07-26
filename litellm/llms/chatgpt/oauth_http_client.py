import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .common_utils import (
    CHATGPT_AUTH_BASE,
    CHATGPT_CLIENT_ID,
    CHATGPT_DEVICE_CODE_URL,
    CHATGPT_DEVICE_TOKEN_URL,
    CHATGPT_DEVICE_VERIFY_URL,
    CHATGPT_OAUTH_TOKEN_URL,
)
from .oauth_account_service import (
    ChatGPTOAuthTokens,
    OAuthInvalidGrantError,
    OAuthRefreshResult,
    OAuthTransientError,
)


@dataclass(frozen=True)
class VerifiedChatGPTIdentity:
    email: str
    account_id: str
    subject: str


@dataclass(frozen=True)
class DeviceAuthorization:
    device_auth_id: str
    user_code: str
    verification_url: str
    interval_seconds: int
    expires_in_seconds: int


@dataclass(frozen=True)
class DeviceAuthorizationPollResult:
    is_pending: bool
    authorization_code: str | None = None
    code_verifier: str | None = None


@dataclass(frozen=True)
class OAuthAuthorizationResult:
    tokens: ChatGPTOAuthTokens
    identity: VerifiedChatGPTIdentity


class ChatGPTIDTokenVerifier(Protocol):
    async def verify(
        self,
        id_token: str,
        expected_nonce: str | None = None,
    ) -> VerifiedChatGPTIdentity: ...


class ChatGPTOAuthHTTPClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        identity_verifier: ChatGPTIDTokenVerifier,
        token_url: str = CHATGPT_OAUTH_TOKEN_URL,
        device_code_url: str = CHATGPT_DEVICE_CODE_URL,
        device_token_url: str = CHATGPT_DEVICE_TOKEN_URL,
    ) -> None:
        self._http_client = http_client
        self._identity_verifier = identity_verifier
        self._token_url = token_url
        self._device_code_url = device_code_url
        self._device_token_url = device_token_url

    async def request_device_authorization(self) -> DeviceAuthorization:
        try:
            response = await self._http_client.post(
                self._device_code_url,
                json={"client_id": CHATGPT_CLIENT_ID},
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OAuthTransientError("Unable to start ChatGPT device authorization") from error
        payload = self._response_payload(response)
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code") or payload.get("usercode")
        if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
            raise OAuthTransientError("Device authorization response is missing required fields")
        return DeviceAuthorization(
            device_auth_id=device_auth_id,
            user_code=user_code,
            verification_url=CHATGPT_DEVICE_VERIFY_URL,
            interval_seconds=self._positive_int(payload.get("interval"), 5),
            expires_in_seconds=self._positive_int(payload.get("expires_in"), 900),
        )

    async def poll_device_authorization(
        self,
        device_auth_id: str,
        user_code: str,
    ) -> DeviceAuthorizationPollResult:
        try:
            response = await self._http_client.post(
                self._device_token_url,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
            )
        except httpx.HTTPError as error:
            raise OAuthTransientError("Unable to poll ChatGPT device authorization") from error
        payload = self._response_payload(response)
        if response.status_code in (403, 404) or payload.get("error") == "authorization_pending":
            return DeviceAuthorizationPollResult(is_pending=True)
        if response.status_code >= 400:
            raise OAuthTransientError(f"Device authorization endpoint returned HTTP {response.status_code}")
        authorization_code = payload.get("authorization_code")
        code_verifier = payload.get("code_verifier")
        if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
            raise OAuthTransientError("Device authorization result is missing required fields")
        return DeviceAuthorizationPollResult(
            is_pending=False,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )

    async def exchange_device_authorization(
        self,
        authorization_code: str,
        code_verifier: str,
    ) -> OAuthAuthorizationResult:
        try:
            response = await self._http_client.post(
                self._token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{CHATGPT_AUTH_BASE}/deviceauth/callback",
                    "client_id": CHATGPT_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
            )
        except httpx.HTTPError as error:
            raise OAuthTransientError("Unable to exchange ChatGPT device authorization") from error
        if response.status_code >= 400:
            raise OAuthTransientError(f"OAuth token endpoint returned HTTP {response.status_code}")
        payload = self._response_payload(response)
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        id_token = payload.get("id_token")
        if not all(isinstance(value, str) and value for value in (access_token, refresh_token, id_token)):
            raise OAuthTransientError("OAuth token response is missing required fields")
        identity = await self._identity_verifier.verify(id_token)
        return OAuthAuthorizationResult(
            tokens=ChatGPTOAuthTokens(
                access_token=access_token,
                refresh_token=refresh_token,
                id_token=id_token,
                expires_at=self._access_token_expiry(access_token),
            ),
            identity=identity,
        )

    async def refresh(self, refresh_token: str) -> OAuthRefreshResult:
        try:
            response = await self._http_client.post(
                self._token_url,
                json={
                    "client_id": CHATGPT_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
            )
        except httpx.HTTPError as error:
            raise OAuthTransientError("OAuth token endpoint is temporarily unavailable") from error

        response_payload = self._response_payload(response)
        if response.status_code == 400 and response_payload.get("error") == "invalid_grant":
            raise OAuthInvalidGrantError("ChatGPT authorization is no longer valid")
        if response.status_code == 429 or response.status_code >= 500:
            raise OAuthTransientError("OAuth token endpoint is temporarily unavailable")
        if response.status_code >= 400:
            raise OAuthTransientError(f"OAuth token endpoint returned HTTP {response.status_code}")

        access_token = response_payload.get("access_token")
        id_token = response_payload.get("id_token")
        if not isinstance(access_token, str) or not isinstance(id_token, str):
            raise OAuthTransientError("OAuth token response is missing required fields")

        await self._identity_verifier.verify(id_token)
        return OAuthRefreshResult(
            access_token=access_token,
            refresh_token=self._optional_string(response_payload, "refresh_token"),
            id_token=id_token,
            expires_at=self._access_token_expiry(access_token),
        )

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _access_token_expiry(access_token: str) -> datetime:
        try:
            parts = access_token.split(".")
            if len(parts) != 3:
                raise ValueError("Invalid access token")
            encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded_payload))
            expires_at = payload.get("exp")
            if not isinstance(expires_at, (int, float)):
                raise ValueError("Access token has no expiry")
            return datetime.fromtimestamp(expires_at, tz=timezone.utc)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise OAuthTransientError("OAuth access token has no usable expiry") from error
