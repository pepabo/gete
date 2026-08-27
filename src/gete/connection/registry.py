"""Connection definitions, built from the catalog and gete.yaml.

Gemini Enterprise hands over one token per authorization. Some services issue
more than one kind of token from a single authorization, so a token sitting
under the expected key is not proof that it belongs to the declared service.
Only tokens that look like the declared service's are accepted. Anything else
is refused rather than sent to a host it was never meant for.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import unquote, urlsplit

from gete.catalog import catalog_connections
from gete.errors import DeclarationError, RetiredConnection, UnknownConnection
from gete.schema import validate_document

# A JWT has three dot-separated segments (two dots) and its base64url header
# starts with "eyJ". Google ID tokens and service account tokens are JWTs;
# none of them may be sent to an external service.
_JWT_DOTS = 2
_JWT_HEADER_PREFIX = "eyJ"

# Google access tokens. A connection that does not declare this prefix must
# never accept one, even when no other connection in the registry claims it;
# otherwise a prefixless service accepts Google tokens by elimination.
GOOGLE_ACCESS_TOKEN_PREFIX = "ya29."

# Stands for the root of a service that differs per installation, wherever the
# connection's URLs are written around it. A definition meant to be shared
# cannot spell a tenant's host, and a stand-in host would be a name a stranger
# could register - and then a user's token would be sent there - so the root is
# left open until the installation names it.
BASE_URL = "{base_url}"


def _rooted(url: str | None, base_url: str | None) -> str | None:
    """The URL with the installation's root put in, or left open without one."""
    if url is None or base_url is None:
        return url
    if BASE_URL in base_url:
        # It would survive the substitution, and the connection would keep
        # asking for a root that is already set.
        raise DeclarationError(
            f"base_url must not contain {BASE_URL}: it is the value that fills it"
        )
    return url.replace(BASE_URL, base_url.rstrip("/"))


def missing_base_url(connection_id: str) -> str:
    """Why an open root is refused, worded once for everywhere it is refused."""
    return (
        f"{connection_id} has no base_url; its URLs are written around the "
        "root of the service, which differs per installation. Set "
        f"connections.{connection_id}.base_url in gete.yaml"
    )


def looks_like_jwt(token: str) -> bool:
    """True for anything shaped like a JWT; none belongs at an external service."""
    return token.count(".") >= _JWT_DOTS or token.startswith(_JWT_HEADER_PREFIX)


@dataclass(frozen=True)
class OAuth:
    """How users authorize the connection.

    scopes maps a scope to the explanation shown on the consent screen; every
    agent that declares the connection gets them. optional_scopes is the menu
    an agent may select from on top of that, in the same shape; none of them
    reach a token unless the agent declares them.
    scope_parameter is the query parameter that carries the user scopes; Slack
    uses user_scope because scope means the app's own permissions there.
    authorization_query, when set, is used verbatim as the authorization URL's
    query string, and Gemini Enterprise appends client_id and redirect_uri.
    Both URLs may be written around {base_url} for a service whose root moves
    with the installation; they are not addresses until it is set.
    pkce asks Gemini Enterprise to carry a code challenge through the flow;
    authorization servers that require one refuse the exchange without it.
    """

    authorization_url: str
    token_url: str
    scopes: Mapping[str, str]
    optional_scopes: Mapping[str, str] = field(default_factory=dict)
    scope_parameter: str = "scope"
    authorization_query: Mapping[str, str] | None = None
    pkce: bool = False

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], base_url: str | None = None
    ) -> "OAuth":
        return cls(
            authorization_url=str(_rooted(data["authorization_url"], base_url)),
            token_url=str(_rooted(data["token_url"], base_url)),
            scopes=dict(data["scopes"]),
            optional_scopes=dict(data.get("optional_scopes", {})),
            scope_parameter=data.get("scope_parameter", "scope"),
            pkce=bool(data.get("pkce", False)),
            authorization_query=(
                dict(data["authorization_query"])
                if "authorization_query" in data
                else None
            ),
        )


def _stays_below(path: str, prefix: str) -> bool:
    """Whether the request path stays below the prefix however a server reads it.

    A dot segment or an encoded separator - percent-encoded any number of
    times - is refused rather than resolved: resolving would have to guess
    how many times the server decodes.
    """
    for segment in path.split("/"):
        decoded = unquote(segment)
        while decoded != segment:
            segment, decoded = decoded, unquote(decoded)
        if segment in (".", "..") or "/" in segment or "\\" in segment:
            return False
    if not prefix.endswith("/"):
        # The schema requires the trailing slash; a definition that dodged it
        # must not widen the ceiling to /prefix-and-more.
        prefix += "/"
    return path.startswith("/" + prefix)


@dataclass(frozen=True)
class Examples:
    """Token shapes, not real tokens. The catalog checks accepts_token against them."""

    accepts: tuple[str, ...] = ()
    rejects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Connection:
    """An external service and the rules for sending a user's token to it."""

    id: str
    display_name: str
    oauth: OAuth
    hosts: frozenset[str] = frozenset()
    # Hosts a download may be redirected to, declared one by one; the token
    # never travels to them. Empty means downloads stay on hosts.
    redirect_hosts: frozenset[str] = frozenset()
    token_prefixes: tuple[str, ...] = ()
    # Prefixes declared by every other connection in the registry, filled in by
    # Registry. A connection without prefixes of its own accepts a token only
    # if none of these match, so a bare from_mapping() connection judges more
    # leniently than the same connection taken from a Registry.
    foreign_prefixes: tuple[str, ...] = ()
    base_url: str | None = None
    docs: str | None = None
    oauth_client: str | None = None
    mcp_url: str | None = None
    retired: str | None = None
    # What a person has to do before anyone can authorize. Prose, because the
    # parts that matter - what the consent screen grants, what cannot be
    # undone - are not steps and no check can see them.
    setup: str | None = None
    # Text shown to users asked to authorize again; None means the default.
    reauthorization: str | None = None
    verified: Mapping[str, str] = field(default_factory=dict)
    examples: Examples = Examples()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Connection":
        """Build from a mapping with the connection schema's shape, id included."""
        hosts = set(data.get("hosts", ()))
        base_url = data.get("base_url")
        if base_url:
            host = urlsplit(base_url).hostname
            if host:
                hosts.add(host)
        examples = data.get("examples", {})
        return cls(
            id=data["id"],
            display_name=data["display_name"],
            oauth=OAuth.from_mapping(data["oauth"], base_url),
            hosts=frozenset(hosts),
            redirect_hosts=frozenset(data.get("redirect_hosts", ())),
            token_prefixes=tuple(data.get("token_prefixes", ())),
            base_url=base_url,
            docs=data.get("docs"),
            oauth_client=data.get("oauth_client"),
            mcp_url=_rooted(data.get("mcp", {}).get("url"), base_url),
            retired=data.get("retired"),
            setup=data.get("setup"),
            reauthorization=data.get("messages", {}).get("reauthorization"),
            verified=dict(data.get("verified", {})),
            examples=Examples(
                accepts=tuple(examples.get("accepts", ())),
                rejects=tuple(examples.get("rejects", ())),
            ),
        )

    @property
    def needs_base_url(self) -> bool:
        """True while a URL still holds {base_url}: the root is not set.

        Read off the URLs rather than declared, so a definition cannot claim to
        need a root it never uses, or use one without saying so. Until the
        installation sets base_url, none of these are addresses and hosts is
        whatever the definition could name without knowing the root.
        """
        return any(
            url is not None and BASE_URL in url
            for url in (
                self.oauth.authorization_url,
                self.oauth.token_url,
                self.mcp_url,
            )
        )

    @property
    def secret_prefix(self) -> str:
        """Prefix of the Secret Manager secrets that hold the OAuth client."""
        return self.oauth_client or f"ge-oauth-{self.id}"

    @property
    def client_id_secret(self) -> str:
        return f"{self.secret_prefix}-client-id"

    @property
    def client_secret_secret(self) -> str:
        return f"{self.secret_prefix}-client-secret"

    def accepts_token(self, token: str) -> bool:
        """Whether the token may be treated as this connection's.

        A declared prefix decides on its own. The JWT heuristic is a guess
        about shape, and Google issues ya29.c.<payload> access tokens that
        carry two dots; the guess must not overrule a prefix the catalog
        vouches for. Without prefixes, the shape is all there is to go on.
        """
        if not token:
            return False
        if self.token_prefixes:
            return token.startswith(self.token_prefixes)
        if looks_like_jwt(token) or token.startswith(GOOGLE_ACCESS_TOKEN_PREFIX):
            return False
        # The service does not announce itself. All that can be said is that
        # the token is not some other service's.
        return not any(token.startswith(prefix) for prefix in self.foreign_prefixes)

    def allows(self, url: str) -> bool:
        """Whether a token may be attached to a request for this URL.

        Only https, and only an exact host match. Prefix or suffix matching
        would accept names such as slack.com.example.com. An entry written as
        host/path/ admits only requests below that path: some platforms serve
        unrelated APIs from one host, and the path is where they part.
        """
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname is None:
            return False
        for entry in self.hosts:
            host, slash, prefix = entry.partition("/")
            if parsed.hostname != host:
                continue
            if not slash or _stays_below(parsed.path, prefix):
                return True
        return False

    def allows_redirect(self, url: str) -> bool:
        """Whether a download may follow a redirect here; the token may not.

        Exact matching against the declared lists only: a named host is no
        safer than an address literal, so nothing is accepted for merely
        looking like a public name.
        """
        parsed = urlsplit(url)
        return self.allows(url) or (
            parsed.scheme == "https" and parsed.hostname in self.redirect_hosts
        )

    def reauthorization_message(self) -> str:
        """Text for the user when the token is missing or unusable.

        It reaches end users, so an installation declares it in their
        language under messages.reauthorization.
        """
        if self.reauthorization is not None:
            return self.reauthorization
        return (
            f"The {self.display_name} authorization could not be confirmed. "
            f"Approve {self.display_name} in Gemini Enterprise and try again."
        )


class Registry:
    """All connections an installation knows about: the catalog plus gete.yaml."""

    def __init__(
        self,
        connections: Iterable[Connection],
        documents: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        by_id = {connection.id: connection for connection in connections}
        # The merged mappings the connections were built from, when known.
        # resolve() embeds them so the runtime can rebuild the same registry.
        self._documents = {key: dict(value) for key, value in (documents or {}).items()}
        self._connections = {
            connection_id: replace(
                connection,
                foreign_prefixes=tuple(
                    prefix
                    for other in by_id.values()
                    if other.id != connection_id
                    for prefix in other.token_prefixes
                ),
            )
            for connection_id, connection in by_id.items()
        }

    @classmethod
    def from_catalog(
        cls,
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        source: str = "gete.yaml",
    ) -> "Registry":
        """Build from the bundled catalog, with gete.yaml's connections applied.

        A catalog id may be given partially: top-level keys replace the
        catalog's, and a top-level mapping such as oauth is merged one level
        deep so its URLs need not be restated. Anything below that, such as
        oauth.scopes, is replaced as a whole; merging it would make a scope
        impossible to remove. Any other id must be a complete definition.
        """
        entries = catalog_connections()
        for connection_id, override in (overrides or {}).items():
            where = f"{source}: connections.{connection_id}"
            if override.get("id", connection_id) != connection_id:
                raise DeclarationError(
                    f"{where}: id {override['id']!r} differs from key {connection_id!r}"
                )
            if connection_id in entries:
                merged = _merge(entries[connection_id], override)
            else:
                merged = {"id": connection_id, **override}
            # The merged document still carries every key the override had, so
            # a misspelled key fails here without a separate partial check.
            validate_document("connection", merged, source=where)
            entries[connection_id] = merged
        return cls.from_documents(entries)

    @classmethod
    def from_documents(cls, documents: Mapping[str, Mapping[str, Any]]) -> "Registry":
        """Build from complete connection mappings keyed by id, as resolve() embeds."""
        return cls(
            (Connection.from_mapping(entry) for entry in documents.values()), documents
        )

    def documents(self) -> dict[str, dict[str, Any]]:
        """The mappings this registry was built from, keyed by id."""
        return {key: dict(value) for key, value in self._documents.items()}

    def ids(self) -> list[str]:
        return sorted(self._connections)

    def all(self, *, include_retired: bool = False) -> Iterator[Connection]:
        for connection_id in self.ids():
            connection = self._connections[connection_id]
            if include_retired or connection.retired is None:
                yield connection

    def get(self, connection_id: str, *, include_retired: bool = False) -> Connection:
        """Look up by id. Unknown ids and retired connections raise with the reason."""
        try:
            connection = self._connections[connection_id]
        except KeyError:
            raise UnknownConnection(
                f"unknown connection {connection_id!r}; known: {', '.join(self.ids())}"
            ) from None
        if connection.retired is not None and not include_retired:
            raise RetiredConnection(
                f"connection {connection_id!r} is retired: {connection.retired}"
            )
        return connection


def _merge(
    base: Mapping[str, Any], override: Mapping[str, Any], *, depth: int = 1
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if (
            depth > 0
            and isinstance(value, Mapping)
            and isinstance(merged.get(key), Mapping)
        ):
            merged[key] = _merge(merged[key], value, depth=depth - 1)
        else:
            merged[key] = value
    return merged
