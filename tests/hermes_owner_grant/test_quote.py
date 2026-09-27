"""G-11: quote normalization and the segment/fragment tiers (HE-OWNER-FORWARD addendum §4.2).

Accept the exact text or whole consecutive sentences of at least 12 chars;
refuse fragments (e.g. "merge" out of "do not merge").
Normalization: NFC, CRLF to LF, curly quotes to ASCII, strip leading '> ' on each line,
collapse whitespace runs, trim. Case is preserved.
"""

import pytest

from hermes_owner_grant import quote


# -- Normalization tests ------------------------------------------------------------------


def test_normalize_unicode_nfc():
    """Decomposed accents are normalized to NFC."""
    # 'e' + combining acute accent -> e with acute accent
    decomposed = "e\u0301te\u0301"
    composed = "\u00e9t\u00e9"
    assert quote.normalize_text(decomposed) == composed


def test_normalize_crlf_to_lf():
    """CRLF and CR are normalized to LF."""
    assert quote.normalize_text("line1\r\nline2\rline3\nline4") == "line1 line2 line3 line4"
    # When preserving line breaks before whitespace collapsing in split_segments:
    segments = quote.split_segments("line1\r\nline2\rline3\nline4")
    assert segments == ["line1", "line2", "line3", "line4"]


def test_normalize_curly_quotes():
    """Curly single and double quotes become ASCII ' and \"."""
    text = "‘single’ and “double” and ‚low‘ and „low-double”"
    assert quote.normalize_text(text) == "'single' and \"double\" and 'low' and \"low-double\""


def test_normalize_strip_leading_markdown_quotes():
    """Leading '> ' on each line is stripped."""
    text = "> First line of prompt.\n> Second line of prompt."
    assert quote.normalize_text(text) == "First line of prompt. Second line of prompt."
    segments = quote.split_segments(text)
    assert segments == ["First line of prompt.", "Second line of prompt."]


def test_normalize_strip_leading_markdown_without_space():
    """Leading '>' without space on each line is also stripped."""
    text = ">First line.\n>Second line."
    segments = quote.split_segments(text)
    assert segments == ["First line.", "Second line."]


def test_normalize_collapse_whitespace_and_trim():
    """Whitespace runs are collapsed to a single space and outer whitespace trimmed."""
    text = "   hello   \t  \n  world   "
    assert quote.normalize_text(text) == "hello world"


def test_normalize_preserves_case():
    """Case is strictly preserved."""
    text = "Merge PR #123 Into Main Branch"
    assert quote.normalize_text(text) == "Merge PR #123 Into Main Branch"


# -- Minimum length check -----------------------------------------------------------------


def test_minimum_quote_length():
    """Quotes under 12 characters after normalization are refused."""
    text = "Yes, do not merge until CI passes."
    # "Yes" is 3 chars
    m1 = quote.match_quote("Yes", text)
    assert m1.ok is False
    assert m1.reason == "quote_too_short"

    # "Approved" is 8 chars
    m2 = quote.match_quote("Approved", text)
    assert m2.ok is False
    assert m2.reason == "quote_too_short"

    # 11 chars
    m3 = quote.match_quote("12345678901", "12345678901 and more")
    assert m3.ok is False
    assert m3.reason == "quote_too_short"

    # 12 chars exact
    m4 = quote.match_quote("123456789012", "123456789012")
    assert m4.ok is True
    assert m4.kind == "exact"


# -- Exact match --------------------------------------------------------------------------


def test_exact_match():
    """Normalized quote equals normalized text."""
    owner_text = "> Deploy the new image to staging once tests pass."
    claimed_quote = "Deploy the new image to staging once tests pass."
    m = quote.match_quote(claimed_quote, owner_text)
    assert m.ok is True
    assert m.kind == "exact"
    assert m.reason is None


def test_exact_match_with_formatting_differences():
    """Exact match succeeds across quote characters, whitespace, and CRLF."""
    owner_text = "“Deploy to prod”\r\n\r\n  immediately after CI passes."
    claimed_quote = "\"Deploy to prod\" immediately after CI passes."
    m = quote.match_quote(claimed_quote, owner_text)
    assert m.ok is True
    assert m.kind == "exact"


# -- Segment match ------------------------------------------------------------------------


def test_whole_segment_match_single_sentence():
    """A whole single sentence segment matches as kind='segment'."""
    owner_text = (
        "Do not merge until CI is green. Deploy to prod after that. Thank you."
    )
    m0 = quote.match_quote("Do not merge until CI is green.", owner_text)
    assert m0.ok is True
    assert m0.kind == "segment"
    assert m0.segment_index == 0

    m1 = quote.match_quote("Deploy to prod after that.", owner_text)
    assert m1.ok is True
    assert m1.kind == "segment"
    assert m1.segment_index == 1


def test_whole_segment_match_line_breaks():
    """Segments split on line breaks as well as punctuation."""
    owner_text = "First paragraph heading\nSecond paragraph content follows."
    m = quote.match_quote("First paragraph heading", owner_text)
    assert m.ok is True
    assert m.kind == "segment"
    assert m.segment_index == 0

    m2 = quote.match_quote("Second paragraph content follows.", owner_text)
    assert m2.ok is True
    assert m2.kind == "segment"
    assert m2.segment_index == 1


def test_consecutive_whole_segments_match():
    """Multiple consecutive whole segments match as kind='segment'."""
    owner_text = (
        "Sentence one is here. Sentence two is here. Sentence three is here."
    )
    claimed = "Sentence one is here. Sentence two is here."
    m = quote.match_quote(claimed, owner_text)
    assert m.ok is True
    assert m.kind == "segment"
    assert m.segment_index == 0


def test_punctuation_delimiters():
    """Sentence boundaries include [.!?;] followed by whitespace."""
    for punct in [".", "!", "?", ";"]:
        text = f"First action is done{punct} Second action starts now."
        m = quote.match_quote("Second action starts now.", text)
        assert m.ok is True
        assert m.kind == "segment"
        assert m.segment_index == 1


# -- Fragment refusal ---------------------------------------------------------------------


def test_fragment_refused_meaning_inversion():
    """Substrings inside a segment (such as 'merge' out of 'do not merge') are refused."""
    owner_text = "Please do not merge until CI is green."

    # "merge until CI is green." is inside the segment, inverted meaning!
    m1 = quote.match_quote("merge until CI is green.", owner_text)
    assert m1.ok is False
    assert m1.kind == "fragment"
    assert m1.reason == "quote_fragment"

    # "do not merge" without the rest of the sentence
    m2 = quote.match_quote("do not merge until", owner_text)
    assert m2.ok is False
    assert m2.kind == "fragment"
    assert m2.reason == "quote_fragment"


def test_fragment_allowed_with_explicit_flag():
    """Fragments are accepted when allow_fragment=True (for audit display)."""
    owner_text = "Please do not merge until CI is green."
    m = quote.match_quote(
        "merge until CI is green.", owner_text, allow_fragment=True
    )
    assert m.ok is True
    assert m.kind == "fragment"
    assert m.reason is None


# -- Mismatch -----------------------------------------------------------------------------


def test_mismatch_completely_different_text():
    """Unrelated text gives kind='mismatch' and reason='quote_mismatch'."""
    owner_text = "Everything looks good to proceed with testing."
    claimed = "Deploy everything straight to production now."
    m = quote.match_quote(claimed, owner_text)
    assert m.ok is False
    assert m.kind == "mismatch"
    assert m.reason == "quote_mismatch"
