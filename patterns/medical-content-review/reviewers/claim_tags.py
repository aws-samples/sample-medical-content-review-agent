# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Attaches the pre-approved claim match tag to every finding, deterministically.

The reviewers are asked to carry `claim_match` on findings that concern a checked
claim, but a model may forget, so the tag cannot be left to the prompt. These helpers
re-derive it in Python by matching a finding's quote against the claims that
`match_claims` already tagged, so every finding in the published report carries one of
`exact` / `partial` / `none` whenever a claims library was used in the run.

Matching is fuzzy on purpose: an editorial finding quotes the content as written, typo
included ("Excelent Safety Profile"), while the claim record holds the same sentence in
full. Nothing here changes a tag the claim matcher itself produced.
"""

import re
from difflib import SequenceMatcher

CLAIM_MATCH_STATUSES = ("exact", "partial", "none")

# The `source` of the claim matcher's own findings, whose tags are the matcher's verdict
CLAIMS_SOURCE = "Pre-approved claims library"

# The kinds of exact match: approved copy reused as written, approved copy whose words
# were rearranged, and one line of a library entry that bundles several statements
# (a headline and its subline). All are approved wording, so all count as `exact`.
MATCH_PRECISIONS = ("verbatim", "reordered", "segment")

# A quote and a claim are the same assertion above this similarity. Set low enough to
# survive typos and truncated quotes, high enough that two different claims on one page
# do not collapse into each other.
MIN_SIMILARITY = 0.6

# Two words are the same word above this similarity, which is what lets "excelent" match
# "excellent" while keeping "placebo" and "placement" apart
TOKEN_SIMILARITY = 0.8

# Words shorter than this carry no signal ("the", "is", "of"), so they are ignored when
# scoring how much of a quote a claim covers
MIN_TOKEN_LENGTH = 4

# A quote has to be at least this long before being contained in a claim means anything:
# a one-word quote is inside half the library by accident
MIN_FRAGMENT_CHARS = 10

# Shorthand that means the same word to a reader, so writing it either way is still the
# approved wording. Deliberately tiny: anything beyond typography is a claim change and
# belongs in front of a reviewer.
WORD_EQUIVALENTS = {
    "&": "and",
    "vs": "versus",
}

# Superscript digits are alphanumeric to Python, so a footnote marker would otherwise
# stick to the word it hangs off ("1.8%¹" against "1.8%") and break the match
FOOTNOTE_MARKERS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

# Markers the claims export writes in plain text: a caret run ("HbA1c^1-5*") and HTML
# superscripts. Their digits would otherwise survive as words ("hba1c 1 5").
CARET_MARKER_RE = re.compile(r"\^[0-9,\-–*†‡§¶#®]*")
SUP_RE = re.compile(r"<sup>.*?</sup>", re.IGNORECASE | re.DOTALL)
HTML_TAG_RE = re.compile(r"</?[a-z][^>]*>", re.IGNORECASE)

# Layout notes in library entries ("[Headline]", "[Body copy]") describe placement, not
# wording. A bracket opening with a digit or "~" ("[98%]", "[~9 of 10]") is copy, so it
# is kept; a markdown link's text is kept too.
LAYOUT_TAG_RE = re.compile(r"\[[A-Za-z][^\[\]]{0,80}\](?!\()")

MIN_SEGMENT_WORDS = 3

# Signs that qualify the number after them ("~9 of 10", "≥50 kg", "P<0.0001") or before
# it ("10+ years"). Dropping one turns an approximate or bounded number into an exact one.
NUMBER_PREFIXES = {
    "~": "~",
    "∼": "~",
    "～": "~",
    "≥": "≥",
    "≤": "≤",
    "<": "<",
    ">": ">",
}
NUMBER_SUFFIX_RE = re.compile(r"(\d) *\+")


def strip_markup(text: object) -> str:
    """Remove footnote markup and layout notes, keeping line breaks

    Parameters
    ----------
    text : object
        Raw quote or claim text

    Returns
    -------
    str
        The text without caret markers, `<sup>` markers, HTML tags, or layout tags
    """
    cleaned = SUP_RE.sub("", str(text))
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    cleaned = CARET_MARKER_RE.sub("", cleaned)
    return LAYOUT_TAG_RE.sub(" ", cleaned)


def segments(text: object) -> list[str]:
    """Split a library entry into its separately laid-out statements

    Parameters
    ----------
    text : object
        Raw library claim text

    Returns
    -------
    list[str]
        Normalised lines of at least :data:`MIN_SEGMENT_WORDS` words; icon labels such
        as "Fast" or "Safe" are too short to stand for a statement
    """
    lines: list[str] = []
    for line in strip_markup(text).splitlines():
        line = line.strip()
        # A line opening in lower case continues a sentence the export wrapped
        if lines and line[:1].islower():
            lines[-1] += " " + line
        elif line:
            lines.append(line)
    parts = (normalise(line) for line in lines)
    return [part for part in parts if len(part.split()) >= MIN_SEGMENT_WORDS]


def normalise(text: object) -> str:
    """Reduce a quote or claim to a comparable form

    Parameters
    ----------
    text : object
        Raw quote or claim text

    Returns
    -------
    str
        Lower-cased text with punctuation and repeated whitespace collapsed, and
        shorthand spelled out. `%` and decimal points survive, because "1.8%" and "18%"
        are different claims, but a sentence-ending period does not: it is formatting,
        not content. So do the signs in :data:`NUMBER_PREFIXES` and a `+` after a number.
        Footnote markup and layout tags are dropped first.
    """
    lowered = NUMBER_SUFFIX_RE.sub(r"\1+ ", strip_markup(text).lower())
    # "[10]+ years" is "10+ years" with the number marked as a placeholder
    lowered = re.sub(r"(\d)\]\+", r"\1+ ", lowered)
    cleaned = []
    for index, char in enumerate(lowered):
        if char in FOOTNOTE_MARKERS:
            cleaned.append("")
        elif char in NUMBER_PREFIXES and lowered[index + 1 :].lstrip()[:1].isdigit():
            # its own token start, so "P<0.0001" and "*P*<0.0001" split alike
            cleaned.append(" " + NUMBER_PREFIXES[char])
        elif char == "+" and lowered[index - 1 : index].isdigit():
            cleaned.append(char)
        elif char.isalnum() or char == "%":
            cleaned.append(char)
        elif char == "&":
            # kept as a word of its own, to be spelled out below
            cleaned.append(" & ")
        elif char == "." and index and lowered[index - 1].isdigit():
            # a decimal point, as opposed to the end of a sentence
            cleaned.append(char if lowered[index + 1 : index + 2].isdigit() else " ")
        else:
            cleaned.append(" ")
    joined = re.sub(r"([~≥≤<>]) +(?=\d)", r"\1", "".join(cleaned))
    return " ".join(WORD_EQUIVALENTS.get(word, word) for word in joined.split())


def wording_key(text: object) -> tuple[str, ...]:
    """Reduce a claim to the words it is made of, order set aside

    Approved wording survives having its clauses swapped: "Versus placebo, HbA1c was
    reduced by 1.8%" is the same approved sentence as "HbA1c was reduced by 1.8% versus
    placebo", so the two must key alike and count as the same claim.

    Parameters
    ----------
    text : object
        Raw claim or quote text

    Returns
    -------
    tuple[str, ...]
        Sorted words of the normalised text
    """
    return tuple(sorted(normalise(text).split()))


def match_precision(quote: object, claim_text: object) -> str:
    """Say which kind of exact match two texts are, if they are one at all

    An approved wording can be reused as written or with its clauses rearranged. Both
    are the same approved claim, but they are worth telling apart in the report: the
    first is untouched approved copy, the second was edited, even if only in layout.

    Parameters
    ----------
    quote : object
        Claim text as it appears in the content
    claim_text : object
        Approved claim text from the library

    Returns
    -------
    str
        `"verbatim"`, `"reordered"`, `"segment"` when the quote is one line of a
        multi-line entry, or `""` when the two are not the same words
    """
    if normalise(quote) == normalise(claim_text) and normalise(quote):
        return "verbatim"
    if same_wording(quote, claim_text):
        return "reordered"
    return "segment" if is_segment(quote, claim_text) else ""


def is_segment(quote: object, claim_text: object) -> bool:
    """Say whether a quote is one line of a multi-line library entry, in any order"""
    parts = segments(claim_text)
    key = wording_key(quote)
    return len(parts) > 1 and bool(key) and any(wording_key(p) == key for p in parts)


def same_wording(first: object, second: object) -> bool:
    """Say whether two texts are the same claim word for word, in any order

    Parameters
    ----------
    first : object
        A claim or quote
    second : object
        The claim or quote to compare it with

    Returns
    -------
    bool
        True when both use exactly the same words, False when either is empty
    """
    key = wording_key(first)
    return bool(key) and key == wording_key(second)


# Spans covering more of a quote than this are the whole quote, not the words at issue
MAX_SPAN_SHARE = 0.6
MAX_SPANS = 5
# A longer run is an added passage, which bold type would not make easier to read
MAX_SPAN_WORDS = 8
HEADLINE_WORDS = 12


def raw_spans(quote: object, runs: list[list[str]]) -> list[str]:
    """Map runs of normalised words back to the quote's own text

    Parameters
    ----------
    quote : object
        The finding's quote as written
    runs : list[list[str]]
        Runs of consecutive words in `normalise` form

    Returns
    -------
    list[str]
        The quote's substring for each run found in it, in quote order, without
        duplicates
    """
    quote = str(quote or "")
    tokens: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\S+", quote):
        for word in normalise(match.group()).split():
            tokens.append((word, match.start(), match.end()))
    words = [word for word, _, _ in tokens]
    found: list[tuple[int, str]] = []
    for run in runs:
        size = len(run)
        if not size or size > len(words):
            continue
        for i in range(len(words) - size + 1):
            if words[i : i + size] == run:
                text = quote[tokens[i][1] : tokens[i + size - 1][2]]
                text = text.strip(" ,.;:!?()[]\"'“”‘’*")
                if text and all(text != t for _, t in found):
                    found.append((tokens[i][1], text))
                break
    return [text for _, text in sorted(found)]


# A number as `normalise` leaves it ("84%", "~9", "10+", "<0.0001"), not a name that
# contains digits ("17a", "23i")
NUMBER_TOKEN_RE = re.compile(r"^[~≥≤<>]?\d+(?:[.,]\d+)*[%+]?$")
# Longer replaced spans are rewordings, which the deviation describes
MAX_NUMERIC_SPAN = 3


def aligned(text: str, approved: str) -> SequenceMatcher:
    """Align the content's words with the closest line of the approved wording

    A bundled entry is compared line by line, so its other lines do not count as dropped.
    """
    content = normalise(text).split()
    library = max(
        (part.split() for part in segments(approved) or [normalise(approved)]),
        key=lambda words: SequenceMatcher(None, words, content).ratio(),
    )
    return SequenceMatcher(None, library, content, autojunk=False)


def numeric_changes(text: str, approved: str) -> list[str]:
    """List the numbers the content changed, as "'2 years' became '3 years'"

    Only replaced numbers count: a number the content drops or adds along with its
    words is a dropped or added qualifier, which the deviation already describes.
    """
    matcher = aligned(text, approved)
    library, content = matcher.a, matcher.b
    changes: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        was, now = library[i1:i2], content[j1:j2]
        numbers_was = [w for w in was if NUMBER_TOKEN_RE.match(w)]
        numbers_now = [w for w in now if NUMBER_TOKEN_RE.match(w)]
        if (
            op == "replace"
            and max(len(was), len(now)) <= MAX_NUMERIC_SPAN
            and numbers_was
            and numbers_now
            and numbers_was != numbers_now
        ):
            # the unchanged word after the number says what it counts ("years")
            unit = (
                library[i2 : i2 + 1] if i2 < len(library) and j2 < len(content) else []
            )
            if unit and content[j2] != unit[0]:
                unit = []
            changes.append(f"'{' '.join(was + unit)}' became '{' '.join(now + unit)}'")
    return changes


def changed_spans(
    text: object, approved: object, matcher: SequenceMatcher | None = None
) -> list[str]:
    """The words of `text` that replace or add to the approved wording

    Parameters
    ----------
    text : object
        The content's wording
    approved : object
        The approved wording it deviates from
    matcher : SequenceMatcher | None
        An alignment of the approved words (a) with the content's (b), both in
        `normalise` form, when the caller has a better one than the plain word diff

    Returns
    -------
    list[str]
        Substrings of `text`; empty when nothing was added or replaced, or when the
        changes are most of the text
    """
    if matcher is None:
        matcher = SequenceMatcher(
            None, normalise(approved).split(), normalise(text).split(), autojunk=False
        )
    content = list(matcher.b)
    runs = [
        content[j1:j2]
        for op, _, _, j1, j2 in matcher.get_opcodes()
        if op in ("replace", "insert")
    ]
    if not content or sum(map(len, runs)) > MAX_SPAN_SHARE * len(content):
        return []
    return raw_spans(text, [r for r in runs if len(r) <= MAX_SPAN_WORDS])[:MAX_SPANS]


def quoted_spans(quote: object, spans: object) -> list[str]:
    """Keep the spans a model named that are words of the quote, in the quote's text"""
    if not isinstance(spans, list):
        return []
    runs = [normalise(span).split() for span in spans if isinstance(span, str)]
    found = raw_spans(quote, [r for r in runs if len(r) <= MAX_SPAN_WORDS])
    total = len(normalise(quote).split())
    if total and sum(len(normalise(s).split()) for s in found) > MAX_SPAN_SHARE * total:
        return []
    return found[:MAX_SPANS]


def short_headline(text: object) -> str:
    """The first sentence of a finding's issue, cut to a headline's length"""
    sentence = re.split(r"(?<=[.!?])\s+", str(text or "").strip(), maxsplit=1)[0]
    words = sentence.split()
    if len(words) <= HEADLINE_WORDS:
        return sentence.rstrip(".")
    return " ".join(words[:HEADLINE_WORDS]).rstrip(",;:.") + " …"


def readable(findings: list[dict]) -> list[dict]:
    """Give every finding a `headline` and `issue_spans` the quote holds"""
    out = []
    for finding in findings:
        finding = dict(finding)
        headline = str(finding.get("headline") or "").strip()
        finding["headline"] = (
            short_headline(headline)
            if headline
            else short_headline(finding.get("issue"))
        )
        finding["issue_spans"] = quoted_spans(
            finding.get("quote"), finding.get("issue_spans")
        )
        out.append(finding)
    return out


def _coverage(short: list[str], long: list[str]) -> float:
    """Score what share of the shorter word list appears in the longer one

    Whole-string similarity alone is not enough: a finding often quotes a fragment of a
    claim ("Excelent Safety Profile" out of a full sentence), and the length difference
    drags the ratio below any usable threshold. Comparing word by word instead keeps a
    fragment scoring high, while a quote about something else scores near zero.

    Parameters
    ----------
    short : list[str]
        Words of the shorter text
    long : list[str]
        Words of the longer text

    Returns
    -------
    float
        Share of the shorter text's content words found in the longer text, 0 when the
        shorter text has too few content words to judge
    """
    content = [word for word in short if len(word) >= MIN_TOKEN_LENGTH]
    if len(content) < 2:
        return 0.0
    matched = sum(
        any(
            SequenceMatcher(None, word, other).ratio() >= TOKEN_SIMILARITY
            for other in long
        )
        for word in content
    )
    return matched / len(content)


def _similarity(quote: str, claim_text: str) -> float:
    """Score how much a finding's quote and a claim overlap, from 0 to 1"""
    if not quote or not claim_text:
        return 0.0
    if quote == claim_text:
        return 1.0

    quote_words, claim_words = quote.split(), claim_text.split()
    shorter_is_quote = len(quote_words) <= len(claim_words)
    short = quote_words if shorter_is_quote else claim_words
    long = claim_words if shorter_is_quote else quote_words
    shorter_text = quote if shorter_is_quote else claim_text

    contained = quote in claim_text or claim_text in quote
    if contained and len(short) >= 2 and len(shorter_text) >= MIN_FRAGMENT_CHARS:
        return 1.0
    return max(_coverage(short, long), SequenceMatcher(None, quote, claim_text).ratio())


def best_claim(finding: dict, claims: list[tuple[dict, str]]) -> dict | None:
    """Find the claim a finding is about, preferring claims on the same page

    A claim quoted as it was extracted wins outright, before any fuzzy scoring: two
    claim records can hold the same sentence rearranged, and the finding belongs to the
    one it actually quotes.

    Parameters
    ----------
    finding : dict
        A reviewer finding, whose `quote` holds the content it is about
    claims : list[tuple[dict, str]]
        Claim records paired with their normalised text

    Returns
    -------
    dict | None
        The matching claim record, or None when the quote is not one of the claims
    """
    quote = normalise(finding.get("quote"))
    if not quote:
        return None

    page = finding.get("page")
    same_page = [pair for pair in claims if pair[0].get("page") == page]
    ordered = same_page + [pair for pair in claims if pair[0].get("page") != page]

    for claim, claim_text in ordered:
        if claim_text == quote:
            return claim
    quote_key = wording_key(quote)
    for claim, claim_text in ordered:
        if wording_key(claim_text) == quote_key:
            return claim

    for candidates in (same_page, claims):
        best: dict | None = None
        best_score = MIN_SIMILARITY
        for claim, claim_text in candidates:
            score = _similarity(quote, claim_text)
            if score > best_score:
                best, best_score = claim, score
        if best:
            return best
    return None


def tag_findings(findings: list[dict], claims: list[dict]) -> list[dict]:
    """Give every finding a claim match tag

    A finding the claim matcher produced keeps its tag. Any other finding is matched
    against the tagged claims by its quote and inherits that claim's status, whatever
    tag the reviewer gave it: a reviewer guessing "none" for approved copy would
    otherwise send approved wording back for substantiation. A finding whose quote is
    not one of the extracted claims is tagged `none`, which is what the library says
    about it: the text is not covered by an approved claim. `none` is not a violation.

    Parameters
    ----------
    findings : list[dict]
        Reviewer findings
    claims : list[dict]
        Claim records from the claims report

    Returns
    -------
    list[dict]
        The findings, each with `claim_match`, `claim_id`, and `claim_precision` set.
        Returned unchanged when the run had no claims library.
    """
    if not claims:
        return findings

    pairs = [(claim, normalise(claim.get("text"))) for claim in claims]
    tagged: list[dict] = []
    for finding in findings:
        status = str(finding.get("claim_match") or "").strip().lower()
        claim_id = str(finding.get("claim_id") or "")
        precision = str(finding.get("claim_precision") or "")
        # A finding without a quote (an asset-level one) has nothing to match on
        known = status in CLAIM_MATCH_STATUSES and (
            finding.get("source") == CLAIMS_SOURCE
            or not normalise(finding.get("quote"))
        )

        # An exact match without its kind is also incomplete: a reviewer may have
        # carried the status through and dropped the qualifier.
        if not known or (status == "exact" and not precision):
            claim = best_claim(finding, pairs) or {}
            if not known:
                status = str(claim.get("match_status") or "none")
                claim_id = str(claim.get("matched_claim_id") or "")
            precision = str(claim.get("match_precision") or "")

        tagged.append(
            {
                **finding,
                "claim_match": status,
                "claim_id": claim_id,
                # Only an exact match has a kind; anything else carries none
                "claim_precision": precision if status == "exact" else "",
            }
        )
    return tagged
