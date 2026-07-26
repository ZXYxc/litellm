import pytest

from litellm.llms.chatgpt.oauth_token_cipher import (
    ChatGPTOAuthTokenCipher,
    MissingOAuthEncryptionKeyError,
)


def test_requires_explicit_litellm_salt_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_SALT_KEY", raising=False)

    with pytest.raises(MissingOAuthEncryptionKeyError, match="LITELLM_SALT_KEY"):
        ChatGPTOAuthTokenCipher.from_environment()


def test_encrypts_and_decrypts_with_credential_bound_aad() -> None:
    cipher = ChatGPTOAuthTokenCipher("stable-test-key")

    encrypted = cipher.encrypt("credential-a", "token-record", "secret-token-value")

    assert encrypted.startswith("chatgpt-oauth:v1:")
    assert "secret-token-value" not in encrypted
    assert cipher.decrypt("credential-a", "token-record", encrypted) == "secret-token-value"


def test_rejects_ciphertext_moved_to_another_credential() -> None:
    cipher = ChatGPTOAuthTokenCipher("stable-test-key")
    encrypted = cipher.encrypt("credential-a", "token-record", "secret-token-value")

    with pytest.raises(ValueError, match="decrypt"):
        cipher.decrypt("credential-b", "token-record", encrypted)


def test_rejects_ciphertext_moved_to_another_field() -> None:
    cipher = ChatGPTOAuthTokenCipher("stable-test-key")
    encrypted = cipher.encrypt("credential-a", "token-record", "secret-token-value")

    with pytest.raises(ValueError, match="decrypt"):
        cipher.decrypt("credential-a", "email", encrypted)


def test_repeated_encryption_uses_unique_nonces() -> None:
    cipher = ChatGPTOAuthTokenCipher("stable-test-key")

    first = cipher.encrypt("credential-a", "token-record", "same-value")
    second = cipher.encrypt("credential-a", "token-record", "same-value")

    assert first != second


def test_email_fingerprint_is_normalized_and_keyed() -> None:
    cipher = ChatGPTOAuthTokenCipher("stable-test-key")

    first = cipher.email_fingerprint(" Alice@Example.COM ")
    second = cipher.email_fingerprint("alice@example.com")

    assert first == second
    assert "alice@example.com" not in first
    assert len(first) == 64
