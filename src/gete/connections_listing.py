"""gete connections: the services an installation can declare."""

from typing import Any

from gete.catalog import catalog_connections
from gete.connection import Connection, Registry
from gete.register import REDIRECT_URI

NOT_VERIFIED = "not verified in Gemini Enterprise"
NONE_DECLARED = "(none)"
BY_ELIMINATION = "(none: by elimination)"


def connections_table(registry: Registry) -> list[dict[str, Any]]:
    """One row per connection, retired ones included, in id order."""
    catalog = set(catalog_connections())
    rows = []
    for connection in registry.all(include_retired=True):
        rows.append(
            {
                "id": connection.id,
                "display_name": connection.display_name,
                "status": "retired" if connection.retired else "available",
                "hosts": ", ".join(sorted(connection.hosts)),
                "token_prefixes": ", ".join(connection.token_prefixes)
                or BY_ELIMINATION,
                "verified": connection.verified.get("gemini_enterprise", NOT_VERIFIED),
                "source": "catalog" if connection.id in catalog else "gete.yaml",
                "retired": connection.retired or "",
            }
        )
    return rows


def format_connection(connection: Connection) -> str:
    """Everything about one connection, for the person who has to prepare it.

    The secret names and the redirect URI are gete's and Gemini Enterprise's
    doing rather than the service's, and they are printed next to what the
    service asks for because registering an OAuth client takes all of it at
    once. Some providers hand out no way to delete a client again, so a person
    guessing at any one of the three has to get it right the first time.
    """
    oauth = connection.oauth
    scopes = [f"{scope}: {text}" for scope, text in oauth.scopes.items()]
    optional = [f"{scope}: {text}" for scope, text in oauth.optional_scopes.items()]
    fields: list[tuple[str, list[str]]] = [
        # The same word the listing uses; the reason gets a line of its own.
        ("status", ["retired" if connection.retired else "available"]),
        *([("retired", [connection.retired])] if connection.retired else []),
        (
            "source",
            ["catalog" if connection.id in catalog_connections() else "gete.yaml"],
        ),
        ("verified", [connection.verified.get("gemini_enterprise", NOT_VERIFIED)]),
        ("docs", [connection.docs or NONE_DECLARED]),
        ("hosts", [", ".join(sorted(connection.hosts)) or NONE_DECLARED]),
        (
            "redirect hosts",
            [", ".join(sorted(connection.redirect_hosts)) or NONE_DECLARED],
        ),
        ("token prefixes", [", ".join(connection.token_prefixes) or BY_ELIMINATION]),
        ("mcp url", [connection.mcp_url or NONE_DECLARED]),
        ("authorization", [oauth.authorization_url]),
        ("token url", [oauth.token_url]),
        ("scopes", scopes or [NONE_DECLARED]),
        # The menu, because the OAuth client has to be prepared for every
        # scope an agent may select, not only the defaults.
        ("optional scopes", optional or [NONE_DECLARED]),
        ("client id", [connection.client_id_secret]),
        ("client secret", [connection.client_secret_secret]),
        ("redirect uri", [REDIRECT_URI]),
    ]
    width = max(len(name) for name, _ in fields)
    lines = [f"{connection.id}  {connection.display_name}"]
    for name, values in fields:
        for index, value in enumerate(values):
            label = name if index == 0 else ""
            lines.append(f"  {label.ljust(width)}  {value}")
    if connection.setup:
        lines.append("")
        lines.append("Before anyone can authorize:")
        # A blank line stays blank: indenting it would leave trailing spaces
        # in output people paste into a ticket.
        lines.extend(
            f"  {line}" if line else ""
            for line in connection.setup.rstrip().splitlines()
        )
    return "\n".join(lines) + "\n"


def format_table(rows: list[dict[str, Any]]) -> str:
    columns = ("id", "status", "source", "verified", "hosts")
    widths = {c: max(len(c), *(len(str(row[c])) for row in rows)) for c in columns}
    lines = ["  ".join(c.ljust(widths[c]) for c in columns)]
    for row in rows:
        lines.append("  ".join(str(row[c]).ljust(widths[c]) for c in columns))
        if row["retired"]:
            lines.append(f"  retired: {row['retired']}")
    return "\n".join(lines) + "\n"
