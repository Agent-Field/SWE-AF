FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: git (worktrees, branches), curl (healthcheck), jq (agent bash),
# openssh-client (optional SSH git), gh CLI (draft PRs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl openssh-client jq && \
    # Install GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    # Install OpenCode CLI for opencode provider
    curl -fsSL https://raw.githubusercontent.com/opencode-ai/opencode/refs/heads/main/install | bash && \
    rm -rf /var/lib/apt/lists/*

# Add OpenCode to PATH for non-interactive shells
ENV PATH="/root/.opencode/bin:${PATH}"

# Git identity — env vars take highest precedence and are inherited by all
# subprocesses including Claude Code agent instances spawned by the SDK
ENV GIT_AUTHOR_NAME="SWE-AF" \
    GIT_AUTHOR_EMAIL="contact@agentfield.com" \
    GIT_COMMITTER_NAME="SWE-AF" \
    GIT_COMMITTER_EMAIL="contact@agentfield.com"

RUN git config --global user.name "SWE-AF" && \
    git config --global user.email "contact@agentfield.com"

# Install uv for fast package installation
RUN pip install --no-cache-dir uv

# Install project dependencies
COPY requirements-docker.txt /app/requirements.txt
RUN uv pip install --system -r /app/requirements.txt

# Copy application code
COPY . /app/

EXPOSE 8003

ENV PORT=8003 \
    AGENTFIELD_SERVER=http://control-plane:8080 \
    NODE_ID=swe-planner

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "-m", "swe_af"]
