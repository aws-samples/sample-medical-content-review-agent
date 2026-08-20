# Local Docker Testing Guide

Test your AgentCore agent by building and running the actual Docker image locally before deployment.

## Why Docker Testing?

| Testing Mode | What It Tests | AWS Resources Needed |
|--------------|---------------|---------------------|
| `test-agent.py --local` | Python code directly via `uv run` | Memory, Gateway, SSM |
| **`test-agent-docker.py`** | **Docker image (production artifact)** | Memory, Gateway, SSM |
| `test-agent.py` (remote) | Deployed agent in AgentCore Runtime | Full deployment |

Docker testing validates:

- Dockerfile builds correctly
- Dependencies install properly in container
- Container starts and responds to health checks
- Agent works in the same containerized environment as production

## Prerequisites

1. **Docker** installed and running (`docker ps` should work)
2. **Deployed stack** - Required for Memory ID, Gateway URL, and SSM parameters
3. **AWS credentials** configured in your environment

## Quick Start

```bash
# Build and test (uses pattern from config.yaml)
python test-scripts/test-agent-docker.py

# Build only (verify Dockerfile without running)
python test-scripts/test-agent-docker.py --build-only

# Skip build, use existing image
python test-scripts/test-agent-docker.py --skip-build

# Test specific pattern
python test-scripts/test-agent-docker.py --pattern medical-content-review
```

## How It Works

1. **Build**: Creates Docker image using the same Dockerfile as CDK deployment
2. **Configure**: Fetches Memory ID and Stack Name from deployed CloudFormation stack
3. **Run**: Starts container with AWS credentials passed through as environment variables
4. **Test**: Opens interactive chat session against `localhost:8080`

```
┌─────────────────────────────────────────────────────────────┐
│  Local Machine                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Docker Container (ARM64)                           │   │
│  │  ┌─────────────────────────────────────────────┐   │   │
│  │  │  Agent                                      │   │   │
│  │  │  - Listens on :8080                         │   │   │
│  │  │  - Uses passed AWS credentials              │   │   │
│  │  └─────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                 │
│                           ▼                                 │
│              http://localhost:8080/invocations              │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────┐
              │  AWS (Deployed Resources)       │
              │  - AgentCore Memory             │
              │  - AgentCore Gateway            │
              │  - SSM Parameters               │
              │  - Secrets Manager              │
              └─────────────────────────────────┘
```

## Environment Variables

The script automatically passes these to the container:

| Variable | Source | Purpose |
|----------|--------|---------|
| `MEMORY_ID` | Stack outputs | AgentCore Memory resource ID |
| `STACK_NAME` | config.yaml | SSM parameter prefix for Gateway lookup |
| `AWS_DEFAULT_REGION` | Stack | AWS region |
| `AWS_ACCESS_KEY_ID` | Local env | AWS authentication |
| `AWS_SECRET_ACCESS_KEY` | Local env | AWS authentication |
| `AWS_SESSION_TOKEN` | Local env | AWS authentication (if using temporary credentials) |
| `ALLOW_LOCAL_USER_ID_HEADER` | Set to `true` by the script | Lets the agent take the user identity from `X-Local-User-Id`, since no Cognito pool signs tokens for a local container (see [Authentication](#authentication-the-local-identity-header)) |

**Important: AWS credentials must be exported as environment variables.** The Docker container cannot read credentials from `~/.aws/credentials` or `~/.aws/config`. Before running, export them:

```bash
# If using aws configure profiles, export the credentials explicitly:
export AWS_ACCESS_KEY_ID=$(aws configure get aws_access_key_id)
export AWS_SECRET_ACCESS_KEY=$(aws configure get aws_secret_access_key)
export AWS_SESSION_TOKEN=$(aws configure get aws_session_token)  # if using temporary credentials
```

## Authentication: The Local Identity Header

In production, the user's JWT arrives in the `Authorization` header and the agent verifies it itself against the Cognito user pool's JWKS (RS256, issuer and expiry checked, client ID matched against the pool's app clients). The user ID comes from the verified token's `sub` claim — never from a `userId` field in the request payload, which prompt injection could set.

A container run has no pool that will sign a token for it, and an unsigned or self-signed token is (correctly) rejected by that verification. So the test scripts name the identity directly in an `X-Local-User-Id` header, which the agent honours **only** when `ALLOW_LOCAL_USER_ID_HEADER` is set — as `test-agent-docker.py` does. The deployed Runtime never sets that variable, and its authorizer forwards only `Authorization` anyway, so the header cannot be used to impersonate anyone in a deployed environment.

| Environment | `ALLOW_LOCAL_USER_ID_HEADER` | Identity source |
|-------------|------------------------------|-----------------|
| Docker / `--local` | `true` | `X-Local-User-Id` header |
| Deployed Runtime | unset | `sub` of the verified Cognito JWT |

## Troubleshooting

### Build fails with "platform mismatch"

AgentCore Runtime requires ARM64 architecture. On x86/amd64 machines, enable emulation:

```bash
# One-time setup for ARM64 emulation
docker run --privileged --rm tonistiigi/binfmt --install all
```

### Container starts but agent fails

Check container logs:

```bash
# Find container ID
docker ps

# View logs
docker logs <container-id>
```

Common issues:

- **Missing AWS credentials**: Ensure `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` if using temporary creds) are set
- **Expired session token**: Refresh your AWS credentials
- **Stack not deployed**: The script needs a deployed stack to fetch Memory ID and SSM parameters

### "Connection refused" on localhost:8080

The agent may still be starting. The script waits up to 30 seconds. If it times out:

1. Check if the container is still running: `docker ps`
2. Check logs for errors: `docker logs <container-id>`

### Gateway authentication fails

Ensure your AWS credentials have access to:

- SSM Parameter Store: `/{stack_name}/*`
- Secrets Manager: `/{stack_name}/machine_client_secret`

### ECS/EKS warnings in logs

These warnings are expected when running locally:

```
AwsEcsResourceDetector failed: Missing ECS_CONTAINER_METADATA_URI...
AwsEksResourceDetector failed: No such file or directory...
```

The OpenTelemetry instrumentation looks for ECS/EKS metadata which doesn't exist locally. These can be safely ignored.

## When to Use Each Testing Mode

| Scenario | Recommended Mode |
|----------|------------------|
| Quick iteration on agent logic | `test-agent.py --local` |
| Verify Dockerfile builds correctly | `test-agent-docker.py --build-only` |
| Full container integration test | `test-agent-docker.py` |
| Test deployed production agent | `test-agent.py` (remote) |
| CI/CD pipeline validation | `test-agent-docker.py --build-only` |

## Advanced Usage

### Manual Docker Commands

If you need more control, you can run Docker commands directly:

```bash
# Build image manually
docker build -f patterns/medical-content-review/Dockerfile \
  -t adr-agent-local \
  --platform linux/arm64 .

# Run with explicit env vars
docker run --rm -it -p 8080:8080 \
  --platform linux/arm64 \
  -e MEMORY_ID=<your-memory-id> \
  -e STACK_NAME=<your-stack-name> \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN \
  -e ALLOW_LOCAL_USER_ID_HEADER=true \
  adr-agent-local

# Test with curl. The agent verifies every JWT against the Cognito pool, and no pool
# signs tokens for a local run, so the identity is named in a header instead. This is
# only honoured because ALLOW_LOCAL_USER_ID_HEADER is set above; the deployed Runtime
# never sets it and takes the user from the token's `sub` claim.
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "X-Local-User-Id: test-user" \
  -d '{"prompt": "Hello", "runtimeSessionId": "test-123"}'
```

### Testing Health Endpoint

```bash
curl http://localhost:8080/ping
# Returns: {"status":"Healthy","time_of_last_update":...}
```

### Viewing Container Logs in Real-Time

```bash
# Start container in foreground (not detached)
docker run --rm -p 8080:8080 \
  --platform linux/arm64 \
  -e MEMORY_ID=<memory-id> \
  -e STACK_NAME=<stack-name> \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN \
  adr-agent-local
```

## Related Documentation

- [Deployment Guide](DEPLOYMENT.md) - Full stack deployment instructions
- [Local Development](LOCAL_DEVELOPMENT.md) - Local development with Docker Compose
