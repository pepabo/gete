"""Typed access to agent.yaml: both spellings of a connection entry."""

from pathlib import Path

from gete.declaration import Agent

WRITE_SHEETS = "https://www.googleapis.com/auth/spreadsheets"


def agent_with(connections: list[object]) -> Agent:
    return Agent(
        directory=Path("agents/mail-triage"),
        data={"name": "mail-triage", "connections": connections},
    )


def test_connections_return_the_id_whichever_form_the_entry_uses() -> None:
    agent = agent_with(["freee", {"id": "google", "scopes": [WRITE_SHEETS]}])
    assert agent.connections == ("freee", "google")


def test_scope_selections_come_only_from_entries_that_select() -> None:
    agent = agent_with(["freee", {"id": "google", "scopes": [WRITE_SHEETS]}])
    assert agent.scope_selections == {"google": (WRITE_SHEETS,)}


def test_an_agent_without_connections_selects_nothing() -> None:
    agent = Agent(directory=Path("agents/mail-triage"), data={"name": "mail-triage"})
    assert agent.connections == ()
    assert agent.scope_selections == {}
