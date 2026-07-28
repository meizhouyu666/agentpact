"""Input value sanitization for audit logs.

Applies desensitization rules before writing to audit storage:
- Card numbers: keep last 4 digits only (e.g., "6222 **** **** 1234")
- Passwords: fully masked
- Amounts: preserved (business-critical)
- ID numbers: keep last 4 digits
- Phone numbers: mask middle digits

Rules are configurable via regex patterns.
"""

import hashlib
import re
from dataclasses import dataclass


@dataclass
class SanitizationRule:
    """A single sanitization rule."""

    name: str
    pattern: re.Pattern
    replacement_fn: callable  # (match) -> str


def _mask_card_number(match: re.Match) -> str:
    """Keep last 4 digits of card number."""
    full = match.group(0).replace(" ", "").replace("-", "")
    if len(full) < 4:
        return "****"
    return "*" * (len(full) - 4) + full[-4:]


def _mask_password(match: re.Match) -> str:
    """Fully mask password values."""
    return match.group(1) + "********"


def _mask_id_number(match: re.Match) -> str:
    """Keep last 4 digits of ID number."""
    full = match.group(0)
    if len(full) <= 4:
        return "****"
    return "*" * (len(full) - 4) + full[-4:]


def _mask_phone(match: re.Match) -> str:
    """Mask middle digits of phone number."""
    full = match.group(0)
    if len(full) >= 11:
        return full[:3] + "****" + full[-4:]
    return "****" + full[-4:] if len(full) > 4 else "****"


# Default rules applied in order
DEFAULT_RULES: list[SanitizationRule] = [
    SanitizationRule(
        name="password",
        pattern=re.compile(
            r"(password|passwd|pwd|密码|口令)\s*[:=：]\s*\S+",
            re.IGNORECASE,
        ),
        replacement_fn=_mask_password,
    ),
    SanitizationRule(
        name="card_number",
        pattern=re.compile(
            r"\b[3-6]\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
        ),
        replacement_fn=_mask_card_number,
    ),
    SanitizationRule(
        name="id_number",
        pattern=re.compile(
            r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
        ),
        replacement_fn=_mask_id_number,
    ),
    SanitizationRule(
        name="phone",
        pattern=re.compile(
            r"\b1[3-9]\d{9}\b",
        ),
        replacement_fn=_mask_phone,
    ),
]


def sanitize_input(
    value: str | None,
    rules: list[SanitizationRule] | None = None,
) -> str | None:
    """Apply sanitization rules to an input value.

    Args:
        value: The raw input value to sanitize.
        rules: Optional custom rules. Defaults to DEFAULT_RULES.

    Returns:
        Sanitized string, or None if input was None.
    """
    if value is None:
        return None

    if rules is None:
        rules = DEFAULT_RULES

    result = value
    for rule in rules:
        result = rule.pattern.sub(rule.replacement_fn, result)

    return result


def hash_raw_value(value: str | None) -> str | None:
    """Compute SHA-256 hash of the raw input value for integrity verification.

    The hash allows verifying that the original value hasn't been tampered with
    without storing the actual sensitive data.
    """
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
