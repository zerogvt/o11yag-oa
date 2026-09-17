# syntax=docker/dockerfile:1

# ---------- Stage 1: build dependencies into a venv ----------
FROM python:3.12-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ---------- Stage 2: slim runtime image ----------
FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8003

COPY server.py config.py data.py o11y.py ./

USER appuser

EXPOSE 8003

# No HEALTHCHECK against /mcp: Streamable HTTP expects an initialized MCP
# session and answers a bare GET with an error, which would look like a dead
# container. Kubernetes uses a TCP probe instead (see the manifest).

CMD ["python", "server.py"]
