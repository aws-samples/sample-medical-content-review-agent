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

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MODEL_ID = os.environ.get(
    "BATCHER_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
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
def batch_content(markdown_s3_uri: str) -> str:
    """Split a markdown document into batches and upload each as its own file.

    The input must be an S3 URI produced by `process_pdf`. The tool asks the model
    to group pages into coherent review units (content + supporting reference
    pages), then writes one markdown file per batch back to S3 under the same prefix
    as the input. Each batch file preserves the `[page N]` tags of its member pages.

    Parameters
    ----------
    markdown_s3_uri : str
        S3 URI of a markdown file produced by `process_pdf`.

    Returns
    -------
    str
        JSON string with shape `{"total_pages": N, "batches": [{"pages": [...],
        "s3_uri": "..."}, ...]}`. Only S3 URIs are returned — inline content is
        not included.
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
        inferenceConfig={"maxTokens": 2048, "temperature": 0},
    )
    raw = response["output"]["message"]["content"][0]["text"]
    batch_page_lists: list[list[int]] = ast.literal_eval(_parse_tagged(raw, "chunks"))

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

    return json.dumps({"total_pages": total_pages, "batches": written})
