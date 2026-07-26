from datetime import datetime, timezone

import pytest

from litellm.proxy.chatgpt_oauth.refresh_worker import ChatGPTOAuthRefreshWorker


class FakeCandidateRepository:
    async def find_refresh_candidate_ids(
        self,
        deadline: datetime,
        limit: int,
    ) -> list[str]:
        return ["credential-a", "credential-b", "credential-unused"]


class FakeAccountService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def refresh_if_needed(
        self,
        credential_id: str,
        refresh_ahead_seconds: int,
    ) -> str:
        self.calls.append((credential_id, refresh_ahead_seconds))
        if credential_id == "credential-b":
            raise RuntimeError("isolated account failure")
        return f"token-{credential_id}"


@pytest.mark.asyncio
async def test_worker_refreshes_only_referenced_accounts_and_isolates_failures() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    service = FakeAccountService()
    worker = ChatGPTOAuthRefreshWorker(
        repository=FakeCandidateRepository(),
        account_service=service,
        clock=lambda: now,
        refresh_ahead_seconds=300,
        batch_size=100,
        max_concurrency=2,
    )

    result = await worker.run_once({"credential-a", "credential-b"})

    assert set(service.calls) == {
        ("credential-a", 300),
        ("credential-b", 300),
    }
    assert result.candidate_count == 2
    assert result.refreshed_count == 1
    assert result.failed_count == 1
