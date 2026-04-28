# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Content batcher: splits PDF pages into logical batches using multimodal LLM."""
import ast
import json
import os
import tempfile
from io import BytesIO

import boto3
from pdf2image import convert_from_path, pdfinfo_from_path
from strands import tool

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MODEL_ID = os.environ.get("BATCHER_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

SYSTEM = """You split medical content pages into logical chunks.
Group each content page with its associated reference/footnote pages.
Output a Python list in <chunks></chunks> tags. Example: <chunks>[[1, 2], [3], [4, 5]]</chunks>"""

PROMPT = "Split these pages into logical content batches."


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    return path.split("/", 1)


def _parse_tagged(text: str, tag: str) -> str:
    if f"<{tag}>" in text and f"</{tag}>" in text:
        return text.split(f"<{tag}>", 1)[-1].rsplit(f"</{tag}>", 1)[0].strip()
    return text.strip()


@tool
def batch_content(s3_uri: str, batch_size: int = 5) -> str:
    """Split a medical content PDF into logical page batches grouping content with its reference pages.

    Args:
        s3_uri: S3 URI of the PDF file
        batch_size: Max pages per sliding window for analysis (default 5)

    Returns:
        JSON with total_pages and batches list.
    """
    bucket, key = _parse_s3_uri(s3_uri)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        s3_client.download_file(bucket, key, tmp.name)
        total_pages = pdfinfo_from_path(tmp.name)["Pages"]

        all_batches = []
        page_idx = 1

        while page_idx <= total_pages:
            end = min(page_idx + batch_size - 1, total_pages)
            images = convert_from_path(tmp.name, dpi=150, first_page=page_idx, last_page=end, thread_count=2)

            if len(images) <= 1:
                all_batches.append([page_idx])
                break

            content = [{"image": {"format": "jpeg", "source": {"bytes": BytesIO(b"").getvalue()}}} for _ in images]
            for i, img in enumerate(images):
                buf = BytesIO()
                img.save(buf, format="JPEG")
                content[i] = {"image": {"format": "jpeg", "source": {"bytes": buf.getvalue()}}}
            content.append({"text": PROMPT})

            response = bedrock_client.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": content}],
                system=[{"text": SYSTEM}],
                inferenceConfig={"maxTokens": 4096, "temperature": 0},
            )

            raw = response["output"]["message"]["content"][0]["text"]
            current = ast.literal_eval(_parse_tagged(raw, "chunks"))
            current = [[x + page_idx - 1 for x in batch] for batch in current]

            if current[-1][-1] < total_pages:
                current = current[:-1]

            all_batches.extend(current)
            page_idx = all_batches[-1][-1] + 1 if all_batches else total_pages + 1

    return json.dumps({"total_pages": total_pages, "batches": all_batches})
