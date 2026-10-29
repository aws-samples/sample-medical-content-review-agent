# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF processor: converts PDF pages to markdown via multimodal OCR and stores on S3."""

import hashlib
import math
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path, PurePosixPath

import boto3
from botocore.exceptions import ClientError
from pdf2image import convert_from_path, pdfinfo_from_path
from PIL import Image
from strands import tool
from utils.inference import get_bedrock_config, sampling_params
from utils.s3 import (
    STAGING_BUCKET,
    is_not_found,
    parse_s3_uri,
    put_bytes,
    require_bucket,
    s3_client,
    safe_session_id,
)

from tools.run_record import note_issue

bedrock_client = boto3.client("bedrock-runtime", config=get_bedrock_config())

MAX_PAGES = 200
UNREADABLE = "[Unreadable page:"
MAX_PARALLEL_PAGES = 5
OCR_MODEL_ID = os.environ.get(
    "OCR_MODEL_ID",
    os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5"),
)
MARKDOWN_PREFIX = "markdowns"

# The model reads at most 2576 px on the long edge and 3.75 MP per image, and Bedrock
# rejects images over 3.75 MB. Anything larger is downscaled until small print is lost.
MAX_IMAGE_EDGE = 2576
MAX_IMAGE_PIXELS = 3_750_000
MAX_IMAGE_BYTES = 3_750_000
# Tall pages (emails, ISI banners) are cut top to bottom into tiles of about a portrait
# page each, overlapping so a line cut at a boundary is whole in one of the two tiles
TILE_ASPECT = 1.4
TILE_OVERLAP = 0.08
MAX_TILES = 20

OCR_MAX_TOKENS = 32_000
OCR_ATTEMPTS = 3

# pdftotext keeps typographic ligatures, which would break word matching downstream
LIGATURES = str.maketrans(
    {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
)


class TruncatedTranscription(RuntimeError):
    """The model hit the output limit; retrying the same request cannot help."""


OCR_SYSTEM = (Path(__file__).parent.parent / "prompts" / "pdf_ocr.txt").read_text()

# Page transcriptions are reused across reviews of the same PDF, keyed by its content
# and by everything that shapes a transcription, so a model, prompt or rendering change
# starts a fresh cache instead of serving stale pages
CACHE_PREFIX = f"{MARKDOWN_PREFIX}/cache"


def _ocr_version(dpi: int) -> str:
    settings = (
        OCR_MODEL_ID,
        OCR_SYSTEM,
        dpi,
        MAX_IMAGE_EDGE,
        MAX_IMAGE_PIXELS,
        TILE_ASPECT,
        TILE_OVERLAP,
        MAX_TILES,
    )
    return hashlib.sha256(repr(settings).encode("utf-8")).hexdigest()[:12]


def _cached_page(key: str) -> str | None:
    try:
        body = s3_client.get_object(Bucket=STAGING_BUCKET, Key=key)["Body"].read()
    except ClientError as e:
        # Without an unconditional s3:ListBucket, S3 answers a missing key with 403
        if is_not_found(e) or e.response.get("Error", {}).get("Code") in (
            "AccessDenied",
            "403",
        ):
            return None
        raise
    return body.decode("utf-8")


def _stem_from_s3_key(key: str) -> str:
    return PurePosixPath(key).stem


def _sanitise_stem(name: str) -> str:
    """Turn an arbitrary filename into an S3-key-safe stem."""
    stem = PurePosixPath(name).stem.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = stem.strip("._-")
    return stem or "document"


def _text_layer(pdf_path: str, page_idx: int) -> str:
    """Return the text embedded in one PDF page, or "" for an image-only page."""
    result = subprocess.run(
        [
            "pdftotext",
            "-q",
            "-enc",
            "UTF-8",
            "-f",
            str(page_idx),
            "-l",
            str(page_idx),
            pdf_path,
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.translate(LIGATURES).strip()


def _render_dpi(pdf_path: str, page_idx: int, dpi: int) -> float:
    """Lower the DPI so the page renders no wider than the model reads

    Interactive visual aids and display pages are 30+ inches wide: at 200 DPI that is a
    30-80 MP image, which `_fit` shrinks to 2576 px anyway after costing seconds and
    hundreds of MB.
    Only the width is capped, since tall pages are tiled top to bottom.
    """
    out = subprocess.run(
        ["pdfinfo", "-f", str(page_idx), "-l", str(page_idx), pdf_path],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    size = re.search(rf"Page\s+{page_idx} size:\s+([\d.]+) x ([\d.]+)", out)
    if not size:
        return dpi
    rot = re.search(rf"Page\s+{page_idx} rot:\s+(\d+)", out)
    width, height = float(size.group(1)), float(size.group(2))
    if rot and int(rot.group(1)) % 180 == 90:
        width = height
    return min(dpi, MAX_IMAGE_EDGE * 72 / width)


def _fit(image: Image.Image) -> Image.Image:
    width, height = image.size
    scale = min(
        1.0,
        MAX_IMAGE_EDGE / max(width, height),
        math.sqrt(MAX_IMAGE_PIXELS / (width * height)),
    )
    if scale >= 1.0:
        return image
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _tiles(image: Image.Image) -> list[Image.Image]:
    """Cut a page image into model-sized tiles, top to bottom."""
    width, height = image.size
    tile_height = int(width * TILE_ASPECT)
    if height <= tile_height * 1.15:
        return [_fit(image)]

    overlap = int(tile_height * TILE_OVERLAP)
    count = math.ceil((height - overlap) / (tile_height - overlap))
    if count > MAX_TILES:
        count = MAX_TILES
        tile_height = math.ceil((height - overlap) / count) + overlap
    step = (height - tile_height) / (count - 1)
    return [
        _fit(image.crop((0, round(i * step), width, round(i * step) + tile_height)))
        for i in range(count)
    ]


def _jpeg(image: Image.Image) -> bytes:
    for quality in (85, 75, 60):
        buf = BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    return buf.getvalue()


def _transcribe(page_idx: int, tiles: list[bytes], text_layer: str) -> str:
    intro = f"Page {page_idx}, as {len(tiles)} image(s) from top to bottom."
    if text_layer:
        intro += (
            "\n\nThe page's text layer, extracted from the PDF:\n<text_layer>\n"
            f"{text_layer}\n</text_layer>"
        )
    content = [{"text": intro + "\n\nExtract all content from this page as markdown."}]
    content += [{"image": {"format": "jpeg", "source": {"bytes": t}}} for t in tiles]

    response = bedrock_client.converse(
        modelId=OCR_MODEL_ID,
        messages=[{"role": "user", "content": content}],
        system=[{"text": OCR_SYSTEM}],
        inferenceConfig={
            "maxTokens": OCR_MAX_TOKENS,
            **sampling_params(OCR_MODEL_ID, 0.0),
        },
    )
    if response.get("stopReason") == "max_tokens":
        raise TruncatedTranscription("transcription was cut off at the output limit")
    blocks = response["output"]["message"]["content"]
    text = "\n".join(b["text"] for b in blocks if "text" in b).strip()
    if not text:
        raise RuntimeError("the model returned no text")
    return text


def _ocr_single_page(pdf_path: str, page_idx: int, dpi: int) -> tuple[int, str, str]:
    """OCR one PDF page. Returns (page_idx, markdown_text, failure reason or "")."""
    try:
        text_layer = _text_layer(pdf_path, page_idx)
        image = convert_from_path(
            pdf_path,
            dpi=_render_dpi(pdf_path, page_idx, dpi),
            first_page=page_idx,
            last_page=page_idx,
            thread_count=1,
            # Reviewer comment boxes and sticky notes are drawn over the content
            hide_annotations=True,
        )[0]
        tiles = [_jpeg(t) for t in _tiles(image)]
        # the decoded page would otherwise stay alive through the model call
        del image
    except Exception as e:  # noqa: BLE001 - one bad page must not fail the document
        return page_idx, f"{UNREADABLE} it could not be rendered ({e})]", str(e)

    error = ""
    for attempt in range(OCR_ATTEMPTS):
        try:
            return page_idx, _transcribe(page_idx, tiles, text_layer), ""
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            print(f"[OCR] page {page_idx} attempt {attempt + 1} failed: {error}")
            if isinstance(e, TruncatedTranscription):
                break
            time.sleep(2**attempt)

    if text_layer:
        note = (
            f"[Text layer only: the page image could not be transcribed ({error})."
            " Reading order, tables and figures are missing.]"
        )
        return page_idx, f"{note}\n\n{text_layer}", error
    return page_idx, f"{UNREADABLE} it could not be transcribed ({error})]", error


@tool
def process_pdf(
    s3_uri: str, original_filename: str = "", session_id: str = "", dpi: int = 200
) -> str:
    """Convert a PDF from S3 into a single markdown file and upload it to S3.

    Pages are OCR'd in parallel (5 at a time) via multimodal LLM calls, grounded in
    the PDF's own text layer where it has one. The result is a concatenation of
    per-page sections wrapped in [page N]...[/page N] tags, containing faithful
    markdown of the page contents including pipe-formatted tables and prose
    descriptions of every figure/diagram. A page that cannot be transcribed is kept
    with a note saying so instead of failing the whole document. Transcribed pages are
    cached under `markdowns/cache/`, so a PDF reviewed again is not transcribed again.

    Parameters
    ----------
    s3_uri : str
        S3 URI of the input PDF, e.g. `s3://bucket/path/document.pdf`.
    original_filename : str
        Human-readable filename the user uploaded (e.g. "dossier.pdf"). Used to
        name the output markdown. If empty or missing, falls back to the S3 key's
        stem.
    session_id : str
        Review session id. Namespaces the output so concurrent reviews of the
        same file do not overwrite each other.
    dpi : int
        Resolution at which pages are rasterized before OCR. Defaults to 200.

    Returns
    -------
    str
        The S3 URI of the uploaded markdown file (suitable as input to
        `batch_content` or reviewer tools).
    """
    require_bucket()
    bucket, key = parse_s3_uri(s3_uri)
    stem = (
        _sanitise_stem(original_filename)
        if original_filename
        else _stem_from_s3_key(key)
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        s3_client.download_file(bucket, key, tmp.name)
        info = pdfinfo_from_path(tmp.name)
        total_pages = min(info["Pages"], MAX_PAGES)
        with open(tmp.name, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        cache_dir = f"{CACHE_PREFIX}/{_ocr_version(dpi)}/{digest}"

        def page(page_idx: int) -> tuple[int, str, str]:
            cache_key = f"{cache_dir}/{page_idx}.md"
            cached = _cached_page(cache_key)
            if cached is not None:
                return page_idx, cached, ""
            result = _ocr_single_page(tmp.name, page_idx, dpi)
            if not result[2]:
                # failed pages are not cached, so the next review retries them
                put_bytes(cache_key, result[1].encode("utf-8"), "text/markdown")
            return result

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PAGES) as pool:
            pages = list(pool.map(page, range(1, total_pages + 1)))

    failed = [p for p, _, error in pages if error]
    if len(failed) == len(pages):
        raise RuntimeError(f"No page of {stem} could be transcribed")
    unreadable = [
        p for p, text, error in pages if error and text.startswith(UNREADABLE)
    ]
    if unreadable:
        note_issue(
            session_id, "ocr", f"pages {unreadable} could not be read", document=stem
        )
    if len(unreadable) < len(failed):
        note_issue(
            session_id,
            "ocr",
            f"pages {[p for p in failed if p not in unreadable]} were read from the text"
            " layer only",
            severity="warning",
            document=stem,
        )
    if info["Pages"] > MAX_PAGES:
        note_issue(
            session_id,
            "ocr",
            f"only the first {MAX_PAGES} of {info['Pages']} pages were read",
            document=stem,
        )

    parts = [f"[page {p}]\n{text}\n[/page {p}]" for p, text, _ in pages]
    document_markdown = f"# Markdown of {stem}\n\n" + "\n\n".join(parts)

    session_dir = safe_session_id(session_id)
    out_key = (
        f"{MARKDOWN_PREFIX}/{session_dir}/{stem}.md"
        if session_dir
        else f"{MARKDOWN_PREFIX}/{stem}.md"
    )
    return put_bytes(out_key, document_markdown.encode("utf-8"), "text/markdown")
