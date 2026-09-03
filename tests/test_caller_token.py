"""Getting the user's token out of the state Gemini Enterprise hands over."""

import logging
from types import SimpleNamespace

import pytest

from gete.connection import Connection, Registry
from gete.connection.runtime import (
    authorization_id,
    caller_token,
    describe_state,
    describe_token,
    token_for,
    usable_token,
)
from gete.request_context import ToolCall, clear_tool_call, set_tool_call

CATALOG = Registry.from_catalog()
GITHUB = CATALOG.get("github")
FREEE = CATALOG.get("freee")
GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
FREEE_TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
# Claims: {"iss": "https://accounts.google.com"}, as in an ID token.
GOOGLE_ISSUED_JWT = (
    "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FjY291bnRzLmdvb2dsZS5jb20ifQ.sig"
)


# Claims: {"exp": 0}. A JWT that names no issuer says as little about its
# origin as an opaque token.
JWT_WITHOUT_ISSUER = "eyJhbGciOiJFZERTQSJ9.eyJleHAiOjB9.sig"
# A connection whose tokens are JWTs of its own making, as an installation
# declares one whose provider issues them.
DECLARED = Registry(
    [
        Connection.from_mapping(
            {
                "id": "declared",
                "display_name": "Declared",
                "hosts": ["api.example.com"],
                "tokens": {"format": "jwt"},
                "oauth": {
                    "authorization_url": "https://api.example.com/authorize",
                    "token_url": "https://api.example.com/token",
                    "scopes": {},
                },
            }
        )
    ]
).get("declared")


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


def test_token_for_looks_under_the_temp_prefix_when_the_bare_key_is_empty() -> None:
    """Agent Engine may forward the token as ephemeral state, under temp:."""
    assert (
        token_for({"temp:agent-github": GITHUB_TOKEN}, "agent-github") == GITHUB_TOKEN
    )
    assert token_for(FakeState({"temp:k": "v"}), "k") == "v"


def test_token_for_prefers_the_bare_key_over_its_temp_twin() -> None:
    state = {"agent-github": "bare", "temp:agent-github": "ephemeral"}
    assert token_for(state, "agent-github") == "bare"


def test_token_for_holds_the_temp_twin_to_the_same_rules() -> None:
    """An empty bare key falls through; an empty or non-string twin is still nothing."""
    assert token_for({"k": "", "temp:k": "v"}, "k") == "v"
    assert token_for({"temp:k": ""}, "k") is None
    assert token_for({"temp:k": 123}, "k") is None
    # Only the whole key is matched under the prefix as well.
    assert token_for({"temp:agent-google-calendar": "ya29.y"}, "agent-google") is None


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


def test_describe_token_says_why_a_jwt_was_refused_without_repeating_it() -> None:
    """A prefixless connection declares no shapes, so the shape message would
    leave an issuer refusal looking like a mystery."""
    assert (
        describe_token(FREEE, GOOGLE_ISSUED_JWT)
        == "a JWT whose issuer does not name this service"
    )
    assert (
        describe_token(FREEE, "eyJhbGciOiJSUzI1NiJ9.x.sig")
        == "a JWT whose claims cannot be read"
    )
    # A declared prefix is the whole judgment; the JWT is described no further.
    assert (
        describe_token(GITHUB, GOOGLE_ISSUED_JWT)
        == "matches none of the declared shapes"
    )


def test_describe_token_says_a_declared_format_was_not_met() -> None:
    """The connection promised JWTs of its own making. An operator who turned
    the provider's expiring-token setting off sees every token refused, and
    the message has to say which promise the token missed."""
    assert (
        describe_token(DECLARED, FREEE_TOKEN) == "not the jwt this connection declares"
    )
    assert describe_token(DECLARED, JWT_WITHOUT_ISSUER) == "a JWT that names no issuer"
    assert (
        describe_token(DECLARED, GOOGLE_ISSUED_JWT)
        == "a JWT whose issuer does not name this service"
    )
    assert FREEE_TOKEN not in describe_token(DECLARED, FREEE_TOKEN)


def test_usable_token_refuses_what_a_declared_format_rules_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Elimination would have taken the opaque token; the declaration is what
    keeps it from travelling."""
    with caplog.at_level(logging.WARNING):
        assert usable_token(DECLARED, "declared", {"declared": FREEE_TOKEN}) is None
    assert FREEE_TOKEN not in caplog.text


def test_caller_token_takes_the_state_from_the_current_call() -> None:
    context = SimpleNamespace(state={"mail-triage-github": GITHUB_TOKEN})
    set_tool_call(ToolCall(context, {"github": "mail-triage-github"}, registry=CATALOG))
    assert caller_token("github") == GITHUB_TOKEN


def test_caller_token_uses_the_agent_specific_key_not_the_connection_name() -> None:
    context = SimpleNamespace(state={"github": GITHUB_TOKEN})
    set_tool_call(ToolCall(context, {"github": "mail-triage-github"}, registry=CATALOG))
    assert caller_token("github") is None


def test_caller_token_finds_the_agent_specific_key_under_the_temp_prefix() -> None:
    context = SimpleNamespace(state={"temp:mail-triage-github": GITHUB_TOKEN})
    set_tool_call(ToolCall(context, {"github": "mail-triage-github"}, registry=CATALOG))
    assert caller_token("github") == GITHUB_TOKEN


def test_usable_token_finds_the_key_under_the_temp_prefix() -> None:
    """The toolsets decide what to offer through this path."""
    state = FakeState({"temp:mail-triage-github": GITHUB_TOKEN})
    assert usable_token(GITHUB, "mail-triage-github", state) == GITHUB_TOKEN
    assert usable_token(GITHUB, "mail-triage-github", {"temp:other": "x"}) is None


def test_a_wrong_shape_under_the_temp_prefix_is_refused_all_the_same(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The authorization arrived, only as ephemeral state; the check is the same."""
    with caplog.at_level(logging.WARNING):
        assert (
            usable_token(
                GITHUB, "agent-github", {"temp:agent-github": "ya29.not-github"}
            )
            is None
        )
        assert caller_token(GITHUB, {"temp:github": "ya29.not-github"}) is None
    assert caplog.text.count("wrong shape") == 2
    assert "ya29.not-github" not in caplog.text


def test_caller_token_outside_a_call_uses_the_connection_name() -> None:
    assert caller_token(GITHUB, {"github": GITHUB_TOKEN}) == GITHUB_TOKEN


def test_caller_token_refuses_the_wrong_shape_and_does_not_fall_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The authorization arrived; what is wrong is its configuration, not the user."""
    state = {"github": "ya29.not-github", "freee": GOOGLE_ISSUED_JWT}
    with caplog.at_level(logging.WARNING):
        assert caller_token(GITHUB, state) is None
        assert caller_token(FREEE, state) is None
    assert "ya29.not-github" not in caplog.text
    assert GOOGLE_ISSUED_JWT not in caplog.text


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
