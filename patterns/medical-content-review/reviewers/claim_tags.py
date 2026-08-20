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

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

# The claims report lives in the container, so it has to be keyed by session: an
# AgentCore Runtime container is reused across invocations, and a review without a
# claims library must never inherit the previous review's claims.
CLAIMS_REPORT_DIR = "/tmp"  # noqa: S108  # nosec B108
CLAIM_MATCH_STATUSES = ("exact", "partial", "none")

# The two kinds of exact match: approved copy reused as written, and approved copy whose
# words were rearranged. Both are the same approved claim, so both count as `exact`.
MATCH_PRECISIONS = ("verbatim", "reordered")

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
        not content.
    """
    lowered = str(text).lower()
    cleaned = []
    for index, char in enumerate(lowered):
        if char in FOOTNOTE_MARKERS:
            cleaned.append("")
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
    return " ".join(
        WORD_EQUIVALENTS.get(word, word) for word in "".join(cleaned).split()
    )


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
        `"verbatim"`, `"reordered"`, or `""` when the two are not the same words
    """
    if normalise(quote) == normalise(claim_text) and normalise(quote):
        return "verbatim"
    return "reordered" if same_wording(quote, claim_text) else ""


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


def claims_report_path(session_id: str) -> str:
    """Compute the container-local path of a session's claims report

    Kept here rather than in `reviewers._common` so that the tagging helpers stay
    importable without the Bedrock dependencies the reviewers pull in.

    Parameters
    ----------
    session_id : str
        The runtime session id the review is running under

    Returns
    -------
    str
        Path `get_claims` writes the session's claims report to
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id)) or "default"
    return f"{CLAIMS_REPORT_DIR}/claims_report_{safe}.json"


def claims_library_marker_path(session_id: str) -> str:
    """Compute the path of the marker saying this session has a claims library

    Parameters
    ----------
    session_id : str
        The runtime session id the review is running under

    Returns
    -------
    str
        Path of the session's claims-library marker
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id)) or "default"
    return f"{CLAIMS_REPORT_DIR}/claims_library_{safe}.json"


def set_claims_library_expected(session_id: str, claims_uri: str) -> None:
    """Record whether this review was given a claims library

    Written from the request payload, before the orchestrator runs, so that whether the
    claims workflow is mandatory is a fact about the request rather than something the
    model decides. The reviewers refuse to run without matched claims when it is set.

    Parameters
    ----------
    session_id : str
        The runtime session id the review is running under
    claims_uri : str
        S3 URI of the uploaded claims spreadsheet, empty when there is none
    """
    marker = Path(claims_library_marker_path(session_id))
    try:
        if claims_uri:
            marker.write_text(json.dumps({"claims_uri": claims_uri}))
        else:
            marker.unlink(missing_ok=True)
    except OSError as e:
        print(f"[CLAIMS] Could not record the claims library marker: {e}")


def claims_library_expected(session_id: str) -> bool:
    """Say whether this review was given a claims library, so matching is mandatory"""
    return Path(claims_library_marker_path(session_id)).exists()


def load_claim_records(session_id: str) -> list[dict]:
    """Load the tagged claims of this session, or an empty list if there are none

    Reads the report `get_claims` leaves in the container for this session. An empty
    list means the run had no claims library (or ran in a different container), in
    which case findings stay untagged — the fail-safe outcome, since tagging findings
    against another session's claims would be worse than not tagging them at all.

    Parameters
    ----------
    session_id : str
        The runtime session id the review is running under

    Returns
    -------
    list[dict]
        Claim records as written by `match_claims`
    """
    report_path = Path(claims_report_path(session_id))
    if not report_path.exists():
        return []
    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    claims = report.get("claims") if isinstance(report, dict) else report
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


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


def _best_claim(finding: dict, claims: list[tuple[dict, str]]) -> dict | None:
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

    A finding that already carries a valid `claim_match` (those the claim matcher
    produced) is left alone. Any other finding is matched against the tagged claims by
    its quote and inherits that claim's status. A finding whose quote is not one of the
    extracted claims is tagged `none`, which is what the library says about it: the
    text is not covered by an approved claim. `none` is not a violation.

    Parameters
    ----------
    findings : list[dict]
        Reviewer findings
    claims : list[dict]
        Claim records from `load_claim_records`

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
        known = status in CLAIM_MATCH_STATUSES

        # An exact match without its kind is also incomplete: the orchestrator may have
        # carried the status through and dropped the qualifier.
        if not known or (status == "exact" and not precision):
            claim = _best_claim(finding, pairs) or {}
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
