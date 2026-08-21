"""Policies: rules applied to every agent from the outside.

gete decides the shape and where the text goes. The text itself belongs to the
installation; none ships with gete.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gete._yaml import read_yaml
from gete.schema import validate_document

# Tools that do not say otherwise are treated as writes. Anything not declared
# read-only is governed as a write; that is the safe side to err on.
DEFAULT_EFFECT = "write"


@dataclass(frozen=True)
class Policy:
    name: str
    when: str
    instruction_prefix: str | None = None
    redact_keys: tuple[str, ...] = ()
    redact_digit_only_keys: tuple[str, ...] = ()
    redact_patterns: tuple[tuple[str, str], ...] = ()
    # "write_tools", "all", a tuple of tool names, or None.
    require_confirmation: str | tuple[str, ...] | None = None
    deny_tools: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Policy":
        redact: Mapping[str, Any] = data.get("redact", {})
        confirmation = data.get("require_confirmation")
        return cls(
            name=data["name"],
            when=data["when"],
            instruction_prefix=data.get("instruction_prefix"),
            redact_keys=tuple(redact.get("keys", ())),
            redact_digit_only_keys=tuple(redact.get("digit_only_keys", ())),
            redact_patterns=tuple(
                (entry["pattern"], entry["replacement"])
                for entry in redact.get("patterns", ())
            ),
            require_confirmation=(
                tuple(confirmation) if isinstance(confirmation, list) else confirmation
            ),
            deny_tools=tuple(data.get("deny_tools", ())),
        )


def load_policy_documents(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Read policy files in the given order and return their entries, schema-checked."""
    documents: list[dict[str, Any]] = []
    for path in paths:
        entries = read_yaml(path)
        validate_document("policy", entries, source=path)
        documents.extend(entries)
    return documents


def load_policies(paths: Iterable[Path]) -> list[Policy]:
    return [Policy.from_mapping(entry) for entry in load_policy_documents(paths)]


def tool_effect(tool: Mapping[str, Any]) -> str:
    """read or write. Only mcp and python tools can declare read; builtin is write."""
    if "mcp" in tool:
        effect: str = tool["mcp"].get("effect", DEFAULT_EFFECT)
        return effect
    if "python" in tool:
        spec = tool["python"]
        if isinstance(spec, Mapping):
            python_effect: str = spec.get("effect", DEFAULT_EFFECT)
            return python_effect
    return DEFAULT_EFFECT


def has_write_tools(agent: Mapping[str, Any]) -> bool:
    return any(tool_effect(tool) == "write" for tool in agent.get("tools", ()))


def policy_applies(policy: Policy, agent: Mapping[str, Any]) -> bool:
    """Evaluate the policy's when against an agent document."""
    if policy.when == "always":
        return True
    if policy.when == "has_write_tools":
        return has_write_tools(agent)
    if policy.when == "has_connections":
        return bool(agent.get("connections"))
    if policy.when == "has_secret_env":
        runtime: Mapping[str, Any] = agent.get("runtime", {})
        return bool(runtime.get("agent_engine", {}).get("secret_env"))
    raise ValueError(f"unknown policy condition {policy.when!r}")


def applicable(policies: Iterable[Policy], agent: Mapping[str, Any]) -> list[Policy]:
    return [policy for policy in policies if policy_applies(policy, agent)]


def compose_instruction(
    policies: Iterable[Policy], agent: Mapping[str, Any], instruction: str
) -> str:
    """Put the applicable policies' prefixes first, in order, then the agent's own text.

    Text that comes first is the hardest for later instructions to override.
    """
    parts = [
        policy.instruction_prefix.strip()
        for policy in applicable(policies, agent)
        if policy.instruction_prefix
    ]
    parts.append(instruction)
    return "\n\n".join(parts)
