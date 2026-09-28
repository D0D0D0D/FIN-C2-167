"""S-3 second-layer guard against disclosure verdict assertions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "disclosure_verdict_lexicon.yaml"
_SENTENCES = re.compile(r"(?<=[。！？.!?])")


@dataclass(frozen=True)
class DisclosureVerdictLexicon:
    categories: dict[str, list[str]]
    replacement: str


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def load_lexicon(path: Path | str = _PATH) -> DisclosureVerdictLexicon:
    with open(path, encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    return DisclosureVerdictLexicon(
        categories={
            key: [_normalise(item) for item in value.get("patterns", [])]
            for key, value in (payload.get("categories") or {}).items()
        },
        replacement=str(payload.get("non_assertive_replacement_template", "")),
    )


def scan_and_sanitize(text: str, lexicon: DisclosureVerdictLexicon) -> tuple[str, bool, list[str]]:
    """Replace only flagged sentences; all-flagged content is fail-closed."""
    if not text or not text.strip():
        return text, True, []
    sentences = [part for part in _SENTENCES.split(text.strip()) if part.strip()] or [text]
    output, detected, flagged = [], [], 0
    for sentence in sentences:
        normal = _normalise(sentence)
        hits = [name for name, patterns in lexicon.categories.items() if any(p in normal for p in patterns)]
        if hits:
            flagged += 1
            detected.extend(hit for hit in hits if hit not in detected)
            output.append(lexicon.replacement)
        else:
            output.append(sentence)
    if not detected:
        return text, True, []
    return (text if flagged == len(sentences) else "".join(output), flagged != len(sentences), detected)


def scan_and_sanitize_fields(value: Any, lexicon: DisclosureVerdictLexicon) -> tuple[Any, bool, list[str]]:
    detected: list[str] = []
    safe = True

    def walk(item: Any) -> Any:
        nonlocal safe
        if isinstance(item, str):
            cleaned, is_safe, categories = scan_and_sanitize(item, lexicon)
            safe = safe and is_safe
            detected.extend(category for category in categories if category not in detected)
            return cleaned
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        return item

    return walk(value), safe, detected
