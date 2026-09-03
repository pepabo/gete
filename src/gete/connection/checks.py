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

# Tokens every connection must refuse, whatever it declares: these are the
# shapes of the Google credentials that must never reach an external service.
# Claims: {"iss": "https://accounts.google.com"}, as in an ID token.
_GOOGLE_ISSUED_JWT_EXAMPLES = (
    "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FjY291bnRzLmdvb2dsZS5jb20ifQ.sig",
    # Claims: {"iss": "agent@project.iam.gserviceaccount.com"}, as in a
    # service account token.
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJpc3MiOiJhZ2VudEBwcm9qZWN0LmlhbS5nc2VydmljZWFjY291bnQuY29tIn0.sig",
)
_GOOGLE_ACCESS_TOKEN_EXAMPLE = GOOGLE_ACCESS_TOKEN_PREFIX + "a0AfH6SMB"


def elimination_problems(
    connection_ids: Iterable[str], registry: Registry
) -> list[str]:
    """Describe connections that cannot be held together, or return an empty list.

    A connection that announces itself neither by a token prefix nor by a
    declared token format accepts whatever no other connection claims. Two of
    them are indistinguishable: a token issued by either authorization passes
    as the other's. Only the connections handed to the same agent can be
    confused that way, so the pairing is what is refused, not the second such
    connection an installation declares. The registry holds every connection
    gete ships as well, and declaring a service of your own must not depend on
    which of those announce themselves.

    A declared format announces the service in every token, so it does not
    take that one place - unless two of them name one issuer, which is the
    same confusion by another route.
    """
    connections = [
        registry.get(connection_id, include_retired=True)
        for connection_id in sorted(set(connection_ids))
    ]
    problems: list[str] = []
    anonymous = [
        connection.id
        for connection in connections
        if not connection.token_prefixes and not connection.token_format
    ]
    if len(anonymous) >= 2:
        problems.append(
            f"{', '.join(anonymous)} declare neither token_prefixes nor "
            "tokens.format; only one of an agent's connections may accept "
            "tokens by elimination, or a token from one of them would be "
            "accepted as another's"
        )
    declared = [connection for connection in connections if connection.token_format]
    for index, connection in enumerate(declared):
        for other in declared[index + 1 :]:
            shared = connection.issuer_hosts & other.issuer_hosts
            if shared:
                problems.append(
                    f"{connection.id}, {other.id} declare tokens.format and are "
                    f"issued by {', '.join(sorted(shared))}; a token from one of "
                    "them would be accepted as the other's"
                )
    return problems


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
    for entry in sorted(connection.hosts):
        # A bare entry admits every path on its host, so a scoped entry for
        # the same host never applies - it reads as a restriction it does not
        # make. base_url puts its host on the list bare, so setting one on a
        # scoped host silently widens the ceiling the same way.
        host, slash, _ = entry.partition("/")
        if not slash or host not in connection.hosts:
            continue
        if connection.base_url and urlsplit(connection.base_url).hostname == host:
            problems.append(
                f"hosts: {entry} never applies; base_url puts {host} on the "
                "list bare, and a bare entry admits every path"
            )
        else:
            problems.append(
                f"hosts: {entry} never applies; the bare {host} entry admits every path"
            )
    if connection.token_format and connection.token_prefixes:
        # The format decides on its own, so the prefixes beside it are never
        # read - and a reader would have to know that to see which of the two
        # rules the connection is actually held to.
        problems.append(
            f"tokens: format {connection.token_format} decides on its own; the "
            "token_prefixes declared beside it are never read"
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
    for scope in sorted(connection.oauth.optional_scopes):
        if scope in connection.oauth.scopes:
            problems.append(
                f"oauth.optional_scopes: {scope} is already a default scope"
            )
    if connection.oauth.optional_scopes and connection.oauth.authorization_query:
        # The verbatim query is the whole authorization URL; a selection
        # would be accepted and then never reach the consent screen.
        problems.append(
            "oauth.optional_scopes: the menu cannot be offered next to a "
            "verbatim authorization_query, which fixes the scopes"
        )
    for token in connection.examples.accepts:
        if not connection.accepts_token(token):
            problems.append(f"examples.accepts: {token!r} is not accepted")
    for token in connection.examples.rejects:
        if connection.accepts_token(token):
            problems.append(f"examples.rejects: {token!r} is accepted")
    for example in _GOOGLE_ISSUED_JWT_EXAMPLES:
        if connection.accepts_token(example):
            problems.append("a Google-issued JWT is accepted")
    claims_google = any(
        prefix.startswith(GOOGLE_ACCESS_TOKEN_PREFIX)
        for prefix in connection.token_prefixes
    )
    if not claims_google and connection.accepts_token(_GOOGLE_ACCESS_TOKEN_EXAMPLE):
        problems.append("a Google access token is accepted")
    # The MCP server is spoken to with the user's token, so its URL must sit
    # where hosts lets the token go - path scoping included.
    if (
        connection.mcp_url is not None
        and not connection.needs_base_url
        and not connection.allows(connection.mcp_url)
    ):
        problems.append(f"mcp.url: {connection.mcp_url} is not covered by hosts")
    return problems
