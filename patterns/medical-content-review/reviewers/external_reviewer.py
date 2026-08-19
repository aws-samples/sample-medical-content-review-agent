# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""External reviewer: cross-checks claims against PubMed, OpenFDA, trials, web."""

from strands import tool
from utils.gateway import create_gateway_mcp_client

from reviewers._common import (
    FINDINGS_SCHEMA_HINT,
    batch_stem,
    build_claims_context,
    claims_workflow_error,
    load_prompt,
    read_s3_text,
    run_inner_agent,
    write_review_json,
)

SYSTEM_PROMPT_TEMPLATE = load_prompt("external_reviewer")


def _tools_section(enabled_sources: list[str]) -> str:
    labels = {
        "pubmed": "gateway___pubmed_search — peer-reviewed biomedical literature",
        "openfda": "gateway___openfda_drug_search — FDA drug label database",
        "clinicaltrials": (
            "gateway___clinicaltrials_search — registered clinical studies"
        ),
        "nova": "gateway___nova_web_search — grounded web search (use sparingly)",
    }
    lines = [f"- {labels[s]}" for s in enabled_sources if s in labels]
    if not lines:
        return (
            "- (no external tools enabled — skip the review and output"
            " <findings>[]</findings>)"
        )
    return "\n".join(lines)


@tool
def run_external_review(
    batch_md_s3_uri: str,
    session_id: str,
    enabled_sources: list[str],
    matched_claims_s3_uri: str = "",
) -> str:
    """Run the external-evidence reviewer on one batch markdown and save to S3.

    Internally spins up a narrow sub-agent that has access to the Gateway
    tools in `enabled_sources` (e.g. PubMed, OpenFDA). The sub-agent cross-checks
    factual claims against these external databases and emits one JSON per batch.

    Parameters
    ----------
    batch_md_s3_uri : str
        S3 URI of a batch markdown file produced by `batch_content`.
    session_id : str
        Orchestrator session id, used to namespace review outputs.
    enabled_sources : list[str]
        Subset of {"pubmed", "openfda", "clinicaltrials", "nova"} the reviewer
        is allowed to call. An empty list means no external lookups happen.
    matched_claims_s3_uri : str
        S3 URI of this batch's `matched_claims` file from `match_claims`, when a
        pre-approved claims library was provided. Claims already approved verbatim
        are excluded from this review; unmatched claims get the lookups.

    Returns
    -------
    str
        S3 URI of the written findings JSON, or an error asking for the claims
        workflow when the review has a claims library and none was passed.
    """
    skipped = claims_workflow_error(session_id, matched_claims_s3_uri, batch_md_s3_uri)
    if skipped:
        return skipped

    markdown = read_s3_text(batch_md_s3_uri)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        tools_section=_tools_section(enabled_sources or []),
        claims_context=build_claims_context(matched_claims_s3_uri),
        schema=FINDINGS_SCHEMA_HINT,
    )
    tools = []
    if enabled_sources:
        tools.append(create_gateway_mcp_client(enabled_sources))
    findings = run_inner_agent(
        system_prompt=system_prompt,
        user_prompt=f"Review this batch:\n\n{markdown}",
        tools=tools,
    )
    return write_review_json(
        session_id, "external", batch_stem(batch_md_s3_uri), findings
    )
