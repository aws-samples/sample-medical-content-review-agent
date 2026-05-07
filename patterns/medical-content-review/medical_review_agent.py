# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import copy
import json
import os
import traceback
from pathlib import Path

os.environ["BYPASS_TOOL_CONSENT"] = "true"

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from review_upload_hook import ReviewS3UploadHook
from reviewers import (
    get_reviews,
    run_external_review,
    run_generic_review,
    run_internal_review,
)
from strands import Agent
from strands.models import BedrockModel, CacheConfig
from strands_tools import file_read, file_write
from utils.auth import extract_user_id_from_context
from utils.inference import get_bedrock_config, get_inference_configs

from tools import batch_content, process_pdf

INFERENCE_CONFIG, _ = get_inference_configs()
BEDROCK_CONFIG = get_bedrock_config()

app = BedrockAgentCoreApp()

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "orchestrator.txt"

# External data sources reachable via the AgentCore Gateway. The orchestrator
# itself does NOT call these — only the external-review sub-agent does. This
# dict exists to validate user-supplied keys and to shape the frontend toggle
# row.
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

DEFAULT_ENABLED_SOURCES = [
    key
    for key in ALL_DATA_SOURCES
    if TOOLS_CONFIG.get(key, {}).get("enabled", True)
    and TOOLS_CONFIG.get(key, {}).get("default_on", True)
]


def load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_PATH) as f:
        return f.read()


def build_context_block(
    session_id: str,
    content_pdf_uri: str | None,
    content_pdf_name: str | None,
    reference_uris: list[str],
    reference_names: list[str],
    enabled_sources: list[str],
) -> str:
    """Build the per-request input block that gets appended to the user prompt."""
    lines = [
        "## Review inputs",
        f"- session_id: `{session_id}`",
        "- content_pdf:",
        f"  - s3_uri: `{content_pdf_uri or '(missing)'}`",
        f"  - original_filename: `{content_pdf_name or '(unknown)'}`",
        "- references:",
    ]
    if reference_uris:
        for i, uri in enumerate(reference_uris):
            name = (
                reference_names[i]
                if i < len(reference_names) and reference_names[i]
                else "(unknown)"
            )
            lines.append(f"  - s3_uri: `{uri}` — original_filename: `{name}`")
    else:
        lines.append("  - (none)")
    lines.append(f"- enabled_sources: {enabled_sources}")
    return "\n".join(lines)


def create_medical_review_agent(
    user_id: str,
    session_id: str,
    external_sources_enabled: bool,
) -> tuple:
    system_prompt = load_system_prompt()

    model_id = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
    bedrock_model = BedrockModel(
        model_id=model_id,
        temperature=INFERENCE_CONFIG["temperature"],
        max_tokens=INFERENCE_CONFIG["maxTokens"],
        streaming=True,
        boto_client_config=BEDROCK_CONFIG,
        cache_config=CacheConfig(strategy="auto"),
    )

    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")

    agentcore_memory_config = AgentCoreMemoryConfig(
        memory_id=memory_id, session_id=session_id, actor_id=user_id
    )
    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_memory_config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    # Only expose the external reviewer when at least one external source is
    # enabled. Removing it from the tool list entirely means the model cannot
    # call it even if it tries — a stricter guard than a prompt instruction.
    tools = [
        file_read,
        file_write,
        process_pdf,
        batch_content,
        run_generic_review,
        run_internal_review,
        get_reviews,
    ]
    if external_sources_enabled:
        tools.insert(5, run_external_review)

    review_upload_hook = ReviewS3UploadHook()

    agent = Agent(
        name="MedicalContentReviewOrchestrator",
        system_prompt=system_prompt,
        tools=tools,
        model=bedrock_model,
        session_manager=session_manager,
        hooks=[review_upload_hook],
        trace_attributes={"user.id": user_id, "session.id": session_id},
    )
    return agent, review_upload_hook


def _truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "... (truncated)"


def _truncate_large_fields(d: dict, max_len: int = 3000) -> None:
    msg = d.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            tr = block.get("toolResult")
            if isinstance(tr, dict) and isinstance(tr.get("content"), list):
                for item in tr["content"]:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        item["text"] = _truncate_text(item["text"], max_len)


def _inject_review_urls(d: dict, urls: dict[str, str]) -> None:
    msg = d.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
        return
    for block in msg["content"]:
        if not isinstance(block, dict):
            continue
        tr = block.get("toolResult")
        if isinstance(tr, dict) and isinstance(tr.get("content"), list):
            for item in tr["content"]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    tags = ""
                    if "review" in urls:
                        tags += f"\n\n[REVIEW_URL:{urls['review']}]"
                    item["text"] += tags
                    return


@app.entrypoint
async def agent_stream(payload, context: RequestContext):
    """
    Main entrypoint for the medical content review orchestrator.

    Payload fields:
    - prompt: User's review request (required)
    - runtimeSessionId: Session ID (required)
    - enabledSources: Subset of {pubmed, openfda, clinicaltrials, nova} (optional)
    - contentPdfUri: S3 URI of the medical content PDF to review
    - referenceUris: List of S3 URIs for reference materials (optional)
    """
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")
    enabled_sources = payload.get("enabledSources") or DEFAULT_ENABLED_SOURCES
    enabled_sources = [s for s in enabled_sources if s in ALL_DATA_SOURCES]
    content_pdf_uri = payload.get("contentPdfUri")
    content_pdf_name = payload.get("contentPdfName") or ""
    reference_uris = payload.get("referenceUris") or []
    reference_names = payload.get("referenceNames") or []

    if not all([user_query, session_id]):
        yield {
            "status": "error",
            "error": "Missing required fields: prompt or runtimeSessionId",
        }
        return

    full_prompt = (
        user_query
        + "\n\n"
        + build_context_block(
            session_id=session_id,
            content_pdf_uri=content_pdf_uri,
            content_pdf_name=content_pdf_name,
            reference_uris=reference_uris,
            reference_names=reference_names,
            enabled_sources=enabled_sources,
        )
    )

    try:
        user_id = extract_user_id_from_context(context)
        agent, review_hook = create_medical_review_agent(
            user_id,
            session_id,
            external_sources_enabled=bool(enabled_sources),
        )

        _keep_keys = {
            "data",
            "delta",
            "current_tool_use",
            "message",
            "result",
            "init_event_loop",
            "start_event_loop",
            "start",
            "type",
        }
        stream = agent.stream_async(full_prompt, session_id=session_id)
        async for event in stream:
            # Deep-copy the subset of keys we forward so that our frontend-only
            # truncation and URL injection do NOT mutate the event objects the
            # agent keeps in its own context — otherwise subsequent tool calls
            # see silently chopped prior tool results.
            d = copy.deepcopy({k: v for k, v in dict(event).items() if k in _keep_keys})
            if not d:
                continue
            if "current_tool_use" in d:
                ctu = d["current_tool_use"]
                d["current_tool_use"] = {
                    "toolUseId": ctu.get("toolUseId"),
                    "name": ctu.get("name"),
                }
            _truncate_large_fields(d, max_len=3000)

            pending = review_hook.take_pending_urls()
            if pending:
                _inject_review_urls(d, pending)

            yield json.loads(json.dumps(d, default=str))

    except Exception as e:
        print(f"[STREAM ERROR] Error in agent_stream: {e}")
        traceback.print_exc()
        yield {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
