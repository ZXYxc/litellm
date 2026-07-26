from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from .common_utils import CHATGPT_AUTH_BASE, CHATGPT_CLIENT_ID
from .oauth_http_client import VerifiedChatGPTIdentity


class OAuthIdentityVerificationError(ValueError):
    pass


class OpenAIIDTokenVerifier:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        issuer: str = CHATGPT_AUTH_BASE,
        client_id: str = CHATGPT_CLIENT_ID,
    ) -> None:
        self._http_client = http_client
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._jwks: jwt.PyJWKSet | None = None

    async def verify(
        self,
        id_token: str,
        expected_nonce: str | None = None,
    ) -> VerifiedChatGPTIdentity:
        try:
            key = await self._signing_key(id_token)
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, ValueError) as error:
            raise OAuthIdentityVerificationError("Unable to verify ChatGPT identity token") from error

        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise OAuthIdentityVerificationError("ChatGPT identity token nonce does not match")
        email = claims.get("email")
        subject = claims.get("sub")
        auth_claims = claims.get("https://api.openai.com/auth")
        account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
        if not all(isinstance(value, str) and value for value in (email, subject, account_id)):
            raise OAuthIdentityVerificationError("ChatGPT identity token is missing identity claims")
        return VerifiedChatGPTIdentity(
            email=email,
            account_id=account_id,
            subject=subject,
        )

    async def _signing_key(self, id_token: str) -> Any:
        header = jwt.get_unverified_header(id_token)
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise OAuthIdentityVerificationError("ChatGPT identity token has no key id")
        jwks = await self._get_jwks()
        for key in jwks.keys:
            if key.key_id == key_id and key.algorithm_name == "RS256":
                return key.key
        self._jwks = None
        refreshed_jwks = await self._get_jwks()
        for key in refreshed_jwks.keys:
            if key.key_id == key_id and key.algorithm_name == "RS256":
                return key.key
        raise OAuthIdentityVerificationError("No trusted key matches the identity token")

    async def _get_jwks(self) -> jwt.PyJWKSet:
        if self._jwks is not None:
            return self._jwks
        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        discovery_response = await self._http_client.get(discovery_url)
        discovery_response.raise_for_status()
        discovery = self._dict_payload(discovery_response)
        if discovery.get("issuer") != self._issuer:
            raise OAuthIdentityVerificationError("OpenID issuer does not match")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not self._is_trusted_jwks_uri(jwks_uri):
            raise OAuthIdentityVerificationError("OpenID key URL is not trusted")
        jwks_response = await self._http_client.get(jwks_uri)
        jwks_response.raise_for_status()
        self._jwks = jwt.PyJWKSet.from_dict(self._dict_payload(jwks_response))
        return self._jwks

    def _is_trusted_jwks_uri(self, jwks_uri: str) -> bool:
        issuer_url = urlparse(self._issuer)
        key_url = urlparse(jwks_uri)
        return key_url.scheme == "https" and key_url.netloc == issuer_url.netloc

    @staticmethod
    def _dict_payload(response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise OAuthIdentityVerificationError("OpenID response is invalid")
        return payload
