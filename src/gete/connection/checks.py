"""Rules beyond the schema, shared by the catalog tests and validate."""

from collections.abc import Iterable
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


def elimination_problems(
    connection_ids: Iterable[str], registry: Registry
) -> list[str]:
    """Describe connections that cannot be held together, or return an empty list.

    A connection without token prefixes accepts whatever no other connection
    claims. Two of them are indistinguishable: a token issued by either
    authorization passes as the other's. Only the connections handed to the
    same agent can be confused that way, so the pairing is what is refused,
    not the second prefixless connection an installation declares. The
    registry holds every connection gete ships as well, and declaring a
    service of your own must not depend on which of those announce themselves.
    """
    prefixless = sorted(
        connection_id
        for connection_id in set(connection_ids)
        if not registry.get(connection_id, include_retired=True).token_prefixes
    )
    if len(prefixless) < 2:
        return []
    return [
        f"{', '.join(prefixless)} declare no token_prefixes; only one of an "
        "agent's connections may accept tokens by elimination, or a token from "
        "one of them would be accepted as another's"
    ]


def connection_problems(connection: Connection, registry: Registry) -> list[str]:
    """Describe what is wrong with the connection, or return an empty list."""
    # A connection taken from the registry knows the other connections'
    # prefixes; one built with from_mapping() alone does not, and would pass
    # checks it should fail.
    if connection.id in registry.ids():
        connection = registry.get(connection.id, include_retired=True)
    problems: list[str] = []
    # A connection whose root is not set yet names no hosts, because the root
    # is where they come from. That is the state a shared definition is written
    # in; validate refuses it where an agent picks it up, not here.
    if not connection.hosts and not connection.needs_base_url:
        problems.append("hosts: at least one host is required")
    for host in sorted(connection.hosts):
        if host in TOO_BROAD_HOSTS:
            problems.append(
                f"hosts: {host} is a whole platform domain, list the API host"
            )
    for other in registry.all(include_retired=True):
        if other.id == connection.id:
            continue
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
    if connection.mcp_url is not None and not connection.needs_base_url:
        mcp_host = urlsplit(connection.mcp_url).hostname
        if mcp_host not in connection.hosts:
            problems.append(f"mcp.url: host {mcp_host} is not in hosts")
    return problems
