# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claim matcher: checks extracted claims against the human-curated claims library.

The library is read-only. Matching never approves a claim and never adds one to the
library — it only decides how much further checking a claim needs:

* `exact`   a human has already cleared this wording -> no further substantiation. Comes
            in two kinds, carried in `match_precision`: `verbatim` (the approved copy as
            written) and `reordered` (the same words, clauses rearranged)
* `partial` the same assertion with deviated wording -> flagged for a human reviewer
* `none`    the library does not cover this assertion -> routed to the reference and
            external-evidence reviewers for verification

A third kind of exact match, `segment`, is one line of a library entry that bundles
several (a headline and its subline). The asset's audience (HCP or patient, decided by
the batcher) limits which library claims a partial match may name, and decides between
rows that carry the same wording.
"""

import datetime as dt
import json
import os

from strands import tool
from tools.asset_audience import AUDIENCE_LABELS, approved_for, read_asset_audience
from tools.run_record import note_issue

from reviewers._common import (
    batch_stem,
    load_prompt,
    parse_tagged_json_array,
    read_s3_json,
    run_inner_agent_text,
    write_claims_json,
    write_review_json,
)
from reviewers.claim_tags import (
    CLAIM_MATCH_STATUSES,
    CLAIMS_SOURCE,
    MATCH_PRECISIONS,
    aligned,
    changed_spans,
    is_segment,
    match_precision,
    normalise,
    numeric_changes,
    same_wording,
    segments,
    wording_key,
)

SYSTEM_PROMPT_TEMPLATE = load_prompt("claim_matcher")

MAX_LIBRARY_CLAIMS = 400

# Library statuses that mean the claim may no longer be used in new content.
UNUSABLE_STATUSES = {
    "withdrawn",
    "retired",
    "rejected",
    "expired",
    "superseded",
    "draft",
    "pending",
    "in review",
    "work in progress",
}

PARTIAL_SCORE = 75
UNUSABLE_SCORE = 85

# model similarity (0-100) a partial match needs, else `none`; tune on a labeled set
PARTIAL_MIN_SIMILARITY = 50
# wording-only partials need more (top false-positive source); number changes ~65
WORDING_PARTIAL_MIN_SIMILARITY = 75

# overrides the library's export date as the day expiry is judged on (ISO date)
AS_OF_ENV = "CLAIMS_AS_OF_DATE"


def _as_of_date(library_claim: dict) -> dt.date:
    """Pick the day expiry is judged on: the override, else the export date, else today

    Judging against today would make a rerun of the same asset give a different result
    once an approved claim's expiry date passes, so the export date is the default.

    Parameters
    ----------
    library_claim : dict
        A claim record produced by `load_claims_library`, which may carry the
        library's export date in `library_as_of`

    Returns
    -------
    datetime.date
        The date set in :data:`AS_OF_ENV`, else the export date, else today
    """
    for value in (os.environ.get(AS_OF_ENV), library_claim.get("library_as_of")):
        try:
            return dt.date.fromisoformat(str(value or "")[:10])
        except ValueError:
            continue
    return dt.date.today()


def _is_usable(library_claim: dict) -> tuple[bool, str]:
    """Decide whether a library claim may still be used, and say why not.

    Parameters
    ----------
    library_claim : dict
        A claim record produced by `load_claims_library`.

    Returns
    -------
    tuple[bool, str]
        `(usable, reason)`. `reason` is empty when the claim is usable.
    """
    status = (library_claim.get("status") or "Approved").strip()
    if status.lower() in UNUSABLE_STATUSES:
        return False, f"the approved claim was marked '{status}'"

    expiry = (library_claim.get("expiry_date") or "").strip()
    if expiry:
        try:
            expiry_date = dt.date.fromisoformat(expiry[:10])
        except ValueError:
            return True, ""
        if expiry_date < _as_of_date(library_claim):
            return False, f"the approved claim expired on {expiry_date.isoformat()}"
    return True, ""


def _audience_reason(library_claim: dict, audience: str) -> str:
    """Say why a claim may not be used for the asset's audience, or "" when it may

    Parameters
    ----------
    library_claim : dict
        A claim record produced by `load_claims_library`
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown)

    Returns
    -------
    str
        The reason the claim is not approved for the audience, or "" when it is
    """
    if approved_for(library_claim, audience):
        return ""
    return (
        f"it is approved for {library_claim.get('audience')} audiences, not for this"
        f" {AUDIENCE_LABELS[audience]} asset"
    )


def _preferred(first: dict, second: dict, audience: str = "") -> dict:
    """Pick the library row to report when two rows carry the same wording

    The export keeps old versions next to current ones, so the same words can sit on a
    Work in Progress row and on its Approved twin, or on a Physician row and on its
    Patient twin. Reporting the unusable one would flag approved copy as unusable.

    Parameters
    ----------
    first : dict
        The row kept so far
    second : dict
        Another row with the same wording
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown)

    Returns
    -------
    dict
        `second` when it ranks higher on usability, then audience approval, else
        `first`
    """

    def rank(claim: dict) -> tuple[bool, bool]:
        """Rank a row by whether it is usable, then approved for the audience

        Parameters
        ----------
        claim : dict
            A library row

        Returns
        -------
        tuple[bool, bool]
            `(usable, approved_for_audience)`, compared in that order
        """
        return _is_usable(claim)[0], approved_for(claim, audience)

    return second if rank(second) > rank(first) else first


def _text_similarity(text: str, approved: str) -> int:
    """Score the word overlap of content and approved wording from 0 to 100

    Parameters
    ----------
    text : str
        The content's wording
    approved : str
        The approved wording

    Returns
    -------
    int
        100 for the same words or one line of a multi-line entry, else the rounded
        word-alignment ratio
    """
    if same_wording(text, approved) or is_segment(text, approved):
        return 100
    return round(100 * aligned(text, approved).ratio())


def _word_delta(text: str, approved: str) -> str:
    """Describe in words what the content changed relative to the approved wording

    Parameters
    ----------
    text : str
        The content's wording
    approved : str
        The approved wording

    Returns
    -------
    str
        The replaced, dropped, and added words as one sentence, or a note that only
        the layout differs
    """
    matcher = aligned(text, approved)
    library, content = matcher.a, matcher.b
    changes: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        was, now = " ".join(library[i1:i2]), " ".join(content[j1:j2])
        if op == "replace":
            changes.append(f"'{was}' became '{now}'")
        elif op == "delete":
            changes.append(f"'{was}' was dropped")
        elif op == "insert":
            changes.append(f"'{now}' was added")
    if not changes:
        return "the wording differs from the approved claim only in its layout."
    return "; ".join(changes) + "."


def _change_headline(text: str, approved: str, claim_id: str) -> str:
    """The first change from the approved wording, when it is short enough to headline

    Parameters
    ----------
    text : str
        The content's wording
    approved : str
        The approved wording
    claim_id : str
        Id of the approved claim, named in the headline

    Returns
    -------
    str
        The first change with the claim id, or a generic headline when that change is
        long or contains a number
    """
    first = _word_delta(text, approved).split("; ")[0].rstrip(".")
    if (
        first.startswith("'")
        and len(first.split()) <= 8
        and not any(char.isdigit() for char in first)
    ):
        return f"{first} ({claim_id})"
    return f"Wording differs from {claim_id}"


def _render_library(library: list[dict]) -> str:
    """Render the library as a compact, id-anchored block for the system prompt."""
    lines: list[str] = []
    for claim in library[:MAX_LIBRARY_CLAIMS]:
        parts = [f'- {claim["claim_id"]} | "{claim["claim_text"]}"']
        meta = [
            f"type: {claim.get('claim_type')}" if claim.get("claim_type") else "",
            f"status: {claim.get('status')}" if claim.get("status") else "",
            f"audience: {claim.get('audience')}" if claim.get("audience") else "",
        ]
        meta_text = ", ".join(m for m in meta if m)
        if meta_text:
            parts.append(f"  ({meta_text})")
        if claim.get("restrictions"):
            parts.append(f"  restrictions: {claim['restrictions']}")
        lines.append("\n".join(parts))
    if len(library) > MAX_LIBRARY_CLAIMS:
        lines.append(
            f"- (… {len(library) - MAX_LIBRARY_CLAIMS} further claims omitted)"
        )
    return "\n".join(lines)


def _exact_verdicts(
    claims: list[dict], library: list[dict], audience: str = ""
) -> tuple[list[dict], list[dict]]:
    """Settle reuse of an approved wording in Python, before any model call.

    `exact` means a human already cleared this wording, which is word-for-word equality
    once case, punctuation, and word order are set aside — not a judgement call. Word
    order is set aside because approved copy is routinely re-laid-out ("Versus placebo,
    HbA1c was reduced by 1.8%" against "HbA1c was reduced by 1.8% versus placebo") and
    the claim being made is the same one. Deciding this here makes the common case
    reproducible and keeps the model's context down to the claims that really need a
    paraphrase judgement.

    Parameters
    ----------
    claims : list[dict]
        Extracted claim records from `extract_claims`.
    library : list[dict]
        Library claims from `load_claims_library`, of every audience: reusing a claim
        approved for another audience is an exact match that gets its own finding.
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown), which decides
        between rows carrying the same wording.

    Returns
    -------
    tuple[list[dict], list[dict]]
        `(verdicts, unresolved)`: verdicts for the claims that reuse an approved
        wording, and the claims still needing a semantic comparison. A verdict on one
        line of a multi-line entry carries the entries sharing that line in
        `candidates`, for :func:`_settle_shared_lines` to choose from.
    """
    by_words: dict[tuple[str, ...], dict] = {}
    # each line of a multi-line entry, keyed like a whole claim, to the entries using it
    by_segment: dict[tuple[str, ...], list[dict]] = {}
    for library_claim in library:
        text = normalise(library_claim.get("claim_text"))
        if not text or not library_claim.get("claim_id"):
            continue
        key = wording_key(text)
        by_words[key] = _preferred(
            by_words.get(key, library_claim), library_claim, audience
        )
        parts = segments(library_claim.get("claim_text"))
        if len(parts) > 1:
            for part in parts:
                by_segment.setdefault(wording_key(part), []).append(library_claim)

    verdicts: list[dict] = []
    unresolved: list[dict] = []
    for claim in claims:
        text = normalise(claim.get("text"))
        key = wording_key(text)
        match = by_words.get(key) if text else None
        candidates = by_segment.get(key, []) if text and not match else []
        if candidates:
            verdicts.append(
                {
                    "claim_ref": claim["claim_ref"],
                    "match_status": "exact",
                    "candidates": candidates,
                }
            )
        elif match:
            claim_id = str(match["claim_id"])
            library_text = normalise(match.get("claim_text"))
            verdicts.append(
                {
                    "claim_ref": claim["claim_ref"],
                    "match_status": "exact",
                    "matched_claim_id": claim_id,
                    # Recorded for the claims report only: reordering does not need a
                    # reviewer, but it is worth showing that the copy was rearranged.
                    "deviation": (
                        ""
                        if text == library_text
                        else "the approved wording, with the words in a different order"
                    ),
                }
            )
        else:
            unresolved.append(claim)
    return verdicts, unresolved


def _prefer_closest_sibling(
    verdicts: list[dict],
    claims: list[dict],
    library_by_id: dict[str, dict],
    audience: str = "",
) -> None:
    """Move a partial match to the sibling entry closest in wording, in place

    Entries that share a line are versions of one claim ("Fast relief" and "Fast relief
    that lasts all day", each over the same subline). The model can pick the shorter
    one for "FAST RELIEF THAT LASTS ALL NIGHT" while naming "lasts all night" as the
    change, so the sibling whose wording is closest wins, if it is usable or the
    model's pick is not. A sibling approved for another audience never wins.

    Parameters
    ----------
    verdicts : list[dict]
        Match verdicts, updated in place
    claims : list[dict]
        Extracted claim records of the batch
    library_by_id : dict[str, dict]
        Library claims keyed by `claim_id`
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown)
    """
    by_line: dict[tuple[str, ...], list[dict]] = {}
    for library_claim in library_by_id.values():
        parts = segments(library_claim.get("claim_text"))
        if len(parts) > 1:
            for part in parts:
                by_line.setdefault(wording_key(part), []).append(library_claim)
    text_by_ref = {claim["claim_ref"]: claim["text"] for claim in claims}

    for verdict in verdicts:
        picked = library_by_id.get(str(verdict.get("matched_claim_id") or ""))
        text = text_by_ref.get(str(verdict.get("claim_ref") or ""))
        if verdict.get("match_status") != "partial" or not picked or not text:
            continue
        siblings = {
            str(sibling["claim_id"]): sibling
            for part in segments(picked.get("claim_text"))
            for sibling in by_line.get(wording_key(part), [])
            if sibling is not picked
            and approved_for(sibling, audience)
            and (_is_usable(sibling)[0] or not _is_usable(picked)[0])
        }
        if not siblings:
            continue
        best = max(
            siblings.values(),
            key=lambda c: _text_similarity(text, c.get("claim_text", "")),
        )
        if _text_similarity(text, best.get("claim_text", "")) > _text_similarity(
            text, picked.get("claim_text", "")
        ):
            verdict["matched_claim_id"] = str(best["claim_id"])
            # the model's sentence describes the entry it picked
            verdict["deviation"] = _word_delta(text, best.get("claim_text", ""))


def _settle_shared_lines(
    verdicts: list[dict], claims: list[dict], audience: str = ""
) -> None:
    """Attribute each line of a multi-line entry to one entry, in place

    A line shared by several entries ("Improves every key symptom of the disease" under
    two headlines) belongs to the entry whose other lines are also in this batch, then
    to an entry another claim of the batch matched (a reworded headline is a partial
    match to its own entry), then to one approved for the asset's audience, then to a
    usable one.

    Parameters
    ----------
    verdicts : list[dict]
        Match verdicts, updated in place; their `candidates` are removed
    claims : list[dict]
        Extracted claim records of the batch
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown)
    """
    batch_keys = {wording_key(claim.get("text")) for claim in claims}
    matched = {str(v.get("matched_claim_id") or "") for v in verdicts} - {""}

    def coverage(library_claim: dict) -> float:
        """Measure the share of an entry's lines that are claims in this batch

        Parameters
        ----------
        library_claim : dict
            A multi-line library entry

        Returns
        -------
        float
            Fraction of the entry's lines found in the batch, from 0 to 1
        """
        parts = segments(library_claim.get("claim_text"))
        return sum(wording_key(p) in batch_keys for p in parts) / len(parts)

    for verdict in verdicts:
        candidates = verdict.pop("candidates", None)
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda c: (
                coverage(c),
                str(c["claim_id"]) in matched,
                approved_for(c, audience),
                _is_usable(c)[0],
                -len(segments(c.get("claim_text"))),
            ),
        )
        missing = [
            part
            for part in segments(best.get("claim_text"))
            if wording_key(part) not in batch_keys
        ]
        verdict["matched_claim_id"] = str(best["claim_id"])
        verdict["deviation"] = (
            "one line of a multi-line approved claim; its other lines are not in this"
            " batch: " + "; ".join(f"'{part}'" for part in missing)
            if missing
            else ""
        )


def _score(value: object) -> int | None:
    """Read a 0-100 score from a model verdict, or None when it gave none

    Parameters
    ----------
    value : object
        The verdict's score as the model wrote it

    Returns
    -------
    int | None
        The score rounded and clamped to 0-100, or None when it is not a number
    """
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return None


def _build_match_records(
    claims: list[dict],
    verdicts: list[dict],
    library_by_id: dict[str, dict],
    audience: str = "",
) -> list[dict]:
    """Join the model's verdicts onto the extracted claims.

    Claims the model failed to return a verdict for fall back to `none`, so a claim
    can never be silently dropped or silently treated as approved.

    Parameters
    ----------
    claims : list[dict]
        Extracted claim records from `extract_claims`.
    verdicts : list[dict]
        Match verdicts emitted by the matcher sub-agent.
    library_by_id : dict[str, dict]
        Library claims keyed by `claim_id`.
    audience : str
        The asset's audience ("hcp", "patient", or "" when unknown). A partial match
        needs a claim approved for it; an exact reuse of a claim approved for another
        audience is recorded as not usable for this asset.

    Returns
    -------
    list[dict]
        One match record per extracted claim.
    """
    # First verdict per claim wins, so the deterministic ones cannot be overridden by a
    # model verdict on a claim it was not asked about.
    by_ref: dict[str, dict] = {}
    for verdict in verdicts:
        ref = str(verdict.get("claim_ref") or "")
        if ref:
            by_ref.setdefault(ref, verdict)

    # the row each wording should be reported against when it appears more than once
    twins: dict[tuple[str, ...], dict] = {}
    for library_claim in library_by_id.values():
        key = wording_key(library_claim.get("claim_text"))
        twins[key] = _preferred(twins.get(key, library_claim), library_claim, audience)

    records: list[dict] = []
    for claim in claims:
        verdict = by_ref.get(claim["claim_ref"], {})
        status = str(verdict.get("match_status") or "none").strip().lower()
        if status not in CLAIM_MATCH_STATUSES:
            status = "none"

        matched_id = str(verdict.get("matched_claim_id") or "").strip()
        library_claim = library_by_id.get(matched_id)
        deviation = str(verdict.get("deviation") or "").strip()
        nearest_id = str(verdict.get("nearest_claim_id") or "").strip()
        similarity = _score(verdict.get("similarity"))
        if "similarity" not in verdict and status == "exact":
            # settled in code: the approved wording itself
            similarity = 100
        threshold = (
            PARTIAL_MIN_SIMILARITY
            if library_claim
            and numeric_changes(claim["text"], library_claim.get("claim_text", ""))
            else WORDING_PARTIAL_MIN_SIMILARITY
        )
        if (
            status == "partial"
            and library_claim
            and similarity is not None
            and similarity < threshold
        ):
            status, nearest_id, library_claim = "none", matched_id, None
            deviation = (
                f"closest to {matched_id} (similarity {similarity}), below the"
                f" partial-match threshold of {threshold}: {deviation}"
            )
        if library_claim:
            library_claim = twins.get(
                wording_key(library_claim.get("claim_text")), library_claim
            )
            matched_id = str(library_claim["claim_id"])
        if (
            status == "partial"
            and library_claim
            and _audience_reason(library_claim, audience)
        ):
            # the deviation's fix would put another audience's wording into this asset
            deviation = (
                f"closest to {matched_id}, but {_audience_reason(library_claim, audience)}:"
                f" {deviation}"
            )
            status, nearest_id, library_claim = "none", matched_id, None
        if not library_claim:
            # A hallucinated or missing id cannot be trusted as a match.
            matched_id = ""
            if status != "none":
                status = "none"
        elif (
            status == "exact"
            and not same_wording(claim["text"], library_claim.get("claim_text"))
            and not is_segment(claim["text"], library_claim.get("claim_text"))
        ):
            # `exact` carries a human's sign-off on this wording, so it is only granted
            # when the same words are used, in whatever order. A model calling a
            # paraphrase exact would skip the reviewer the deviation should reach.
            status = "partial"
            deviation = deviation or _word_delta(
                claim["text"], library_claim.get("claim_text", "")
            )
        if status == "partial" and similarity == 100:
            similarity = 99

        usable, unusable_reason = (
            _is_usable(library_claim) if library_claim else (True, "")
        )
        audience_reason = (
            _audience_reason(library_claim, audience) if library_claim else ""
        )
        if usable and audience_reason:
            usable, unusable_reason = False, audience_reason
        nearest = library_by_id.get(nearest_id) if status == "none" else None
        compared = library_claim or nearest
        changes = (
            numeric_changes(claim["text"], library_claim.get("claim_text", ""))
            if status == "partial"
            else []
        )

        records.append(
            {
                **claim,
                "match_status": status,
                "matched_claim_id": matched_id,
                "matched_claim_text": (
                    library_claim["claim_text"] if library_claim else ""
                ),
                "library_status": (
                    (library_claim.get("status") or "Approved") if library_claim else ""
                ),
                "library_claim_usable": usable if library_claim else None,
                "library_audience": (
                    library_claim.get("audience", "") if library_claim else ""
                ),
                "audience_approved": not audience_reason if library_claim else None,
                # Which kind of exact match this is, computed here rather than taken
                # from the verdict: "verbatim", "reordered", or "" for anything else
                "match_precision": (
                    match_precision(claim["text"], library_claim.get("claim_text"))
                    if library_claim
                    else ""
                ),
                "deviation": deviation,
                # model similarity and word overlap, 0-100, vs matched or nearest claim
                "similarity": similarity,
                "text_similarity": (
                    _text_similarity(claim["text"], compared.get("claim_text", ""))
                    if compared
                    else None
                ),
                "nearest_claim_id": str(nearest["claim_id"]) if nearest else "",
                "numeric_deviation": bool(changes),
                "numeric_changes": changes,
                "unusable_reason": unusable_reason,
                # Anything that is not an exact match against a currently usable
                # approved claim still has to be checked downstream.
                "requires_verification": not (status == "exact" and usable),
            }
        )
    return records


def _findings_from_matches(
    records: list[dict], library_by_id: dict[str, dict]
) -> list[dict]:
    """Derive findings from match records, deterministically.

    Three situations are reported here because the claims library alone settles them:
    content that deviates from an approved wording, content that reuses a claim the
    library no longer allows, and content that reuses a claim approved for another
    audience. A partial match to an unusable claim gets both
    findings, so the deviation (a changed number, say) is not hidden behind the status.
    Claims with no match produce NO finding — they are not violations, they are simply
    routed to the other reviewers for verification.

    Parameters
    ----------
    records : list[dict]
        Match records from `_build_match_records`.
    library_by_id : dict[str, dict]
        Library claims keyed by `claim_id`.

    Returns
    -------
    list[dict]
        Findings in the shared reviewer schema.
    """
    findings: list[dict] = []
    for record in records:
        matched_id = record["matched_claim_id"]
        library_claim = library_by_id.get(matched_id, {})
        approved_text = record["matched_claim_text"]
        job_code = library_claim.get("job_code", "")
        reference = " | ".join(
            part
            for part in (
                f'Approved claim {matched_id}: "{approved_text}"' if matched_id else "",
                library_claim.get("reference", ""),
                f"job code {job_code}" if job_code else "",
            )
            if part
        )

        unusable = bool(matched_id) and record["library_claim_usable"] is False
        if unusable and _is_usable(library_claim)[0]:
            # usable in itself, but approved for another audience
            findings.append(
                {
                    "page": record["page"],
                    "quote": record["text"],
                    "headline": f"{matched_id} is approved for another audience",
                    "issue": (
                        f"This claim is the wording of pre-approved claim {matched_id},"
                        f" but {record['unusable_reason']}. A claim may only be used"
                        " for the audience it was approved for."
                    ),
                    "fix": (
                        "Use a claim approved for this asset's audience, or route this"
                        " one to medical/regulatory review for this audience."
                    ),
                    "reference": reference,
                    "source": CLAIMS_SOURCE,
                    "type": "mandatory",
                    "score": UNUSABLE_SCORE,
                    "claim_match": record["match_status"],
                    "claim_id": matched_id,
                    "claim_precision": record["match_precision"],
                    "library_finding": "audience",
                }
            )
        elif unusable:
            status = str(library_claim.get("status") or "").strip()
            # an Approved claim past its expiry date
            status = "expired" if status.lower() in ("", "approved") else status
            findings.append(
                {
                    "page": record["page"],
                    "quote": record["text"],
                    "headline": f"{matched_id} is no longer usable ({status})",
                    "issue": (
                        f"This claim reuses pre-approved claim {matched_id}, but"
                        f" {record['unusable_reason']}. It may not be used in new"
                        " content until a reviewer re-approves it."
                    ),
                    "fix": (
                        "Remove the claim or route it back to medical/regulatory"
                        " review for re-approval."
                    ),
                    "reference": reference,
                    "source": CLAIMS_SOURCE,
                    "type": "mandatory",
                    "score": UNUSABLE_SCORE,
                    "claim_match": record["match_status"],
                    "claim_id": matched_id,
                    "claim_precision": record["match_precision"],
                    "library_finding": "status",
                }
            )
        if record["match_status"] == "partial":
            deviation = record["deviation"] or (
                "the wording differs from the approved claim"
            )
            numbers = (
                f" Numeric deviation: {'; '.join(record['numeric_changes'])}."
                if record["numeric_deviation"]
                else ""
            )
            findings.append(
                {
                    "page": record["page"],
                    "quote": record["text"],
                    "headline": (
                        f"Number changed: {record['numeric_changes'][0]}"
                        if record["numeric_deviation"]
                        else _change_headline(record["text"], approved_text, matched_id)
                    ),
                    "issue_spans": changed_spans(
                        record["text"],
                        approved_text,
                        aligned(record["text"], approved_text),
                    ),
                    "issue": (
                        f"Deviates from pre-approved claim {matched_id}: {deviation}"
                        f"{numbers} A reviewer has to confirm the change or the"
                        " approved wording has to be restored."
                    ),
                    "fix": (
                        f"Do not restore the wording of {matched_id}: it is no longer"
                        " usable either. Route the claim to medical/regulatory review."
                        if unusable
                        else f'Use the approved wording: "{approved_text}"'
                    ),
                    "reference": reference,
                    "source": CLAIMS_SOURCE,
                    "type": "mandatory",
                    "score": PARTIAL_SCORE,
                    "claim_match": "partial",
                    "claim_id": matched_id,
                    "claim_precision": record["match_precision"],
                    "library_finding": "deviation",
                    "numeric_deviation": record["numeric_deviation"],
                }
            )
    return findings


@tool
def match_claims(
    extracted_claims_s3_uri: str,
    claims_library_s3_uri: str,
    session_id: str,
) -> str:
    """Match one batch's extracted claims against the pre-approved claims library.

    Tags every claim `exact` (`verbatim`, `reordered` or `segment`), `partial`, or
    `none`, and writes two artifacts to S3: the tagged claim records (for the claims
    report and the UI) and the findings that follow directly from the library —
    deviations from an approved wording, reuse of a claim the library no longer allows,
    and reuse of a claim approved for another audience. A claim the library does not
    cover produces no finding: it still has to be verified against the reference
    documents and external databases by the other reviewers.

    Parameters
    ----------
    extracted_claims_s3_uri : str
        S3 URI returned by `extract_claims` for this batch.
    claims_library_s3_uri : str
        S3 URI returned by `load_claims_library`.
    session_id : str
        Review session id, used to namespace claim artifacts.

    Returns
    -------
    str
        JSON string of shape `{"matched_claims_s3_uri": "s3://...",
        "findings_s3_uri": "s3://...", "total_claims": N,
        "counts": {"exact": N, "partial": N, "none": N},
        "exact_matches": {"verbatim": N, "reordered": N, "segment": N},
        "requires_verification": N}`. The internal and external reviewers read
        `matched_claims_s3_uri` to skip the already-approved claims.
    """
    claims = read_s3_json(extracted_claims_s3_uri)
    library = read_s3_json(claims_library_s3_uri)
    if not isinstance(claims, list):
        claims = []
    if not isinstance(library, list):
        library = []
    stem = batch_stem(extracted_claims_s3_uri).replace("extracted_", "", 1)

    library_by_id = {
        str(claim.get("claim_id")): claim
        for claim in library
        if isinstance(claim, dict) and claim.get("claim_id")
    }

    audience = str(read_asset_audience(session_id).get("audience") or "")
    # partial candidates: only claims approved for the asset's audience
    candidates = [c for c in library_by_id.values() if approved_for(c, audience)]

    # Verbatim reuse is settled in Python; the model only judges paraphrases.
    verdicts, unresolved = _exact_verdicts(
        claims, list(library_by_id.values()), audience
    )
    if unresolved and candidates:
        payload = json.dumps(
            [
                {
                    "claim_ref": claim["claim_ref"],
                    "page": claim["page"],
                    "text": claim["text"],
                    "claim_type": claim["claim_type"],
                }
                for claim in unresolved
            ],
            indent=2,
        )
        text = run_inner_agent_text(
            system_prompt=SYSTEM_PROMPT_TEMPLATE.format(
                audience=(
                    f"This asset is written for a {AUDIENCE_LABELS[audience]} audience."
                    " The library below lists only the claims approved for that"
                    " audience.\n\n"
                    if audience in AUDIENCE_LABELS
                    else ""
                ),
                library=_render_library(candidates),
            ),
            user_prompt=f"Extracted claims to match:\n\n{payload}",
            tools=[],
        )
        judged = parse_tagged_json_array(text, "matches")
        if judged is None:
            note_issue(
                session_id,
                "match",
                "the matcher's reply had no parsable verdicts, so its claims count as no match",
                batch=stem,
                claims=len(unresolved),
            )
        verdicts += judged or []
        answered = {str(v.get("claim_ref")) for v in verdicts}
        skipped = [c["claim_ref"] for c in unresolved if c["claim_ref"] not in answered]
        if judged is not None and skipped:
            note_issue(
                session_id,
                "match",
                f"the matcher gave no verdict for {len(skipped)} claims, so they count as no match",
                severity="warning",
                batch=stem,
                claims=skipped,
            )
    _prefer_closest_sibling(verdicts, claims, library_by_id, audience)
    _settle_shared_lines(verdicts, claims, audience)

    records = _build_match_records(claims, verdicts, library_by_id, audience)
    findings = _findings_from_matches(records, library_by_id)

    matched_uri = write_claims_json(session_id, "matched", stem, records)
    findings_uri = write_review_json(session_id, "claims", stem, findings)

    counts = dict.fromkeys(CLAIM_MATCH_STATUSES, 0)
    precision_counts = dict.fromkeys(MATCH_PRECISIONS, 0)
    for record in records:
        counts[record["match_status"]] += 1
        if record["match_precision"] in precision_counts:
            precision_counts[record["match_precision"]] += 1

    return json.dumps(
        {
            "matched_claims_s3_uri": matched_uri,
            "findings_s3_uri": findings_uri,
            "total_claims": len(records),
            "counts": counts,
            "exact_matches": precision_counts,
            "requires_verification": sum(
                1 for r in records if r["requires_verification"]
            ),
        }
    )
