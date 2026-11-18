FROM python:3.12-slim

ARG INSTALL_RENDER=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GEO_MCP_DATA_DIR=/data GEO_MCP_PROJECTS=/app/config/projects.yaml \
    GEO_MCP_HOST=0.0.0.0 GEO_MCP_PORT=8765

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && if [ "$INSTALL_RENDER" = "true" ]; then pip install --no-cache-dir playwright && playwright install --with-deps chromium; fi
COPY config ./config

RUN useradd --create-home --uid 10001 geo && mkdir -p /data && chown geo:geo /data
USER geo
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz').status==200 else 1)"

CMD ["geo-mcp", "serve", "--transport", "http"]
