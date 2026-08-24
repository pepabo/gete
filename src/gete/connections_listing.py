"""gete connections: the services an installation can declare."""

from typing import Any

from gete.catalog import catalog_connections
from gete.connection import Registry

NOT_VERIFIED = "not verified in Gemini Enterprise"


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
                or "(none: by elimination)",
                "verified": connection.verified.get("gemini_enterprise", NOT_VERIFIED),
                "source": "catalog" if connection.id in catalog else "gete.yaml",
                "retired": connection.retired or "",
            }
        )
    return rows


def format_table(rows: list[dict[str, Any]]) -> str:
    columns = ("id", "status", "source", "verified", "hosts")
    widths = {c: max(len(c), *(len(str(row[c])) for row in rows)) for c in columns}
    lines = ["  ".join(c.ljust(widths[c]) for c in columns)]
    for row in rows:
        lines.append("  ".join(str(row[c]).ljust(widths[c]) for c in columns))
        if row["retired"]:
            lines.append(f"  retired: {row['retired']}")
    return "\n".join(lines) + "\n"
