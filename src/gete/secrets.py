"""validate --check-secrets: the secrets a deployment reads exist and have a version.

Agent Engine fails at deployment with "could not access one or more secrets"
when a secret has no enabled version. It reads like a permission problem and
usually is not.
"""

from gete.connection import Registry
from gete.declaration import Problem, Project
from gete.gcp import GcpApi, GcpError

SECRET_MANAGER = "https://secretmanager.googleapis.com/v1"


def secrets_needed(project: Project) -> dict[str, list[str]]:
    """Secret name to the agents needing it: secret_env and OAuth clients."""
    registry = Registry.from_catalog(project.connection_overrides)
    shared = project.data.get("shared_credentials", {})
    needed: dict[str, list[str]] = {}
    for agent in project.agents:
        names = list(agent.secret_env.values())
        # Delivery wires a shared credential's token_secret into secret_env,
        # so the deployment reads it like any other entry here.
        for name in agent.shared_credentials:
            secret = shared.get(name, {}).get("token_secret")
            if secret:
                names.append(str(secret))
        if agent.data.get("registration"):
            for connection_id in agent.connections:
                connection = registry.get(connection_id)
                names.extend(
                    (connection.client_id_secret, connection.client_secret_secret)
                )
        for name in names:
            needed.setdefault(name, []).append(agent.name)
    return needed


def check_secrets(project: Project, gcp: GcpApi) -> list[Problem]:
    """One problem per secret that is missing or has no enabled version."""
    gcp_project = str(project.data["project"])
    problems: list[Problem] = []
    for name, agents in sorted(secrets_needed(project).items()):
        source = f"secret {name} (used by {', '.join(agents)})"
        url = f"{SECRET_MANAGER}/projects/{gcp_project}/secrets/{name}/versions"
        try:
            versions = gcp.list_all(url, "versions")
        except GcpError as error:
            problems.append(
                Problem(source, f"not found or not readable: {error.message}")
            )
            continue
        if not any(version.get("state") == "ENABLED" for version in versions):
            problems.append(
                Problem(source, "has no enabled version; Agent Engine cannot read it")
            )
    return problems
