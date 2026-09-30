"""Rules beyond the schema, shared by the catalog tests and validate."""

from collections.abc import Iterable
from urllib.parse import urlsplit

from gete.connection.registry import (
    GOOGLE_ACCESS_TOKEN_PREFIX,
    Connection,
    OAuth,
    Registry,
)

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
    take that one place - unless one issuer stands behind two of the
    connections judging tokens by issuer, which is the same confusion by
    another route. An anonymous connection is one of those: it takes a JWT
    naming its own authorization server as readily as a declaring one does,
    so a shared issuer confuses the two whichever of them declared.

    Prefixes that overlap are that confusion among the connections that do
    announce themselves: a token carrying the shared prefix passes as
    either's. A service that is run in more than one place - hosted by its
    vendor, and again on an installation's own server - issues the same
    shapes from each, so the registry may hold both under ids of their own.
    What is refused is one agent holding the two.
    """
    connections = [
        registry.get(connection_id, include_retired=True)
        for connection_id in sorted(set(connection_ids))
    ]
    problems: list[str] = []
    anonymous = [
        connection
        for connection in connections
        if not connection.token_prefixes and connection.token_format is None
    ]
    if len(anonymous) >= 2:
        problems.append(
            f"{', '.join(connection.id for connection in anonymous)} declare "
            "neither token_prefixes nor tokens.format; only one of an agent's "
            "connections may accept tokens by elimination, or a token from one "
            "of them would be accepted as another's"
        )
    # A prefix decides on its own and before any issuer does, so a connection
    # held to one judges no token by who issued it, and shares an issuer with
    # nothing.
    by_issuer = [
        connection
        for connection in connections
        if connection.token_format is not None or not connection.token_prefixes
    ]
    for index, connection in enumerate(by_issuer):
        for other in by_issuer[index + 1 :]:
            if connection.token_format is None and other.token_format is None:
                # Already refused above, by everything the pair fails to say
                # about itself rather than by the one issuer they happen to
                # share; naming it here would say it twice.
                continue
            shared = connection.issuer_hosts & other.issuer_hosts
            if shared:
                problems.append(
                    f"{connection.id}, {other.id} both accept tokens issued by "
                    f"{', '.join(sorted(shared))}; a token from one of them "
                    "would be accepted as the other's"
                )
    # A declared format decides before any prefix is read, so a connection
    # held to one takes no token for its prefix.
    by_prefix = [
        connection for connection in connections if connection.token_format is None
    ]
    for index, connection in enumerate(by_prefix):
        for other in by_prefix[index + 1 :]:
            overlapping = _overlapping_prefixes(connection, other)
            if overlapping:
                problems.append(
                    f"{connection.id}, {other.id} both accept tokens starting "
                    f"with {', '.join(map(repr, overlapping))}; a token from "
                    "one of them would be accepted as the other's"
                )
    return problems


def _overlapping_prefixes(connection: Connection, other: Connection) -> list[str]:
    """The prefixes a token can carry and be accepted by both connections.

    Where one prefix extends the other, the longer one is named: both
    connections accept exactly the tokens that start with it.
    """
    overlapping: set[str] = set()
    for prefix in connection.token_prefixes:
        for theirs in other.token_prefixes:
            if prefix.startswith(theirs):
                overlapping.add(prefix)
            elif theirs.startswith(prefix):
                overlapping.add(theirs)
    return sorted(overlapping)


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
    if connection.token_format is not None and connection.token_prefixes:
        # The format decides on its own, so the prefixes beside it are never
        # read - and a reader would have to know that to see which of the two
        # rules the connection is actually held to.
        problems.append(
            f"tokens: format {connection.token_format} decides on its own; the "
            "token_prefixes declared beside it are never read"
        )
    if connection.oauth is not None:
        problems.extend(_oauth_problems(connection.oauth))
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


def _oauth_problems(oauth: OAuth) -> list[str]:
    problems: list[str] = []
    for scope in sorted(oauth.optional_scopes):
        if scope in oauth.scopes:
            problems.append(
                f"oauth.optional_scopes: {scope} is already a default scope"
            )
    if oauth.optional_scopes and oauth.authorization_query:
        # The verbatim query is the whole authorization URL; a selection
        # would be accepted and then never reach the consent screen.
        problems.append(
            "oauth.optional_scopes: the menu cannot be offered next to a "
            "verbatim authorization_query, which fixes the scopes"
        )
    return problems


def app_problems(connection: Connection) -> list[str]:
    """What an app connection still lacks before a token can be issued.

    A catalog entry leaves the App open, the way it leaves a moving root
    open, so the gap is refused where an agent picks the connection up.
    """
    app = connection.app
    if app is None:
        return []
    where = f"connections.{connection.id}.app"
    problems = [
        f"{connection.id} has no app.{name}; set {where}.{name} in gete.yaml"
        for name, value in (
            ("app_id", app.app_id),
            ("private_key_secret", app.private_key_secret),
            ("repositories", app.repositories),
            # Without it a token carries everything the installation was
            # granted, which is exactly what the ceiling is there to stop.
            ("permissions", app.permissions),
        )
        if not value
    ]
    owners = sorted({repository.partition("/")[0] for repository in app.repositories})
    if len(owners) > 1:
        problems.append(
            f"{connection.id}: app.repositories belong to {', '.join(owners)}; a "
            "token is issued by one installation, and an installation belongs "
            "to one account"
        )
    return problems
