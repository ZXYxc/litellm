from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol


class ChatGPTOAuthAccountStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REAUTH_REQUIRED = "reauth_required"


@dataclass(frozen=True)
class ChatGPTOAuthTokens:
    access_token: str
    refresh_token: str
    id_token: str
    expires_at: datetime


@dataclass(frozen=True)
class OAuthRefreshResult:
    access_token: str
    refresh_token: str | None
    id_token: str
    expires_at: datetime


@dataclass(frozen=True)
class ChatGPTOAuthAccount:
    credential_id: str
    status: ChatGPTOAuthAccountStatus
    tokens: ChatGPTOAuthTokens
    token_version: int
    refresh_failure_count: int = 0

    def with_tokens(self, tokens: ChatGPTOAuthTokens) -> "ChatGPTOAuthAccount":
        return replace(
            self,
            tokens=tokens,
            token_version=self.token_version + 1,
            refresh_failure_count=0,
        )

    def with_status(self, status: ChatGPTOAuthAccountStatus) -> "ChatGPTOAuthAccount":
        return replace(self, status=status)


class OAuthInvalidGrantError(Exception):
    pass


class OAuthTransientError(Exception):
    pass


class OAuthRefreshInProgressError(Exception):
    pass


class ChatGPTOAuthAccountUnavailableError(Exception):
    pass


class ChatGPTOAuthClient(Protocol):
    async def refresh(self, refresh_token: str) -> OAuthRefreshResult: ...


class ChatGPTOAuthAccountRepository(Protocol):
    async def get(self, credential_id: str) -> ChatGPTOAuthAccount | None: ...

    async def try_acquire_refresh_lease(
        self,
        credential_id: str,
        expected_token_version: int,
        owner: str,
        lease_until: datetime,
    ) -> bool: ...

    async def compare_and_swap_tokens(
        self,
        credential_id: str,
        expected_token_version: int,
        tokens: ChatGPTOAuthTokens,
    ) -> bool: ...

    async def mark_reauth_required(self, credential_id: str, reason: str) -> None: ...

    async def record_transient_refresh_failure(
        self,
        credential_id: str,
        retry_at: datetime,
        reason: str,
    ) -> None: ...

    async def release_refresh_lease(self, credential_id: str, owner: str) -> None: ...


class ChatGPTOAuthAccountService:
    def __init__(
        self,
        repository: ChatGPTOAuthAccountRepository,
        oauth_client: ChatGPTOAuthClient,
        clock: Callable[[], datetime],
        lease_owner: str,
        lease_seconds: int = 30,
    ) -> None:
        self._repository = repository
        self._oauth_client = oauth_client
        self._clock = clock
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds

    async def get_valid_access_token(
        self,
        credential_id: str,
        refresh_ahead_seconds: int = 60,
    ) -> str:
        account = await self._get_active_account(credential_id)
        if not self._needs_refresh(account, refresh_ahead_seconds):
            return account.tokens.access_token
        return await self.refresh_if_needed(credential_id, refresh_ahead_seconds)

    async def refresh_if_needed(
        self,
        credential_id: str,
        refresh_ahead_seconds: int,
    ) -> str:
        account = await self._get_active_account(credential_id)
        if not self._needs_refresh(account, refresh_ahead_seconds):
            return account.tokens.access_token

        lease_until = self._clock() + timedelta(seconds=self._lease_seconds)
        acquired = await self._repository.try_acquire_refresh_lease(
            credential_id=credential_id,
            expected_token_version=account.token_version,
            owner=self._lease_owner,
            lease_until=lease_until,
        )
        if not acquired:
            current = await self._get_active_account(credential_id)
            if current.token_version != account.token_version or not self._needs_refresh(
                current, refresh_ahead_seconds
            ):
                return current.tokens.access_token
            raise OAuthRefreshInProgressError(f"OAuth credential refresh is already in progress: {credential_id}")

        try:
            current = await self._get_active_account(credential_id)
            if not self._needs_refresh(current, refresh_ahead_seconds):
                return current.tokens.access_token
            return await self._refresh_with_lease(current)
        finally:
            await self._repository.release_refresh_lease(credential_id, self._lease_owner)

    async def _refresh_with_lease(self, account: ChatGPTOAuthAccount) -> str:
        try:
            result = await self._oauth_client.refresh(account.tokens.refresh_token)
        except OAuthInvalidGrantError:
            await self._repository.mark_reauth_required(account.credential_id, "invalid_grant")
            raise
        except OAuthTransientError:
            retry_at = self._clock() + timedelta(seconds=min(30 * (2**account.refresh_failure_count), 300))
            await self._repository.record_transient_refresh_failure(
                account.credential_id,
                retry_at,
                "transient_error",
            )
            raise

        tokens = ChatGPTOAuthTokens(
            access_token=result.access_token,
            refresh_token=result.refresh_token or account.tokens.refresh_token,
            id_token=result.id_token,
            expires_at=result.expires_at,
        )
        updated = await self._repository.compare_and_swap_tokens(
            credential_id=account.credential_id,
            expected_token_version=account.token_version,
            tokens=tokens,
        )
        if updated:
            return tokens.access_token

        current = await self._get_active_account(account.credential_id)
        if current.token_version != account.token_version:
            return current.tokens.access_token
        raise OAuthTransientError(f"OAuth credential token update conflicted: {account.credential_id}")

    async def _get_active_account(self, credential_id: str) -> ChatGPTOAuthAccount:
        account = await self._repository.get(credential_id)
        if account is None:
            raise KeyError(f"ChatGPT OAuth credential not found: {credential_id}")
        if account.status != ChatGPTOAuthAccountStatus.ACTIVE:
            raise ChatGPTOAuthAccountUnavailableError(
                f"ChatGPT OAuth credential is {account.status.value}: {credential_id}"
            )
        return account

    def _needs_refresh(
        self,
        account: ChatGPTOAuthAccount,
        refresh_ahead_seconds: int,
    ) -> bool:
        return account.tokens.expires_at <= self._clock() + timedelta(seconds=refresh_ahead_seconds)
