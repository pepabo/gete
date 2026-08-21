"""Redaction of tool results, driven by the policies' redact rules."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gete.policies import Policy

REDACTED = "[redacted]"


@dataclass(frozen=True)
class RedactRules:
    """Keys to hide, keys to reduce to a digit count, and patterns for text."""

    keys: tuple[str, ...] = ()
    digit_only_keys: tuple[str, ...] = ()
    patterns: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_policies(cls, policies: Iterable["Policy"]) -> "RedactRules":
        """Combine the rules of several policies, keeping their order."""
        keys: list[str] = []
        digit_only: list[str] = []
        patterns: list[tuple[str, str]] = []
        for policy in policies:
            keys.extend(key for key in policy.redact_keys if key not in keys)
            digit_only.extend(
                key for key in policy.redact_digit_only_keys if key not in digit_only
            )
            patterns.extend(policy.redact_patterns)
        return cls(
            keys=tuple(keys),
            digit_only_keys=tuple(digit_only),
            patterns=tuple(patterns),
        )


def mask_digits(value: Any) -> str:
    """Replace a value by its digit count. The count is evidence; the digits are not."""
    digits = re.sub(r"[^0-9]", "", str(value))
    return f"[{len(digits)} digits]" if digits else REDACTED


def redact_text(text: str, rules: RedactRules) -> str:
    """Apply the patterns in order to free text."""
    for pattern, replacement in rules.patterns:
        text = re.sub(pattern, replacement, text)
    return text


def redact(value: Any, rules: RedactRules) -> Any:
    """Walk dicts and lists, masking listed keys and running patterns over strings."""
    if isinstance(value, dict):
        return {key: _redact_item(key, item, rules) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, rules) for item in value]
    if isinstance(value, str):
        return redact_text(value, rules)
    return value


def _redact_item(key: str, value: Any, rules: RedactRules) -> Any:
    if key in rules.digit_only_keys:
        return mask_digits(value)
    if key in rules.keys:
        return REDACTED
    return redact(value, rules)
