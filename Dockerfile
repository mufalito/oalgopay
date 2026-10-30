# OalgoPay — one image, two roles (docker-compose.yml picks the entrypoint):
#   gateway: uv run run.py
#   core:    bin/blocknet-core-amd64-linux --api 0.0.0.0:18512 --data /app/data/blocknet

# ---- stage 1: fetch + verify the Blocknet core release ---------------------
FROM debian:bookworm-slim AS core
ARG CORE_VERSION=0.22.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN curl -sLO "https://github.com/blocknetprivacy/core/releases/download/v${CORE_VERSION}/blocknet-core-amd64-linux-${CORE_VERSION}.zip" \
 && curl -sLO "https://github.com/blocknetprivacy/core/releases/download/v${CORE_VERSION}/SHA256SUMS.txt" \
 && grep " blocknet-core-amd64-linux-${CORE_VERSION}.zip\$" SHA256SUMS.txt | sha256sum -c - \
 && unzip -o "blocknet-core-amd64-linux-${CORE_VERSION}.zip" \
 && chmod +x blocknet-core-amd64-linux

# ---- stage 2: the app --------------------------------------------------------
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY bin/README.md ./bin/README.md
COPY run.py config.example.json ./
COPY --from=core /tmp/blocknet-core-amd64-linux ./bin/blocknet-core-amd64-linux
ENV OALGOPAY_API_BASE=http://core:18512 \
    OALGOPAY_DATADIR=/app/data/blocknet
EXPOSE 8096
CMD ["uv", "run", "--no-dev", "run.py"]
