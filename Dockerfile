FROM python:3.14-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.14-slim AS builder
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c "import ast; ast.parse(open('server.py').read()); print('server.py syntax OK')"

FROM python:3.14-slim AS server
LABEL org.opencontainers.image.title="cline-hermes-bridge"
LABEL org.opencontainers.image.description="MCP bridge exposing Cline session history and process control for Hermes Agent"
LABEL org.opencontainers.image.url="https://github.com/anthropics/cline-hermes-bridge"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY server.py .
COPY README.md .
COPY LICENSE .

# Ensure clean entrypoint
ENTRYPOINT ["python", "-u", "server.py"]

# Stdio-based communication (no ports exposed)
# The server reads/writes JSON-RPC 2.0 over stdin/stdout
