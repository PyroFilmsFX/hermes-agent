"""Quote normalization and segment/fragment matching for owner grants.

Unit U4 of the #60 owner-grant verifier (HE-OWNER-FORWARD addendum §4.2, G-11).
Normalizes text (NFC, line breaks->LF, curly quotes, strip markdown quotes, collapse whitespace,
trim). Enforces exact and segment tiers; refuses fragments by default (e.g. "merge" out of "do not
merge"). Minimum quote length is 12 characters. Case is preserved.

Segments are sentences, not lines (see ``split_segments``). Every rule errs toward "not a
boundary": a missed boundary only makes a real sentence quotable as a larger unit, while a false
boundary would let a quote drop a governing "Do not" and invert the owner's decision.
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


# Every character Python treats as a line break becomes LF; U+2029 is a paragraph break.
_LINE_BREAK_MAP = str.maketrans(
    {
        "\r": "\n",
        "\x0b": "\n",
        "\x0c": "\n",
        "\x1c": "\n",
        "\x1d": "\n",
        "\x1e": "\n",
        "\x85": "\n",
        "\u2028": "\n",
        "\u2029": "\n\n",
    }
)


def _pre_normalize(text: str) -> str:
    """Apply normalization steps that preserve line structure."""
    # 1. Unicode NFC
    t = unicodedata.normalize("NFC", text)
    # 2. CRLF, CR, NEL, VT, FF and the Unicode line/paragraph separators to LF
    t = t.replace("\r\n", "\n").translate(_LINE_BREAK_MAP)
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


# A list item: '- ', '* ', '+ ', a bullet, '1. ' or '1) ' at the start of a line.
_LIST_MARKER_RE = re.compile(r"(?:[-*+\u2022]|\d{1,9}[.)])\s+")
# A run of sentence-final punctuation plus closing quotes/emphasis, then whitespace or the end.
_TERMINATOR_RE = re.compile(r"([.!?]+)([\"'*_`]*)(?=\s|\Z)")
# Characters that may sit directly before the terminator: a closed quote, bracket or emphasis.
_BEFORE_TERMINATOR = "\"')]}*_`"
# Characters skipped when looking at the first letter of the next sentence.
_OPENERS = "\"'*_`([{\u00ab\u2039\u300c\u300e"
# Paired brackets and quote marks; a terminator inside an open pair is not a sentence end.
_BRACKETS = {
    "(": ")",
    "[": "]",
    "{": "}",
    "\u00ab": "\u00bb",
    "\u2039": "\u203a",
    "\u300c": "\u300d",
    "\u300e": "\u300f",
}
_CLOSING_BRACKETS = frozenset(_BRACKETS.values())
_ABBREVIATIONS = frozenset(
    (
        "al", "approx", "cf", "co", "corp", "dept", "dr", "e.g", "eg", "esp", "est", "etc",
        "fig", "i.e", "ie", "inc", "incl", "jr", "ltd", "min", "mr", "mrs", "ms", "mt", "no",
        "nos", "p.s", "prof", "re", "sr", "st", "viz", "vol", "vs",
    )
)


def _is_sentence_end(
    text: str, start: int, run: str, closers: str, abbreviations: bool = True
) -> bool:
    """Whether the terminator ``run`` at ``text[start]`` ends the sentence begun at 0.

    Not a sentence end: an ellipsis ('...' / '. . .'), a terminator that does not follow a
    word or a closed quote/bracket ('(!)'), a period after an abbreviation or an initial
    ('e.g.', 'Dr.', 'J.'; skipped when ``abbreviations`` is false), or a terminator inside
    an open bracket or an open double quote.
    """
    if run.count(".") > 1:
        return False
    before = text[:start]
    if not before:
        return False
    last = before[-1]
    if not (last.isalnum() or last in _BEFORE_TERMINATOR):
        return False
    if run == "." and abbreviations:
        token = before.split()[-1].lstrip(_OPENERS).lower()
        if (
            len(token) <= 1
            or "." in token
            or token in _ABBREVIATIONS
            or (len(token) == 2 and token[0] == "." and token[1].isalpha())
        ):
            return False
    depth = 0
    for ch in before:
        if ch in _BRACKETS:
            depth += 1
        elif ch in _CLOSING_BRACKETS and depth:
            depth -= 1
    if depth:
        return False
    quoted = before + run + closers
    if quoted.count('"') % 2:
        return False
    return _open_single_quotes(quoted) == 0


def _open_single_quotes(text: str) -> int:
    """Count single quotes still open at the end of ``text``.

    An opening quote follows the start, whitespace or an opener and precedes a non-space; a
    closing quote follows a non-space and precedes whitespace, punctuation or the end. An
    apostrophe inside a word ("don't") is neither.
    """
    depth = 0
    for i, ch in enumerate(text):
        if ch != "'":
            continue
        prev = text[i - 1] if i else " "
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if (prev.isspace() or prev in _OPENERS) and not nxt.isspace():
            depth += 1
        elif not prev.isspace() and not nxt.isalnum() and depth:
            depth -= 1
    return depth


def _starts_sentence(text: str) -> bool:
    """Whether ``text`` begins like a new sentence: a capital letter or a digit, after any
    opening quotes/brackets/emphasis. Lower case, dashes, ellipses and uncased scripts do not."""
    stripped = text.lstrip().lstrip(_OPENERS)
    return bool(stripped) and (stripped[0].isupper() or stripped[0].isdigit())


def _sentences(unit: str) -> List[str]:
    """Split one block of text (lines already joined by spaces) into sentences.

    A sentence ends at '.', '!' or '?' (plus closing quotes/emphasis) followed by whitespace,
    when ``_is_sentence_end`` holds and the rest ``_starts_sentence``.
    Semicolons, colons, dashes and ellipses never end a sentence.
    """
    out = []
    begin = 0
    for m in _TERMINATOR_RE.finditer(unit):
        end = m.end()
        if end >= len(unit):
            break
        current = unit[begin:end]
        offset = m.start(1) - begin
        if _is_sentence_end(current, offset, m.group(1), m.group(2)) and _starts_sentence(
            unit[end:]
        ):
            out.append(unit[begin:end].strip())
            begin = end
    tail = unit[begin:].strip()
    if tail:
        out.append(tail)
    return out


def _closed(sentence: str, abbreviations: bool = True) -> bool:
    """Whether ``sentence`` ends with a real sentence terminator (see ``_is_sentence_end``)."""
    m = None
    for m in _TERMINATOR_RE.finditer(sentence):
        pass
    if m is None or m.end() != len(sentence):
        return False
    return _is_sentence_end(sentence, m.start(1), m.group(1), m.group(2), abbreviations)


def split_segments(text: str) -> List[str]:
    """Split owner text into sentence segments (whitespace-collapsed, trimmed, non-empty).

    A line break is whitespace inside a sentence. Text is first cut into blocks: a block starts
    at the beginning, after a blank line (a paragraph), or at a list-item line ('- ', '* ',
    '+ ', a bullet, '1. ', '1) '; the marker is dropped); other lines continue the block.
    Inside a block, sentences end per ``_sentences``. Between blocks there is a boundary only
    when the previous block ends with a finished sentence (``_closed``; between two sibling
    items a final 'A.' or 'etc.' counts as finished, since the item marker is explicit). A
    list whose first item follows an unfinished sentence (e.g. 'Do NOT:'), or any item after
    an unfinished item, is governed by it: the rest of that list joins the governing segment.
    """
    t = _pre_normalize(text)
    blocks = []  # (is_item, [lines])
    gap = True
    for line in t.split("\n"):
        stripped = line.strip()
        if not stripped:
            gap = True
            continue
        marker = _LIST_MARKER_RE.match(stripped)
        if marker:
            blocks.append((True, [stripped[marker.end():]]))
        elif gap or not blocks:
            blocks.append((False, [stripped]))
        else:
            blocks[-1][1].append(stripped)
        gap = False

    segments = []  # type: List[str]
    governed = False
    prev_item = False
    for is_item, lines in blocks:
        sentences = [
            re.sub(r"\s+", " ", s).strip()
            for s in _sentences(re.sub(r"\s+", " ", " ".join(lines)).strip())
        ]
        sentences = [s for s in sentences if s]
        if not sentences:
            continue
        if not is_item:
            governed = False
        sibling = is_item and prev_item
        join = bool(segments) and (
            (sibling and governed) or not _closed(segments[-1], abbreviations=not sibling)
        )
        if is_item and join:
            governed = True
        if join:
            segments[-1] = segments[-1] + " " + sentences[0]
            sentences = sentences[1:]
        segments.extend(sentences)
        prev_item = is_item
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
