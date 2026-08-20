#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


"""
Interactive agent chat tester for local and remote agents

Tests agent invocation with conversation continuity:
- Remote mode (default): Chat with deployed agent via Cognito authentication
- Local mode (--local): Chat with agent running on localhost:8080
- Automatically detects pattern from config.yaml

Usage:
    # Remote agent testing (prompts for credentials)
    uv run scripts/test-agent.py

    # Local agent testing (agent must be running on localhost:8080)
    uv run scripts/test-agent.py --local

    # Override pattern from config
    uv run scripts/test-agent.py --pattern medical-content-review

    # Full medical content review without the UI: local files are uploaded to the
    # staging bucket's uploads/ prefix, s3:// URIs are passed through as they are
    uv run scripts/test-agent.py --content-pdf ./brochure.pdf \
        --reference ./dossier.pdf --claims ./approved_claims.xlsx --prompt "Review it"
"""

import argparse
import atexit
import getpass
import json
import signal
import socket
import subprocess  # nosec B404 - subprocess used securely with explicit parameters
import sys
import time
import uuid
from pathlib import Path

import boto3
import requests
from colorama import Fore, Style

# Add scripts directory to path for reliable imports
scripts_dir = Path(__file__).parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

# Import shared utilities
from utils import (
    authenticate_cognito,
    generate_session_id,
    get_stack_config,
    print_msg,
    print_section,
)

# Global variable to track agent process
_agent_process: subprocess.Popen | None = None


def generate_trace_id() -> str:
    """
    Generate X-Amzn-Trace-Id header value for AWS request tracing.

    Returns:
        str: Trace ID in AWS X-Ray format
    """
    timestamp_hex = format(int(time.time()), "x")
    return f"1-{timestamp_hex}-{generate_session_id()}"


def check_port_available(port: int = 8080) -> bool:
    """
    Check if a port is available for connection.

    Args:
        port (int): Port number to check

    Returns:
        bool: True if port is available, False otherwise
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        result = sock.connect_ex(("localhost", port))
        sock.close()
        return result == 0
    except Exception:
        return False


def start_local_agent(
    memory_id: str, region: str, stack_name: str, pattern: str
) -> subprocess.Popen:
    """
    Start the local agent in a background process.

    Args:
        memory_id (str): Memory ID for the agent
        region (str): AWS region
        stack_name (str): CloudFormation stack name for SSM parameter lookup
        pattern (str): Agent pattern name (e.g., 'medical-content-review')

    Returns:
        subprocess.Popen: Subprocess object for the running agent
    """
    global _agent_process

    # Map pattern to agent file
    pattern_files = {
        "medical-content-review": "medical_review_agent.py",
    }

    agent_file = pattern_files.get(pattern)
    if not agent_file:
        print_msg(f"Unknown pattern: {pattern}", "error")
        print(f"Available patterns: {', '.join(pattern_files.keys())}")
        sys.exit(1)

    agent_path = Path(__file__).parent.parent / "patterns" / pattern / agent_file

    if not agent_path.exists():
        print_msg(f"Agent file not found: {agent_path}", "error")
        sys.exit(1)

    # Security validation: ensure agent_path is within the patterns directory
    patterns_dir = Path(__file__).parent.parent / "patterns"
    try:
        agent_path.resolve().relative_to(patterns_dir.resolve())
    except ValueError:
        print_msg(
            f"Security error: Agent path outside patterns directory: {agent_path}",
            "error",
        )
        sys.exit(1)

    print(f"Starting local agent at {agent_path}...")
    print(f"  Pattern: {pattern}")
    print(f"  Memory ID: {memory_id}")
    print(f"  Region: {region}")
    print(f"  Stack Name: {stack_name}\n")

    # Set up environment variables. The last one lets the agent take the user identity
    # from the X-Local-User-Id header: no Cognito pool signs tokens for a local run, and
    # the deployed Runtime never sets it, so this stays a local-only affordance.
    env = {
        **dict(subprocess.os.environ),
        "MEMORY_ID": memory_id,
        "AWS_DEFAULT_REGION": region,
        "STACK_NAME": stack_name,
        "ALLOW_LOCAL_USER_ID_HEADER": "true",
    }

    # Start agent process
    try:
        _agent_process = subprocess.Popen(  # nosec B607 B603 - command constructed from validated path, shell=False
            ["uv", "run", str(agent_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,  # Explicitly disable shell
        )

        # Wait for agent to start (check port becomes available)
        print("Waiting for agent to start on port 8080...")
        for i in range(30):  # Wait up to 30 seconds
            if check_port_available(8080):
                print_msg("Agent started successfully", "success")
                return _agent_process
            time.sleep(1)

        print_msg("Agent failed to start (timeout)", "error")
        _agent_process.terminate()
        sys.exit(1)

    except Exception as e:
        print_msg(f"Failed to start agent: {e}", "error")
        sys.exit(1)


def stop_local_agent() -> None:
    """Stop the local agent process if running."""
    global _agent_process
    if _agent_process:
        print("\nStopping local agent...")
        _agent_process.terminate()
        try:
            _agent_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _agent_process.kill()
        print_msg("Agent stopped", "success")


# Register cleanup handler
atexit.register(stop_local_agent)


def signal_handler(sig, frame):
    """Handle interrupt signal."""
    print("\n")
    stop_local_agent()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


def upload_to_staging(path: Path, bucket: str) -> str:
    """
    Upload a local file to the staging bucket's uploads/ prefix.

    The agent's execution role may read that prefix only, so anything the agent has to
    open has to live there - this mirrors what the pre-signed upload API does for the UI.

    Args:
        path (Path): Local file to upload
        bucket (str): Staging bucket name (StagingBucketName stack output)

    Returns:
        str: The s3:// URI of the uploaded object
    """
    key = f"uploads/{uuid.uuid4().hex}{path.suffix.lower()}"
    boto3.client("s3").upload_file(str(path), bucket, key)
    print(f"  Uploaded {path.name} -> s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def resolve_attachment(value: str, bucket: str) -> tuple[str, str]:
    """
    Turn a CLI file argument into the (s3 URI, display name) pair the payload needs.

    Args:
        value (str): Local path or an existing s3:// URI
        bucket (str): Staging bucket name, used when the file has to be uploaded

    Returns:
        tuple[str, str]: The s3:// URI and the original filename
    """
    if value.startswith("s3://"):
        return value, value.rsplit("/", 1)[-1]

    path = Path(value).expanduser()
    if not path.is_file():
        print_msg(f"File not found: {value}", "error")
        sys.exit(1)
    if not bucket:
        print_msg(
            "Cannot upload local files: the stack has no StagingBucketName output. "
            "Pass s3:// URIs instead.",
            "error",
        )
        sys.exit(1)
    return upload_to_staging(path, bucket), path.name


def build_attachments(args: argparse.Namespace, bucket: str) -> dict:
    """
    Build the payload fields that point the review at its documents.

    Args:
        args (argparse.Namespace): Parsed command-line arguments
        bucket (str): Staging bucket name for uploading local files

    Returns:
        dict: Payload fields to merge into the invocation, empty when nothing was passed
    """
    attachments: dict = {}

    if args.content_pdf:
        uri, name = resolve_attachment(args.content_pdf, bucket)
        attachments["contentPdfUri"] = uri
        attachments["contentPdfName"] = name

    if args.reference:
        resolved = [resolve_attachment(ref, bucket) for ref in args.reference]
        attachments["referenceUris"] = [uri for uri, _ in resolved]
        attachments["referenceNames"] = [name for _, name in resolved]

    if args.claims:
        uri, name = resolve_attachment(args.claims, bucket)
        # claimsUri is what switches claim extraction and matching on; claimsName lets
        # the parser pick its reader from the extension
        attachments["claimsUri"] = uri
        attachments["claimsName"] = name

    if args.enabled_sources is not None:
        attachments["enabledSources"] = [
            source.strip()
            for source in args.enabled_sources.split(",")
            if source.strip()
        ]

    return attachments


def invoke_agent(
    url: str,
    prompt: str,
    session_id: str,
    user_id: str = "local-test-user",
    headers: dict[str, str] | None = None,
    attachments: dict | None = None,
) -> None:
    """
    Invoke agent and print raw streaming events in real-time.

    Args:
        url (str): Agent endpoint URL
        prompt (str): User prompt/query
        session_id (str): Session ID for conversation continuity
        user_id (str): User ID sent in the X-Local-User-Id header, local mode only.
            In remote mode the real Cognito JWT carries the user identity, user_id is
            never sent in the payload to prevent prompt injection impersonation.
        headers (Optional[Dict[str, str]]): Optional HTTP headers
        attachments (Optional[dict]): Document/claims payload fields from the CLI
    """
    payload = {
        "prompt": prompt,
        "runtimeSessionId": session_id,
    }
    payload.update(attachments or {})

    if headers is None:
        # Local mode: there is no Cognito pool to sign or verify a token against, so
        # the identity is named directly. Deployed runs always go through the JWT.
        headers = {"X-Local-User-Id": user_id}
    headers["Content-Type"] = "application/json"

    try:
        response = requests.post(
            url, headers=headers, json=payload, stream=True, timeout=300
        )

        if response.status_code != 200:
            print(f"Error: HTTP {response.status_code}: {response.text}")
            return

        # Parse streaming events and display clean text output
        print(f"{Fore.GREEN}Agent:{Style.RESET_ALL} ", end="", flush=True)
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])

                # LangGraph: AIMessageChunk with content array
                if chunk.get("type") == "AIMessageChunk" and isinstance(
                    chunk.get("content"), list
                ):
                    for block in chunk["content"]:
                        if block.get("type") == "text" and block.get("text"):
                            print(block["text"], end="", flush=True)
                        elif block.get("type") == "tool_use" and block.get("name"):
                            print(
                                f"\n{Fore.YELLOW}[Tool: {block['name']}]{Style.RESET_ALL} ",
                                end="",
                                flush=True,
                            )

                # LangGraph: ToolMessage result
                elif chunk.get("type") == "tool":
                    result = chunk.get("content", "")
                    if len(result) > 200:
                        result = result[:200] + "..."
                    print(
                        f"\n{Fore.YELLOW}[Result: {result}]{Style.RESET_ALL}",
                        flush=True,
                    )

                # Strands: text token
                elif isinstance(chunk.get("data"), str):
                    print(chunk["data"], end="", flush=True)

                # Strands: tool use
                elif chunk.get("current_tool_use") and chunk.get(
                    "current_tool_use", {}
                ).get("name"):
                    tool = chunk["current_tool_use"]
                    if chunk.get("delta", {}).get("toolUse", {}).get("input") == "":
                        print(
                            f"\n{Fore.YELLOW}[Tool: {tool['name']}]{Style.RESET_ALL} ",
                            end="",
                            flush=True,
                        )

                # Strands: tool result
                elif chunk.get("message", {}).get("role") == "user":
                    for content in chunk["message"].get("content", []):
                        if "toolResult" in content:
                            result = str(content["toolResult"].get("content", ""))
                            if len(result) > 200:
                                result = result[:200] + "..."
                            print(
                                f"\n{Fore.YELLOW}[Result: {result}]{Style.RESET_ALL}",
                                flush=True,
                            )

            except (json.JSONDecodeError, KeyError):
                continue
        print()  # Final newline

    except requests.exceptions.ConnectionError:
        print_msg(f"Could not connect to {url}", "error")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")


def run_chat(
    local_mode: bool,
    config: dict[str, str],
    attachments: dict | None = None,
    single_prompt: str | None = None,
) -> None:
    """
    Run interactive chat session, or a single invocation when a prompt is given.

    Args:
        local_mode (bool): Whether to use local mode
        config (Dict[str, str]): Configuration dictionary
        attachments (Optional[dict]): Document/claims payload fields from the CLI
        single_prompt (Optional[str]): Send this prompt once and return, no chat loop
    """
    session_id = generate_session_id()
    # Attachments are sent with the first invocation only: the agent OCRs them into the
    # session folder once, and later turns work off that markdown
    pending_attachments = dict(attachments or {})

    print_section("Interactive Agent Chat")
    print(f"Session ID: {session_id}")
    print(
        f"Mode: {'Local (localhost:8080)' if local_mode else 'Remote (deployed agent)'}"
    )
    for field, value in pending_attachments.items():
        print(f"{field}: {value}")
    if not single_prompt:
        print(
            f"\n{Fore.YELLOW}💡 Type 'exit' or 'quit' to end, or press Ctrl+C{Style.RESET_ALL}\n"
        )

    while True:
        try:
            if single_prompt:
                prompt = single_prompt.strip()
                print(f"{Fore.CYAN}You:{Style.RESET_ALL} {prompt}")
            else:
                prompt = input(f"{Fore.CYAN}You:{Style.RESET_ALL} ").strip()

            if not prompt:
                continue

            if prompt.lower() in ["exit", "quit"]:
                print(f"\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
                break

            # Invoke agent
            start_time = time.time()
            turn_attachments = pending_attachments
            pending_attachments = {}

            if local_mode:
                # Local mode
                invoke_agent(
                    url="http://localhost:8080/invocations",
                    prompt=prompt,
                    session_id=session_id,
                    user_id="local-test-user",
                    attachments=turn_attachments,
                )
            else:
                # Remote mode
                endpoint = f"https://bedrock-agentcore.{config['region']}.amazonaws.com"
                escaped_arn = requests.utils.quote(config["runtime_arn"], safe="")
                url = f"{endpoint}/runtimes/{escaped_arn}/invocations?qualifier=DEFAULT"

                headers = {
                    "Authorization": f"Bearer {config['access_token']}",
                    "X-Amzn-Trace-Id": generate_trace_id(),
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                }

                invoke_agent(
                    url=url,
                    prompt=prompt,
                    session_id=session_id,
                    headers=headers,
                    attachments=turn_attachments,
                )

            elapsed = time.time() - start_time
            print(f"\n{Fore.CYAN}[Completed in {elapsed:.2f}s]{Style.RESET_ALL}\n")

            if single_prompt:
                break

        except KeyboardInterrupt:
            print(f"\n\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
            break
        except EOFError:
            print(f"\n\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
            break


def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Interactive agent chat tester (local or remote)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Remote agent (prompts for credentials)
  uv run scripts/test-agent.py

  # Local agent on localhost:8080 (uses pattern from config.yaml)
  uv run scripts/test-agent.py --local

  # Override pattern for local testing
  uv run scripts/test-agent.py --local --pattern medical-content-review

  # A full medical content review without the UI
  uv run scripts/test-agent.py --content-pdf ./brochure.pdf \\
      --reference ./dossier.pdf --claims ./approved_claims.xlsx \\
      --prompt "Review this content and produce a detailed report"

Notes:
  - Remote mode: Tests deployed agent
  - Local mode: Pattern read from infra-cdk/config.yaml to start correct agent
  - Use --pattern to override the config value for local testing
  - Interactive by default; --prompt sends one message and exits
  - Local file arguments are uploaded to the staging bucket's uploads/ prefix, which
    is the only prefix the agent's role may read; s3:// URIs are passed through
        """,
    )

    parser.add_argument(
        "--local",
        action="store_true",
        help="Test local agent on localhost:8080 (default: remote)",
    )

    parser.add_argument(
        "--pattern",
        type=str,
        help="Override agent pattern from config (e.g., 'medical-content-review')",
    )

    parser.add_argument(
        "--content-pdf",
        type=str,
        help="Medical content document to review (local path or s3:// URI)",
    )

    parser.add_argument(
        "--reference",
        type=str,
        action="append",
        default=[],
        help="Reference material to verify claims against (repeatable)",
    )

    parser.add_argument(
        "--claims",
        type=str,
        help="Pre-approved claims spreadsheet (.xlsx/.xlsm/.csv), enabling claim matching",
    )

    parser.add_argument(
        "--enabled-sources",
        type=str,
        help=(
            "Comma-separated external sources (pubmed,openfda,clinicaltrials,nova); "
            "pass an empty string to disable external evidence"
        ),
    )

    parser.add_argument(
        "--prompt",
        type=str,
        help="Send a single prompt and exit instead of starting an interactive chat",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    print("=" * 60)
    print("AgentCore Interactive Chat Tester")
    print("=" * 60 + "\n")

    args = parse_arguments()
    config: dict[str, str] = {}

    # Get stack configuration
    stack_cfg = get_stack_config()

    # LOCAL MODE
    if args.local:
        # Determine pattern: CLI arg > config.yaml > default (only needed for local mode)
        pattern = (
            args.pattern
            if args.pattern
            else stack_cfg.get("pattern", "medical-content-review")
        )
        print(f"Using pattern: {pattern}\n")
        print_section("LOCAL MODE - Auto-starting agent")

        # Get memory configuration
        memory_arn = stack_cfg["outputs"]["MemoryArn"]
        memory_id = memory_arn.split("/")[-1]
        region = stack_cfg["region"]
        stack_name = stack_cfg["stack_name"]

        # Check if agent is already running
        if check_port_available(8080):
            print_msg("Agent already running on localhost:8080", "info")
            print("Using existing agent instance...\n")
        else:
            # Start the agent
            start_local_agent(memory_id, region, stack_name, pattern)

    # REMOTE MODE
    else:
        print_section("REMOTE MODE - Testing deployed agent")

        stack_cfg = get_stack_config()
        print(f"Stack: {stack_cfg['stack_name']}\n")

        # Get configuration from CloudFormation outputs
        print("Fetching configuration from stack outputs...")
        outputs = stack_cfg["outputs"]

        # Validate required outputs exist
        required_outputs = ["CognitoUserPoolId", "CognitoClientId", "RuntimeArn"]
        missing = [key for key in required_outputs if key not in outputs]
        if missing:
            print_msg(f"Missing required stack outputs: {', '.join(missing)}", "error")
            sys.exit(1)

        print_msg("Configuration fetched")

        runtime_arn = outputs["RuntimeArn"]
        region = stack_cfg["region"]

        # Get credentials
        print_section("Authentication")

        username = input("Enter username: ").strip()
        if not username:
            print_msg("Username is required", "error")
            sys.exit(1)
        password = getpass.getpass(f"Enter password for {username}: ")

        # Authenticate
        access_token, id_token, user_id = authenticate_cognito(
            outputs["CognitoUserPoolId"], outputs["CognitoClientId"], username, password
        )

        # Use access token for AgentCore runtime (JWT authorizer)
        config["access_token"] = access_token
        config["runtime_arn"] = runtime_arn
        config["region"] = region
        print(f"\nRuntime ARN: {runtime_arn}")
        print(f"Region: {region}\n")

    # Upload any local documents before the first invocation, so the payload only ever
    # carries s3:// URIs
    attachments: dict = {}
    if args.content_pdf or args.reference or args.claims or args.enabled_sources:
        print_section("Attachments")
        attachments = build_attachments(
            args, stack_cfg["outputs"].get("StagingBucketName", "")
        )

    # Run interactive chat, or a single invocation when --prompt was given
    run_chat(args.local, config, attachments, args.prompt)


if __name__ == "__main__":
    main()
