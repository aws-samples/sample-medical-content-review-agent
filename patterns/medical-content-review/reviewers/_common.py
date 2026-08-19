# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the three reviewer sub-agents.

Each reviewer is a `@tool` exposed to the orchestrator. Internally it spins up
a narrow Strands Agent with just the tools it needs, runs a single review on
one batch markdown, and writes its findings JSON to S3. Only the S3 URI of
the written JSON is returned to the orchestrator — reviewer output never
flows through the orchestrator's context window.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

import boto3
from strands import Agent
from strands.models import BedrockModel, CacheConfig
from utils.inference import get_bedrock_config, get_inference_configs

from reviewers.claim_tags import claims_library_expected

s3_client = boto3.client("s3")

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Read a reviewer prompt template from patterns/medical-content-review/prompts/."""
    return (PROMPTS_DIR / f"{name}.txt").read_text()


INFERENCE_CONFIG, _ = get_inference_configs()
BEDROCK_CONFIG = get_bedrock_config()
MODEL_ID = os.environ.get(
    "REVIEWER_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5"),
)
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
REVIEWS_PREFIX = "reviews"
CLAIMS_PREFIX = "claims"


def _require_bucket() -> str:
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")
    return STAGING_BUCKET


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    return path.split("/", 1)


def read_s3_text(s3_uri: str) -> str:
    bucket, key = parse_s3_uri(s3_uri)
    return s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def read_s3_json(s3_uri: str) -> list | dict:
    """Read a JSON document from S3. Returns `[]` when the object is unparsable."""
    try:
        return json.loads(read_s3_text(s3_uri))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def batch_stem(batch_md_s3_uri: str) -> str:
    """e.g. s3://.../markdowns/foo_batch_3.md -> foo_batch_3"""
    _, key = parse_s3_uri(batch_md_s3_uri)
    return PurePosixPath(key).stem


def safe_session_id(session_id: str) -> str:
    """Reduce a session id to the characters that are safe inside an S3 key."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)


def _write_json(prefix: str, session_id: str, name: str, payload: list | dict) -> str:
    bucket = _require_bucket()
    key = f"{prefix}/{safe_session_id(session_id)}/{name}.json"
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


def write_review_json(
    session_id: str, kind: str, batch_stem_value: str, findings: list[dict]
) -> str:
    return _write_json(
        REVIEWS_PREFIX, session_id, f"{kind}_{batch_stem_value}", findings
    )


def write_claims_json(
    session_id: str, kind: str, batch_stem_value: str, payload: list | dict
) -> str:
    """Write a per-batch claims artefact (`extracted_*` or `matched_*`) to S3."""
    return _write_json(CLAIMS_PREFIX, session_id, f"{kind}_{batch_stem_value}", payload)


def build_reviewer_model() -> BedrockModel:
    return BedrockModel(
        model_id=MODEL_ID,
        temperature=INFERENCE_CONFIG["temperature"],
        max_tokens=INFERENCE_CONFIG["maxTokens"],
        streaming=False,
        boto_client_config=BEDROCK_CONFIG,
        cache_config=CacheConfig(strategy="auto"),
    )


FINDINGS_SCHEMA_HINT = """Each finding object must have these fields:
- `page`: int, page number the issue was found on
- `quote`: str, exact quote from the document
- `issue`: str, description of the problem
- `fix`: str, concrete suggested correction
- `reference`: str, supporting reference or quote (empty string if none)
- `source`: str, the source document or database the reference came from ("" if none)
- `type`: str, "mandatory" (incorrect info / adherence) or "optional" (clarity)
- `score`: int, severity 1-100 (>=70 for mandatory, <50 for optional)
- `claim_match`: str, "exact" | "partial" | "none" when the finding is about a claim
  that was checked against the pre-approved claims library, else ""
- `claim_id`: str, id of the pre-approved claim involved ("" if none)"""

CLAIM_MATCH_STATUSES = ("exact", "partial", "none")


NO_CLAIMS_CONTEXT = (
    "(no pre-approved claims library was provided for this review — treat every claim"
    " in the batch as unverified)"
)

CLAIMS_WORKFLOW_ERROR = (
    "ERROR: this review was given a pre-approved claims library, so claim extraction and"  # noqa: E501
    " matching are mandatory and must finish before any reviewer runs. Call"
    " `extract_claims` for every batch, then `match_claims` for every batch, then call"
    " this reviewer again passing the batch's `matched_claims_s3_uri`."
)

# Batches already sent back once for missing matched claims, so the guard asks for the
# claims workflow but cannot trap a run in a loop
_CLAIMS_REFUSED: set[tuple[str, str]] = set()


def claims_workflow_error(
    session_id: str, matched_claims_s3_uri: str | None, batch_md_s3_uri: str
) -> str | None:
    """Refuse a reviewer that skipped the claims workflow the request asked for

    Whether a claims library exists comes from the request payload, not from the
    orchestrator, so a model that decides to skip extraction and matching is stopped
    here rather than quietly producing a review with no claim context. Reviews without a
    library are unaffected: nothing is expected, so nothing is refused.

    Each batch is refused at most once. If matching genuinely cannot produce a result —
    an unparsable spreadsheet, a failing claims write — the second attempt goes through
    and the run degrades to a review without claim tags, which is far better than a run
    that never finishes.

    Parameters
    ----------
    session_id : str
        The same `session_id` the reviewer was called with
    matched_claims_s3_uri : str | None
        The batch's matched-claims URI as passed by the orchestrator
    batch_md_s3_uri : str
        The batch the reviewer was asked to review, so each batch is refused once

    Returns
    -------
    str | None
        The error to return to the orchestrator, or None when the reviewer may proceed
    """
    if matched_claims_s3_uri or not claims_library_expected(session_id):
        return None

    key = (session_id, batch_md_s3_uri)
    if key in _CLAIMS_REFUSED:
        print(
            f"[CLAIMS] {batch_md_s3_uri} still has no matched claims — reviewing it"
            " without claim context rather than blocking the run"
        )
        return None
    _CLAIMS_REFUSED.add(key)
    return CLAIMS_WORKFLOW_ERROR


def build_claims_context(matched_claims_s3_uri: str | None) -> str:
    """Summarise a batch's claim-match results for a downstream reviewer's prompt.

    Exact matches were already cleared by a human, so the reviewer is told to leave
    them alone. Claims with no match are the reviewer's priority: the library not
    covering them says nothing about whether they are true.

    Parameters
    ----------
    matched_claims_s3_uri : str | None
        S3 URI of the batch's `matched_*.json` written by `match_claims`, or None
        when the review is running without a claims library.

    Returns
    -------
    str
        A markdown block to inject into the reviewer's system prompt.
    """
    if not matched_claims_s3_uri:
        return NO_CLAIMS_CONTEXT

    records = read_s3_json(matched_claims_s3_uri)
    if not isinstance(records, list) or not records:
        return NO_CLAIMS_CONTEXT

    buckets: dict[str, list[str]] = {status: [] for status in CLAIM_MATCH_STATUSES}
    for record in records:
        if not isinstance(record, dict):
            continue
        status = record.get("match_status")
        if status not in buckets:
            continue
        line = f'- p{record.get("page", "?")}: "{record.get("text", "")}"'
        matched_id = record.get("matched_claim_id")
        if status == "exact" and matched_id:
            line += f" (approved claim {matched_id})"
        elif status == "partial" and matched_id:
            line += f" — deviates from {matched_id}: {record.get('deviation', '')}"
        buckets[status].append(line)

    sections = [
        (
            "Already approved word for word by a human reviewer — do NOT flag these"
            " and do not spend lookups on them:",
            buckets["exact"],
        ),
        (
            "Same assertion as an approved claim but with deviated wording. The"
            " deviation is already recorded; verify whether what the content actually"
            " says is supportable:",
            buckets["partial"],
        ),
        (
            "Not covered by the claims library, so nothing is known about them yet."
            " These are your priority — verify each one:",
            buckets["none"],
        ),
    ]
    blocks = [f"{heading}\n" + "\n".join(lines) for heading, lines in sections if lines]
    return "\n\n".join(blocks) if blocks else NO_CLAIMS_CONTEXT


def run_inner_agent_text(
    system_prompt: str,
    user_prompt: str,
    tools: list,
) -> str:
    """Run a narrow sub-agent and return its final text verbatim."""
    agent = Agent(
        model=build_reviewer_model(),
        system_prompt=system_prompt,
        tools=tools,
    )
    return str(agent(user_prompt))


def parse_tagged_json_array(text: str, tag: str) -> list[dict]:
    """Pull one JSON array of objects out of `<tag>...</tag>` in a model response.

    Falls back to parsing the whole response when the tag is absent, and returns
    an empty list when the payload is not a JSON array of objects.
    """
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    payload = match.group(1).strip() if match else text.strip()

    # Strip accidental ```json fences
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload).strip()

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def run_inner_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list,
) -> list[dict]:
    """Run a narrow sub-agent. It must emit a single JSON array in <findings> tags."""
    text = run_inner_agent_text(system_prompt, user_prompt, tools)
    return parse_tagged_json_array(text, "findings")
