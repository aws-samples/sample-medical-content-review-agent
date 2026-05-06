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

s3_client = boto3.client("s3")

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Read a reviewer prompt template from patterns/medical-content-review/prompts/."""
    return (PROMPTS_DIR / f"{name}.txt").read_text()


INFERENCE_CONFIG, _ = get_inference_configs()
BEDROCK_CONFIG = get_bedrock_config()
MODEL_ID = os.environ.get(
    "REVIEWER_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
)
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
REVIEWS_PREFIX = "reviews"


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


def batch_stem(batch_md_s3_uri: str) -> str:
    """e.g. s3://.../markdowns/foo_batch_3.md -> foo_batch_3"""
    _, key = parse_s3_uri(batch_md_s3_uri)
    return PurePosixPath(key).stem


def write_review_json(
    session_id: str, kind: str, batch_stem_value: str, findings: list[dict]
) -> str:
    bucket = _require_bucket()
    safe_session = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    key = f"{REVIEWS_PREFIX}/{safe_session}/{kind}_{batch_stem_value}.json"
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(findings, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


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
- `score`: int, severity 1-100 (>=70 for mandatory, <50 for optional)"""


def run_inner_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list,
) -> list[dict]:
    """Run a narrow sub-agent. It must emit a single JSON array in <findings> tags."""
    agent = Agent(
        model=build_reviewer_model(),
        system_prompt=system_prompt,
        tools=tools,
    )
    result = agent(user_prompt)
    text = str(result)

    match = re.search(r"<findings>(.*?)</findings>", text, re.DOTALL)
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
