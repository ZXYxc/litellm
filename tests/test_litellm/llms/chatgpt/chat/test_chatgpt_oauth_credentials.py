from unittest.mock import MagicMock, patch

from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig


@patch("litellm.llms.chatgpt.chat.transformation.Authenticator")
def test_database_credential_uses_resolved_token_without_file_authentication(
    mock_authenticator_class: MagicMock,
) -> None:
    mock_authenticator = MagicMock()
    mock_authenticator_class.return_value = mock_authenticator
    config = ChatGPTConfig()

    _, token, _ = config._get_openai_compatible_provider_info(
        model="gpt-5",
        api_base=None,
        api_key="database-access-token",
        custom_llm_provider="chatgpt",
    )
    headers = config.validate_environment(
        headers={
            "Authorization": "Bearer attacker-token",
            "ChatGPT-Account-Id": "attacker-account",
        },
        model="gpt-5",
        messages=[],
        optional_params={},
        litellm_params={
            "chatgpt_oauth_credential_id": "credential-a",
            "chatgpt_oauth_resolved": True,
            "chatgpt_account_id": "database-account-id",
        },
        api_key="database-access-token",
    )

    assert token == "database-access-token"
    assert headers["Authorization"] == "Bearer database-access-token"
    assert headers["ChatGPT-Account-Id"] == "database-account-id"
    mock_authenticator.get_access_token.assert_not_called()
    mock_authenticator.get_account_id.assert_not_called()
