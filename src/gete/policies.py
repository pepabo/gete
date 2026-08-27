"""Policies: rules applied to every agent from the outside.

gete decides the shape and where the text goes. The text itself belongs to the
installation; none ships with gete.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gete._yaml import read_yaml
from gete.errors import DeclarationError
from gete.schema import validate_document
from gete.shared_credentials import SHARED_CREDENTIALS

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
    redact_patterns: tuple[tuple[str, str, int | None], ...] = ()
    # Mask texts, when the policy sets them; None means "no opinion".
    redact_hidden_mask: str | None = None
    redact_digits_mask: str | None = None
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
                _pattern(entry) for entry in redact.get("patterns", ())
            ),
            redact_hidden_mask=redact.get("masks", {}).get("hidden"),
            redact_digits_mask=redact.get("masks", {}).get("digits"),
            require_confirmation=(
                tuple(confirmation) if isinstance(confirmation, list) else confirmation
            ),
            deny_tools=tuple(data.get("deny_tools", ())),
        )


def duplicate_policy_names(entries: Iterable[Mapping[str, Any]]) -> list[str]:
    """Names that appear more than once, in order of their second appearance.

    A policy's name identifies it in logs and docs; two policies sharing one
    could not be told apart there.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in entries:
        name = str(entry["name"])
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    return duplicates


def _pattern(entry: Mapping[str, Any]) -> tuple[str, str, int | None]:
    """One redact pattern, with the group whose digits replace {digits}, if any."""
    import re

    group = entry.get("digits_group")
    if group is not None and group > re.compile(entry["pattern"]).groups:
        raise DeclarationError(
            f"digits_group {group} names a group the pattern "
            f"{entry['pattern']!r} does not have"
        )
    return (entry["pattern"], entry["replacement"], group)


def load_policy_documents(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Read policy files in the given order and return their entries, schema-checked."""
    documents: list[dict[str, Any]] = []
    for path in paths:
        entries = read_yaml(path)
        validate_document("policy", entries, source=path)
        documents.extend(entries)
    duplicates = duplicate_policy_names(documents)
    if duplicates:
        raise DeclarationError(f"policy names are not unique: {', '.join(duplicates)}")
    return documents


def load_policies(paths: Iterable[Path]) -> list[Policy]:
    return [Policy.from_mapping(entry) for entry in load_policy_documents(paths)]


def tool_effect(tool: Mapping[str, Any]) -> str:
    """read or write. mcp, openapi, and python can declare read; builtin is write."""
    if "mcp" in tool:
        effect: str = tool["mcp"].get("effect", DEFAULT_EFFECT)
        return effect
    if "openapi" in tool:
        openapi_effect: str = tool["openapi"].get("effect", DEFAULT_EFFECT)
        return openapi_effect
    if "python" in tool:
        spec = tool["python"]
        if isinstance(spec, Mapping):
            python_effect: str = spec.get("effect", DEFAULT_EFFECT)
            return python_effect
    return DEFAULT_EFFECT


def has_write_tools(agent: Mapping[str, Any]) -> bool:
    # A shared credential's tools are not in the tools list, but the writes
    # among them are writes all the same; the write policies must see them.
    if any(
        SHARED_CREDENTIALS[name].has_write_tools
        for name in agent.get("shared_credentials", ())
        if name in SHARED_CREDENTIALS
    ):
        return True
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
    if policy.when == "has_shared_credentials":
        return bool(agent.get("shared_credentials"))
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
