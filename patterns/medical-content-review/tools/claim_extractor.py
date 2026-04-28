# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claim extractor: extracts medical claims/statements from PDF page batches."""
import json
import os
import re
import tempfile
from io import BytesIO

import boto3
import defusedxml.ElementTree as ET
from pdf2image import convert_from_path
from strands import tool

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MODEL_ID = os.environ.get("EXTRACTOR_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

SYSTEM = """Extract claims/statements from medical content page images that need fact-checking.
For each, provide: text (exact quote), reference (if any), full_claim (normalized), page number.
Output as XML: <list><item><text>...</text><reference>...</reference><full_claim>...</full_claim><page>N</page></item></list>
Skip generic text that doesn't need verification."""

PROMPT = "Extract all medical claims and statements from these pages that need to be verified."


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    return path.split("/", 1)


def _parse_tagged(text: str, tag: str) -> str:
    if f"<{tag}>" in text and f"</{tag}>" in text:
        return text.split(f"<{tag}>", 1)[-1].rsplit(f"</{tag}>", 1)[0].strip()
    return text.strip()


def _xml_to_claims(xml_str: str) -> list:
    xml_str = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_str)
    xml_str = re.sub(r"&(?!(?:amp|lt|gt|quot|apos);)", "&amp;", xml_str)
    if not xml_str.strip().startswith("<root>"):
        xml_str = f"<root>{xml_str}</root>"
    root = ET.fromstring(xml_str)
    claims = []
    for item in root.findall("item"):
        claim = {}
        for child in item:
            val = (child.text or "").strip()
            claim[child.tag] = int(val) if child.tag == "page" and val.isdigit() else val
        claims.append(claim)
    return claims


@tool
def extract_claims(s3_uri: str, pages: list[int]) -> str:
    """Extract medical claims and statements from a batch of PDF pages.

    Args:
        s3_uri: S3 URI of the PDF file
        pages: List of page numbers to process as a batch (e.g. [1, 2, 3])

    Returns:
        JSON with pages, claims list, and count.
    """
    bucket, key = _parse_s3_uri(s3_uri)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        s3_client.download_file(bucket, key, tmp.name)

        content = []
        for page_num in pages:
            images = convert_from_path(tmp.name, dpi=200, first_page=page_num, last_page=page_num, thread_count=2)
            buf = BytesIO()
            images[0].save(buf, format="JPEG")
            content.append({"image": {"format": "jpeg", "source": {"bytes": buf.getvalue()}}})
        content.append({"text": PROMPT})

        response = bedrock_client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": content}],
            system=[{"text": SYSTEM}],
            inferenceConfig={"maxTokens": 8192, "temperature": 0},
        )

        raw = response["output"]["message"]["content"][0]["text"]
        try:
            claims = _xml_to_claims(_parse_tagged(raw, "list"))
        except Exception:
            claims = []

    return json.dumps({"pages": pages, "claims": claims, "count": len(claims)})
