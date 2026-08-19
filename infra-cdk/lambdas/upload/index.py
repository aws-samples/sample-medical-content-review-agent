# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Upload API Lambda - pre-signed S3 URLs, plus claims-spreadsheet parsing.

The claims library is parsed here as well as inside the agent so the UI can show the
approved claims as soon as the file is uploaded, instead of only once the review has
started. Both call the very same parser module (`claims_parser`, copied into this
asset at synth time from patterns/medical-content-review/tools/) and write the same
S3 key, so the preview always shows what the matcher will actually use. This endpoint
is an optimisation, never a precondition: a caller invoking the agent runtime directly
skips it, and `load_claims_library` parses the file during the run exactly as before.
"""

import json
import os
import re
import uuid

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    InternalServerError,
)
from aws_lambda_powertools.logging.correlation_paths import API_GATEWAY_REST
from aws_lambda_powertools.utilities.typing import LambdaContext
from claims_parser import library_key, parse_claims_file
from pydantic import BaseModel, Field

BUCKET_NAME = os.environ["BUCKET_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

# Only files this API itself handed out an upload URL for may be parsed, and a claims
# library that does not fit in a request/response is a configuration problem, not a
# file to stream
UPLOADS_PREFIX = "uploads/"
MAX_CLAIMS_FILE_BYTES = 20 * 1024 * 1024

# The shape `/upload` mints below, so the parse endpoint accepts only keys this API
# could have created itself rather than denylisting the traversal spellings
UPLOADED_KEY_RE = re.compile(rf"{UPLOADS_PREFIX}[0-9a-f]{{32}}\.[a-z0-9]{{1,16}}")
# A client filename decides the extension, so keep it to plain characters: it ends up
# in a key, and "a.b/c" would otherwise put a slash there
EXTENSION_RE = re.compile(r"[^A-Za-z0-9]")

cors_origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
cors_config = CORSConfig(
    allow_origin=cors_origins[0] if cors_origins else "*",
    extra_origins=cors_origins[1:] if len(cors_origins) > 1 else None,
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayRestResolver(cors=cors_config)
s3_client = boto3.client("s3")


class UploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default="application/pdf")


@app.post("/upload")
@tracer.capture_method
def create_upload_url():
    body: dict = app.current_event.json_body  # type: ignore[assignment]
    req = UploadRequest(**body)

    raw_ext = req.filename.rsplit(".", 1)[-1] if "." in req.filename else "pdf"
    ext = EXTENSION_RE.sub("", raw_ext).lower()[:16] or "bin"
    key = f"{UPLOADS_PREFIX}{uuid.uuid4().hex}.{ext}"

    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": key, "ContentType": req.content_type},
        ExpiresIn=300,
    )

    return {
        "uploadUrl": url,
        "s3Uri": f"s3://{BUCKET_NAME}/{key}",
        "key": key,
    }


class ParseClaimsRequest(BaseModel):
    s3Uri: str = Field(..., min_length=6, max_length=1024)  # noqa: N815
    filename: str = Field(default="", max_length=255)


def _uploaded_key(s3_uri: str) -> str:
    """Validate an `s3://` URI and return its key, refusing anything but an upload

    Parameters
    ----------
    s3_uri : str
        URI as sent by the client

    Returns
    -------
    str
        The object key inside the staging bucket

    Raises
    ------
    BadRequestError
        When the URI points anywhere other than this bucket's uploads prefix, so the
        endpoint cannot be used to read other objects
    """
    if not s3_uri.startswith("s3://"):
        raise BadRequestError("s3Uri must be an s3:// URI")
    bucket, _, key = s3_uri[5:].partition("/")
    if bucket != BUCKET_NAME or not UPLOADED_KEY_RE.fullmatch(key):
        raise BadRequestError(f"s3Uri must point at {BUCKET_NAME}/{UPLOADS_PREFIX}")
    return key


@app.post("/claims/parse")
@tracer.capture_method
def parse_claims():
    """Parse an uploaded claims spreadsheet and return its rows for the preview"""
    body: dict = app.current_event.json_body  # type: ignore[assignment]
    req = ParseClaimsRequest(**body)
    key = _uploaded_key(req.s3Uri)

    head = s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
    if head["ContentLength"] > MAX_CLAIMS_FILE_BYTES:
        raise BadRequestError(
            f"The claims file is larger than {MAX_CLAIMS_FILE_BYTES // (1024 * 1024)}MB"
        )

    raw = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    try:
        parsed = parse_claims_file(raw, req.filename or key)
    except ValueError as e:
        # The message names the columns that were read, which is what the user needs
        # in order to fix the spreadsheet
        raise BadRequestError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse the claims file")
        raise InternalServerError("Could not read the claims file") from e

    out_key = library_key(key, req.filename)
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=out_key,
        Body=json.dumps(parsed["claims"], indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    return {
        "claimsS3Uri": f"s3://{BUCKET_NAME}/{out_key}",
        "totalClaims": len(parsed["claims"]),
        "byStatus": parsed["by_status"],
        "columns": parsed["columns"],
        "columnMapping": parsed["column_mapping"],
        "unmappedColumns": parsed["unmapped_columns"],
        "headerRow": parsed["header_row"],
        "claims": parsed["claims"],
    }


@logger.inject_lambda_context(correlation_id_path=API_GATEWAY_REST)
@tracer.capture_lambda_handler
def handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
