# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared factory for the AgentCore Gateway MCP client."""

from __future__ import annotations

import os
import re

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from utils.auth import get_gateway_access_token
from utils.ssm import get_ssm_parameter

# Mirrors medical_review_agent.ALL_DATA_SOURCES key -> gateway tool name
GATEWAY_TOOL_NAMES: dict[str, str] = {
    "pubmed": "pubmed_search",
    "openfda": "openfda_drug_search",
    "clinicaltrials": "clinicaltrials_search",
    "nova": "nova_web_search",
}


def create_gateway_mcp_client(enabled_sources: list[str] | None = None) -> MCPClient:
    """Build an MCP client pointed at the stack's AgentCore Gateway.

    Fetches a fresh OAuth access token and the gateway URL from SSM each
    invocation. When `enabled_sources` is non-empty, the returned client only
    exposes tools whose names match the allow-list — so a reviewer sub-agent
    sees only what the user has toggled on.
    """
    stack_name = os.environ.get("STACK_NAME")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")
    if not stack_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid STACK_NAME format")

    gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")
    access_token = get_gateway_access_token()

    tool_filters = None
    if enabled_sources is not None:
        allowed_tool_names = [
            GATEWAY_TOOL_NAMES[key]
            for key in enabled_sources
            if key in GATEWAY_TOOL_NAMES
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
