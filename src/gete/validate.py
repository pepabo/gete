"""Rules applied after the schemas: what the declarations promise each other."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gete._yaml import read_yaml
from gete.connection import Registry
from gete.connection.checks import connection_problems, elimination_problems
from gete.declaration import Agent, Problem, Project
from gete.errors import DeclarationError, GeteError
from gete.policies import duplicate_policy_names
from gete.schema import problems as schema_problems
from gete.shared_credentials import SHARED_CREDENTIALS

# Authorization ids are <name>-<connection> and must fit a DNS-style label.
MAX_AUTHORIZATION_ID_LENGTH = 63

# The agent's service account is <name>-ae, and GCP wants an account id of 6
# to 30 characters. Both ends are reached by names the agent schema accepts.
SERVICE_ACCOUNT_SUFFIX = "-ae"
MIN_SERVICE_ACCOUNT_ID_LENGTH = 6
MAX_SERVICE_ACCOUNT_ID_LENGTH = 30

# Agent Engine sets these itself and rejects a spec that repeats them.
RESERVED_ENV_NAMES = frozenset({"GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"})


def validate_project(project: Project) -> list[Problem]:
    """Return every problem in the project, or an empty list when it is sound."""
    found: list[Problem] = list(project.problems)
    registry = _registry(project, found)
    found.extend(_policy_problems(project))
    for agent in project.agents:
        found.extend(_agent_problems(project, agent, registry))
    return found


def _registry(project: Project, found: list[Problem]) -> Registry:
    try:
        registry = Registry.from_catalog(
            project.connection_overrides, source=project.display(project.path)
        )
    except DeclarationError as error:
        found.append(Problem(project.display(project.path), str(error)))
        return Registry.from_catalog()
    for connection_id in project.connection_overrides:
        connection = registry.get(connection_id, include_retired=True)
        found.extend(
            Problem(
                project.display(project.path), f"connections.{connection_id}: {message}"
            )
            for message in connection_problems(connection, registry)
        )
    return registry


def _policy_problems(project: Project) -> list[Problem]:
    found: list[Problem] = []
    entries: list[Any] = []
    for path in project.policy_files:
        source = project.display(path)
        if not path.is_file():
            found.append(Problem(source, "policy file does not exist"))
            continue
        document = read_yaml(path)
        messages = schema_problems("policy", document)
        found.extend(Problem(source, message) for message in messages)
        if not messages:
            entries.extend(document)
    for name in duplicate_policy_names(entries):
        found.append(
            Problem(
                project.display(project.path), f"policies: name {name!r} is used twice"
            )
        )
    return found


def _agent_problems(
    project: Project, agent: Agent, registry: Registry
) -> list[Problem]:
    source = project.display(agent.path)
    found: list[str] = []
    if agent.name != agent.directory.name:
        found.append(
            f"name {agent.name!r} differs from directory {agent.directory.name!r}; "
            "the module, service account, and archive are named after the directory"
        )
    account = f"{agent.name}{SERVICE_ACCOUNT_SUFFIX}"
    if not (
        MIN_SERVICE_ACCOUNT_ID_LENGTH <= len(account) <= MAX_SERVICE_ACCOUNT_ID_LENGTH
    ):
        found.append(
            f"name: service account {account!r} is not between "
            f"{MIN_SERVICE_ACCOUNT_ID_LENGTH} and {MAX_SERVICE_ACCOUNT_ID_LENGTH} "
            "characters; Terraform would be refused at apply time"
        )
    known: set[str] = set()
    for connection_id in agent.connections:
        try:
            connection = registry.get(connection_id)
        except GeteError as error:
            found.append(f"connections: {error}")
            continue
        known.add(connection_id)
        if connection.needs_base_url:
            # The definition left the root open because it moves with the
            # installation. Nothing it declares is an address until then: not
            # the hosts a token may go to, and not the URL users consent at.
            found.append(
                f"connections: {connection_id} has no base_url; its URLs are "
                "written around the root of the service, which differs per "
                f"installation. Set connections.{connection_id}.base_url in "
                "gete.yaml"
            )
        authorization_id = f"{agent.name}-{connection_id}"
        if len(authorization_id) > MAX_AUTHORIZATION_ID_LENGTH:
            found.append(
                f"connections: authorization id {authorization_id!r} is longer than "
                f"{MAX_AUTHORIZATION_ID_LENGTH} characters"
            )
    found.extend(
        f"connections: {message}" for message in elimination_problems(known, registry)
    )
    declared_shared: Mapping[str, Any] = project.data.get("shared_credentials", {})
    for name in agent.shared_credentials:
        credential = SHARED_CREDENTIALS.get(name)
        if credential is None:
            # The schema already refused the name; this only guards a schema
            # that ran ahead of the registry.
            continue
        if not declared_shared.get(name, {}).get("token_secret"):
            found.append(
                f"shared_credentials: {name} has no token_secret under "
                "shared_credentials in gete.yaml; the deployment would run "
                "without a credential"
            )
        for block, values in (("env", agent.env), ("secret_env", agent.secret_env)):
            if credential.token_env in values:
                # Delivered from gete.yaml; an agent pointing the variable at
                # a value of its own choosing would swap the credential unseen.
                found.append(
                    f"runtime.agent_engine.{block}: {credential.token_env} is "
                    f"delivered from shared_credentials.{name}.token_secret in "
                    "gete.yaml; the agent does not set it"
                )
    instruction = agent.instruction_path
    if instruction is not None and not instruction.is_file():
        found.append(f"instruction: {project.display(instruction)} does not exist")
    source_dir = agent.source
    if source_dir is not None and not source_dir.is_dir():
        found.append(f"source: {project.display(source_dir)} is not a directory")
    requirements = agent.requirements
    if requirements is not None and not requirements.is_file():
        found.append(f"requirements: {project.display(requirements)} does not exist")
    for index, tool in enumerate(agent.tools):
        found.extend(
            f"tools[{index}]: {message}"
            for message in _tool_problems(project, agent, tool, registry, known)
        )
    # Empty env values are fine: they document that the knob exists, and
    # delivery drops them before Agent Engine, which refuses them, sees
    # anything.
    for name in agent.env:
        if name in RESERVED_ENV_NAMES:
            found.append(
                f"runtime.agent_engine.env: {name} is reserved by Agent Engine"
            )
    # Empty secret names get no such pass: nothing drops them, and the
    # Terraform module addresses the named secret, so apply fails on "".
    for name, secret in agent.secret_env.items():
        if secret == "":
            found.append(
                f"runtime.agent_engine.secret_env: {name} names no secret; "
                "rejected at apply"
            )
    return [Problem(source, message) for message in found]


def _tool_problems(
    project: Project,
    agent: Agent,
    tool: Mapping[str, Any],
    registry: Registry,
    known: set[str],
) -> list[str]:
    if "builtin" in tool:
        name: str = tool["builtin"]
        if not _builtin_exists(name):
            return [f"builtin: google.adk.tools has no {name!r}"]
        return []
    if "python" in tool:
        spec = tool["python"]
        ref: str = spec if isinstance(spec, str) else spec["ref"]
        return _python_ref_problems(project, agent, ref)
    if "mcp" in tool:
        return _mcp_problems(tool["mcp"], registry, known)
    return []


def _builtin_exists(name: str) -> bool:
    import google.adk.tools as builtin_tools

    return hasattr(builtin_tools, name)


def _python_ref_problems(project: Project, agent: Agent, ref: str) -> list[str]:
    """Locate the module below source without importing it.

    Importing needs the deployment's dependencies, which the machine running
    validate may not have. The import check is a separate, slower step.
    """
    source = agent.source
    if source is None:
        return [
            f"python: {ref!r} needs source to be set; the module is packaged from there"
        ]
    if not source.is_dir():
        return []
    module = ref.split(":", 1)[0]
    relative = Path(*module.split("."))
    candidates = (
        source / relative.with_suffix(".py"),
        source / relative / "__init__.py",
    )
    if not any(candidate.is_file() for candidate in candidates):
        return [f"python: module {module!r} not found below {project.display(source)}"]
    return []


def _mcp_problems(
    mcp: Mapping[str, Any], registry: Registry, known: set[str]
) -> list[str]:
    connection_id: str | None = mcp.get("connection")
    if connection_id is None:
        return []
    if connection_id not in known:
        return [
            f"mcp: connection {connection_id!r} is not in this agent's connections, "
            "so no token would be available for it"
        ]
    url: str = mcp["url"]
    connection = registry.get(connection_id)
    if not connection.allows(url):
        hosts = ", ".join(sorted(connection.hosts))
        return [
            f"mcp: {url} is not a host of connection {connection_id!r} ({hosts}); "
            "the token would leave the service"
        ]
    return []
