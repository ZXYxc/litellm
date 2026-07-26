import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from litellm.llms.chatgpt.oauth_account_service import (
    ChatGPTOAuthAccount,
    ChatGPTOAuthAccountStatus,
    ChatGPTOAuthTokens,
)
from litellm.llms.chatgpt.oauth_token_cipher import ChatGPTOAuthTokenCipher


class PrismaChatGPTOAuthAccountRepository:
    def __init__(
        self,
        prisma_client: Any,
        cipher: ChatGPTOAuthTokenCipher,
        clock: Callable[[], datetime],
    ) -> None:
        self._prisma_client = prisma_client
        self._cipher = cipher
        self._clock = clock

    @property
    def table(self) -> Any:
        if self._prisma_client is None:
            raise RuntimeError("A database is required for ChatGPT database OAuth")
        return self._prisma_client.db.litellm_chatgptoauthaccounttable

    async def create_account(
        self,
        credential_id: str,
        credential_name: str,
        email: str,
        account_id: str,
        tokens: ChatGPTOAuthTokens,
        user_id: str,
    ) -> ChatGPTOAuthAccount:
        record = await self.table.create(
            data={
                "credential_id": credential_id,
                "credential_name": credential_name,
                "provider": "chatgpt",
                "email_encrypted": self._cipher.encrypt(credential_id, "email", email),
                "email_fingerprint": self._cipher.email_fingerprint(email),
                "account_id_encrypted": self._cipher.encrypt(
                    credential_id,
                    "account-id",
                    account_id,
                ),
                "token_record_encrypted": self._encrypt_tokens(credential_id, tokens),
                "status": ChatGPTOAuthAccountStatus.ACTIVE.value,
                "expires_at": tokens.expires_at,
                "token_version": 1,
                "refresh_failure_count": 0,
                "created_by": user_id,
                "updated_by": user_id,
            }
        )
        return self._to_account(record)

    async def get(self, credential_id: str) -> ChatGPTOAuthAccount | None:
        record = await self.table.find_unique(where={"credential_id": credential_id})
        if record is None:
            return None
        return self._to_account(record)

    async def get_account_id(self, credential_id: str) -> str:
        record = await self.table.find_unique(where={"credential_id": credential_id})
        if record is None:
            raise KeyError(f"ChatGPT OAuth credential not found: {credential_id}")
        data = self._to_dict(record)
        return self._cipher.decrypt(
            credential_id,
            "account-id",
            data["account_id_encrypted"],
        )

    async def find_refresh_candidate_ids(
        self,
        deadline: datetime,
        limit: int,
    ) -> list[str]:
        records = await self.table.find_many(
            where={
                "status": ChatGPTOAuthAccountStatus.ACTIVE.value,
                "expires_at": {"lte": deadline},
                "OR": [
                    {"refresh_backoff_until": None},
                    {"refresh_backoff_until": {"lte": self._clock()}},
                ],
            },
            order={"expires_at": "asc"},
            take=limit,
        )
        return [self._to_dict(record)["credential_id"] for record in records]

    async def list_admin_accounts(self) -> list[dict[str, Any]]:
        records = await self.table.find_many(order={"credential_name": "asc"})
        accounts: list[dict[str, Any]] = []
        for record in records:
            data = self._to_dict(record)
            accounts.append(
                {
                    "credential_id": data["credential_id"],
                    "credential_name": data["credential_name"],
                    "email": self._cipher.decrypt(
                        data["credential_id"],
                        "email",
                        data["email_encrypted"],
                    ),
                    "status": data["status"],
                    "expires_at": data["expires_at"],
                    "token_version": data["token_version"],
                    "refresh_backoff_until": data.get("refresh_backoff_until"),
                }
            )
        return accounts

    async def set_status(
        self,
        credential_id: str,
        status: ChatGPTOAuthAccountStatus,
        user_id: str,
    ) -> bool:
        updated = await self.table.update_many(
            where={"credential_id": credential_id},
            data={
                "status": status.value,
                "updated_by": user_id,
                "refresh_lease_owner": None,
                "refresh_lease_until": None,
            },
        )
        return updated == 1

    async def try_acquire_refresh_lease(
        self,
        credential_id: str,
        expected_token_version: int,
        owner: str,
        lease_until: datetime,
    ) -> bool:
        updated = await self.table.update_many(
            where={
                "credential_id": credential_id,
                "status": ChatGPTOAuthAccountStatus.ACTIVE.value,
                "token_version": expected_token_version,
                "OR": [
                    {"refresh_lease_until": None},
                    {"refresh_lease_until": {"lt": self._clock()}},
                ],
            },
            data={
                "refresh_lease_owner": owner,
                "refresh_lease_until": lease_until,
                "updated_by": owner,
            },
        )
        return updated == 1

    async def compare_and_swap_tokens(
        self,
        credential_id: str,
        expected_token_version: int,
        tokens: ChatGPTOAuthTokens,
    ) -> bool:
        updated = await self.table.update_many(
            where={
                "credential_id": credential_id,
                "token_version": expected_token_version,
            },
            data={
                "token_record_encrypted": self._encrypt_tokens(credential_id, tokens),
                "expires_at": tokens.expires_at,
                "token_version": {"increment": 1},
                "refresh_lease_owner": None,
                "refresh_lease_until": None,
                "refresh_failure_count": 0,
                "refresh_backoff_until": None,
                "last_refresh_error": None,
            },
        )
        return updated == 1

    async def mark_reauth_required(self, credential_id: str, reason: str) -> None:
        await self.table.update_many(
            where={"credential_id": credential_id},
            data={
                "status": ChatGPTOAuthAccountStatus.REAUTH_REQUIRED.value,
                "refresh_lease_owner": None,
                "refresh_lease_until": None,
                "last_refresh_error": reason[:128],
            },
        )

    async def record_transient_refresh_failure(
        self,
        credential_id: str,
        retry_at: datetime,
        reason: str,
    ) -> None:
        await self.table.update_many(
            where={"credential_id": credential_id},
            data={
                "refresh_failure_count": {"increment": 1},
                "refresh_backoff_until": retry_at,
                "refresh_lease_owner": None,
                "refresh_lease_until": None,
                "last_refresh_error": reason[:128],
            },
        )

    async def release_refresh_lease(self, credential_id: str, owner: str) -> None:
        await self.table.update_many(
            where={
                "credential_id": credential_id,
                "refresh_lease_owner": owner,
            },
            data={
                "refresh_lease_owner": None,
                "refresh_lease_until": None,
            },
        )

    def _encrypt_tokens(
        self,
        credential_id: str,
        tokens: ChatGPTOAuthTokens,
    ) -> str:
        payload = json.dumps(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "id_token": tokens.id_token,
                "expires_at": tokens.expires_at.isoformat(),
            },
            separators=(",", ":"),
        )
        return self._cipher.encrypt(credential_id, "token-record", payload)

    def _to_account(self, record: Any) -> ChatGPTOAuthAccount:
        data = self._to_dict(record)
        encrypted_tokens = data["token_record_encrypted"]
        token_payload = json.loads(
            self._cipher.decrypt(
                data["credential_id"],
                "token-record",
                encrypted_tokens,
            )
        )
        return ChatGPTOAuthAccount(
            credential_id=data["credential_id"],
            status=ChatGPTOAuthAccountStatus(data["status"]),
            tokens=ChatGPTOAuthTokens(
                access_token=token_payload["access_token"],
                refresh_token=token_payload["refresh_token"],
                id_token=token_payload["id_token"],
                expires_at=datetime.fromisoformat(token_payload["expires_at"]),
            ),
            token_version=data["token_version"],
            refresh_failure_count=data.get("refresh_failure_count", 0),
        )

    @staticmethod
    def _to_dict(record: Any) -> dict[str, Any]:
        if isinstance(record, dict):
            return record
        if hasattr(record, "model_dump"):
            return record.model_dump()
        if hasattr(record, "dict"):
            return record.dict()
        if hasattr(record, "__dict__"):
            return vars(record)
        return dict(record)
