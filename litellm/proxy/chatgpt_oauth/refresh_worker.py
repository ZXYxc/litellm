import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from prisma.errors import PrismaError

from litellm.llms.chatgpt.oauth_account_service import (
    ChatGPTOAuthAccountService,
    ChatGPTOAuthAccountUnavailableError,
    OAuthInvalidGrantError,
    OAuthRefreshInProgressError,
    OAuthTransientError,
)


class ChatGPTOAuthRefreshCandidateRepository(Protocol):
    async def find_refresh_candidate_ids(
        self,
        deadline: datetime,
        limit: int,
    ) -> list[str]: ...


@dataclass(frozen=True)
class ChatGPTOAuthRefreshRunResult:
    candidate_count: int
    refreshed_count: int
    failed_count: int


class ChatGPTOAuthRefreshWorker:
    def __init__(
        self,
        repository: ChatGPTOAuthRefreshCandidateRepository,
        account_service: ChatGPTOAuthAccountService,
        clock: Callable[[], datetime],
        refresh_ahead_seconds: int = 300,
        batch_size: int = 100,
        max_concurrency: int = 5,
    ) -> None:
        self._repository = repository
        self._account_service = account_service
        self._clock = clock
        self._refresh_ahead_seconds = refresh_ahead_seconds
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run_once(self, referenced_credential_ids: set[str]) -> ChatGPTOAuthRefreshRunResult:
        deadline = self._clock() + timedelta(seconds=self._refresh_ahead_seconds)
        candidates = await self._repository.find_refresh_candidate_ids(
            deadline,
            self._batch_size,
        )
        referenced_candidates = [
            credential_id for credential_id in candidates if credential_id in referenced_credential_ids
        ]
        outcomes = await asyncio.gather(*(self._refresh_one(credential_id) for credential_id in referenced_candidates))
        refreshed_count = sum(outcomes)
        return ChatGPTOAuthRefreshRunResult(
            candidate_count=len(referenced_candidates),
            refreshed_count=refreshed_count,
            failed_count=len(referenced_candidates) - refreshed_count,
        )

    async def _refresh_one(self, credential_id: str) -> int:
        async with self._semaphore:
            try:
                await self._account_service.refresh_if_needed(
                    credential_id,
                    self._refresh_ahead_seconds,
                )
                return 1
            except (
                ChatGPTOAuthAccountUnavailableError,
                KeyError,
                OAuthInvalidGrantError,
                OAuthRefreshInProgressError,
                OAuthTransientError,
                PrismaError,
                RuntimeError,
            ):
                return 0
