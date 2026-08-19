# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Authentication utilities for agent patterns.

Provides:
- Secure user identity extraction from JWT tokens in the AgentCore Runtime
  RequestContext (prevents impersonation via prompt injection).
- OAuth2 client credentials flow for machine-to-machine Gateway authentication.
"""

import base64
import logging
import os
from functools import lru_cache
from typing import Any

import boto3
import jwt
import requests
from bedrock_agentcore.runtime import RequestContext
from jwt.exceptions import PyJWTError
from utils.ssm import get_ssm_parameter

logger = logging.getLogger(__name__)

# Cognito signs with RS256. Pinning the algorithm here is what stops a token that asks
# to be verified with something else — or with "none" — from being accepted.
JWT_ALGORITHMS = ["RS256"]

# Outside a deployed runtime there is no user pool to verify a token against, so the
# local test harness names the user in this header instead of sending a token that
# nothing could check. STACK_NAME is always set in the deployed runtime, so this path
# cannot be reached in production.
LOCAL_USER_ID_HEADER = "X-Local-User-Id"


def _header(request_headers: dict[str, str], name: str) -> str | None:
    """
    Look a request header up without depending on the casing the caller used

    Parameters
    ----------
    request_headers : dict[str, str]
        Headers as handed over by the Runtime
    name : str
        Header name to find, in any casing

    Returns
    -------
    str | None
        The header value, or None when it was not sent
    """
    lowered = name.lower()
    for key, value in request_headers.items():
        if key.lower() == lowered:
            return value
    return None


@lru_cache(maxsize=1)
def _cognito_issuer() -> str:
    """
    Build the issuer URL of the user pool whose tokens this runtime accepts

    The pool is the one the Runtime's own JWT authorizer was configured with, read from
    the same SSM parameter the deployment writes, so the two can never drift apart.

    Returns
    -------
    str
        Issuer URL, e.g. ``https://cognito-idp.eu-west-1.amazonaws.com/<pool id>``
    """
    stack_name = os.environ["STACK_NAME"]
    region = os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    user_pool_id = get_ssm_parameter(f"/{stack_name}/cognito-user-pool-id")
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


@lru_cache(maxsize=1)
def _allowed_client_ids() -> frozenset[str]:
    """
    Read the app client IDs whose tokens this runtime accepts

    Returns
    -------
    frozenset[str]
        The browser client and the machine-to-machine client

    Raises
    ------
    ValueError
        When neither client ID can be read, since accepting a token without knowing
        who it was issued to would defeat the check
    """
    stack_name = os.environ["STACK_NAME"]
    client_ids = set()
    for parameter in ("cognito-user-pool-client-id", "machine_client_id"):
        try:
            client_ids.add(get_ssm_parameter(f"/{stack_name}/{parameter}"))
        except ValueError:
            logger.warning("No %s parameter for stack %s", parameter, stack_name)
    if not client_ids:
        raise ValueError(
            f"No Cognito app client IDs found in SSM for stack {stack_name}"
        )
    return frozenset(client_ids)


@lru_cache(maxsize=1)
def _jwk_client() -> jwt.PyJWKClient:
    """
    Build the JWKS client for the pool, cached so keys are fetched once per container

    Returns
    -------
    jwt.PyJWKClient
        Client that resolves a token's `kid` to the pool's public signing key
    """
    return jwt.PyJWKClient(
        f"{_cognito_issuer()}/.well-known/jwks.json", cache_keys=True
    )


def _verified_claims(token: str) -> dict[str, Any]:
    """
    Verify a Cognito JWT and return its claims

    The signature is checked against the pool's published key, along with the issuer,
    the expiry, and the client the token was issued to. The Runtime's authorizer
    already does this before the agent is reached; repeating it here means a token is
    never trusted on the strength of a caller's word alone.

    Parameters
    ----------
    token : str
        The raw JWT, without the "Bearer " prefix

    Returns
    -------
    dict[str, Any]
        The verified claims

    Raises
    ------
    ValueError
        When the token fails verification or was issued to another client
    """
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=JWT_ALGORITHMS,
            issuer=_cognito_issuer(),
            # A Cognito access token carries no `aud` claim, so the client is checked
            # below against `client_id` instead of by PyJWT
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
        )
    except PyJWTError as e:
        raise ValueError(f"JWT token failed verification: {e}") from e

    presented = claims.get("client_id") or claims.get("aud")
    presented_ids = set(presented) if isinstance(presented, list) else {presented}
    if not presented_ids & _allowed_client_ids():
        raise ValueError(
            "JWT token was issued to a client this runtime does not accept"
        )
    return claims


def extract_user_id_from_context(context: RequestContext) -> str:
    """
    Securely extract the user ID from the JWT token in the request context.

    The token's signature, issuer, expiry, and app client are verified against the
    Cognito user pool before any claim is read. AgentCore Runtime validates the token
    too, but verifying it here as well means the identity does not rest on the Runtime
    being configured correctly. The user ID is taken from the token's 'sub' claim
    rather than from the request payload, which prevents impersonation via prompt
    injection.

    Args:
        context (RequestContext): The request context provided by AgentCore
            Runtime, containing validated request headers including the
            Authorization JWT.

    Returns:
        str: The user ID (sub claim) extracted from the validated JWT token.

    Raises:
        ValueError: If the Authorization header is missing or the JWT does
            not contain a 'sub' claim.
    """
    request_headers = context.request_headers
    if not request_headers:
        raise ValueError(
            "No request headers found in context. "
            "Ensure the Runtime is configured with a request header allowlist "
            "that includes the Authorization header."
        )

    # A local run has no pool to verify against, so the harness passes the identity
    # directly. Unreachable once deployed, where STACK_NAME is always set.
    if not os.environ.get("STACK_NAME"):
        local_user_id = _header(request_headers, LOCAL_USER_ID_HEADER)
        if local_user_id:
            logger.warning(
                "No STACK_NAME set — trusting unverified %s header for local run",
                LOCAL_USER_ID_HEADER,
            )
            return local_user_id

    auth_header = _header(request_headers, "Authorization")
    if not auth_header:
        raise ValueError(
            "No Authorization header found in request context. "
            "Ensure the Runtime is configured with JWT inbound auth "
            "and the Authorization header is in the request header allowlist."
        )

    # Remove "Bearer " prefix to get the raw JWT token
    token = (
        auth_header[len("Bearer ") :]
        if auth_header.startswith("Bearer ")
        else auth_header
    )

    claims = _verified_claims(token)

    user_id = claims.get("sub")
    if not user_id:
        raise ValueError(
            "JWT token does not contain a 'sub' claim. Cannot determine user identity."
        )

    logger.info("Extracted user_id from JWT: %s", user_id)
    return user_id


def get_secret(secret_name: str) -> str:
    """
    Fetch a secret value from AWS Secrets Manager.

    Secrets Manager is designed for storing sensitive information like passwords,
    API keys, and other secrets with automatic rotation capabilities.

    Args:
        secret_name (str): The name or ARN of the secret to retrieve.

    Returns:
        str: The secret value as a string.

    Raises:
        ValueError: If the secret is not found or cannot be accessed.
        RuntimeError: If there's an AWS service error.
    """
    region = os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    secrets_client = boto3.client("secretsmanager", region_name=region)

    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        return response["SecretString"]
    except secrets_client.exceptions.ResourceNotFoundException as e:
        raise ValueError(f"Secret not found: {secret_name}") from e
    except secrets_client.exceptions.InvalidParameterException as e:
        raise ValueError(f"Invalid secret parameter: {secret_name}") from e
    except secrets_client.exceptions.InvalidRequestException as e:
        raise ValueError(f"Invalid request for secret: {secret_name}") from e
    except secrets_client.exceptions.DecryptionFailureException as e:
        raise RuntimeError(f"Failed to decrypt secret: {secret_name}") from e
    except secrets_client.exceptions.InternalServiceErrorException as e:
        raise RuntimeError(
            f"AWS Secrets Manager service error for secret: {secret_name}"
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Unexpected error retrieving secret {secret_name}: {str(e)}"
        ) from e


def get_gateway_access_token() -> str:
    """
    Get an OAuth2 access token using the client credentials flow.

    This implements machine-to-machine authentication where the agent acts as
    a client that needs to authenticate with Cognito to get permission to call
    the Gateway. The client credentials flow is used for server-to-server
    communication without user login.

    Returns:
        str: A valid OAuth2 access token for Gateway authentication.

    Raises:
        KeyError: If the STACK_NAME environment variable is not set.
        Exception: If the token request fails or the response is invalid.
    """
    stack_name = os.environ["STACK_NAME"]
    region = os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )

    logger.info("Getting access token for stack: %s, region: %s", stack_name, region)

    # Get Cognito configuration from SSM and Secrets Manager
    cognito_domain = get_ssm_parameter(f"/{stack_name}/cognito_provider")
    client_id = get_ssm_parameter(f"/{stack_name}/machine_client_id")
    client_secret = get_secret(f"/{stack_name}/machine_client_secret")

    logger.info("Cognito domain: %s", cognito_domain)

    # Prepare OAuth2 token request
    token_url = f"https://{cognito_domain}/oauth2/token"

    # Create Basic Auth header (base64-encoded client_id:client_secret)
    credentials = f"{client_id}:{client_secret}"
    b64_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "client_credentials",
        "scope": f"{stack_name}-gateway/read {stack_name}-gateway/write",
    }

    logger.info("Requesting token from: %s", token_url)

    # Request access token from Cognito
    response = requests.post(url=token_url, headers=headers, data=data, timeout=30)

    if response.status_code != 200:
        logger.error("Token request failed: %s", response.status_code)
        logger.error("Response: %s", response.text)
        raise Exception(
            f"Failed to get access token: {response.status_code} - {response.text}"
        )

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        logger.error("No access_token in response: %s", token_data)
        raise Exception("No access_token in Cognito response")

    logger.info("Successfully obtained access token")
    return access_token
