# ResQ-AI triage agent for Amazon Bedrock AgentCore Runtime (ARM64 is required by AgentCore).
#
# Build from backend/ and push to ECR repo resq-agentcore-runtime:
#   docker buildx build --platform linux/arm64 -f agent_runtime/Dockerfile -t <acct>.dkr.ecr.<region>.amazonaws.com/resq-agentcore-runtime:<tag> --push .
#
# Serves POST /invocations and GET /ping on port 8080 (BedrockAgentCoreApp).
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOCKER_CONTAINER=1 \
    MOCK_AWS=False \
    SEED_DEMO_DATA=False \
    AGENT_BACKEND=local

WORKDIR /app

COPY agent_runtime/requirements.txt ./agent_runtime/requirements.txt
RUN pip install --no-cache-dir -r agent_runtime/requirements.txt

# Only the modules the runtime imports: config, schemas and the agent package.
COPY app/__init__.py ./app/__init__.py
COPY app/config.py ./app/config.py
COPY app/models ./app/models
COPY app/agents ./app/agents
COPY agent_runtime ./agent_runtime

RUN useradd --create-home --uid 1000 agent && chown -R agent /app
USER agent

EXPOSE 8080
CMD ["python", "-m", "agent_runtime.app"]
