"""Redaction: what leaves a tool result before the model sees it."""

from typing import Any

from gete.policies import Policy
from gete.redact import RedactRules, redact, redact_text

RULES = RedactRules(
    keys=("bank_name", "account_holder"),
    digit_only_keys=("account_number",),
    patterns=((r"(?i)iban[:\s]*([A-Z]{2}\d{2}[A-Z0-9]{11,30})", "IBAN [redacted]"),),
)


def test_listed_keys_are_masked_at_any_depth() -> None:
    value: dict[str, Any] = {
        "partner": {
            "bank_name": "Example Bank",
            "accounts": [{"account_holder": "A. Person"}],
        },
        "note": "fine",
    }
    assert redact(value, RULES) == {
        "partner": {
            "bank_name": "[redacted]",
            "accounts": [{"account_holder": "[redacted]"}],
        },
        "note": "fine",
    }


def test_digit_only_keys_keep_the_digit_count() -> None:
    """A missing digit means a returned transfer; the count matters, not the number."""
    assert redact({"account_number": "1234567"}, RULES) == {
        "account_number": "[7 digits]"
    }
    assert redact({"account_number": "12-34"}, RULES) == {
        "account_number": "[4 digits]"
    }
    assert redact({"account_number": 98765}, RULES) == {"account_number": "[5 digits]"}
    assert redact({"account_number": ""}, RULES) == {"account_number": "[redacted]"}


def test_patterns_apply_to_every_string_value() -> None:
    text = "Pay to IBAN DE89370400440532013000 by Friday"
    assert redact_text(text, RULES) == "Pay to IBAN [redacted] by Friday"
    assert redact({"memo": [text]}, RULES) == {
        "memo": ["Pay to IBAN [redacted] by Friday"]
    }


def test_values_that_are_not_containers_or_strings_pass_through() -> None:
    assert redact(42, RULES) == 42
    assert redact(None, RULES) is None
    assert redact(True, RULES) is True


def test_no_rules_means_nothing_changes() -> None:
    value = {"bank_name": "Example Bank"}
    assert redact(value, RedactRules()) == value


def test_rules_combine_from_policies_in_order() -> None:
    first = Policy.from_mapping(
        {
            "name": "a",
            "when": "always",
            "redact": {
                "keys": ["bank_name"],
                "patterns": [{"pattern": "x", "replacement": "1"}],
            },
        }
    )
    second = Policy.from_mapping(
        {
            "name": "b",
            "when": "always",
            "redact": {
                "keys": ["iban"],
                "digit_only_keys": ["account_number"],
                "patterns": [{"pattern": "y", "replacement": "2"}],
            },
        }
    )
    rules = RedactRules.from_policies([first, second])
    assert rules.keys == ("bank_name", "iban")
    assert rules.digit_only_keys == ("account_number",)
    assert rules.patterns == (("x", "1"), ("y", "2"))


def test_mask_strings_come_from_the_policy() -> None:
    """Consent screens and agents speak the installation's language; masks must too."""
    rules = RedactRules(
        keys=("bank_name",),
        digit_only_keys=("account_number",),
        hidden="[非表示]",
        digits="[{n}桁]",
    )
    assert redact({"bank_name": "Example Bank"}, rules) == {"bank_name": "[非表示]"}
    assert redact({"account_number": "1234567"}, rules) == {"account_number": "[7桁]"}
    assert redact({"account_number": ""}, rules) == {"account_number": "[非表示]"}


def test_default_masks_are_unchanged() -> None:
    rules = RedactRules(keys=("k",), digit_only_keys=("d",))
    assert redact({"k": "x", "d": "12"}, rules) == {
        "k": "[redacted]",
        "d": "[2 digits]",
    }


def test_masks_flow_from_policies_and_the_last_policy_wins() -> None:
    first = Policy.from_mapping(
        {
            "name": "a",
            "when": "always",
            "redact": {"keys": ["bank_name"], "masks": {"hidden": "[hidden-a]"}},
        }
    )
    second = Policy.from_mapping(
        {
            "name": "b",
            "when": "always",
            "redact": {"masks": {"hidden": "[非表示]", "digits": "[{n}桁]"}},
        }
    )
    rules = RedactRules.from_policies([first, second])
    assert rules.hidden == "[非表示]"
    assert rules.digits == "[{n}桁]"
    assert RedactRules.from_policies([first]).digits == "[{n} digits]"


def test_mask_texts_are_not_format_strings() -> None:
    """A brace in declared text must not crash redaction mid tool call."""
    rules = RedactRules(
        digit_only_keys=("account_number",),
        digits="[{n} digits] {see policy}",
    )
    assert redact({"account_number": "1234567"}, rules) == {
        "account_number": "[7 digits] {see policy}"
    }
