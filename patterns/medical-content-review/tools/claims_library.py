# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claims library loader: parses an approved-claims Excel/CSV file into JSON on S3."""

import json
import os

import boto3
from botocore.config import Config
from strands import tool

from tools.claims_parser import library_key, parse_claims_file

s3_client = boto3.client(
    "s3",
    region_name=os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    ),
    config=Config(s3={"addressing_style": "virtual"}),
)

STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
CLAIMS_PREFIX = "claims"

# The frontend previews the parsed library next to the uploaded PDFs, so it needs to be
# able to fetch it directly
URL_EXPIRATION = 3600


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    path = s3_uri[5:]
    bucket, key = path.split("/", 1)
    return bucket, key


@tool
def load_claims_library(s3_uri: str, original_filename: str = "") -> str:
    """Parse a pre-approved claims file (.xlsx/.xlsm/.csv) from S3 into JSON on S3.

    The claims library is the list of statements a medical/regulatory team has
    already cleared. It is the FIRST thing content claims are checked against —
    only claims with no approved match need to be verified against reference
    documents or public databases.

    Column headers are matched case-insensitively against common spellings
    (`claim_text`/`claim`/`text`, `claim_id`/`id`, `status`, `primary_reference`,
    `usage_restrictions`, ...), then against looser patterns for the columns that
    change the outcome ("Approved Claim Wording", "MLR Approval State", ...). The
    header row is located rather than assumed, so a title row above it is fine.
    Unrecognised columns are preserved under `extra`. Rows without any claim text
    are skipped, as are non-table sheets such as a README tab.

    Parameters
    ----------
    s3_uri : str
        S3 URI of the uploaded claims file, e.g. `s3://bucket/uploads/x.xlsx`.
    original_filename : str
        Human-readable filename the user uploaded. Used to name the output JSON
        and to pick the parser when the S3 key has no useful extension.

    Returns
    -------
    str
        JSON string of shape `{"claims_s3_uri": "s3://...", "total_claims": N,
        "by_status": {...}, "columns": [...], "column_mapping": {...},
        "unmapped_columns": [...]}`, followed by a `[CLAIMS_LIB_URL:...]` tag holding
        a pre-signed URL of the parsed library so the frontend can show it beside the
        uploaded documents. The claim texts themselves are NOT returned — pass
        `claims_s3_uri` on to `match_claims`.
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")

    bucket, key = _parse_s3_uri(s3_uri)
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()

    parsed = parse_claims_file(body, original_filename or key)
    claims = parsed["claims"]

    # Same key the upload API writes, so parsing an already-previewed library just
    # rewrites identical content instead of leaving a second copy behind
    out_key = library_key(key, original_filename, CLAIMS_PREFIX)
    s3_client.put_object(
        Bucket=STAGING_BUCKET,
        Key=out_key,
        Body=json.dumps(claims, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    result = json.dumps(
        {
            "claims_s3_uri": f"s3://{STAGING_BUCKET}/{out_key}",
            "total_claims": len(claims),
            "by_status": parsed["by_status"],
            "columns": parsed["columns"],
            "column_mapping": parsed["column_mapping"],
            "unmapped_columns": parsed["unmapped_columns"],
        }
    )

    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": STAGING_BUCKET, "Key": out_key},
            ExpiresIn=URL_EXPIRATION,
        )
        result += f"\n\n[CLAIMS_LIB_URL:{url}]"
    except Exception as e:  # noqa: BLE001 - the library itself is already persisted
        print(f"[CLAIMS] Failed to pre-sign the claims library URL: {e}")

    return result
