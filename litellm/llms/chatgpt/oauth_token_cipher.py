import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class MissingOAuthEncryptionKeyError(RuntimeError):
    pass


class ChatGPTOAuthTokenCipher:
    _PREFIX = "chatgpt-oauth:v1:"

    def __init__(self, root_key: str) -> None:
        if not root_key:
            raise MissingOAuthEncryptionKeyError("LITELLM_SALT_KEY must be set for ChatGPT database OAuth")
        self._encryption_key = hmac.new(
            root_key.encode("utf-8"),
            b"litellm:chatgpt-oauth:encryption:v1",
            hashlib.sha256,
        ).digest()
        self._fingerprint_key = hmac.new(
            root_key.encode("utf-8"),
            b"litellm:chatgpt-oauth:fingerprint:v1",
            hashlib.sha256,
        ).digest()

    @classmethod
    def from_environment(cls) -> "ChatGPTOAuthTokenCipher":
        root_key = os.getenv("LITELLM_SALT_KEY")
        if not root_key:
            raise MissingOAuthEncryptionKeyError("LITELLM_SALT_KEY must be set for ChatGPT database OAuth")
        return cls(root_key)

    def encrypt(self, credential_id: str, field: str, plaintext: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self._aad(credential_id, field),
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{self._PREFIX}{encoded}"

    def decrypt(self, credential_id: str, field: str, encrypted: str) -> str:
        if not encrypted.startswith(self._PREFIX):
            raise ValueError("Unable to decrypt ChatGPT OAuth value")
        try:
            payload = base64.b64decode(
                encrypted[len(self._PREFIX) :],
                altchars=b"-_",
                validate=True,
            )
            if len(payload) < 29:
                raise ValueError("Invalid ciphertext length")
            plaintext = AESGCM(self._encryption_key).decrypt(
                payload[:12],
                payload[12:],
                self._aad(credential_id, field),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise ValueError("Unable to decrypt ChatGPT OAuth value") from error

    def email_fingerprint(self, email: str) -> str:
        normalized_email = email.strip().casefold().encode("utf-8")
        return hmac.new(
            self._fingerprint_key,
            normalized_email,
            hashlib.sha256,
        ).hexdigest()

    def _aad(self, credential_id: str, field: str) -> bytes:
        return f"chatgpt-oauth|v1|{credential_id}|{field}".encode("utf-8")
