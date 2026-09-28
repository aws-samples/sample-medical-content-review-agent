# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import re
from typing import Any

import botocore

BEDROCK_READ_TIMEOUT = 600
BEDROCK_CONNECT_TIMEOUT = 600
BEDROCK_MAX_ATTEMPTS = 3
BEDROCK_MAX_CONNECTIONS = 10

MAX_TOKENS = 64_000
THINKING_TOKENS = 2_000
TEMPERATURE = 0.0

INFERENCE_CONFIG = {
    "stopSequences": [],  # words after which the generation is stopped
    "maxTokens": MAX_TOKENS,  # max tokens to be generated
    "temperature": TEMPERATURE,  # randomness of the model's output
}

# claude-opus-4-7, claude-sonnet-5, claude-fable-5-1, ... (a dated suffix such as
# claude-sonnet-4-20250514 is not a minor version, hence the lookahead)
CLAUDE_VERSION_RE = re.compile(r"claude-[a-z]+-(\d+)(?:-(\d)(?!\d))?")
# first Claude version that rejects temperature/top_p/top_k with a 400
SAMPLING_REMOVED_FROM = (4, 7)

REASONING_CONFIG = {
    "thinking": {
        "type": "enabled",  # whether extended thinking is enabled
        "budget_tokens": THINKING_TOKENS,  # max tokens for thinking budget
    }
}


def get_inference_configs() -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Get inference and reasoning parameters for Bedrock language models.

    Returns
    -------
    tuple[dict[str, Any], dict[str, Any]]
        Tuple containing:
        - Inference config dict with temperature, maxTokens,
          topP and stopSequences parameters
        - Reasoning config dict with thinking settings
    """

    inference_config = INFERENCE_CONFIG.copy()
    reasoning_config = REASONING_CONFIG.copy()

    if reasoning_config["thinking"]["type"] == "enabled":
        inference_config["temperature"] = 1.0  # required in thinking mode
    else:
        reasoning_config = {
            "thinking": {
                "type": "disabled",
            }
        }

    return inference_config, reasoning_config


def supports_sampling_params(model_id: str) -> bool:
    """
    Check whether a Bedrock model accepts sampling parameters such as temperature

    Claude Opus 4.7 and every later Claude model (Sonnet 5, Opus 5, Fable 5, ...)
    reject temperature, top_p and top_k with a 400, while older Claude models and
    non-Claude models still accept them. Claude 3 era IDs (claude-3-7-sonnet-...)
    put the version before the family name, so they never match and count as older.

    Parameters
    ----------
    model_id : str
        Bedrock model or inference profile ID, e.g. "global.anthropic.claude-sonnet-5"

    Returns
    -------
    bool
        True if sampling parameters can be sent to this model
    """
    match = CLAUDE_VERSION_RE.search(model_id)
    if not match:
        return True
    version = (int(match.group(1)), int(match.group(2) or 0))
    return version < SAMPLING_REMOVED_FROM


def sampling_params(model_id: str, temperature: float) -> dict[str, float]:
    """
    Build the sampling parameters a model accepts, for splatting into a request

    Parameters
    ----------
    model_id : str
        Bedrock model or inference profile ID
    temperature : float
        Temperature to use where the model supports one

    Returns
    -------
    dict[str, float]
        ``{"temperature": temperature}``, or an empty dict for models that reject it
    """
    return {"temperature": temperature} if supports_sampling_params(model_id) else {}


def get_bedrock_config() -> botocore.config.Config:
    """
    Get botocore configuration for Bedrock API calls.

    Returns
    -------
    botocore.config.Config
        Configuration object with read timeout and retry settings for Bedrock client
    """
    return botocore.config.Config(
        read_timeout=BEDROCK_READ_TIMEOUT,
        connect_timeout=BEDROCK_CONNECT_TIMEOUT,
        retries={
            "max_attempts": BEDROCK_MAX_ATTEMPTS,
            "mode": "adaptive",
        },
        max_pool_connections=BEDROCK_MAX_CONNECTIONS,
    )
