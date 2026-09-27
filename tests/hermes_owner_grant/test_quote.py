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
    # A line break is whitespace inside a sentence, never a boundary on its own.
    segments = quote.split_segments("line1\r\nline2\rline3\nline4")
    assert segments == ["line1 line2 line3 line4"]
    segments = quote.split_segments("Line one.\r\nLine two.\rLine three.\nLine four.")
    assert segments == ["Line one.", "Line two.", "Line three.", "Line four."]


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
    """A line break ends a segment only after a finished sentence."""
    owner_text = "First paragraph heading\nSecond paragraph content follows."
    m = quote.match_quote("First paragraph heading", owner_text)
    assert (m.ok, m.kind, m.reason) == (False, "fragment", "quote_fragment")
    m2 = quote.match_quote("Second paragraph content follows.", owner_text)
    assert (m2.ok, m2.kind, m2.reason) == (False, "fragment", "quote_fragment")

    owner_text = "First paragraph heading.\nSecond paragraph content follows."
    m = quote.match_quote("First paragraph heading.", owner_text)
    assert (m.ok, m.kind, m.segment_index) == (True, "segment", 0)
    m2 = quote.match_quote("Second paragraph content follows.", owner_text)
    assert (m2.ok, m2.kind, m2.segment_index) == (True, "segment", 1)


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
    """Sentence boundaries are [.!?] followed by whitespace; ';' and ':' are not."""
    for punct in [".", "!", "?", "?!"]:
        text = f"First action is done{punct} Second action starts now."
        m = quote.match_quote("Second action starts now.", text)
        assert m.ok is True
        assert m.kind == "segment"
        assert m.segment_index == 1
    for punct in [";", ":", ",", " -", " \u2014", "...", "\u2026"]:
        text = f"First action is done{punct} Second action starts now."
        m = quote.match_quote("Second action starts now.", text)
        assert (m.ok, m.reason) == (False, "quote_fragment"), punct


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


# -- Sentence segmentation: a line break is not a sentence boundary (review P1) -----------

_RELEASE = "ship the release to production."


def _refused(quote_text, owner_text):
    m = quote.match_quote(quote_text, owner_text)
    assert (m.ok, m.kind, m.reason) == (False, "fragment", "quote_fragment"), (
        quote_text,
        owner_text,
        m,
    )


def _segment(quote_text, owner_text):
    m = quote.match_quote(quote_text, owner_text)
    assert (m.ok, m.kind) == (True, "segment"), (quote_text, owner_text, m)
    return m


@pytest.mark.parametrize(
    "owner_text,claimed",
    [
        ("Do not\nship the release to production.", _RELEASE),
        ("Never\nmerge the pull request into main.", "merge the pull request into main."),
        (
            "I don't want you to\ndeploy the new build to prod.",
            "deploy the new build to prod.",
        ),
        ("Do NOT —\nmerge the pull request today.", "merge the pull request today."),
        ("Do NOT—\nmerge the pull request today.", "merge the pull request today."),
        # Unicode line/paragraph separators, NEL, CR, CRLF, VT and FF are line breaks too.
        ("Do not ship the release to production.", _RELEASE),
        ("Do not ship the release to production.", _RELEASE),
        ("Do not\u0085ship the release to production.", _RELEASE),
        ("Do not\rship the release to production.", _RELEASE),
        ("Do not\r\nship the release to production.", _RELEASE),
        ("Do not\x0bship the release to production.", _RELEASE),
        ("Do not\x0cship the release to production.", _RELEASE),
        ("Do not  ship the release to production.", _RELEASE),
        # A blank line (paragraph) after an unfinished sentence does not end it.
        ("Do not\n\nship the release to production.", _RELEASE),
        ("Do not\r\n\r\nship the release to production.", _RELEASE),
        ("> Do not\n>\n> ship the release to production.", _RELEASE),
        # A list introduced by an unfinished sentence stays governed by it.
        ("Do NOT:\n- ship the release to production.\n- merge the PR today.", _RELEASE),
        ("Do NOT:\n- ship the release to production.\n- merge the PR today.",
         "merge the PR today."),
        ("Never do these:\n\n1. ship the release to production.\n2. merge it.", _RELEASE),
        ("- Do not:\n  - ship the release to production.\n  - merge the PR today.",
         "merge the PR today."),
        ("Do not\n- ship the release to production.", _RELEASE),
        # Clause punctuation is not a sentence boundary.
        ("Do not do the following; ship the release to production.", _RELEASE),
        ("Do not do the following: ship the release to production.", _RELEASE),
        ("Do not do this — ship the release to production.", _RELEASE),
        ("Do not... ship the release to production.", _RELEASE),
        ("Do not… ship the release to production.", _RELEASE),
        ("Do not . . . ship the release to production.", _RELEASE),
        ("Do not... Ship the release to production.", "Ship the release to production."),
        # Abbreviations, initials and lower-case continuations are not sentence ends.
        ("Do not e.g. ship the release to production.", _RELEASE),
        ("Do not, i.e. ship the release to production.", _RELEASE),
        ("Never ask Dr. Ship the release to production.", "Ship the release to production."),
        ("Don't let J. Ship the release to production.", "Ship the release to production."),
        ("Do not stop.\nship the release to production.", _RELEASE),
        # Terminators inside brackets or an open quote do not end the sentence.
        ("Do not (!) Ship the release to production.", "Ship the release to production."),
        ("Do not (seriously!) Ship the release to production.",
         "Ship the release to production."),
        ('Never reply "OK. Ship the release to production." to anyone.',
         "Ship the release to production."),
        ("Never type 'ok. Ship the release to production.'",
         "Ship the release to production.'"),
        ("Never type \u2018ok. Ship the release to production.\u2019",
         "Ship the release to production.'"),
        ("Never type \u00abok. Ship the release to production.\u00bb",
         "Ship the release to production.\u00bb"),
        ("Never type \u300cok. Ship the release to production.\u300d",
         "Ship the release to production.\u300d"),
        # The next sentence must start with a capital or a digit, not a dash or ellipsis.
        ("Do not stop. \u2014 ship the release to production.",
         "\u2014 ship the release to production."),
        ("Do not stop. ...ship the release to production.",
         "...ship the release to production."),
        # Markdown headings, rules and HTML breaks do not create boundaries.
        ("# Do not\nship the release to production.", _RELEASE),
        ("Do not\n---\nship the release to production.", _RELEASE),
        ("Do not<br>ship the release to production.", _RELEASE),
        # Markdown emphasis and quote markers do not create boundaries.
        ("**Do not**\nship the release to production.", _RELEASE),
        ("_Never_\n> ship the release to production.", _RELEASE),
    ],
)
def test_split_points_that_are_not_sentence_boundaries_refuse_the_fragment(
    owner_text, claimed
):
    _refused(claimed, owner_text)


def test_review_repro_do_not_newline_ship_is_a_fragment():
    m = quote.match_quote(
        "ship the release to production.", "Do not\nship the release to production."
    )
    assert (m.ok, m.kind, m.reason) == (False, "fragment", "quote_fragment")


@pytest.mark.parametrize(
    "owner_text,claimed,index",
    [
        # A finished sentence followed by a line break still ends there.
        ("Hold the hotfix.\nShip the release to production.",
         "Ship the release to production.", 1),
        ("Hold the hotfix. Ship the release to production.",
         "Ship the release to production.", 1),
        ("Hold the hotfix!\r\nShip the release to production.",
         "Ship the release to production.", 1),
        ('He said "stop." Ship the release to production.',
         "Ship the release to production.", 1),
        ("Hold the hotfix (for now). Ship the release to production.",
         "Ship the release to production.", 1),
        ("**Hold the hotfix.** Ship the release to production.",
         "Ship the release to production.", 1),
        ("Is CI green? Ship the release to production.", "Ship the release to production.", 1),
        # Paragraphs after a finished sentence.
        ("Hold the hotfix.\n\nship the release to production.", _RELEASE, 1),
        ("Hold the hotfix. Ship the release to production.",
         "Ship the release to production.", 1),
        # Bullets: every finished item is its own segment.
        ("- ship the release to production.\n- merge the PR today.", _RELEASE, 0),
        ("- ship the release to production.\n- merge the PR today.",
         "merge the PR today.", 1),
        ("* ship the release to production.\n* merge the PR today.",
         "merge the PR today.", 1),
        ("• ship the release to production.\n• merge the PR today.",
         "merge the PR today.", 1),
        ("1. ship the release to production.\n2. merge the PR today.",
         "merge the PR today.", 1),
        ("1) ship the release to production.\n2) merge the PR today.",
         "merge the PR today.", 1),
        ("Approved.\n- ship the release to production.\n- merge the PR today.",
         "merge the PR today.", 2),
        ("- ship the release to production.\n\n- merge the PR today.",
         "merge the PR today.", 1),
        # A wrapped item continues onto the next line.
        ("- ship the release\n  to production.\n- merge the PR today.",
         "ship the release to production.", 0),
    ],
)
def test_real_sentence_paragraph_and_item_boundaries_still_match(
    owner_text, claimed, index
):
    assert _segment(claimed, owner_text).segment_index == index


def test_consecutive_bullets_match_as_consecutive_segments():
    owner_text = "- ship the release to production.\n- merge the PR today.\n- tag it now."
    m = _segment("ship the release to production. merge the PR today.", owner_text)
    assert m.segment_index == 0


def test_split_segments_examples():
    assert quote.split_segments("Do not\nship the release.") == [
        "Do not ship the release."
    ]
    assert quote.split_segments("Hold it.\nShip it now.") == ["Hold it.", "Ship it now."]
    assert quote.split_segments("- ship it.\n- merge it.") == ["ship it.", "merge it."]
    assert quote.split_segments("Do NOT:\n- ship it.\n- merge it.") == [
        "Do NOT: ship it. merge it."
    ]
    assert quote.split_segments("- ship A.\n- merge B.") == ["ship A.", "merge B."]
    # Inside prose, and before the first item, a single-letter initial is not a sentence end.
    assert quote.split_segments("Ask J.\nShip it now.") == ["Ask J. Ship it now."]
    assert quote.split_segments("Ask Dr.\n- ship it.\n- merge it.") == [
        "Ask Dr. ship it. merge it."
    ]
    assert quote.split_segments("- ship A\n- merge B") == ["ship A merge B"]
