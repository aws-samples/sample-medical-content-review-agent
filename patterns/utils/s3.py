# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""S3 access shared by the review tools and reviewers."""

import json
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME")
URL_EXPIRATION = 3600
NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}

s3_client = boto3.client(
    "s3",
    region_name=os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    ),
    # signature v4 URLs work under every bucket encryption, SSE-KMS included
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


def require_bucket() -> str:
    """Return the staging bucket name, or raise when it is not configured

    Returns
    -------
    str
        The staging bucket name
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")
    return STAGING_BUCKET


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Split an S3 URI into its bucket and key

    Parameters
    ----------
    s3_uri : str
        URI of the form `s3://bucket/key`

    Returns
    -------
    tuple[str, str]
        The bucket and the key
    """
    bucket, key = s3_uri[5:].split("/", 1)
    return bucket, key


def safe_session_id(session_id: object) -> str:
    """Reduce a session id to the characters that are safe inside an S3 key.

    Parameters
    ----------
    session_id : object
        Session id, converted with `str()`

    Returns
    -------
    str
        The id with every character outside `[a-zA-Z0-9_-]` replaced by `_`
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id))


def is_not_found(error: ClientError) -> bool:
    """Whether an S3 client error means the object or bucket does not exist

    Parameters
    ----------
    error : ClientError
        Error raised by the S3 client

    Returns
    -------
    bool
        True when its error code is one of :data:`NOT_FOUND_CODES`
    """
    return error.response.get("Error", {}).get("Code") in NOT_FOUND_CODES


def read_text(s3_uri: str) -> str:
    """Read an S3 object as UTF-8 text

    Parameters
    ----------
    s3_uri : str
        URI of the object

    Returns
    -------
    str
        The object's body, decoded as UTF-8
    """
    bucket, key = parse_s3_uri(s3_uri)
    return s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def read_json(key: str, bucket: str | None = None) -> object | None:
    """A JSON object from S3, or None when it is missing or unparsable

    Parameters
    ----------
    key : str
        Object key
    bucket : str | None
        Bucket to read from; the staging bucket when None

    Returns
    -------
    object | None
        The parsed JSON, or None when the object is missing or not valid JSON
    """
    try:
        body = s3_client.get_object(Bucket=bucket or STAGING_BUCKET, Key=key)["Body"]
        return json.loads(body.read())
    except ClientError as e:
        if is_not_found(e):
            return None
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def list_keys(prefix: str) -> list[str]:
    """Every key under a prefix of the staging bucket

    Parameters
    ----------
    prefix : str
        Key prefix to list

    Returns
    -------
    list[str]
        Every key under the prefix
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=prefix)
        for obj in page.get("Contents") or []
    ]


def put_bytes(key: str, body: bytes, content_type: str) -> str:
    """Write an object to the staging bucket; return its S3 URI

    Parameters
    ----------
    key : str
        Object key
    body : bytes
        Object content
    content_type : str
        MIME type stored with the object

    Returns
    -------
    str
        The object's S3 URI
    """
    s3_client.put_object(
        Bucket=STAGING_BUCKET, Key=key, Body=body, ContentType=content_type
    )
    return f"s3://{STAGING_BUCKET}/{key}"


def put_json(key: str, payload: object) -> str:
    """Write a value to the staging bucket as indented JSON

    Parameters
    ----------
    key : str
        Object key
    payload : object
        Value to serialize; values JSON cannot encode are written with `str()`

    Returns
    -------
    str
        The object's S3 URI
    """
    return put_bytes(
        key,
        json.dumps(payload, indent=2, default=str).encode("utf-8"),
        "application/json",
    )


def presign(bucket: str, key: str, **params: str) -> str:
    """Create a presigned GET URL for an S3 object

    Parameters
    ----------
    bucket : str
        Bucket of the object
    key : str
        Object key
    **params : str
        Extra `get_object` parameters, such as `ResponseContentDisposition`

    Returns
    -------
    str
        A URL valid for :data:`URL_EXPIRATION` seconds
    """
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key, **params},
        ExpiresIn=URL_EXPIRATION,
    )
