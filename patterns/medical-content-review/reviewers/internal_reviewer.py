# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Internal reviewer: cross-checks content against attached reference markdowns."""

import boto3
from strands import tool

from reviewers._common import (
    FINDINGS_SCHEMA_HINT,
    batch_stem,
    load_prompt,
    parse_s3_uri,
    read_s3_text,
    run_inner_agent,
    write_review_json,
)

s3_client = boto3.client("s3")

SYSTEM_PROMPT_TEMPLATE = load_prompt("internal_reviewer")


@tool
def run_internal_review(
    batch_md_s3_uri: str,
    session_id: str,
    reference_md_uris: list[str],
) -> str:
    """Run the internal-reference reviewer on one batch markdown and save to S3.

    Internally spins up a narrow sub-agent with a single helper tool
    (`read_reference_markdown`) that lets it pull the text of any of the
    provided reference markdowns from S3 on demand.

    Parameters
    ----------
    batch_md_s3_uri : str
        S3 URI of a batch markdown file produced by `batch_content`.
    session_id : str
        Orchestrator session id, used to namespace review outputs.
    reference_md_uris : list[str]
        S3 URIs of the reference markdown files (also produced by `process_pdf`).
        The reviewer is allowed to read any subset of these.

    Returns
    -------
    str
        S3 URI of the written findings JSON.
    """
    content_md = read_s3_text(batch_md_s3_uri)
    allow_set = set(reference_md_uris or [])

    @tool
    def read_reference_markdown(s3_uri: str) -> str:
        """Read one reference markdown file from S3 and return its full text."""
        if s3_uri not in allow_set:
            return f"ERROR: {s3_uri} is not in the allow-list of reference URIs."
        bucket, key = parse_s3_uri(s3_uri)
        return (
            s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        )

    ref_list = (
        "\n".join(f"- {u}" for u in reference_md_uris)
        if reference_md_uris
        else "- (no references attached — output <findings>[]</findings>)"
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        ref_list=ref_list, schema=FINDINGS_SCHEMA_HINT
    )
    findings = run_inner_agent(
        system_prompt=system_prompt,
        user_prompt=f"Review this batch against the references:\n\n{content_md}",
        tools=[read_reference_markdown] if reference_md_uris else [],
    )
    return write_review_json(
        session_id, "internal", batch_stem(batch_md_s3_uri), findings
    )
