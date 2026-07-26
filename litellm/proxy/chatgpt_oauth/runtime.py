import asyncio
import os
import random
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from prisma.errors import PrismaError

import litellm
from litellm.llms.chatgpt.id_token_verifier import OpenAIIDTokenVerifier
from litellm.llms.chatgpt.oauth_account_service import ChatGPTOAuthAccountService
from litellm.llms.chatgpt.oauth_http_client import ChatGPTOAuthHTTPClient
from litellm.llms.chatgpt.oauth_token_cipher import ChatGPTOAuthTokenCipher
from litellm.proxy.hooks.chatgpt_oauth_credentials import (
    ChatGPTOAuthCredentialHook,
    ChatGPTOAuthCredentialResolver,
)

from .account_repository import PrismaChatGPTOAuthAccountRepository
from .device_flow_service import ChatGPTDeviceFlowService
from .flow_repository import PrismaChatGPTOAuthFlowRepository
from .refresh_worker import ChatGPTOAuthRefreshWorker


@dataclass
class ChatGPTOAuthRuntime:
    http_client: httpx.AsyncClient
    repository: PrismaChatGPTOAuthAccountRepository
    account_service: ChatGPTOAuthAccountService
    device_flow_service: ChatGPTDeviceFlowService
    credential_hook: ChatGPTOAuthCredentialHook
    refresh_worker: ChatGPTOAuthRefreshWorker
    router: Any
    refresh_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.refresh_task is None:
            self.refresh_task = asyncio.create_task(self._refresh_loop())

    async def close(self) -> None:
        if self.refresh_task is not None:
            self.refresh_task.cancel()
            try:
                await self.refresh_task
            except asyncio.CancelledError:
                pass
            self.refresh_task = None
        if self.credential_hook in litellm.callbacks:
            litellm.callbacks.remove(self.credential_hook)
        await self.http_client.aclose()

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh_worker.run_once(self._referenced_credential_ids())
            except asyncio.CancelledError:
                raise
            except PrismaError:
                pass
            await asyncio.sleep(60 + random.uniform(0, 5))

    def _referenced_credential_ids(self) -> set[str]:
        model_list = getattr(self.router, "model_list", None) or []
        return {
            credential_id
            for deployment in model_list
            if isinstance(deployment, dict)
            for params in [deployment.get("litellm_params")]
            if isinstance(params, dict)
            for credential_id in [params.get("chatgpt_oauth_credential_id")]
            if isinstance(credential_id, str) and credential_id
        }


@dataclass
class ChatGPTOAuthRuntimeHolder:
    runtime: ChatGPTOAuthRuntime | None = None


_runtime_holder = ChatGPTOAuthRuntimeHolder()


async def initialize_chatgpt_oauth_runtime(
    prisma_client: Any,
    router: Any,
) -> ChatGPTOAuthRuntime | None:
    if _runtime_holder.runtime is not None:
        return _runtime_holder.runtime
    if not os.getenv("LITELLM_SALT_KEY"):
        return None

    def clock() -> datetime:
        return datetime.now(timezone.utc)

    http_client = httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
    cipher = ChatGPTOAuthTokenCipher.from_environment()
    repository = PrismaChatGPTOAuthAccountRepository(prisma_client, cipher, clock)
    flow_repository = PrismaChatGPTOAuthFlowRepository(prisma_client, cipher)
    verifier = OpenAIIDTokenVerifier(http_client)
    oauth_client = ChatGPTOAuthHTTPClient(http_client, verifier)
    account_service = ChatGPTOAuthAccountService(
        repository,
        oauth_client,
        clock,
        f"{socket.gethostname()}:{uuid4()}",
    )
    resolver = ChatGPTOAuthCredentialResolver(account_service, repository)
    credential_hook = ChatGPTOAuthCredentialHook(resolver)
    refresh_worker = ChatGPTOAuthRefreshWorker(repository, account_service, clock)
    device_flow_service = ChatGPTDeviceFlowService(
        oauth_client,
        flow_repository,
        repository,
        clock,
    )
    runtime = ChatGPTOAuthRuntime(
        http_client=http_client,
        repository=repository,
        account_service=account_service,
        device_flow_service=device_flow_service,
        credential_hook=credential_hook,
        refresh_worker=refresh_worker,
        router=router,
    )
    if not any(isinstance(callback, ChatGPTOAuthCredentialHook) for callback in litellm.callbacks):
        litellm.callbacks.append(credential_hook)
    runtime.start()
    _runtime_holder.runtime = runtime
    return runtime


def get_chatgpt_oauth_runtime() -> ChatGPTOAuthRuntime | None:
    return _runtime_holder.runtime


async def shutdown_chatgpt_oauth_runtime() -> None:
    runtime = _runtime_holder.runtime
    if runtime is not None:
        await runtime.close()
        _runtime_holder.runtime = None
