FROM python:3.12-slim

# Claude Code is needed at runtime so the server can spawn it.
# Install via the official npm package.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY agentrelay ./agentrelay
COPY hook.py ./

RUN pip install --no-cache-dir .

ENV AGENTRELAY_HOST=0.0.0.0 \
    AGENTRELAY_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# When deployed (Fly.io, Render, etc.) you have a stable hostname already, so
# no tunnel is needed. Run the server directly.
CMD ["agentrelay", "run", "--no-tunnel", "--host", "0.0.0.0", "--port", "8000"]
