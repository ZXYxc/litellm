from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from litellm.llms.chatgpt.oauth_account_service import ChatGPTOAuthTokens
from litellm.llms.chatgpt.oauth_token_cipher import ChatGPTOAuthTokenCipher
from litellm.proxy.chatgpt_oauth.account_repository import (
    PrismaChatGPTOAuthAccountRepository,
)


class FakeAccountTable:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    async def create(self, data: Dict[str, Any]) -> SimpleNamespace:
        self.rows[data["credential_id"]] = dict(data)
        return SimpleNamespace(**data)

    async def find_unique(self, where: Dict[str, Any]) -> Optional[SimpleNamespace]:
        row = self.rows.get(where["credential_id"])
        return SimpleNamespace(**row) if row is not None else None

    async def update_many(self, where: Dict[str, Any], data: Dict[str, Any]) -> int:
        row = self.rows.get(where["credential_id"])
        if row is None or not self._matches(row, where):
            return 0
        for key, value in data.items():
            if isinstance(value, dict) and "increment" in value:
                row[key] += value["increment"]
            else:
                row[key] = value
        return 1

    def _matches(self, row: Dict[str, Any], where: Dict[str, Any]) -> bool:
        token_version = where.get("token_version")
        if token_version is not None and row["token_version"] != token_version:
            return False
        lease_owner = where.get("refresh_lease_owner")
        if lease_owner is not None and row.get("refresh_lease_owner") != lease_owner:
            return False
        conditions: List[Dict[str, Any]] = where.get("OR", [])
        if not conditions:
            return True
        return any(
            row.get("refresh_lease_until") is None
            if condition.get("refresh_lease_until") is None
            else row.get("refresh_lease_until") < condition["refresh_lease_until"]["lt"]
            for condition in conditions
        )


def make_repository(
    now: datetime,
) -> tuple[PrismaChatGPTOAuthAccountRepository, FakeAccountTable]:
    table = FakeAccountTable()
    db = SimpleNamespace(litellm_chatgptoauthaccounttable=table)
    prisma_client = SimpleNamespace(db=db)
    repository = PrismaChatGPTOAuthAccountRepository(
        prisma_client,
        ChatGPTOAuthTokenCipher("stable-test-key"),
        lambda: now,
    )
    return repository, table


@pytest.mark.asyncio
async def test_create_encrypts_sensitive_fields_and_get_decrypts_tokens() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository, table = make_repository(now)
    tokens = ChatGPTOAuthTokens(
        access_token="access-secret",
        refresh_token="refresh-secret",
        id_token="id-secret",
        expires_at=now + timedelta(hours=1),
    )

    await repository.create_account(
        credential_id="credential-a",
        credential_name="account-a",
        email="alice@example.com",
        account_id="chatgpt-account-a",
        tokens=tokens,
        user_id="admin-a",
    )
    account = await repository.get("credential-a")

    stored = str(table.rows["credential-a"])
    assert "access-secret" not in stored
    assert "refresh-secret" not in stored
    assert "alice@example.com" not in stored
    assert "chatgpt-account-a" not in stored
    assert account is not None
    assert account.tokens == tokens


@pytest.mark.asyncio
async def test_refresh_lease_and_token_update_use_versioned_compare_and_swap() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    repository, table = make_repository(now)
    initial_tokens = ChatGPTOAuthTokens(
        access_token="access-old",
        refresh_token="refresh-old",
        id_token="id-old",
        expires_at=now + timedelta(seconds=30),
    )
    await repository.create_account(
        credential_id="credential-a",
        credential_name="account-a",
        email="alice@example.com",
        account_id="chatgpt-account-a",
        tokens=initial_tokens,
        user_id="admin-a",
    )

    acquired = await repository.try_acquire_refresh_lease(
        "credential-a",
        expected_token_version=1,
        owner="replica-a",
        lease_until=now + timedelta(seconds=30),
    )
    duplicate = await repository.try_acquire_refresh_lease(
        "credential-a",
        expected_token_version=1,
        owner="replica-b",
        lease_until=now + timedelta(seconds=30),
    )
    new_tokens = ChatGPTOAuthTokens(
        access_token="access-new",
        refresh_token="refresh-new",
        id_token="id-new",
        expires_at=now + timedelta(hours=1),
    )
    updated = await repository.compare_and_swap_tokens("credential-a", 1, new_tokens)
    stale_update = await repository.compare_and_swap_tokens("credential-a", 1, initial_tokens)

    assert acquired is True
    assert duplicate is False
    assert updated is True
    assert stale_update is False
    assert table.rows["credential-a"]["token_version"] == 2
    assert table.rows["credential-a"]["refresh_lease_owner"] is None
