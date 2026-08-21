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
from urllib.parse import urlsplit

from gete.catalog import catalog_connections
from gete.errors import DeclarationError, RetiredConnection, UnknownConnection
from gete.schema import validate_document

# A JWT has three dot-separated segments and its base64url header starts with
# "eyJ". Google ID tokens and service account tokens are JWTs; none of them
# may be sent to an external service.
_JWT_SEGMENTS = 2
_JWT_HEADER_PREFIX = "eyJ"

# Google access tokens. A connection that does not declare this prefix must
# never accept one, even when no other connection in the registry claims it;
# otherwise a prefixless service accepts Google tokens by elimination.
GOOGLE_ACCESS_TOKEN_PREFIX = "ya29."


def looks_like_jwt(token: str) -> bool:
    """True for anything shaped like a JWT; none belongs at an external service."""
    return token.count(".") >= _JWT_SEGMENTS or token.startswith(_JWT_HEADER_PREFIX)


@dataclass(frozen=True)
class OAuth:
    """How users authorize the connection.

    scopes maps a scope to the explanation shown on the consent screen.
    scope_parameter is the query parameter that carries the user scopes; Slack
    uses user_scope because scope means the app's own permissions there.
    authorization_query, when set, is used verbatim as the authorization URL's
    query string, and Gemini Enterprise appends client_id and redirect_uri.
    """

    authorization_url: str
    token_url: str
    scopes: Mapping[str, str]
    scope_parameter: str = "scope"
    authorization_query: Mapping[str, str] | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OAuth":
        return cls(
            authorization_url=data["authorization_url"],
            token_url=data["token_url"],
            scopes=dict(data["scopes"]),
            scope_parameter=data.get("scope_parameter", "scope"),
            authorization_query=(
                dict(data["authorization_query"])
                if "authorization_query" in data
                else None
            ),
        )


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
    token_prefixes: tuple[str, ...] = ()
    # Prefixes declared by every other connection in the registry. A connection
    # without prefixes of its own accepts a token only if none of these match.
    foreign_prefixes: tuple[str, ...] = ()
    base_url: str | None = None
    docs: str | None = None
    oauth_client: str | None = None
    mcp_url: str | None = None
    retired: str | None = None
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
            oauth=OAuth.from_mapping(data["oauth"]),
            hosts=frozenset(hosts),
            token_prefixes=tuple(data.get("token_prefixes", ())),
            base_url=base_url,
            docs=data.get("docs"),
            oauth_client=data.get("oauth_client"),
            mcp_url=data.get("mcp", {}).get("url"),
            retired=data.get("retired"),
            verified=dict(data.get("verified", {})),
            examples=Examples(
                accepts=tuple(examples.get("accepts", ())),
                rejects=tuple(examples.get("rejects", ())),
            ),
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

        Google's own access tokens start with ya29. and contain a single dot,
        so they pass the JWT check and are then decided by prefix like any other.
        """
        if not token or looks_like_jwt(token):
            return False
        if self.token_prefixes:
            return token.startswith(self.token_prefixes)
        if token.startswith(GOOGLE_ACCESS_TOKEN_PREFIX):
            return False
        # The service does not announce itself. All that can be said is that
        # the token is not some other service's.
        return not any(token.startswith(prefix) for prefix in self.foreign_prefixes)

    def allows(self, url: str) -> bool:
        """Whether a token may be attached to a request for this URL.

        Only https, and only an exact host match. Prefix or suffix matching
        would accept names such as slack.com.example.com.
        """
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in self.hosts

    def reauthorization_message(self) -> str:
        """Text for the user when the token is missing or has the wrong shape."""
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
