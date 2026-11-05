# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import traceback

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from reviewers.pipeline import ReviewInputs, run_review
from tools.run_record import RunRecorder
from utils.auth import extract_user_id_from_context

app = BedrockAgentCoreApp()

# data sources behind the AgentCore Gateway, called only by the external reviewer
ALL_DATA_SOURCES = {
    "pubmed": "PubMed Search",
    "openfda": "OpenFDA Drug Search",
    "clinicaltrials": "ClinicalTrials.gov Search",
    "nova": "Nova Web Grounding",
}


def _load_tools_config() -> dict:
    raw = os.environ.get("TOOLS_CONFIG", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


TOOLS_CONFIG = _load_tools_config()

# sources deployed behind the Gateway; a disabled one has no Gateway target to call
DEPLOYED_DATA_SOURCES = [
    key for key in ALL_DATA_SOURCES if TOOLS_CONFIG.get(key, {}).get("enabled", True)
]

DEFAULT_ENABLED_SOURCES = [
    key
    for key in DEPLOYED_DATA_SOURCES
    if TOOLS_CONFIG.get(key, {}).get("default_on", True)
]


@app.entrypoint
async def agent_stream(payload, context: RequestContext):
    """
    Main entrypoint for the medical content review.

    Payload fields:
    - runtimeSessionId: Session ID (required)
    - contentPdfUri: S3 URI of the medical content PDF to review (required)
    - contentPdfName: Original filename of the content PDF (optional)
    - enabledSources: Subset of {pubmed, openfda, clinicaltrials, nova} (optional)
    - referenceUris / referenceNames: Reference PDFs and their filenames (optional)
    - claimsUri / claimsName: Pre-approved claims spreadsheet (optional)
    """
    session_id = payload.get("runtimeSessionId")
    content_pdf_uri = payload.get("contentPdfUri")
    if not all([session_id, content_pdf_uri]):
        yield {
            "status": "error",
            "error": "Missing required fields: runtimeSessionId or contentPdfUri",
        }
        return

    enabled_sources = payload.get("enabledSources") or DEFAULT_ENABLED_SOURCES
    inputs = ReviewInputs(
        session_id=session_id,
        content_pdf_uri=content_pdf_uri,
        content_pdf_name=payload.get("contentPdfName") or "",
        reference_uris=payload.get("referenceUris") or [],
        reference_names=payload.get("referenceNames") or [],
        claims_uri=payload.get("claimsUri") or "",
        claims_name=payload.get("claimsName") or "",
        enabled_sources=[s for s in enabled_sources if s in DEPLOYED_DATA_SOURCES],
    )

    run_recorder = RunRecorder(
        session_id,
        content_pdf_uri,
        claims_expected=bool(inputs.claims_uri),
        references_expected=bool(inputs.reference_uris),
        external_expected=bool(inputs.enabled_sources),
    )
    run_error: str | None = None
    try:
        # verifies the caller's token before any work is done
        extract_user_id_from_context(context)
        async for event in run_review(inputs, run_recorder):
            yield event
    except Exception as e:
        run_error = str(e) or type(e).__name__
        print(f"[STREAM ERROR] Error in agent_stream: {e}")
        traceback.print_exc()
        yield {"status": "error", "error": run_error}
    finally:
        run_recorder.finish(run_error)


if __name__ == "__main__":
    app.run()
