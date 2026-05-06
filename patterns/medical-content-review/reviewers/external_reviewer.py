# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""External reviewer: cross-checks claims against PubMed, OpenFDA, ClinicalTrials, web."""

from strands import tool
from utils.gateway import create_gateway_mcp_client

from reviewers._common import (
    FINDINGS_SCHEMA_HINT,
    batch_stem,
    read_s3_text,
    run_inner_agent,
    write_review_json,
)

SYSTEM_PROMPT_TEMPLATE = """You are an external-evidence medical reviewer. Given a single batch of
document markdown (with `[page N]` tags), verify factual medical claims (drug names, indications,
dosages, trial results, efficacy numbers, safety signals) against the external databases available
as Gateway tools.

Available Gateway tools (names start with `gateway___`):
{tools_section}

Rules:
- For each claim worth checking, call the most relevant tool(s). Fan out multiple tool calls in
  a single turn when you have independent claims.
- Treat a 404 / "no results" response as "unverified", not as an error.
- Flag a claim if external evidence contradicts it, names a drug/trial/study that does not
  exist in the databases, or has numbers (dosage, sample size, efficacy %) that don't match.
- Do NOT flag claims that external evidence supports, nor claims that simply can't be verified
  (absence of evidence is not evidence of error).

Output exactly one JSON array of finding objects wrapped in <findings></findings> tags.
{schema}

If no external issues found, output `<findings>[]</findings>`."""


def _tools_section(enabled_sources: list[str]) -> str:
    labels = {
        "pubmed": "gateway___pubmed_search — peer-reviewed biomedical literature",
        "openfda": "gateway___openfda_drug_search — FDA drug label database",
        "clinicaltrials": "gateway___clinicaltrials_search — registered clinical studies",
        "nova": "gateway___nova_web_search — grounded web search (use sparingly)",
    }
    lines = [f"- {labels[s]}" for s in enabled_sources if s in labels]
    if not lines:
        return "- (no external tools enabled — skip the review and output <findings>[]</findings>)"
    return "\n".join(lines)


@tool
def run_external_review(
    batch_md_s3_uri: str,
    session_id: str,
    enabled_sources: list[str],
) -> str:
    """Run the external-evidence reviewer on a single batch markdown and save findings to S3.

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

    Returns
    -------
    str
        S3 URI of the written findings JSON.
    """
    markdown = read_s3_text(batch_md_s3_uri)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        tools_section=_tools_section(enabled_sources or []),
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
