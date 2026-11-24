"""Redaction applied to everything that leaves the process for a model or a report."""
from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
]
_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|card|cvv|pan|aadhaar)")


_DIGIT_RUN = re.compile(r"(?<![\w-])\d(?:[ -]?\d){11,18}(?![\w-])")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_number(match: re.Match[str]) -> str:
    """Mask payment-card numbers (Luhn-valid, 13 to 19 digits) and 12-digit identity numbers."""
    digits = re.sub(r"[ -]", "", match.group())
    if len(digits) == 12 or (13 <= len(digits) <= 19 and _luhn(digits)):
        return "[REDACTED_NUMBER]"
    return match.group()


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _DIGIT_RUN.sub(_mask_number, text)


def redact(value: Any) -> Any:
    """Return a deep copy with secrets and personal identifiers masked."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: "[REDACTED]" if isinstance(k, str) and _SENSITIVE_KEYS.search(k) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
