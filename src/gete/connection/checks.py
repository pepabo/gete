"""Rules beyond the schema, shared by the catalog tests and validate."""

from urllib.parse import urlsplit

from gete.connection.registry import GOOGLE_ACCESS_TOKEN_PREFIX, Connection, Registry

# Platform domains under which unrelated parties host services. Hosts are
# matched exactly, so listing one of these is almost certainly a mistake
# made in the belief that subdomains would match.
TOO_BROAD_HOSTS = frozenset(
    {
        "googleapis.com",
        # Serves storage, compute, and oauth2 next to the Workspace APIs.
        "www.googleapis.com",
        "google.com",
        "amazonaws.com",
        "cloudfront.net",
        "run.app",
        "cloudfunctions.net",
        "azurewebsites.net",
        "herokuapp.com",
        "github.io",
        "vercel.app",
        "netlify.app",
    }
)

# Shapes every connection must refuse, whatever it declares.
_JWT_EXAMPLE = "eyJhbGciOiJSUzI1NiJ9.e30.sig"
_GOOGLE_ACCESS_TOKEN_EXAMPLE = GOOGLE_ACCESS_TOKEN_PREFIX + "a0AfH6SMB"


def connection_problems(connection: Connection, registry: Registry) -> list[str]:
    """Describe what is wrong with the connection, or return an empty list."""
    # A connection taken from the registry knows the other connections'
    # prefixes; one built with from_mapping() alone does not, and would pass
    # checks it should fail.
    if connection.id in registry.ids():
        connection = registry.get(connection.id, include_retired=True)
    problems: list[str] = []
    if not connection.hosts:
        problems.append("hosts: at least one host is required")
    for host in sorted(connection.hosts):
        if host in TOO_BROAD_HOSTS:
            problems.append(
                f"hosts: {host} is a whole platform domain, list the API host"
            )
    for other in registry.all(include_retired=True):
        if other.id == connection.id:
            continue
        if not connection.token_prefixes and not other.token_prefixes:
            # Two services that do not announce themselves would accept each
            # other's tokens; elimination can only tell one such service apart.
            problems.append(
                f"token_prefixes: empty, like {other.id}; only one connection may "
                "accept tokens by elimination"
            )
        for prefix in connection.token_prefixes:
            for theirs in other.token_prefixes:
                if prefix.startswith(theirs) or theirs.startswith(prefix):
                    problems.append(
                        f"token_prefixes: {prefix!r} overlaps {theirs!r} ({other.id})"
                    )
    for token in connection.examples.accepts:
        if not connection.accepts_token(token):
            problems.append(f"examples.accepts: {token!r} is not accepted")
    for token in connection.examples.rejects:
        if connection.accepts_token(token):
            problems.append(f"examples.rejects: {token!r} is accepted")
    if connection.accepts_token(_JWT_EXAMPLE):
        problems.append("a JWT-shaped token is accepted")
    claims_google = any(
        prefix.startswith(GOOGLE_ACCESS_TOKEN_PREFIX)
        for prefix in connection.token_prefixes
    )
    if not claims_google and connection.accepts_token(_GOOGLE_ACCESS_TOKEN_EXAMPLE):
        problems.append("a Google access token is accepted")
    if connection.mcp_url is not None:
        mcp_host = urlsplit(connection.mcp_url).hostname
        if mcp_host not in connection.hosts:
            problems.append(f"mcp.url: host {mcp_host} is not in hosts")
    return problems
