"""The per-tool-call context: what a tool can learn about who is calling."""

import asyncio
from types import SimpleNamespace

from gete.request_context import (
    ToolCall,
    caller_fingerprint,
    clear_tool_call,
    current_tool_call,
    set_tool_call,
)


def teardown_function() -> None:
    clear_tool_call()


def test_same_user_gives_the_same_fingerprint() -> None:
    assert caller_fingerprint(SimpleNamespace(user_id="u1")) == caller_fingerprint(
        SimpleNamespace(user_id="u1")
    )


def test_different_users_give_different_fingerprints() -> None:
    assert caller_fingerprint(SimpleNamespace(user_id="u1")) != caller_fingerprint(
        SimpleNamespace(user_id="u2")
    )


def test_fingerprint_does_not_contain_the_identifier() -> None:
    assert "someone@example.com" not in caller_fingerprint(
        SimpleNamespace(user_id="someone@example.com")
    )


def test_fingerprint_is_unknown_without_a_user() -> None:
    assert caller_fingerprint(SimpleNamespace()) == "unknown"
    assert caller_fingerprint() == "unknown"


def test_fingerprint_uses_the_current_tool_call_when_no_context_is_given() -> None:
    set_tool_call(ToolCall(SimpleNamespace(user_id="u1")))
    assert caller_fingerprint() == caller_fingerprint(SimpleNamespace(user_id="u1"))


def test_explicit_tool_context_wins_over_the_current_call() -> None:
    set_tool_call(ToolCall(SimpleNamespace(user_id="u1")))
    assert caller_fingerprint(SimpleNamespace(user_id="u2")) == caller_fingerprint(
        SimpleNamespace(user_id="u2")
    )


def test_nothing_is_current_outside_a_tool_call() -> None:
    assert current_tool_call() is None


async def test_tool_calls_do_not_leak_between_tasks() -> None:
    """ADK runs each tool call in its own task; what one sets must not reach another."""

    async def call(user: str) -> str:
        set_tool_call(ToolCall(SimpleNamespace(user_id=user)))
        await asyncio.sleep(0)
        return caller_fingerprint()

    a, b = await asyncio.gather(call("a"), call("b"))
    assert a != b
    assert current_tool_call() is None
