"""Redaction of tool results, driven by the policies' redact rules."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gete.policies import Policy

REDACTED = "[redacted]"
DIGITS = "[{n} digits]"


@dataclass(frozen=True)
class RedactRules:
    """Keys to hide, keys to reduce to a digit count, and patterns for text.

    hidden and digits are the replacement texts, so an installation can mask
    in its own language; {n} in digits carries the digit count.
    """

    keys: tuple[str, ...] = ()
    digit_only_keys: tuple[str, ...] = ()
    # (pattern, replacement) or (pattern, replacement, digits_group). With a
    # group, {digits} in the replacement carries the count of digits in that
    # group's match, formatted like digit_only_keys values.
    patterns: tuple[tuple[str, str] | tuple[str, str, int | None], ...] = ()
    hidden: str = REDACTED
    digits: str = DIGITS

    @classmethod
    def from_policies(cls, policies: Iterable["Policy"]) -> "RedactRules":
        """Combine the rules of several policies, keeping their order.

        Masks are single values, not lists; the last policy that sets one wins.
        """
        keys: list[str] = []
        digit_only: list[str] = []
        patterns: list[tuple[str, str] | tuple[str, str, int | None]] = []
        hidden = REDACTED
        digits = DIGITS
        for policy in policies:
            keys.extend(key for key in policy.redact_keys if key not in keys)
            digit_only.extend(
                key for key in policy.redact_digit_only_keys if key not in digit_only
            )
            patterns.extend(policy.redact_patterns)
            if policy.redact_hidden_mask is not None:
                hidden = policy.redact_hidden_mask
            if policy.redact_digits_mask is not None:
                digits = policy.redact_digits_mask
        return cls(
            keys=tuple(keys),
            digit_only_keys=tuple(digit_only),
            patterns=tuple(patterns),
            hidden=hidden,
            digits=digits,
        )


def mask_digits(value: Any, rules: RedactRules | None = None) -> str:
    """Replace a value by its digit count. The count is evidence; the digits are not."""
    rules = rules or RedactRules()
    digits = re.sub(r"[^0-9]", "", str(value))
    # Not str.format: any other brace in the declared text would raise while
    # a tool result is being redacted. {n} is the only token there is.
    return rules.digits.replace("{n}", str(len(digits))) if digits else rules.hidden


def redact_text(text: str, rules: RedactRules) -> str:
    """Apply the patterns in order to free text."""
    for entry in rules.patterns:
        pattern, replacement = entry[0], entry[1]
        group = entry[2] if len(entry) > 2 else None
        if group is None:
            text = re.sub(pattern, replacement, text)
        else:
            text = re.sub(pattern, _count_digits(replacement, group, rules), text)
    return text


def _count_digits(
    replacement: str, group: int, rules: RedactRules
) -> "Callable[[re.Match[str]], str]":
    """Expand the replacement with {digits} carrying the group's digit count.

    The count is evidence the way digit_only_keys keeps it; the digits are not.
    """

    def expand(match: re.Match[str]) -> str:
        count = len(re.sub(r"[^0-9]", "", match.group(group) or ""))
        return match.expand(
            replacement.replace("{digits}", rules.digits.replace("{n}", str(count)))
        )

    return expand


def redact(value: Any, rules: RedactRules) -> Any:
    """Walk the containers, masking listed keys and running patterns over strings.

    Tuples and sets are walked like lists: a python tool may answer with any
    container, and one left untouched would carry its strings past the rules.
    """
    if isinstance(value, dict):
        # Keys carry data too. The rules match on the original key name; the
        # patterns then rewrite what the model gets to see of it. Two keys
        # rewritten to the same text stay separate, deterministically
        # numbered entries - masking must never swallow a value.
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            visible: Any = redact_text(key, rules) if isinstance(key, str) else key
            if isinstance(visible, str) and visible in masked:
                base, index = visible, 2
                while visible in masked:
                    visible = f"{base} [{index}]"
                    index += 1
            masked[visible] = _redact_item(key, item, rules)
        return masked
    if isinstance(value, list):
        return [redact(item, rules) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, rules) for item in value)
    if isinstance(value, frozenset):
        return frozenset(redact(item, rules) for item in value)
    if isinstance(value, set):
        return {redact(item, rules) for item in value}
    if isinstance(value, str):
        return redact_text(value, rules)
    return value


def _redact_item(key: str, value: Any, rules: RedactRules) -> Any:
    if key in rules.digit_only_keys:
        return mask_digits(value, rules)
    if key in rules.keys:
        return rules.hidden
    return redact(value, rules)
