# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Content batcher: splits a markdown document into per-batch markdown files on S3."""

import ast
import json
import os
import re
from pathlib import Path, PurePosixPath

import boto3
from strands import tool
from utils.inference import sampling_params

from tools.asset_audience import write_asset_audience
from tools.run_record import note_issue

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MODEL_ID = os.environ.get(
    "BATCHER_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5"),
)
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
MARKDOWN_PREFIX = "markdowns"

SYSTEM = (Path(__file__).parent.parent / "prompts" / "batch_content.txt").read_text()

PROMPT_TEMPLATE = (
    "Here is the full document as markdown. Pages are delimited by"
    " `[page N]` / `[/page N]` tags.\n\n"
    "Group the pages into logical review batches as specified.\n\n"
    "Document:\n{markdown}"
)

PAGE_PATTERN = re.compile(r"\[page (\d+)\]\n(.*?)\n\[/page \1\]", re.DOTALL)
AUDIENCE_RE = re.compile(r"<audience>\s*(hcp|patient)\s*</audience>", re.IGNORECASE)
AUDIENCE_REASON_RE = re.compile(r"<audience_reason>(.*?)</audience_reason>", re.DOTALL)


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    return path.split("/", 1)


def _parse_tagged(text: str, tag: str) -> str:
    if f"<{tag}>" in text and f"</{tag}>" in text:
        return text.split(f"<{tag}>", 1)[-1].rsplit(f"</{tag}>", 1)[0].strip()
    return text.strip()


def _load_pages(markdown: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    for match in PAGE_PATTERN.finditer(markdown):
        pages[int(match.group(1))] = match.group(2).strip()
    return pages


@tool
def batch_content(markdown_s3_uri: str, session_id: str = "") -> str:
    """Split a markdown document into batches and upload each as its own file.

    The input must be an S3 URI produced by `process_pdf`. The tool asks the model
    to group pages into coherent review units (content + supporting reference
    pages), then writes one markdown file per batch back to S3 under the same prefix
    as the input. Each batch file preserves the `[page N]` tags of its member pages.
    The same call decides who the document is written for (HCP or patient), and
    stores it for `match_claims` to read back.

    Parameters
    ----------
    markdown_s3_uri : str
        S3 URI of a markdown file produced by `process_pdf`.
    session_id : str
        Review session id, which keys the stored audience; without one, no audience
        is stored and library claims of every audience are matched.

    Returns
    -------
    str
        JSON string with shape `{"total_pages": N, "asset_audience": "hcp" |
        "patient" | "", "batches": [{"pages": [...], "s3_uri": "..."}, ...]}`. Only
        S3 URIs are returned — inline content is not included.
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")

    bucket, key = _parse_s3_uri(markdown_s3_uri)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    pages = _load_pages(body)
    if not pages:
        raise ValueError(f"No [page N] sections found in {markdown_s3_uri}")
    total_pages = max(pages)

    response = bedrock_client.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [{"text": PROMPT_TEMPLATE.format(markdown=body)}],
            }
        ],
        system=[{"text": SYSTEM}],
        inferenceConfig={"maxTokens": 2048, **sampling_params(MODEL_ID, 0.0)},
    )
    # Models that think first return a reasoning block before the text
    blocks = response["output"]["message"]["content"]
    raw = "\n".join(b["text"] for b in blocks if "text" in b)
    batch_page_lists: list[list[int]] = ast.literal_eval(_parse_tagged(raw, "chunks"))

    audience_match = AUDIENCE_RE.search(raw)
    audience = audience_match.group(1).lower() if audience_match else ""
    if session_id:
        reason_match = AUDIENCE_REASON_RE.search(raw)
        write_asset_audience(
            session_id, audience, reason_match.group(1).strip() if reason_match else ""
        )
        if not audience:
            note_issue(
                session_id,
                "batch",
                "the batcher did not say who the document is for, so library claims"
                " of every audience are matched",
                severity="warning",
            )

    stem = PurePosixPath(key).stem
    written: list[dict] = []
    for i, page_nums in enumerate(batch_page_lists, start=1):
        parts = [
            f"[page {p}]\n{pages[p]}\n[/page {p}]" for p in page_nums if p in pages
        ]
        batch_md = (
            f"# Batch {i} (pages {', '.join(map(str, page_nums))})\n\n"
            + "\n\n".join(parts)
        )
        out_key = f"{MARKDOWN_PREFIX}/{stem}_batch_{i}.md"
        s3_client.put_object(
            Bucket=STAGING_BUCKET,
            Key=out_key,
            Body=batch_md.encode("utf-8"),
            ContentType="text/markdown",
        )
        written.append(
            {"pages": page_nums, "s3_uri": f"s3://{STAGING_BUCKET}/{out_key}"}
        )

    return json.dumps(
        {"total_pages": total_pages, "asset_audience": audience, "batches": written}
    )
