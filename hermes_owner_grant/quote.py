"""Quote normalization and segment/fragment matching for owner grants.

Unit U4 of the #60 owner-grant verifier (HE-OWNER-FORWARD addendum §4.2, G-11).
Normalizes text (NFC, CRLF->LF, curly quotes, strip markdown quotes, collapse whitespace, trim).
Enforces exact and segment tiers; refuses fragments by default (e.g. "merge" out of "do not merge").
Minimum quote length is 12 characters. Case is preserved.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

_CURLY_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }
)


def _pre_normalize(text: str) -> str:
    """Apply normalization steps that preserve line structure."""
    # 1. Unicode NFC
    t = unicodedata.normalize("NFC", text)
    # 2. CRLF and CR to LF
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # 3. Curly quotes to ASCII
    t = t.translate(_CURLY_QUOTE_MAP)
    # 4. Strip leading '> ' or '>' markdown quote on each line
    lines = []
    for line in t.split("\n"):
        l_stripped = line.lstrip(" ")
        if l_stripped.startswith(">"):
            l_stripped = l_stripped[1:]
            if l_stripped.startswith(" "):
                l_stripped = l_stripped[1:]
        lines.append(l_stripped)
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    """Normalize text completely (including collapsing whitespace runs and trimming).

    Case is preserved.
    """
    t = _pre_normalize(text)
    # 5. Collapse whitespace runs & 6. Trim
    return re.sub(r"\s+", " ", t).strip()


def split_segments(text: str) -> List[str]:
    """Split text into segments before whitespace is collapsed.

    Segments split on line breaks and on sentence ends ([.!?;] followed by whitespace).
    Each resulting segment is whitespace-collapsed and trimmed. Empty segments are omitted.
    """
    t = _pre_normalize(text)
    # Split on newlines or sentence endings followed by whitespace
    raw_segments = re.split(r"\n+|(?<=[.!?;])\s+", t)
    segments = []
    for raw in raw_segments:
        norm = re.sub(r"\s+", " ", raw).strip()
        if norm:
            segments.append(norm)
    return segments


class QuoteMatch:
    """Result of quote matching against owner text."""

    def __init__(
        self,
        ok: bool,
        kind: Optional[str],
        reason: Optional[str] = None,
        segment_index: Optional[int] = None,
        normalized_quote: str = "",
        normalized_text: str = "",
    ):
        self.ok = ok
        self.kind = kind
        self.reason = reason
        self.segment_index = segment_index
        self.normalized_quote = normalized_quote
        self.normalized_text = normalized_text

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "reason": self.reason,
            "segment_index": self.segment_index,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.as_dict()

    def __repr__(self) -> str:
        return (
            f"QuoteMatch(ok={self.ok!r}, kind={self.kind!r}, "
            f"reason={self.reason!r}, segment_index={self.segment_index!r})"
        )


def match_quote(
    quote: str,
    text: str,
    *,
    allow_fragment: bool = False,
    min_len: int = 12,
) -> QuoteMatch:
    """Match a claimed quote against the owner text.

    Tiers:
    - exact: normalized quote equals normalized text.
    - segment: quote equals one or more consecutive whole segments.
    - fragment: substring within a segment or text. Refused unless allow_fragment=True.
    - mismatch: quote is not found in text.
    Quotes shorter than min_len (default 12) are refused.
    """
    norm_quote = normalize_text(quote)
    norm_text = normalize_text(text)

    # Minimum length enforcement
    if len(norm_quote) < min_len:
        return QuoteMatch(
            ok=False,
            kind=None,
            reason="quote_too_short",
            segment_index=None,
            normalized_quote=norm_quote,
            normalized_text=norm_text,
        )

    # Exact tier
    if norm_quote == norm_text:
        return QuoteMatch(
            ok=True,
            kind="exact",
            reason=None,
            segment_index=None,
            normalized_quote=norm_quote,
            normalized_text=norm_text,
        )

    # Segment tier: single segment or consecutive segments
    segments = split_segments(text)
    n = len(segments)
    for start in range(n):
        for end in range(start + 1, n + 1):
            candidate = " ".join(segments[start:end])
            if norm_quote == candidate:
                return QuoteMatch(
                    ok=True,
                    kind="segment",
                    reason=None,
                    segment_index=start,
                    normalized_quote=norm_quote,
                    normalized_text=norm_text,
                )

    # Fragment tier: check if quote is a substring within the full text or any segment
    is_fragment = False
    if norm_quote in norm_text:
        is_fragment = True
    else:
        for s in segments:
            if norm_quote in s:
                is_fragment = True
                break

    if is_fragment:
        if allow_fragment:
            return QuoteMatch(
                ok=True,
                kind="fragment",
                reason=None,
                segment_index=None,
                normalized_quote=norm_quote,
                normalized_text=norm_text,
            )
        else:
            return QuoteMatch(
                ok=False,
                kind="fragment",
                reason="quote_fragment",
                segment_index=None,
                normalized_quote=norm_quote,
                normalized_text=norm_text,
            )

    return QuoteMatch(
        ok=False,
        kind="mismatch",
        reason="quote_mismatch",
        segment_index=None,
        normalized_quote=norm_quote,
        normalized_text=norm_text,
    )
