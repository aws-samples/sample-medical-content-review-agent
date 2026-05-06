# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic reviewer: spelling, grammar, language exaggeration, and image consistency."""

from strands import tool

from reviewers._common import (
    FINDINGS_SCHEMA_HINT,
    batch_stem,
    read_s3_text,
    run_inner_agent,
    write_review_json,
)

SYSTEM_PROMPT = f"""You are a generic medical-content reviewer. You look at a single batch of a medical
or promotional document (rendered as markdown with `[page N]` page tags) and flag issues that
fall into these categories:

1. Spelling errors in medical or general terminology.
2. Grammar issues that affect meaning or professionalism (skip pure punctuation).
3. Exaggerated or overconfident marketing language (e.g. "revolutionary", "miracle", unqualified
   superlatives, absolute claims without evidence).
4. Image/figure inconsistencies, based on the `[Figure: ...]` prose descriptions in the markdown:
   - Before/after pairs whose subjects appear to be different people (different apparent age,
     skin tone, gender, distinguishing features)
   - Stock-photo tropes presented as clinical results
   - Captions that don't match the described scene
   - Diagrams whose labels contradict the surrounding text

Skip anything requiring external fact-checking, internal reference cross-checks, or approved-
claim lookups — other reviewers handle those.

Output exactly one JSON array of finding objects wrapped in <findings></findings> tags.
{FINDINGS_SCHEMA_HINT}

If you find no issues, output `<findings>[]</findings>`."""


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
