FROM python:3.12-slim

# Override at build time (--build-arg) where pypi.org is slow, e.g.
# https://mirrors.aliyun.com/pypi/simple/ — defaults to upstream PyPI.
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY proseforge ./proseforge
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[api]"
COPY . .

# Offline default embedding engine (bge-m3 via llama.cpp): bake the GGUF
# weights and the llama-server binary into the image at build time so a
# network-isolated runtime never downloads. The runtime lookup prefers
# /opt/proseforge/models over the download cache (BUNDLED_MODELS_ROOTS in
# proseforge/infrastructure/embeddings/llama_server.py). Disable with
# --build-arg BUNDLE_EMBEDDINGS=0 (e.g. offline CI builds).
ARG BUNDLE_EMBEDDINGS=1
ARG LLAMA_CPP_TAG=b10291
RUN if [ "$BUNDLE_EMBEDDINGS" = "1" ]; then \
        python packaging/models/fetch.py --gguf BAAI/bge-m3 \
        && python packaging/models/fetch_llama_bin.py --platform linux --tag "$LLAMA_CPP_TAG" \
        && mkdir -p /opt/proseforge/models \
        && mv packaging/models/gguf /opt/proseforge/models/gguf \
        && mv packaging/models/llama-bin /opt/proseforge/models/llama-bin; \
    else \
        mkdir -p /opt/proseforge/models; \
    fi

RUN addgroup --system --gid 10001 proseforge \
    && adduser --system --uid 10001 --ingroup proseforge proseforge \
    && mkdir -p /data/blobs /data/backups \
    && chown -R proseforge:proseforge /app /data

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
USER proseforge
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint-api.sh"]
CMD ["uvicorn", "proseforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
