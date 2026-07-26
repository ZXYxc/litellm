from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from litellm.llms.chatgpt.oauth_account_service import (
    ChatGPTOAuthAccount,
    ChatGPTOAuthAccountService,
    ChatGPTOAuthAccountStatus,
    ChatGPTOAuthTokens,
    OAuthInvalidGrantError,
    OAuthRefreshResult,
    OAuthTransientError,
)


class InMemoryAccountRepository:
    def __init__(self, account: ChatGPTOAuthAccount) -> None:
        self.account = account
        self.lease_acquisitions = 0
        self.cas_updates = 0
        self.transient_failures = 0

    async def get(self, credential_id: str) -> Optional[ChatGPTOAuthAccount]:
        if credential_id != self.account.credential_id:
            return None
        return self.account

    async def try_acquire_refresh_lease(
        self,
        credential_id: str,
        expected_token_version: int,
        owner: str,
        lease_until: datetime,
    ) -> bool:
        if credential_id != self.account.credential_id:
            return False
        if expected_token_version != self.account.token_version:
            return False
        self.lease_acquisitions += 1
        return True

    async def compare_and_swap_tokens(
        self,
        credential_id: str,
        expected_token_version: int,
        tokens: ChatGPTOAuthTokens,
    ) -> bool:
        if credential_id != self.account.credential_id:
            return False
        if expected_token_version != self.account.token_version:
            return False
        self.cas_updates += 1
        self.account = self.account.with_tokens(tokens)
        return True

    async def mark_reauth_required(self, credential_id: str, reason: str) -> None:
        self.account = self.account.with_status(ChatGPTOAuthAccountStatus.REAUTH_REQUIRED)

    async def record_transient_refresh_failure(
        self,
        credential_id: str,
        retry_at: datetime,
        reason: str,
    ) -> None:
        self.transient_failures += 1

    async def release_refresh_lease(self, credential_id: str, owner: str) -> None:
        return None


class FakeOAuthClient:
    def __init__(self, result: OAuthRefreshResult) -> None:
        self.result = result
        self.calls = 0
        self.error: Optional[Exception] = None

    async def refresh(self, refresh_token: str) -> OAuthRefreshResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def make_account(expires_at: datetime) -> ChatGPTOAuthAccount:
    return ChatGPTOAuthAccount(
        credential_id="credential-a",
        status=ChatGPTOAuthAccountStatus.ACTIVE,
        tokens=ChatGPTOAuthTokens(
            access_token="access-old",
            refresh_token="refresh-old",
            id_token="id-old",
            expires_at=expires_at,
        ),
        token_version=3,
    )


def make_refresh_result(
    now: datetime,
    refresh_token: Optional[str] = "refresh-new",
) -> OAuthRefreshResult:
    return OAuthRefreshResult(
        access_token="access-new",
        refresh_token=refresh_token,
        id_token="id-new",
        expires_at=now + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_returns_current_access_token_outside_refresh_window() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(minutes=10)))
    oauth_client = FakeOAuthClient(make_refresh_result(now))
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    token = await service.get_valid_access_token("credential-a", refresh_ahead_seconds=60)

    assert token == "access-old"
    assert oauth_client.calls == 0
    assert repository.lease_acquisitions == 0


@pytest.mark.asyncio
async def test_refreshes_expiring_token_and_persists_rotated_refresh_token() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(seconds=30)))
    oauth_client = FakeOAuthClient(make_refresh_result(now))
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    token = await service.get_valid_access_token("credential-a", refresh_ahead_seconds=60)

    assert token == "access-new"
    assert repository.account.tokens.refresh_token == "refresh-new"
    assert repository.account.token_version == 4
    assert repository.cas_updates == 1


@pytest.mark.asyncio
async def test_refresh_keeps_existing_refresh_token_when_response_omits_one() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(seconds=30)))
    oauth_client = FakeOAuthClient(make_refresh_result(now, refresh_token=None))
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    await service.get_valid_access_token("credential-a", refresh_ahead_seconds=60)

    assert repository.account.tokens.refresh_token == "refresh-old"


@pytest.mark.asyncio
async def test_invalid_grant_marks_account_for_reauthentication() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(seconds=30)))
    oauth_client = FakeOAuthClient(make_refresh_result(now))
    oauth_client.error = OAuthInvalidGrantError("authorization revoked")
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    with pytest.raises(OAuthInvalidGrantError):
        await service.get_valid_access_token("credential-a", refresh_ahead_seconds=60)

    assert repository.account.status == ChatGPTOAuthAccountStatus.REAUTH_REQUIRED


@pytest.mark.asyncio
async def test_transient_refresh_failure_is_recorded_without_replacing_tokens() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(seconds=30)))
    oauth_client = FakeOAuthClient(make_refresh_result(now))
    oauth_client.error = OAuthTransientError("token endpoint unavailable")
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    with pytest.raises(OAuthTransientError):
        await service.get_valid_access_token("credential-a", refresh_ahead_seconds=60)

    assert repository.transient_failures == 1
    assert repository.account.tokens.access_token == "access-old"
    assert repository.account.token_version == 3


@pytest.mark.asyncio
async def test_missing_database_credential_fails_without_fallback() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository = InMemoryAccountRepository(make_account(now + timedelta(minutes=10)))
    oauth_client = FakeOAuthClient(make_refresh_result(now))
    service = ChatGPTOAuthAccountService(repository, oauth_client, lambda: now, "replica-a")

    with pytest.raises(KeyError, match="missing-credential"):
        await service.get_valid_access_token("missing-credential", refresh_ahead_seconds=60)

    assert oauth_client.calls == 0
