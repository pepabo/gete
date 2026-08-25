"""What runs inside Agent Engine: the agent built from agent.resolved.yaml."""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gete.connection.runtime import authorization_id
from gete.declaration import Agent, Resolved, load_resolved, resolved_from_document
from gete.policies import applicable, compose_instruction
from gete.redact import RedactRules
from gete.runtime.callbacks import bind_tool_call, redact_results, safe_tool_error
from gete.runtime.tools import build_tools
from gete.shared_credentials import SHARED_CREDENTIALS


def build(path: Path) -> Any:
    """Build the ADK LlmAgent from a resolved declaration on disk.

    Tests use this; it does not touch Vertex AI. The returned agent has the
    policies' text in front of its instruction, its tools, and the callbacks
    that carry the user's token to the tools and redact what they return.
    """
    return _build(load_resolved(path))


def build_document(document: Mapping[str, Any], directory: Path) -> Any:
    """Build from a resolved document in memory, with paths relative to directory."""
    return _build(resolved_from_document(document, directory))


def _build(resolved: Resolved) -> Any:
    from google.adk.agents import LlmAgent

    agent = resolved.agent
    policies = applicable(resolved.policies, resolved.data)
    rules = RedactRules.from_policies(policies)
    authorizations = {
        connection_id: authorization_id(agent.name, connection_id)
        for connection_id in agent.connections
    }
    return LlmAgent(
        # ADK wants an identifier. The display name lives in the registration.
        name=agent.name.replace("-", "_"),
        model=agent.data["model"],
        description=agent.data["description"],
        instruction=compose_instruction(
            resolved.policies, resolved.data, _with_shared_credential_rules(agent)
        ),
        tools=build_tools(
            agent,
            policies,
            authorizations=authorizations,
            registry=resolved.registry,
            confirmation=resolved.confirmation,
        ),
        before_tool_callback=bind_tool_call(authorizations, resolved.registry, rules),
        after_tool_callback=redact_results(rules),
        on_tool_error_callback=safe_tool_error(rules),
    )


def _with_shared_credential_rules(agent: Agent) -> str:
    """The declared credentials' rules in front of the agent's own text.

    The policies' prefixes still come first; the agent's text stays last,
    where it is the easiest for later instructions to override. The rules
    ship with the credential, not with the agent - a rule each agent had to
    copy would be missing exactly where it was forgotten.
    """
    parts = [SHARED_CREDENTIALS[name].instruction for name in agent.shared_credentials]
    parts.append(agent.instruction_text())
    return "\n\n".join(parts)


def app(path: Path) -> Any:
    """Wrap the agent for Agent Engine, which looks for an object named app.

    Importing AdkApp initializes Vertex AI, so it happens here and not at
    module import; tests never reach this function.
    """
    from vertexai.agent_engines import AdkApp

    # httpx logs every request URL, query string included, at INFO. The query
    # is the user's work and has no place in the deployment's logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return AdkApp(agent=build(path))
