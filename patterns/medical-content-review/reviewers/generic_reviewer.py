# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic reviewer: spelling, grammar, language exaggeration, and image consistency."""

from strands import tool

from reviewers._common import (
    FINDINGS_SCHEMA_HINT,
    batch_stem,
    load_prompt,
    read_s3_text,
    run_inner_agent,
    write_review_json,
)

SYSTEM_PROMPT = load_prompt("generic_reviewer").format(schema=FINDINGS_SCHEMA_HINT)


@tool
def run_generic_review(batch_md_s3_uri: str, session_id: str) -> str:
    """Run the generic reviewer on a single batch markdown and save findings to S3.

    Internally spins up a narrow sub-agent that checks spelling, grammar,
    language exaggeration, and figure/image description consistency. The
    sub-agent has no external tools — it works purely off the provided batch
    markdown.

    Parameters
    ----------
    batch_md_s3_uri : str
        S3 URI of a batch markdown file produced by `batch_content`.
    session_id : str
        The orchestrator's runtime session id, used to namespace review outputs
        under `reviews/{session_id}/`.

    Returns
    -------
    str
        S3 URI of the written findings JSON. Nothing else is returned.
    """
    markdown = read_s3_text(batch_md_s3_uri)
    findings = run_inner_agent(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=f"Review this batch:\n\n{markdown}",
        tools=[],
    )
    return write_review_json(
        session_id, "generic", batch_stem(batch_md_s3_uri), findings
    )
