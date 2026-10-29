# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the reviewer sub-agents.

Each reviewer is a `@tool` the review pipeline calls once per batch. It runs a narrow
Strands Agent with just the tools it needs on one batch markdown, and writes its
findings JSON to S3, returning only the S3 URI.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

from strands import Agent
from strands.models import BedrockModel, CacheConfig
from tools.run_record import note_issue
from utils.inference import (
    get_bedrock_config,
    get_inference_configs,
    sampling_params,
)
from utils.s3 import (
    parse_s3_uri,
    put_json,
    read_text,
    require_bucket,
    safe_session_id,
)

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
REVIEWS_PREFIX = "reviews"
CLAIMS_PREFIX = "claims"
# kept under the name the reviewers import it by
read_s3_text = read_text


def claims_report_key(session_id: str) -> str:
    return f"{CLAIMS_PREFIX}/{safe_session_id(session_id)}/claims_report.json"


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


def _write_json(prefix: str, session_id: str, name: str, payload: list | dict) -> str:
    require_bucket()
    return put_json(f"{prefix}/{safe_session_id(session_id)}/{name}.json", payload)


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
        **sampling_params(MODEL_ID, INFERENCE_CONFIG["temperature"]),
        max_tokens=INFERENCE_CONFIG["maxTokens"],
        streaming=False,
        boto_client_config=BEDROCK_CONFIG,
        cache_config=CacheConfig(strategy="auto"),
    )


FINDINGS_SCHEMA_HINT = """Each finding object must have these fields:
- `page`: int, page number the issue was found on
- `quote`: str, exact quote from the document
- `headline`: str, the problem in at most 10 words, for a reviewer scanning a list
  ("Superlative: 'the only'", "'reduced' became 'reduces'", "Misspelt 'psoratic'")
- `issue_spans`: list of str, the words of `quote` that are the problem, each copied
  exactly from `quote` and a few words long ("the only", "reduces"); [] when the problem
  is the whole quote or something missing from it
- `issue`: str, the problem in one or two short sentences: what is wrong and why it
  matters, without restating the quote
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


# a backslash that starts no JSON escape
INVALID_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def parse_tagged_json_array(text: str, tag: str) -> list[dict] | None:
    """Pull one JSON array of objects out of `<tag>...</tag>` in a model response.

    Falls back to parsing the whole response when the tag is absent, and returns None
    when no payload is a JSON array, so a reply with no answer is told apart from an
    empty answer. When the model emits several tagged blocks ("Wait, correcting:
    ..."), the last one that parses is its answer.
    """
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL) or [text]
    for block in reversed(blocks):
        # Strip accidental ```json fences
        payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", block.strip()).strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # copy quoted from markdown keeps its escapes ("\*All trials ..."), which
            # are not JSON escapes
            try:
                data = json.loads(INVALID_ESCAPE_RE.sub(r"\\\\", payload))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return None





def run_inner_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list,
) -> list[dict] | None:
    """Run a narrow sub-agent that answers with one JSON array in <findings> tags

    Returns None when the reply holds no parsable array.
    """
    text = run_inner_agent_text(system_prompt, user_prompt, tools)
    return parse_tagged_json_array(text, "findings")


def reviewer_findings(
    findings: list[dict] | None, session_id: str, stage: str, batch: str
) -> list[dict]:
    """Findings to save for a batch, noting a reply that had none to parse"""
    if findings is None:
        note_issue(
            session_id,
            stage,
            "the reviewer's reply had no parsable findings",
            batch=batch,
        )
        return []
    return findings
