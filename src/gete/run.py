"""gete run: a local conversation; tokens arrive the way Agent Engine sends them."""

import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from gete.connection import authorization_id
from gete.declaration import Agent, Project, resolve
from gete.errors import DeclarationError
from gete.runtime import build_document

TOKEN_ENV_PREFIX = "GETE_TOKEN_"


def initial_state(
    agent_name: str, connections: Iterable[str], environ: Mapping[str, str]
) -> dict[str, str]:
    """Tokens from GETE_TOKEN_<CONNECTION>, keyed by the agent's authorization names.

    The runtime then reads them exactly as it reads the state Gemini Enterprise
    fills in, so a tool that works here works there under the same key.
    """
    state: dict[str, str] = {}
    for connection_id in connections:
        variable = TOKEN_ENV_PREFIX + connection_id.upper().replace("-", "_")
        token = environ.get(variable)
        if token:
            state[authorization_id(agent_name, connection_id)] = token
    return state


def find_agent(project: Project, name: str) -> Agent:
    for agent in project.agents:
        if agent.name == name:
            return agent
    known = ", ".join(agent.name for agent in project.agents) or "(none)"
    raise DeclarationError(f"no agent named {name!r}; known: {known}")


def build_local_agent(project: Project, name: str) -> Any:
    """The LlmAgent the archive would carry, built in memory from the declaration."""
    agent = find_agent(project, name)
    return build_document(resolve(project, agent), agent.directory)


async def converse(
    agent: Any,
    state: Mapping[str, str],
    prompts: Iterable[str],
    say: Callable[[str], None],
    *,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Run each prompt through ADK's Runner in one session and say the final answers."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    service = InMemorySessionService()  # type: ignore[no-untyped-call]
    session = await service.create_session(
        app_name="gete", user_id="local", state=dict(state)
    )
    runner = Runner(agent=agent, app_name="gete", session_service=service)
    for prompt in prompts:
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(
            user_id="local", session_id=session.id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                say("".join(part.text or "" for part in event.content.parts))
