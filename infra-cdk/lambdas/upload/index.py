# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Upload API Lambda - generates pre-signed S3 URLs for file uploads."""

import os
import uuid

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig
from aws_lambda_powertools.logging.correlation_paths import API_GATEWAY_REST
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, Field

BUCKET_NAME = os.environ["BUCKET_NAME"]
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

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

    ext = req.filename.rsplit(".", 1)[-1] if "." in req.filename else "pdf"
    key = f"uploads/{uuid.uuid4().hex}.{ext}"

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


@logger.inject_lambda_context(correlation_id_path=API_GATEWAY_REST)
@tracer.capture_lambda_handler
def handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
