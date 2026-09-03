"""Connections: which tokens a connection accepts and where it lets them go."""

import base64
import json
from typing import Any

import pytest

from gete.connection import Connection, Registry
from gete.connection.checks import connection_problems, elimination_problems
from gete.errors import DeclarationError, RetiredConnection, UnknownConnection


def jwt_with(claims: Any) -> str:
    """A JWT-shaped token carrying these claims. The signature is never checked."""

    def encode(part: Any) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.sig"


EXAMPLE: dict[str, Any] = {
    "id": "example",
    "display_name": "Example",
    "hosts": ["api.example.com"],
    "oauth": {
        "authorization_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": {"read": "Read data"},
    },
}


def connection(**patch: Any) -> Connection:
    return Connection.from_mapping({**EXAMPLE, **patch})


@pytest.fixture
def catalog() -> Registry:
    return Registry.from_catalog()


def test_token_is_accepted_only_with_a_declared_prefix(catalog: Registry) -> None:
    github = catalog.get("github")
    assert github.accepts_token("gho_16C7e42F292c6912E7710c838347Ae178B4a")
    assert not github.accepts_token("ya29.a0AfH6SMB")
    assert not github.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")


def test_prefixless_connection_accepts_by_elimination(catalog: Registry) -> None:
    """freee tokens carry no prefix; anything resembling another service is refused."""
    freee = catalog.get("freee")
    assert freee.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")
    assert not freee.accepts_token("ya29.a0AfH6SMB")
    assert not freee.accepts_token("gho_16C7e42F292c6912E7710c838347Ae178B4a")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a.b.c",
        "eyJ-only",
        "eyJhbGciOiJSUzI1NiJ9.x.sig",  # claims are not base64url JSON
        "eyJhbGciOiJSUzI1NiJ9.Ingi.sig",  # claims decode to "x", not a mapping
        "eyJhbGciOiJSUzI1NiJ9.e30.sig.extra",  # four segments are no JWT
    ],
)
def test_unreadable_jwt_shapes_and_empty_tokens_are_never_accepted(
    catalog: Registry, token: str
) -> None:
    """A JWT whose claims cannot be read is refused rather than guessed about."""
    for entry in catalog.all(include_retired=True):
        assert not entry.accepts_token(token), entry.id


def test_claims_nested_too_deep_to_read_are_refused_rather_than_raising(
    catalog: Registry,
) -> None:
    """A payload of nothing but brackets must not blow up the judgment."""
    brackets = b"[" * 100_000 + b"]" * 100_000
    deep = base64.urlsafe_b64encode(brackets).rstrip(b"=").decode()
    token = f"eyJhbGciOiJSUzI1NiJ9.{deep}.sig"
    for entry in catalog.all(include_retired=True):
        assert not entry.accepts_token(token), entry.id


def test_google_issued_jwts_are_never_accepted(catalog: Registry) -> None:
    """ID tokens and service account tokens always name a Google issuer, and
    none of them may be sent to an external service."""
    for entry in catalog.all(include_retired=True):
        for issuer in (
            "https://accounts.google.com",
            "accounts.google.com",
            "agent@project.iam.gserviceaccount.com",
        ):
            assert not entry.accepts_token(jwt_with({"iss": issuer})), entry.id


def test_a_jwt_naming_no_issuer_is_accepted_by_elimination() -> None:
    """Zendesk issues JWTs whose claims name no issuer. They say as little
    about their origin as an opaque token, so they are judged like one."""
    alone = Registry([connection()]).get("example")
    assert alone.accepts_token(jwt_with({"exp": 0}))


def test_a_jwt_naming_the_connections_own_service_is_accepted() -> None:
    """The issuer of a service's tokens is its authorization server, which
    may live beside the API rather than on it; both count as the service."""
    alone = Registry([connection()]).get("example")
    assert alone.accepts_token(jwt_with({"iss": "https://api.example.com"}))
    assert alone.accepts_token(jwt_with({"iss": "api.example.com"}))
    assert alone.accepts_token(jwt_with({"iss": "https://auth.example.com"}))


def test_a_jwt_naming_the_installation_root_is_accepted() -> None:
    entry = Connection.from_mapping({**ROOTED, "base_url": "https://acme.example.com"})
    assert entry.accepts_token(jwt_with({"iss": "https://acme.example.com"}))


@pytest.mark.parametrize(
    "issuer",
    [
        "https://accounts.google.com",  # Google ID token
        "agent@project.iam.gserviceaccount.com",  # service account token
        "https://idp.example.org",  # some other service's authorization server
        "https://api.example.com.evil.example",  # suffix must not pass as a match
        5,  # RFC 7519 wants a string; anything else names nothing
        None,  # present but null is just as far from a string
        "https://[",  # names no host; must be refused, not raise
    ],
)
def test_a_jwt_naming_any_other_issuer_is_refused(issuer: Any) -> None:
    alone = Registry([connection()]).get("example")
    assert not alone.accepts_token(jwt_with({"iss": issuer}))


def test_prefixless_connection_alone_still_refuses_google_access_tokens() -> None:
    """Elimination must not depend on google being present in the registry."""
    alone = Registry([connection()]).get("example")
    assert not alone.accepts_token("ya29.a0AfH6SMB")
    # ya29.c.<payload> carries two dots but is no JWT; the prefix refuses it
    # before any claims are looked for.
    assert not alone.accepts_token("ya29.c.Ko8BuAT7abcdef")
    assert alone.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")


JWT_TOKENS: dict[str, Any] = {"tokens": {"format": "jwt"}}


def test_a_declared_jwt_format_accepts_a_token_the_service_issued() -> None:
    """The declaration says the tokens are JWTs of this service's own making,
    and the issuer is what says so."""
    entry = Registry([connection(**JWT_TOKENS)]).get("example")
    assert entry.accepts_token(jwt_with({"iss": "https://auth.example.com"}))
    assert entry.accepts_token(jwt_with({"iss": "api.example.com"}))


def test_a_declared_jwt_format_refuses_a_token_that_is_not_a_jwt() -> None:
    """Without the declaration elimination would take it; with it, the
    connection promises a shape and holds itself to it."""
    declared = Registry([connection(**JWT_TOKENS)]).get("example")
    anonymous = Registry([connection()]).get("example")
    opaque = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert anonymous.accepts_token(opaque)
    assert not declared.accepts_token(opaque)


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 0},  # a JWT that names no issuer says nothing about its origin
        {"iss": "https://idp.example.org"},
        {"iss": "https://accounts.google.com"},
        {"iss": None},
    ],
)
def test_a_declared_jwt_format_refuses_a_jwt_from_anywhere_else(claims: Any) -> None:
    entry = Registry([connection(**JWT_TOKENS)]).get("example")
    assert not entry.accepts_token(jwt_with(claims))


@pytest.mark.parametrize(
    "token",
    ["", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.x.sig", "ya29.a0AfH6SMB", "gho_16C7e42F29"],
)
def test_a_declared_jwt_format_refuses_what_it_cannot_read(token: str) -> None:
    entry = Registry([connection(**JWT_TOKENS)]).get("example")
    assert not entry.accepts_token(token)


def test_a_declared_jwt_format_is_judged_before_any_foreign_prefix() -> None:
    """Elimination against the others' prefixes is what the declaration replaces."""
    declared = connection(**JWT_TOKENS)
    other = connection(id="other", token_prefixes=["ex_"])
    entry = Registry([declared, other]).get("example")
    assert entry.foreign_prefixes == ("ex_",)
    assert entry.accepts_token(jwt_with({"iss": "https://api.example.com"}))
    assert not entry.accepts_token("something_no_prefix_matches")


def test_a_token_format_this_gete_cannot_judge_accepts_nothing() -> None:
    """A resolved declaration outlives the gete that wrote it. The schema
    refuses an unknown format where it is written; where it is only read,
    falling back to elimination would widen what the declaration narrowed."""
    entry = Registry([connection(tokens={"format": "paseto"})]).get("example")
    assert not entry.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")
    assert not entry.accepts_token(jwt_with({"iss": "https://api.example.com"}))


def test_a_declared_jwt_format_accepts_the_installation_root_as_the_issuer() -> None:
    """The root is where the service lives for a rooted connection, and its
    tokens are issued there."""
    entry = Connection.from_mapping(
        {**ROOTED, **JWT_TOKENS, "base_url": "https://acme.example.com"}
    )
    assert entry.accepts_token(jwt_with({"iss": "https://acme.example.com"}))
    assert not entry.accepts_token(jwt_with({"iss": "https://other.example.com"}))


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://api.example.com/v1/things", True),
        ("https://API.EXAMPLE.COM:443/v1/things", True),
        ("http://api.example.com/v1/things", False),
        ("https://api.example.com.evil.example/v1", False),
        ("https://sub.api.example.com/v1", False),
        ("https://example.com/api", False),
        ("not a url", False),
    ],
)
def test_allows_only_https_to_an_exactly_declared_host(url: str, allowed: bool) -> None:
    assert connection().allows(url) is allowed


def test_base_url_host_is_allowed_too() -> None:
    """GitHub Enterprise changes the host per organization; the base URL carries it."""
    entry = connection(base_url="https://api.github.example.com/")
    assert entry.allows("https://api.github.example.com/repos")
    assert entry.allows("https://api.example.com/v1")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://shared.example.com/api/v1/things", True),
        ("https://shared.example.com/api/", True),
        ("https://shared.example.com/api", False),
        ("https://shared.example.com/apix/v1", False),
        ("https://shared.example.com/other/v1", False),
        ("https://shared.example.com/", False),
        ("https://shared.example.com/api/../other/v1", False),
        ("https://shared.example.com/api/%2e%2e/other/v1", False),
        ("https://shared.example.com/api/%252e%252e/other/v1", False),
        ("https://shared.example.com/api/..%2fother/v1", False),
        ("https://shared.example.com/api/x%5c../other", False),
        ("http://shared.example.com/api/v1", False),
    ],
)
def test_a_path_scoped_host_allows_only_requests_below_the_path(
    url: str, allowed: bool
) -> None:
    """Some platforms serve unrelated APIs from one host; a host/path/ entry
    admits one API without admitting the platform. A dot segment or an encoded
    separator is refused rather than resolved, because resolving would have to
    guess how many times the server decodes."""
    entry = connection(hosts=["shared.example.com/api/"])
    assert entry.allows(url) is allowed


def test_a_plain_hostname_entry_keeps_admitting_every_path() -> None:
    entry = connection(hosts=["api.example.com", "shared.example.com/api/"])
    assert entry.allows("https://api.example.com/anything/at/all")
    assert not entry.allows("https://shared.example.com/anything/at/all")


def test_a_redirect_may_go_below_a_path_scoped_host_but_not_beside_it() -> None:
    entry = connection(
        hosts=["shared.example.com/api/"], redirect_hosts=["cdn.example.com"]
    )
    assert entry.allows_redirect("https://shared.example.com/api/download")
    assert not entry.allows_redirect("https://shared.example.com/other/download")
    assert entry.allows_redirect("https://cdn.example.com/download")


def test_oauth_client_defaults_to_ge_oauth_id() -> None:
    entry = connection()
    assert entry.secret_prefix == "ge-oauth-example"
    assert entry.client_id_secret == "ge-oauth-example-client-id"
    assert entry.client_secret_secret == "ge-oauth-example-client-secret"
    assert (
        connection(oauth_client="shared-client").client_id_secret
        == "shared-client-client-id"
    )


def test_optional_scopes_are_a_menu_next_to_the_defaults() -> None:
    entry = connection(
        oauth={**EXAMPLE["oauth"], "optional_scopes": {"write": "Change data"}}
    )
    assert entry.oauth.scopes == {"read": "Read data"}
    assert entry.oauth.optional_scopes == {"write": "Change data"}


def test_a_connection_without_a_menu_offers_nothing() -> None:
    assert connection().oauth.optional_scopes == {}


def test_an_optional_scope_repeated_in_the_defaults_is_reported() -> None:
    entry = connection(
        oauth={**EXAMPLE["oauth"], "optional_scopes": {"read": "Read data"}}
    )
    assert any(
        "read" in problem for problem in connection_problems(entry, Registry([entry]))
    )


def test_a_menu_next_to_a_verbatim_authorization_query_is_reported() -> None:
    """The query is used verbatim; no selection could ever reach the consent screen."""
    entry = connection(
        oauth={
            **EXAMPLE["oauth"],
            "authorization_query": {"response_type": "code"},
            "optional_scopes": {"write": "Change data"},
        }
    )
    assert any(
        "authorization_query" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_catalog_entry_can_be_overridden_partially() -> None:
    registry = Registry.from_catalog(
        {"github": {"base_url": "https://api.github.example.com"}}
    )
    github = registry.get("github")
    assert "api.github.example.com" in github.hosts
    assert "api.github.com" in github.hosts
    assert github.oauth.token_url == "https://github.com/login/oauth/access_token"


def test_override_can_replace_nested_oauth_values_without_restating_the_rest() -> None:
    registry = Registry.from_catalog(
        {"google": {"oauth": {"scopes": {"openid": "Identify you"}}}}
    )
    google = registry.get("google")
    assert google.oauth.scopes == {"openid": "Identify you"}
    assert (
        google.oauth.authorization_url == "https://accounts.google.com/o/oauth2/v2/auth"
    )


def test_private_connection_must_be_complete() -> None:
    with pytest.raises(DeclarationError, match="internal"):
        Registry.from_catalog({"internal": {"display_name": "Internal API"}})


def test_private_connection_takes_its_id_from_the_key() -> None:
    registry = Registry.from_catalog(
        {"internal": {k: v for k, v in EXAMPLE.items() if k != "id"}}
    )
    assert registry.get("internal").id == "internal"


def test_override_may_not_rename_the_connection() -> None:
    with pytest.raises(DeclarationError, match="id"):
        Registry.from_catalog({"github": {"id": "gh"}})


def test_overrides_are_checked_against_the_schema() -> None:
    with pytest.raises(DeclarationError, match="hots"):
        Registry.from_catalog({"github": {"hots": ["api.github.com"]}})


def test_unknown_connection_names_the_known_ones(catalog: Registry) -> None:
    with pytest.raises(UnknownConnection, match="freee"):
        catalog.get("nope")


def test_retired_connection_explains_why(catalog: Registry) -> None:
    with pytest.raises(RetiredConnection, match="connector"):
        catalog.get("slack")
    assert catalog.get("slack", include_retired=True).retired


def test_overlapping_prefixes_between_connections_are_reported() -> None:
    """Elimination only works if no two services can claim the same token."""
    a = connection(id="a", token_prefixes=["tok_"])
    b = connection(id="b", token_prefixes=["tok_v2_"])
    registry = Registry([a, b])
    assert any(
        "tok_" in problem
        for problem in connection_problems(registry.get("a"), registry)
    )


def test_too_broad_hosts_are_reported() -> None:
    """Exact matching does not excuse listing a whole platform domain."""
    entry = connection(hosts=["googleapis.com"])
    assert any(
        "googleapis.com" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_connection_without_hosts_is_reported() -> None:
    entry = Connection.from_mapping({k: v for k, v in EXAMPLE.items() if k != "hosts"})
    assert any(
        "hosts" in problem for problem in connection_problems(entry, Registry([entry]))
    )


def test_a_path_scoped_entry_next_to_the_bare_host_is_reported() -> None:
    """The bare entry admits every path, so the scoped one reads as a
    restriction it does not make."""
    entry = connection(hosts=["shared.example.com", "shared.example.com/api/"])
    assert any(
        "shared.example.com/api/" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_a_base_url_on_the_scoped_host_is_reported_as_the_source() -> None:
    """base_url puts its host on the list bare; a scoped entry cannot narrow it."""
    entry = connection(
        hosts=["shared.example.com/api/"], base_url="https://shared.example.com"
    )
    assert any(
        "base_url" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_a_path_scoped_entry_without_the_bare_host_is_not_reported() -> None:
    entry = connection(hosts=["api.example.com", "shared.example.com/api/"])
    assert connection_problems(entry, Registry([entry])) == []


def test_mcp_host_must_be_a_declared_host() -> None:
    entry = connection(mcp={"url": "https://mcp.example.com/mcp"})
    assert any(
        "mcp.example.com" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )
    good = connection(
        hosts=["api.example.com", "mcp.example.com"],
        mcp={"url": "https://mcp.example.com/mcp"},
    )
    assert connection_problems(good, Registry([good])) == []


def test_mcp_url_below_a_path_scoped_host_is_a_declared_host() -> None:
    entry = connection(
        hosts=["shared.example.com/api/"],
        mcp={"url": "https://shared.example.com/api/mcp"},
    )
    assert connection_problems(entry, Registry([entry])) == []


def test_mcp_url_beside_the_scoped_path_is_reported() -> None:
    """The MCP server is spoken to with the token; beside the path is off-limits."""
    entry = connection(
        hosts=["shared.example.com/api/"],
        mcp={"url": "https://shared.example.com/mcp"},
    )
    assert any(
        "mcp.url" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_examples_are_checked_against_accepts_token() -> None:
    entry = connection(
        token_prefixes=["ex_"], examples={"accepts": ["other_1"], "rejects": ["ex_1"]}
    )
    problems = connection_problems(entry, Registry([entry]))
    assert any("other_1" in problem for problem in problems)
    assert any("ex_1" in problem for problem in problems)


def test_declared_prefix_wins_over_the_dot_heuristic(catalog: Registry) -> None:
    """Google also issues ya29.c.<payload> tokens; two dots must not make them JWTs."""
    google = catalog.get("google")
    assert google.accepts_token("ya29.c.Ko8BuAT7abcdef")
    assert google.accepts_token("ya29.a0AfH6SMB")


def test_prefixed_connection_still_refuses_jwts_and_foreign_tokens(
    catalog: Registry,
) -> None:
    github = catalog.get("github")
    assert not github.accepts_token("eyJhbGciOiJSUzI1NiJ9.e30.sig")
    assert not github.accepts_token("a.b.c")


def test_two_prefixless_connections_are_reported_as_one_pairing() -> None:
    """Elimination cannot tell two services apart when neither announces itself."""
    freee = connection(id="freee", token_prefixes=[])
    internal = connection(id="internal", token_prefixes=[])
    registry = Registry([freee, internal])
    found = elimination_problems(["internal", "freee"], registry)
    assert [p for p in found if "freee" in p and "internal" in p] == found
    assert len(found) == 1


def test_a_prefixless_connection_is_no_problem_on_its_own() -> None:
    """The registry holds all of them; only what one agent holds can collide."""
    freee = connection(id="freee", token_prefixes=[])
    internal = connection(id="internal", token_prefixes=[])
    registry = Registry([freee, internal])
    assert elimination_problems(["internal"], registry) == []
    assert connection_problems(registry.get("internal"), registry) == []


def test_connections_that_announce_themselves_never_collide(catalog: Registry) -> None:
    """Prefixes are checked against each other; only the prefixless are counted."""
    assert elimination_problems(["freee", "github", "google"], catalog) == []


def test_a_declared_token_format_is_not_an_acceptance_by_elimination() -> None:
    """It takes only tokens its own service issued, so the connection beside
    it keeps every token that announces itself no other way."""
    zendesk = connection(id="zendesk", token_prefixes=[], **JWT_TOKENS)
    internal = connection(
        id="internal",
        token_prefixes=[],
        hosts=["api.internal.example.com"],
        oauth={
            "authorization_url": "https://auth.internal.example.com/authorize",
            "token_url": "https://auth.internal.example.com/token",
            "scopes": {"read": "Read internal data"},
        },
    )
    registry = Registry([zendesk, internal])
    assert elimination_problems(["zendesk", "internal"], registry) == []


def test_a_shared_issuer_confuses_a_declared_format_and_an_anonymous_one() -> None:
    """The anonymous connection takes a JWT naming its own authorization
    server too, so one issuer serving both is the same confusion as two
    declaring connections sharing one - the declaration says nothing the
    other's token does not say as well."""
    declared = connection(id="declared", **JWT_TOKENS)
    anonymous = connection(id="anonymous", hosts=["api.other.example.com"])
    registry = Registry([declared, anonymous])
    token = jwt_with({"iss": "https://auth.example.com"})
    assert registry.get("declared").accepts_token(token)
    assert registry.get("anonymous").accepts_token(token)
    found = elimination_problems(["declared", "anonymous"], registry)
    assert len(found) == 1, found
    assert "auth.example.com" in found[0], found


def test_an_anonymous_pair_sharing_an_issuer_is_reported_once() -> None:
    """Neither can be told from the other by anything at all; naming the
    issuer they share on top of that would say it twice."""
    one = connection(id="one")
    two = connection(id="two", hosts=["api.two.example.com"])
    found = elimination_problems(["one", "two"], Registry([one, two]))
    assert len(found) == 1, found


def test_two_declared_token_formats_from_different_services_never_collide() -> None:
    """Each takes only what its own issuer named; neither reaches the other's."""
    one = connection(id="one", **JWT_TOKENS)
    two = connection(
        id="two",
        hosts=["api.two.example.com"],
        oauth={
            "authorization_url": "https://auth.two.example.com/authorize",
            "token_url": "https://auth.two.example.com/token",
            "scopes": {"read": "Read data"},
        },
        **JWT_TOKENS,
    )
    assert elimination_problems(["one", "two"], Registry([one, two])) == []


def test_two_declared_token_formats_issued_by_one_host_are_reported() -> None:
    """Both would accept a JWT that names the shared issuer, so a token from
    either authorization passes as the other's."""
    api = connection(id="api", **JWT_TOKENS)
    mcp = connection(id="mcp", hosts=["mcp.example.com"], **JWT_TOKENS)
    found = elimination_problems(["api", "mcp"], Registry([api, mcp]))
    assert [p for p in found if "api" in p and "mcp" in p] == found
    assert len(found) == 1


def test_prefixes_next_to_a_declared_token_format_are_reported() -> None:
    """The format decides on its own, so the prefixes would never be read."""
    entry = connection(token_prefixes=["ex_"], **JWT_TOKENS)
    problems = connection_problems(entry, Registry([entry]))
    assert any("tokens" in problem for problem in problems), problems


def test_a_connection_that_accepts_google_issued_jwts_is_reported() -> None:
    """Naming Google's authorization server as one's own host would let ID
    tokens through the issuer match; the checks must catch the declaration."""
    entry = connection(hosts=["accounts.google.com"])
    assert any(
        "Google-issued" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_www_googleapis_is_too_broad() -> None:
    """storage, compute, and oauth2 share www.googleapis.com with Workspace APIs."""
    entry = connection(hosts=["www.googleapis.com"])
    problems = connection_problems(entry, Registry([entry]))
    assert any("www.googleapis.com" in p for p in problems)


def test_connection_checks_use_the_registry_view_of_the_connection() -> None:
    """A bare from_mapping() connection knows no foreign prefixes; the checks must."""
    bare = connection(
        token_prefixes=[], examples={"accepts": ["gho_looks_like_github"]}
    )
    registry = Registry([bare, Registry.from_catalog().get("github")])
    assert any(
        "gho_looks_like_github" in p for p in connection_problems(bare, registry)
    )


def test_github_does_not_accept_classic_personal_access_tokens(
    catalog: Registry,
) -> None:
    """OAuth never issues ghp_ tokens; accepting them widens the door for nothing."""
    assert not catalog.get("github").accepts_token(
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
    )


def test_reauthorization_message_can_be_declared_per_connection() -> None:
    """The prompt reaches end users; an installation writes it in their language."""
    entry = connection(messages={"reauthorization": "LOCALIZED-REAUTHORIZE-PROMPT"})
    assert entry.reauthorization_message() == "LOCALIZED-REAUTHORIZE-PROMPT"


def test_reauthorization_message_defaults_to_english() -> None:
    message = connection().reauthorization_message()
    assert "Example" in message and "Gemini Enterprise" in message


def test_rejected_message_can_be_declared_per_connection() -> None:
    """A refused token is a different situation, so it gets its own text."""
    entry = connection(messages={"rejected": "LOCALIZED-REJECTED-PROMPT"})
    assert entry.rejected_message() == "LOCALIZED-REJECTED-PROMPT"


def test_rejected_message_defaults_to_telling_the_user_the_operator_resets() -> None:
    """Approving again shows no consent screen while a credential is held."""
    message = connection().rejected_message()
    assert "Example" in message
    assert "will not help" in message
    assert "operator" in message
    assert message != connection().reauthorization_message()


ROOTED: dict[str, Any] = {
    "id": "rooted",
    "display_name": "Rooted",
    "hosts": [],
    "token_prefixes": [],
    "oauth": {
        "authorization_url": "{base_url}/oauth/authorizations/new",
        "token_url": "{base_url}/oauth/tokens",
        "scopes": {},
    },
    "mcp": {"url": "{base_url}/mcp"},
}


def test_the_installation_root_fills_every_url_written_around_it() -> None:
    entry = Connection.from_mapping({**ROOTED, "base_url": "https://acme.example.com"})
    assert entry.oauth.authorization_url == (
        "https://acme.example.com/oauth/authorizations/new"
    )
    assert entry.oauth.token_url == "https://acme.example.com/oauth/tokens"
    assert entry.mcp_url == "https://acme.example.com/mcp"
    assert entry.hosts == frozenset({"acme.example.com"})
    assert not entry.needs_base_url


def test_a_trailing_slash_on_the_root_does_not_double_up() -> None:
    entry = Connection.from_mapping({**ROOTED, "base_url": "https://acme.example.com/"})
    assert entry.oauth.token_url == "https://acme.example.com/oauth/tokens"


def test_without_the_root_no_stand_in_host_is_admitted() -> None:
    """A stand-in is a name someone can register, and tokens would be sent there."""
    entry = Connection.from_mapping(ROOTED)
    assert entry.needs_base_url
    assert entry.hosts == frozenset()
    assert not entry.allows("https://base_url/mcp")
    assert not entry.allows("https://acme.example.com/mcp")


def test_a_connection_that_names_its_hosts_never_needs_a_root() -> None:
    assert not connection().needs_base_url


def test_the_root_itself_may_not_contain_the_placeholder() -> None:
    """It would survive substitution and the connection would ask for a root forever."""
    with pytest.raises(DeclarationError, match="base_url"):
        Connection.from_mapping(
            {**ROOTED, "base_url": "https://acme.example.com/{base_url}"}
        )
