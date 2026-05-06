# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    DOCKER_CONTAINER=1 \
    OTEL_PYTHON_LOG_CORRELATION=true \
    PYTHONUNBUFFERED=1

COPY pyproject.toml .
COPY gateway/ gateway/

# System deps for pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*

COPY patterns/medical-content-review/requirements.txt requirements.txt
RUN uv pip install --no-cache -r requirements.txt && \
    uv pip install --no-cache aws-opentelemetry-distro==0.10.1

RUN uv pip install --no-cache -e . --no-deps && \
    uv pip install --no-cache requests>=2.31.0

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

COPY patterns/medical-content-review/medical_review_agent.py .
COPY patterns/medical-content-review/review_upload_hook.py .
COPY patterns/medical-content-review/prompts/ prompts/
COPY patterns/medical-content-review/reviewers/ reviewers/
COPY patterns/medical-content-review/tools/ tools/
COPY patterns/utils/ utils/

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

CMD ["opentelemetry-instrument", "python", "-m", "medical_review_agent"]
