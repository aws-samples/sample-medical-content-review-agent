# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF processor: converts PDF pages to images and extracts text via multimodal OCR."""

import os
import tempfile
from io import BytesIO

import boto3
from pdf2image import convert_from_path, pdfinfo_from_path
from strands import tool

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MAX_PAGES_PER_CALL = 50
OCR_MODEL_ID = os.environ.get(
    "OCR_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
)

OCR_SYSTEM = "Extract ALL text from the page image as clean markdown. Preserve headings, lists, tables, footnotes, references. Describe visual elements in [brackets]."


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    bucket, key = path.split("/", 1)
    return bucket, key


@tool
def process_pdf(
    s3_uri: str, start_page: int = 1, end_page: int = 0, dpi: int = 200
) -> str:
    """Process a medical content PDF from S3: converts pages to images and extracts text via multimodal OCR.

    Args:
        s3_uri: S3 URI of the PDF file (e.g. 's3://bucket/path/document.pdf')
        start_page: First page to process, 1-indexed (default 1)
        end_page: Last page to process inclusive (default 0 means all pages, max 50 per call)
        dpi: Image resolution for page rendering (default 200)

    Returns:
        Extracted markdown text with page tags.
    """
    bucket, key = _parse_s3_uri(s3_uri)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        s3_client.download_file(bucket, key, tmp.name)
        info = pdfinfo_from_path(tmp.name)
        total_pages = info["Pages"]

        actual_end = min(
            end_page if end_page > 0 else total_pages,
            total_pages,
            start_page + MAX_PAGES_PER_CALL - 1,
        )

        document_markdown = ""
        for page_idx in range(start_page, actual_end + 1):
            images = convert_from_path(
                tmp.name,
                dpi=dpi,
                first_page=page_idx,
                last_page=page_idx,
                thread_count=2,
            )
            buf = BytesIO()
            images[0].save(buf, format="JPEG")

            response = bedrock_client.converse(
                modelId=OCR_MODEL_ID,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": f"Extract all text from page {page_idx} as markdown."
                            },
                            {
                                "image": {
                                    "format": "jpeg",
                                    "source": {"bytes": buf.getvalue()},
                                }
                            },
                        ],
                    }
                ],
                system=[{"text": OCR_SYSTEM}],
                inferenceConfig={"maxTokens": 8192, "temperature": 0},
            )
            page_text = response["output"]["message"]["content"][0]["text"].strip()
            document_markdown += (
                f"\n<page_{page_idx}>\n{page_text}\n</page_{page_idx}>\n"
            )

    return f"Processed pages {start_page}-{actual_end} of {total_pages} from {s3_uri}\n\n{document_markdown}"
