"""Holding an mcp: block's argument rules against the tools it offers."""

from typing import Any

from gete.mcp import declaration_problems

URL = "https://mcp.example.com/mcp"


def block(**overrides: Any) -> dict[str, Any]:
    return {"url": URL, "allow": ["search", "fetch"], **overrides}


def test_a_block_that_fixes_an_allowed_tools_argument_has_no_problems() -> None:
    sound = block(params={"search": {"scope": {"value": "team-handbook"}}})
    assert declaration_problems(sound) == []


def test_params_must_name_a_tool_in_allow() -> None:
    found = declaration_problems(block(params={"serach": {"q": {"value": 1}}}))
    assert found == ["params: 'serach' is not one of this block's allow"]


def test_params_needs_an_allow_list_to_hold_its_names_against() -> None:
    """With every tool offered, a misspelt name in params would fix nothing,
    and the tool it meant would be offered unfixed with nobody told."""
    found = declaration_problems(
        {"url": URL, "params": {"search": {"q": {"value": 1}}}}
    )
    assert found == [
        "params: names tools out of allow, and this block has no allow; "
        "a misspelt name would leave the tool it meant as the server lists it"
    ]
