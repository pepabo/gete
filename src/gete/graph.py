"""gete graph: Mermaid drawn from the declarations; no second diagram to go stale."""

import re
from urllib.parse import urlsplit

from gete.connection import Registry
from gete.declaration import Project
from gete.errors import GeteError

# Marks what acts with a credential the agent holds rather than the caller's.
BOT = " (bot)"


def label(text: str) -> str:
    """Text safe inside a Mermaid label. Display names and refs are prose.

    A quote would end the label early and leave the rest as syntax; Mermaid
    reads the HTML entity instead. Line breaks become its own <br/>.
    """
    return (
        str(text)
        .replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("\r\n", "<br/>")
        .replace("\n", "<br/>")
    )


def mermaid(project: Project, names: list[str] | None = None) -> str:
    registry = Registry.from_catalog(project.connection_overrides)
    lines = ["flowchart LR"]
    engines: set[str] = set()
    for agent in project.agents:
        if names and agent.name not in names:
            continue
        node = _ident(agent.name)
        engine = agent.engine
        if engine and engine not in engines:
            engines.add(engine)
            lines.append(
                f'  GE_{_ident(engine)}["Gemini Enterprise<br/>{label(engine)}"]'
            )
        lines.append(f'  {node}["{label(agent.name)}"]')
        if engine:
            lines.append(f"  GE_{_ident(engine)} --> {node}")
        connected: set[str] = set()
        for index, tool in enumerate(agent.tools):
            tool_node = f"{node}_tool_{index}"
            if "builtin" in tool:
                lines.append(f'  {node} --> {tool_node}["{label(tool["builtin"])}"]')
            elif "python" in tool:
                spec = tool["python"]
                ref = spec if isinstance(spec, str) else spec["ref"]
                lines.append(f'  {node} --> {tool_node}["python<br/>{label(ref)}"]')
            elif "mcp" in tool:
                host = urlsplit(tool["mcp"]["url"]).hostname or "mcp"
                lines.append(f'  {node} --> {tool_node}[("MCP<br/>{label(host)}")]')
                connection = tool["mcp"].get("connection")
                if connection:
                    connected.add(connection)
                    lines.append(
                        f"  {node} -. {_via(registry, connection)} .-> {tool_node}"
                    )
            elif "openapi" in tool:
                count = len(tool["openapi"]["operations"])
                noun = "operation" if count == 1 else "operations"
                lines.append(
                    f'  {node} --> {tool_node}[("openapi<br/>{count} {noun}")]'
                )
                connection = tool["openapi"]["connection"]
                connected.add(connection)
                lines.append(
                    f"  {node} -. {_via(registry, connection)} .-> {tool_node}"
                )
        for name in agent.shared_credentials:
            # Marked as the bot it is: the diagram must not read as if these
            # tools acted with the caller's authorization.
            lines.append(
                f'  {node} --> {node}_shared_{_ident(name)}["{label(name)}{BOT}"]'
            )
        for connection_id in agent.connections:
            if connection_id in connected:
                continue
            connection = registry.get(connection_id, include_retired=True)
            display = connection.display_name
            if connection.app is not None:
                display += BOT
            lines.append(
                f"  {node} -. {_via(registry, connection_id)} .-> "
                f'conn_{_ident(connection_id)}[("{label(display)}")]'
            )
    return "\n".join(lines) + "\n"


def _via(registry: Registry, connection_id: str) -> str:
    """The edge label: the connection, marked when it acts as an App.

    An app connection reaches GitHub with the App's token whoever calls, so
    the diagram must not read as if it carried the caller's authorization.
    """
    try:
        connection = registry.get(connection_id, include_retired=True)
    except GeteError:
        # validate reports the unknown id; the diagram still draws the rest.
        return connection_id
    return connection_id + (BOT if connection.app is not None else "")


def _ident(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", text)
