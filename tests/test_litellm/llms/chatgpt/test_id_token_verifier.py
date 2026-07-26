import json
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from litellm.llms.chatgpt.id_token_verifier import (
    OpenAIIDTokenVerifier,
    OAuthIdentityVerificationError,
)


def make_verifier_fixture(
    issuer: str = "https://auth.openai.test",
) -> tuple[httpx.MockTransport, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": "test-client-id",
            "sub": "subject-a",
            "email": "alice@example.com",
            "nonce": "nonce-a",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-a"},
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"},
            )
        return httpx.Response(200, json={"keys": [jwk]})

    return httpx.MockTransport(handler), token


@pytest.mark.asyncio
async def test_verifies_signature_claims_nonce_and_identity() -> None:
    transport, token = make_verifier_fixture()
    async with httpx.AsyncClient(transport=transport) as client:
        verifier = OpenAIIDTokenVerifier(
            client,
            issuer="https://auth.openai.test",
            client_id="test-client-id",
        )
        identity = await verifier.verify(token, expected_nonce="nonce-a")

    assert identity.email == "alice@example.com"
    assert identity.account_id == "account-a"
    assert identity.subject == "subject-a"


@pytest.mark.asyncio
async def test_rejects_wrong_nonce() -> None:
    transport, token = make_verifier_fixture()
    async with httpx.AsyncClient(transport=transport) as client:
        verifier = OpenAIIDTokenVerifier(
            client,
            issuer="https://auth.openai.test",
            client_id="test-client-id",
        )
        with pytest.raises(OAuthIdentityVerificationError, match="nonce"):
            await verifier.verify(token, expected_nonce="nonce-b")


@pytest.mark.asyncio
async def test_rejects_untrusted_issuer() -> None:
    transport, token = make_verifier_fixture(issuer="https://attacker.test")
    async with httpx.AsyncClient(transport=transport) as client:
        verifier = OpenAIIDTokenVerifier(
            client,
            issuer="https://auth.openai.test",
            client_id="test-client-id",
        )
        with pytest.raises(OAuthIdentityVerificationError):
            await verifier.verify(token)
