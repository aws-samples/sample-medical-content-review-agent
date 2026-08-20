# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claim extractor: lists every claim a content batch makes, before any checking."""

import json

from strands import tool

from reviewers._common import (
    batch_stem,
    load_prompt,
    parse_tagged_json_array,
    read_s3_text,
    run_inner_agent_text,
    write_claims_json,
)

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


def _normalise(raw_claims: list[dict], stem: str) -> list[dict]:
    """Assign stable refs and coerce the model's fields into the stored schema.

    The claim ref is generated here rather than asked of the model so that it is
    guaranteed unique and stable across the extract -> match -> aggregate chain.

    Parameters
    ----------
    raw_claims : list[dict]
        Claim objects as emitted by the extractor sub-agent.
    stem : str
        Batch stem (e.g. `content_batch_2`), used to namespace the claim refs.

    Returns
    -------
    list[dict]
        Claim records with `claim_ref`, `page`, `text` and `claim_type`.
    """
    claims: list[dict] = []
    for item in raw_claims:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        page = item.get("page")
        claim_type = str(item.get("claim_type") or "other").strip().lower()
        claims.append(
            {
                "claim_ref": f"{stem}-c{len(claims) + 1}",
                "page": page if isinstance(page, int) else 0,
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
        Orchestrator session id, used to namespace claim artefacts.

    Returns
    -------
    str
        JSON string of shape `{"extracted_claims_s3_uri": "s3://...",
        "total_claims": N, "by_type": {...}}`. The claim texts are not returned —
        pass `extracted_claims_s3_uri` on to `match_claims`.
    """
    markdown = read_s3_text(batch_md_s3_uri)
    stem = batch_stem(batch_md_s3_uri)

    text = run_inner_agent_text(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=f"Extract every claim from this batch:\n\n{markdown}",
        tools=[],
    )
    claims = _normalise(parse_tagged_json_array(text, "claims"), stem)

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
