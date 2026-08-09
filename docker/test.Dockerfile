FROM python:3.12-slim

# Override at build time (--build-arg) where pypi.org is slow, e.g.
# https://mirrors.aliyun.com/pypi/simple/ — defaults to upstream PyPI.
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY proseforge ./proseforge
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[api,dev]"
COPY . .

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["sh", "/app/docker/entrypoint-test.sh"]
