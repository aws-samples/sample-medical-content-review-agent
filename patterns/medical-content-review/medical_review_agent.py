# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import re
import traceback
from pathlib import Path

os.environ["BYPASS_TOOL_CONSENT"] = "true"

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from mcp.client.streamable_http import streamablehttp_client
from review_upload_hook import ReviewS3UploadHook
from strands import Agent
from strands.models import BedrockModel, CacheConfig
from strands.tools.mcp import MCPClient
from strands_tools import file_read, file_write
from tools import process_pdf, batch_content, extract_claims
from utils.auth import extract_user_id_from_context, get_gateway_access_token
from utils.inference import get_bedrock_config, get_inference_configs
from utils.ssm import get_ssm_parameter

INFERENCE_CONFIG, _ = get_inference_configs()
BEDROCK_CONFIG = get_bedrock_config()

app = BedrockAgentCoreApp()

SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.txt"

# Gateway data sources for reference verification
ALL_DATA_SOURCES = {
    "s3": {
        "name": "S3 Text Reader",
        "tool": "s3_text_reader",
        "description": "Read text files and PDFs from S3",
    },
    "pubmed": {
        "name": "PubMed Search",
        "tool": "pubmed_search",
        "description": "Search PubMed for biomedical literature to verify claims",
    },
    "openfda": {
        "name": "OpenFDA Drug Search",
        "tool": "openfda_drug_search",
        "description": "Search FDA drug label database for pharmaceutical information",
    },
    "bedrock_kb": {
        "name": "Knowledge Base Search",
        "tool": "knowledge_base_search",
        "description": "Query Amazon Bedrock Knowledge Bases for approved claims and guidelines",
        "requires_params": True,
    },
    "nova": {
        "name": "Nova Web Grounding",
        "tool": "nova_web_search",
        "description": "Web search via Amazon Nova with citations",
    },
}


def _load_tools_config() -> dict:
    raw = os.environ.get("TOOLS_CONFIG", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


TOOLS_CONFIG = _load_tools_config()

DATA_SOURCES = {
    key: source
    for key, source in ALL_DATA_SOURCES.items()
    if TOOLS_CONFIG.get(key, {}).get("enabled", True)
}

DEFAULT_ENABLED_SOURCES = [
    key for key in DATA_SOURCES if TOOLS_CONFIG.get(key, {}).get("default_on", False)
]


def load_system_prompt(
    enabled_sources: list[str] | None = None,
    content_pdf_uri: str | None = None,
    reference_uris: list[str] | None = None,
    claims_uris: list[str] | None = None,
) -> str:
    with open(SYSTEM_PROMPT_PATH) as f:
        base_prompt = f.read()

    if enabled_sources is None:
        enabled_sources = DEFAULT_ENABLED_SOURCES

    tools_section = "### Data Retrieval (via Gateway)\n"
    tools_section += "The following Gateway tools are available (names start with `gateway___`):\n"
    for source_key in enabled_sources:
        if source_key in DATA_SOURCES:
            source = DATA_SOURCES[source_key]
            tools_section += f"- {source['name']}: {source['description']}\n"

    if not any(s in DATA_SOURCES for s in enabled_sources):
        tools_section += "- No external data sources enabled\n"

    if "s3" in enabled_sources:
        if content_pdf_uri:
            tools_section += "\n### Content PDF to Review\n"
            tools_section += f"- `{content_pdf_uri}`\n"
        if reference_uris:
            tools_section += "\n### Reference Materials (S3)\n"
            tools_section += "Read these to verify claims against source data:\n"
            for uri in reference_uris:
                tools_section += f"- `{uri}`\n"
        if claims_uris:
            tools_section += "\n### Approved Claims (S3)\n"
            tools_section += "Read these to check statements against pre-approved claims:\n"
            for uri in claims_uris:
                tools_section += f"- `{uri}`\n"

    pattern = r"### Data Retrieval \(via Gateway\)\n(?:- .*\n)*"
    base_prompt = re.sub(pattern, tools_section, base_prompt)

    return base_prompt


def create_gateway_mcp_client(
    access_token: str,
    enabled_sources: list[str] | None = None,
) -> MCPClient:
    stack_name = os.environ.get("STACK_NAME")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")
    if not stack_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid STACK_NAME format")

    gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")

    tool_filters = None
    if enabled_sources is not None:
        allowed_tool_names = [
            DATA_SOURCES[key]["tool"] for key in enabled_sources if key in DATA_SOURCES
        ]
        if allowed_tool_names:
            pattern = re.compile(
                r"^.*___(" + "|".join(re.escape(n) for n in allowed_tool_names) + r")$"
            )
            tool_filters = {"allowed": [pattern]}

    return MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url, headers={"Authorization": f"Bearer {access_token}"}
        ),
        tool_filters=tool_filters,
        prefix="gateway",
    )


def create_medical_review_agent(
    user_id: str,
    session_id: str,
    enabled_sources: list[str] | None = None,
    content_pdf_uri: str | None = None,
    reference_uris: list[str] | None = None,
    claims_uris: list[str] | None = None,
) -> tuple:
    system_prompt = load_system_prompt(enabled_sources, content_pdf_uri, reference_uris, claims_uris)

    model_id = os.environ.get(
        "MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
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

    tools: list = [file_read, file_write, process_pdf, batch_content, extract_claims]

    access_token = get_gateway_access_token()
    gateway_client = create_gateway_mcp_client(access_token, enabled_sources)
    tools.append(gateway_client)

    review_upload_hook = ReviewS3UploadHook()

    agent = Agent(
        name="MedicalContentReviewAgent",
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
    Main entrypoint for the medical content review agent.

    Payload fields:
    - prompt: User's review request (required)
    - runtimeSessionId: Session ID (required)
    - enabledSources: List of enabled data sources (optional)
    - contentPdfUri: S3 URI of the medical content PDF to review (optional)
    - referenceUris: List of S3 URIs for reference materials (optional)
    - claimsUris: List of S3 URIs for approved claims files (optional)
    """
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")
    enabled_sources = payload.get("enabledSources")
    content_pdf_uri = payload.get("contentPdfUri")
    reference_uris = payload.get("referenceUris", [])
    claims_uris = payload.get("claimsUris", [])

    if not all([user_query, session_id]):
        yield {"status": "error", "error": "Missing required fields: prompt or runtimeSessionId"}
        return

    # Build the full prompt with PDF context
    full_prompt = user_query
    if content_pdf_uri:
        full_prompt += f"\n\nMedical content PDF to review: {content_pdf_uri}"
    if reference_uris:
        full_prompt += f"\n\nReference materials: {', '.join(reference_uris)}"
    if claims_uris:
        full_prompt += f"\n\nApproved claims files: {', '.join(claims_uris)}"

    try:
        user_id = extract_user_id_from_context(context)

        agent, review_hook = create_medical_review_agent(
            user_id, session_id, enabled_sources,
            content_pdf_uri, reference_uris or None, claims_uris or None,
        )

        _keep_keys = {
            "data", "delta", "current_tool_use", "message", "result",
            "init_event_loop", "start_event_loop", "start", "type",
        }
        stream = agent.stream_async(full_prompt, session_id=session_id)
        async for event in stream:
            d = {k: v for k, v in dict(event).items() if k in _keep_keys}
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
