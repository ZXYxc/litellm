import json
from datetime import datetime
from typing import Any

from litellm.llms.chatgpt.oauth_token_cipher import ChatGPTOAuthTokenCipher


class PrismaChatGPTOAuthFlowRepository:
    def __init__(self, prisma_client: Any, cipher: ChatGPTOAuthTokenCipher) -> None:
        self._prisma_client = prisma_client
        self._cipher = cipher

    @property
    def table(self) -> Any:
        return self._prisma_client.db.litellm_chatgptoauthflowtable

    async def create(
        self,
        flow_id: str,
        credential_id: str,
        admin_id: str,
        expires_at: datetime,
        record: dict[str, Any],
    ) -> None:
        encrypted_record = self._cipher.encrypt(
            flow_id,
            "flow-record",
            json.dumps(record, separators=(",", ":")),
        )
        await self.table.create(
            data={
                "flow_id": flow_id,
                "credential_id": credential_id,
                "encrypted_flow_record": encrypted_record,
                "requested_by": admin_id,
                "purpose": "create",
                "status": "pending",
                "expires_at": expires_at,
            }
        )

    async def get_for_admin(
        self,
        flow_id: str,
        admin_id: str,
    ) -> dict[str, Any] | None:
        row = await self.table.find_unique(where={"flow_id": flow_id})
        if row is None:
            return None
        data = self._to_dict(row)
        if data["requested_by"] != admin_id:
            return None
        record = json.loads(
            self._cipher.decrypt(
                flow_id,
                "flow-record",
                data["encrypted_flow_record"],
            )
        )
        return {**data, "record": record}

    async def claim(self, flow_id: str, admin_id: str) -> bool:
        updated = await self.table.update_many(
            where={
                "flow_id": flow_id,
                "requested_by": admin_id,
                "status": "pending",
            },
            data={"status": "exchanging"},
        )
        return updated == 1

    async def complete(self, flow_id: str, admin_id: str) -> None:
        await self.table.update_many(
            where={"flow_id": flow_id, "requested_by": admin_id},
            data={"status": "complete"},
        )

    async def release(self, flow_id: str, admin_id: str) -> None:
        await self.table.update_many(
            where={
                "flow_id": flow_id,
                "requested_by": admin_id,
                "status": "exchanging",
            },
            data={"status": "pending"},
        )

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return row
        if hasattr(row, "model_dump"):
            return row.model_dump()
        if hasattr(row, "dict"):
            return row.dict()
        return vars(row)
