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
"""

import datetime as dt
import json

from strands import tool

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
    MATCH_PRECISIONS,
    match_precision,
    normalise,
    same_wording,
    wording_key,
)

SYSTEM_PROMPT_TEMPLATE = load_prompt("claim_matcher")

MAX_LIBRARY_CLAIMS = 400
VALID_STATUSES = ("exact", "partial", "none")

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
}

PARTIAL_SCORE = 75
UNUSABLE_SCORE = 85
CLAIMS_SOURCE = "Pre-approved claims library"


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
        if expiry_date < dt.date.today():
            return False, f"the approved claim expired on {expiry_date.isoformat()}"
    return True, ""


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
    claims: list[dict], library: list[dict]
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
        Library claims from `load_claims_library`.

    Returns
    -------
    tuple[list[dict], list[dict]]
        `(verdicts, unresolved)`: verdicts for the claims that reuse an approved
        wording, and the claims still needing a semantic comparison.
    """
    by_words: dict[tuple[str, ...], tuple[str, str]] = {}
    for library_claim in library:
        text = normalise(library_claim.get("claim_text"))
        claim_id = str(library_claim.get("claim_id") or "")
        if text and claim_id:
            by_words.setdefault(wording_key(text), (claim_id, text))

    verdicts: list[dict] = []
    unresolved: list[dict] = []
    for claim in claims:
        text = normalise(claim.get("text"))
        match = by_words.get(wording_key(text)) if text else None
        if match:
            claim_id, library_text = match
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


def _build_match_records(
    claims: list[dict], verdicts: list[dict], library_by_id: dict[str, dict]
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
    records: list[dict] = []

    for claim in claims:
        verdict = by_ref.get(claim["claim_ref"], {})
        status = str(verdict.get("match_status") or "none").strip().lower()
        if status not in VALID_STATUSES:
            status = "none"

        matched_id = str(verdict.get("matched_claim_id") or "").strip()
        library_claim = library_by_id.get(matched_id)
        if not library_claim:
            # A hallucinated or missing id cannot be trusted as a match.
            matched_id = ""
            if status != "none":
                status = "none"
        elif status == "exact" and not same_wording(
            claim["text"], library_claim.get("claim_text")
        ):
            # `exact` carries a human's sign-off on this wording, so it is only granted
            # when the same words are used, in whatever order. A model calling a
            # paraphrase exact would skip the reviewer the deviation should reach.
            status = "partial"

        usable, unusable_reason = (
            _is_usable(library_claim) if library_claim else (True, "")
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
                # Which kind of exact match this is, computed here rather than taken
                # from the verdict: "verbatim", "reordered", or "" for anything else
                "match_precision": (
                    match_precision(claim["text"], library_claim.get("claim_text"))
                    if library_claim
                    else ""
                ),
                "deviation": str(verdict.get("deviation") or "").strip(),
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

    Two situations are reported here because the claims library alone settles them:
    content that deviates from an approved wording, and content that reuses a claim
    the library no longer allows. Claims with no match produce NO finding — they are
    not violations, they are simply routed to the other reviewers for verification.

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

        if matched_id and record["library_claim_usable"] is False:
            findings.append(
                {
                    "page": record["page"],
                    "quote": record["text"],
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
                }
            )
        elif record["match_status"] == "partial":
            deviation = record["deviation"] or (
                "the wording differs from the approved claim"
            )
            findings.append(
                {
                    "page": record["page"],
                    "quote": record["text"],
                    "issue": (
                        f"Deviates from pre-approved claim {matched_id}: {deviation}"
                        " A reviewer has to confirm the change or the approved wording"
                        " has to be restored."
                    ),
                    "fix": f'Use the approved wording: "{approved_text}"',
                    "reference": reference,
                    "source": CLAIMS_SOURCE,
                    "type": "mandatory",
                    "score": PARTIAL_SCORE,
                    "claim_match": "partial",
                    "claim_id": matched_id,
                    "claim_precision": record["match_precision"],
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

    Tags every claim `exact` (`verbatim` or `reordered`), `partial`, or `none`, and
    writes two artefacts to S3:
    the tagged claim records (for the claims report and the UI) and the findings that
    follow directly from the library — deviations from an approved wording, and reuse
    of a claim the library no longer allows.

    Claims tagged `none` deliberately produce no finding: the library not covering a
    claim is not a violation, it just means the claim still has to be verified against
    the reference documents and external databases by the other reviewers.

    Parameters
    ----------
    extracted_claims_s3_uri : str
        S3 URI returned by `extract_claims` for this batch.
    claims_library_s3_uri : str
        S3 URI returned by `load_claims_library`.
    session_id : str
        Orchestrator session id, used to namespace claim artefacts.

    Returns
    -------
    str
        JSON string of shape `{"matched_claims_s3_uri": "s3://...",
        "findings_s3_uri": "s3://...", "total_claims": N,
        "counts": {"exact": N, "partial": N, "none": N},
        "exact_matches": {"verbatim": N, "reordered": N},
        "requires_verification": N}`. Pass `matched_claims_s3_uri` on to the
        internal and external reviewers so they skip the already-approved claims.
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

    # Verbatim reuse is settled in Python; the model only judges paraphrases.
    verdicts, unresolved = _exact_verdicts(claims, list(library_by_id.values()))
    if unresolved and library_by_id:
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
                library=_render_library(library)
            ),
            user_prompt=f"Extracted claims to match:\n\n{payload}",
            tools=[],
        )
        verdicts += parse_tagged_json_array(text, "matches")

    records = _build_match_records(claims, verdicts, library_by_id)
    findings = _findings_from_matches(records, library_by_id)

    matched_uri = write_claims_json(session_id, "matched", stem, records)
    findings_uri = write_review_json(session_id, "claims", stem, findings)

    counts = dict.fromkeys(VALID_STATUSES, 0)
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
