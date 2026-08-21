"""Getting the user's token out of the state Gemini Enterprise hands over."""

import logging
from types import SimpleNamespace

import pytest

from gete.connection import Registry
from gete.connection.runtime import (
    authorization_id,
    caller_token,
    describe_state,
    describe_token,
    token_for,
)
from gete.request_context import ToolCall, clear_tool_call, set_tool_call

CATALOG = Registry.from_catalog()
GITHUB = CATALOG.get("github")
FREEE = CATALOG.get("freee")
GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
FREEE_TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
JWT = "eyJhbGciOiJSUzI1NiJ9.e30.sig"


def teardown_function() -> None:
    clear_tool_call()


class FakeState:
    """ADK's State is not a dict; it has to_dict."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def to_dict(self) -> dict[str, object]:
        return dict(self._values)


def test_authorization_id_is_agent_then_connection() -> None:
    """One authorization serves one agent, so two agents on freee need two names."""
    assert authorization_id("finance-agent", "freee") == "finance-agent-freee"


def test_authorization_id_must_be_a_label_of_at_most_63() -> None:
    with pytest.raises(ValueError, match="63"):
        authorization_id("a" * 60, "freee")
    with pytest.raises(ValueError):
        authorization_id("Finance", "freee")


def test_token_for_matches_the_key_exactly() -> None:
    """A prefix match would hand out google_calendar when google was asked for."""
    state = {"agent-google": "ya29.x", "agent-google-calendar": "ya29.y"}
    assert token_for(state, "agent-google") == "ya29.x"
    assert token_for(state, "agent-goo") is None


def test_token_for_ignores_empty_and_non_string_values() -> None:
    assert token_for({"k": ""}, "k") is None
    assert token_for({"k": 123}, "k") is None
    assert token_for(FakeState({"k": "v"}), "k") == "v"


def test_describe_state_shows_keys_types_and_lengths_but_never_values() -> None:
    described = describe_state(FakeState({"agent-freee": FREEE_TOKEN, "turn": 3}))
    assert described == [
        {"key": "agent-freee", "type": "str", "length": len(FREEE_TOKEN)},
        {"key": "turn", "type": "int", "length": None},
    ]
    assert FREEE_TOKEN not in str(described)


def test_describe_token_names_the_prefix_not_the_value() -> None:
    assert describe_token(GITHUB, GITHUB_TOKEN) == "starts with gho_"
    assert describe_token(GITHUB, "ya29.x") == "matches none of the declared shapes"
    assert GITHUB_TOKEN not in describe_token(GITHUB, GITHUB_TOKEN)


def test_caller_token_takes_the_state_from_the_current_call() -> None:
    context = SimpleNamespace(state={"mail-triage-github": GITHUB_TOKEN})
    set_tool_call(ToolCall(context, {"github": "mail-triage-github"}, registry=CATALOG))
    assert caller_token("github") == GITHUB_TOKEN


def test_caller_token_uses_the_agent_specific_key_not_the_connection_name() -> None:
    context = SimpleNamespace(state={"github": GITHUB_TOKEN})
    set_tool_call(ToolCall(context, {"github": "mail-triage-github"}, registry=CATALOG))
    assert caller_token("github") is None


def test_caller_token_outside_a_call_uses_the_connection_name() -> None:
    assert caller_token(GITHUB, {"github": GITHUB_TOKEN}) == GITHUB_TOKEN


def test_caller_token_refuses_the_wrong_shape_and_does_not_fall_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The authorization arrived; what is wrong is its configuration, not the user."""
    state = {"github": "ya29.not-github", "freee": JWT}
    with caplog.at_level(logging.WARNING):
        assert caller_token(GITHUB, state) is None
        assert caller_token(FREEE, state) is None
    assert "ya29.not-github" not in caplog.text
    assert JWT not in caplog.text


def test_caller_token_warns_with_the_state_shape_when_nothing_arrived(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert caller_token(GITHUB, {"other": "x"}) is None
    assert "other" in caplog.text
    assert "github" in caplog.text


def test_caller_token_accepts_prefixless_tokens_by_elimination() -> None:
    assert caller_token(FREEE, {"freee": FREEE_TOKEN}) == FREEE_TOKEN
    assert caller_token(FREEE, {"freee": GITHUB_TOKEN}) is None
