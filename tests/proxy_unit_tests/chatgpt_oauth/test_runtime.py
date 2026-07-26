from collections.abc import Mapping

import pytest

from litellm.proxy.chatgpt_oauth.runtime import get_chatgpt_oauth_trust_env


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({}, False),
        ({"CHATGPT_OAUTH_TRUST_ENV": "false"}, False),
        ({"CHATGPT_OAUTH_TRUST_ENV": "FALSE"}, False),
        ({"CHATGPT_OAUTH_TRUST_ENV": "true"}, True),
        ({"CHATGPT_OAUTH_TRUST_ENV": " TRUE "}, True),
    ),
)
def test_get_chatgpt_oauth_trust_env(
    environment: Mapping[str, str],
    expected: bool,
) -> None:
    assert get_chatgpt_oauth_trust_env(environment) is expected


def test_get_chatgpt_oauth_trust_env_rejects_invalid_value() -> None:
    with pytest.raises(
        ValueError,
        match="CHATGPT_OAUTH_TRUST_ENV must be either true or false",
    ):
        get_chatgpt_oauth_trust_env({"CHATGPT_OAUTH_TRUST_ENV": "enabled"})
