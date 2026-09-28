"""Thin masking glue over the framework-standard PII detector.

Detection is deliberately limited to ``shared.security.pii_detector.detect_pii``.
The template keeps only span replacement because the SDK detects but does not
rewrite values.  In particular, undetected Japanese names pass through by the
user-approved B-5 policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.security.pii_detector import detect_pii


@dataclass
class _RoleTokens:
    """Give SDK-detected Latin names a stable local role token."""

    values: dict[str, str] = field(default_factory=dict)

    def token(self, raw: str) -> str:
        if raw not in self.values:
            index = len(self.values)
            suffix = chr(ord("A") + index) if index < 26 else str(index)
            self.values[raw] = f"関係者{suffix}"
        return self.values[raw]


def sanitize_disclosure_pii(text: str) -> tuple[str, int, bool]:
    """Mask only framework detector findings, retaining the tuple contract."""
    if not text:
        return "", 0, True
    try:
        findings = detect_pii(text)
        tokens = _RoleTokens()
        replacements: list[tuple[int, int, str]] = []
        for finding in findings:
            start, end = int(finding["start"]), int(finding["end"])
            if start < 0 or end < start or end > len(text):
                raise ValueError("detect_pii returned an invalid span")
            replacement = (
                tokens.token(str(finding.get("value", text[start:end])))
                if finding.get("type") == "name"
                else "[PII情報除去済み]"
            )
            replacements.append((start, end, replacement))
    except Exception:  # detector or masking failure is the only unresolvable condition
        return text, 0, False
    if not replacements:
        return text, 0, True
    replacements.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    chosen: list[tuple[int, int, str]] = []
    for item in replacements:
        if not chosen or item[0] >= chosen[-1][1]:
            chosen.append(item)
    sanitized = text
    for start, end, replacement in reversed(chosen):
        sanitized = sanitized[:start] + replacement + sanitized[end:]
    return sanitized, len(chosen), True


def sanitize_value(value: Any) -> tuple[Any, int, bool]:
    """Recursively apply the framework-detector masking glue to JSON values."""
    if isinstance(value, str):
        return sanitize_disclosure_pii(value)
    if isinstance(value, list):
        total, output = 0, []
        for item in value:
            clean, count, ok = sanitize_value(item)
            if not ok:
                return value, total, False
            output.append(clean)
            total += count
        return output, total, True
    if isinstance(value, dict):
        total, output = 0, {}
        for key, item in value.items():
            clean, count, ok = sanitize_value(item)
            if not ok:
                return value, total, False
            output[key] = clean
            total += count
        return output, total, True
    return value, 0, True
