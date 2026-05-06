# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF processor: converts PDF pages to markdown via multimodal OCR and stores on S3."""

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path, PurePosixPath

import boto3
from pdf2image import convert_from_path, pdfinfo_from_path
from strands import tool

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")

MAX_PAGES = 200
MAX_PARALLEL_PAGES = 5
OCR_MODEL_ID = os.environ.get(
    "OCR_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
)
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
MARKDOWN_PREFIX = "markdowns"

OCR_SYSTEM = (Path(__file__).parent.parent / "prompts" / "pdf_ocr.txt").read_text()


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    bucket, key = path.split("/", 1)
    return bucket, key


def _stem_from_s3_key(key: str) -> str:
    return PurePosixPath(key).stem


def _sanitise_stem(name: str) -> str:
    """Turn an arbitrary filename into an S3-key-safe stem."""
    stem = PurePosixPath(name).stem.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = stem.strip("._-")
    return stem or "document"


def _ocr_single_page(pdf_path: str, page_idx: int, dpi: int) -> tuple[int, str]:
    """OCR one PDF page. Returns (page_idx, markdown_text)."""
    images = convert_from_path(
        pdf_path,
        dpi=dpi,
        first_page=page_idx,
        last_page=page_idx,
        thread_count=1,
    )
    buf = BytesIO()
    images[0].save(buf, format="JPEG")

    response = bedrock_client.converse(
        modelId=OCR_MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": f"Extract all content from page {page_idx} as markdown."},
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
    return page_idx, page_text


@tool
def process_pdf(s3_uri: str, original_filename: str = "", dpi: int = 200) -> str:
    """Convert a PDF from S3 into a single markdown file and upload it to S3.

    Pages are OCR'd in parallel (5 at a time) via multimodal LLM calls. The result
    is a concatenation of per-page sections wrapped in [page N]...[/page N] tags,
    containing faithful markdown of the page contents including pipe-formatted tables
    and prose descriptions of every figure/diagram.

    Parameters
    ----------
    s3_uri : str
        S3 URI of the input PDF, e.g. `s3://bucket/path/document.pdf`.
    original_filename : str
        Human-readable filename the user uploaded (e.g. "dossier.pdf"). Used to
        name the output markdown. If empty or missing, falls back to the S3 key's
        stem.
    dpi : int
        Resolution at which pages are rasterized before OCR. Defaults to 200.

    Returns
    -------
    str
        The S3 URI of the uploaded markdown file (suitable as input to
        `batch_content` or reviewer tools). Nothing else is returned.
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")

    bucket, key = _parse_s3_uri(s3_uri)
    stem = (
        _sanitise_stem(original_filename)
        if original_filename
        else _stem_from_s3_key(key)
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        s3_client.download_file(bucket, key, tmp.name)
        info = pdfinfo_from_path(tmp.name)
        total_pages = min(info["Pages"], MAX_PAGES)

        results: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PAGES) as pool:
            futures = [
                pool.submit(_ocr_single_page, tmp.name, p, dpi)
                for p in range(1, total_pages + 1)
            ]
            for fut in futures:
                page_idx, text = fut.result()
                results[page_idx] = text

    parts = [f"[page {p}]\n{results[p]}\n[/page {p}]" for p in sorted(results)]
    document_markdown = f"# Markdown of {stem}\n\n" + "\n\n".join(parts)

    out_key = f"{MARKDOWN_PREFIX}/{stem}.md"
    s3_client.put_object(
        Bucket=STAGING_BUCKET,
        Key=out_key,
        Body=document_markdown.encode("utf-8"),
        ContentType="text/markdown",
    )
    return f"s3://{STAGING_BUCKET}/{out_key}"
