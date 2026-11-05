# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claim extractor: lists every claim a content batch makes, before any checking."""

import json

from strands import tool
from tools.content_batcher import PAGE_PATTERN
from tools.run_record import note_issue
from utils.s3 import read_text

from reviewers._common import (
    batch_stem,
    load_prompt,
    parse_tagged_json_array,
    run_inner_agent_text,
    write_claims_json,
)
from reviewers.claim_tags import normalise

SYSTEM_PROMPT = load_prompt("claim_extractor")

VALID_CLAIM_TYPES = {
    "efficacy",
    "safety",
    "dosing",
    "indication",
    "mechanism",
    "comparative",
    "quality_of_life",
    "other",
}

EXTRACT_ATTEMPTS = 2
# share of a claim's words a page must hold to be taken as the page it came from
MIN_PAGE_OVERLAP = 0.8


def _batch_pages(markdown: str) -> dict[int, str]:
    """Split a batch's markdown into its pages, normalized for matching claim text

    Parameters
    ----------
    markdown : str
        The batch markdown, with the content batcher's page markers

    Returns
    -------
    dict[int, str]
        The normalized text of each page, keyed by page number
    """
    return {
        int(m.group(1)): normalise(m.group(2)) for m in PAGE_PATTERN.finditer(markdown)
    }


def _locate(text: str, pages: dict[int, str]) -> int:
    """The batch page a claim's text comes from, or 0 when no page clearly holds it

    Parameters
    ----------
    text : str
        The claim text as the model extracted it
    pages : dict[int, str]
        The batch's normalized pages, keyed by page number

    Returns
    -------
    int
        The page number holding the claim, or 0 when no single page clearly does
    """
    if len(pages) == 1:
        return next(iter(pages))
    claim = normalise(text)
    holding = [page for page, body in pages.items() if f" {claim} " in f" {body} "]
    if len(holding) == 1:
        return holding[0]
    words = set(claim.split())
    if not words:
        return 0
    scores = {
        page: len(words & set(body.split())) / len(words)
        for page, body in pages.items()
    }
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] >= MIN_PAGE_OVERLAP and (len(ranked) == 1 or ranked[0] > ranked[1]):
        return best
    return 0


def _normalise(raw_claims: list[dict], stem: str, pages: dict[int, str]) -> list[dict]:
    """Assign stable refs and coerce the model's fields into the stored schema.

    The claim ref is generated here rather than asked of the model so that it is
    guaranteed unique and stable across the extract -> match -> aggregate chain.

    Parameters
    ----------
    raw_claims : list[dict]
        Claim objects as emitted by the extractor sub-agent.
    stem : str
        Batch stem (e.g. `content_batch_2`), used to namespace the claim refs.
    pages : dict[int, str]
        The batch's pages, normalized, to place a claim the model gave no valid page

    Returns
    -------
    list[dict]
        Claim records with `claim_ref`, `page`, `text` and `claim_type`. `page` is 0
        only when the model gave none of the batch's pages and the text is on no
        single page.
    """
    claims: list[dict] = []
    for item in raw_claims:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        page = item.get("page")
        if not isinstance(page, int) or page not in pages:
            page = _locate(text, pages)
        claim_type = str(item.get("claim_type") or "other").strip().lower()
        claims.append(
            {
                "claim_ref": f"{stem}-c{len(claims) + 1}",
                "page": page,
                "text": text,
                "claim_type": claim_type
                if claim_type in VALID_CLAIM_TYPES
                else "other",
            }
        )
    return claims


@tool
def extract_claims(batch_md_s3_uri: str, session_id: str) -> str:
    """Extract every claim made in one batch markdown and save them to S3.

    This is the first step of the claims workflow: it produces the list of claims
    the document makes, independently of whether they are approved or supportable.
    `match_claims` then checks that list against the pre-approved claims library.

    Parameters
    ----------
    batch_md_s3_uri : str
        S3 URI of a batch markdown file produced by `batch_content`.
    session_id : str
        Review session id, used to namespace claim artifacts.

    Returns
    -------
    str
        JSON string of shape `{"extracted_claims_s3_uri": "s3://...",
        "total_claims": N, "by_type": {...}}`. The claim texts stay on S3, at
        `extracted_claims_s3_uri`, which `match_claims` reads.
    """
    markdown = read_text(batch_md_s3_uri)
    stem = batch_stem(batch_md_s3_uri)

    raw = None
    # a long reply now and then comes back as broken JSON; asking again fixes it
    for _ in range(EXTRACT_ATTEMPTS):
        text = run_inner_agent_text(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"Extract every claim from this batch:\n\n{markdown}",
            tools=[],
        )
        raw = parse_tagged_json_array(text, "claims")
        if raw is not None:
            break
    if raw is None:
        note_issue(
            session_id,
            "extract",
            "the extractor's reply had no parsable claims",
            batch=stem,
        )
    claims = _normalise(raw or [], stem, _batch_pages(markdown))
    unplaced = [c["claim_ref"] for c in claims if not c["page"]]
    if unplaced:
        note_issue(
            session_id,
            "extract",
            f"{len(unplaced)} claims have no page",
            severity="warning",
            batch=stem,
            claims=unplaced,
        )

    s3_uri = write_claims_json(session_id, "extracted", stem, claims)
    by_type: dict[str, int] = {}
    for claim in claims:
        by_type[claim["claim_type"]] = by_type.get(claim["claim_type"], 0) + 1

    return json.dumps(
        {
            "extracted_claims_s3_uri": s3_uri,
            "total_claims": len(claims),
            "by_type": by_type,
        }
    )
