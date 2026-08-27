# Studium backend image (infrastructure §4.2).
#
# Two stages. The builder installs into a virtualenv; the runtime copies the
# virtualenv and the source and installs nothing. The point is not image size --
# it is that the runtime image has no compiler and no pip cache, so a
# vulnerability in a build tool is not a vulnerability in the thing serving
# learners.
#
# python:3.12-slim-bookworm, pinned to the minor version. §4.2 does not name a
# Python; pyproject requires >=3.12 and the CI workflows all run 3.12, and a
# runtime that floats to 3.13 while CI stays on 3.12 is a difference nobody
# tests.

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential for anything without a wheel on this platform. pdfplumber's
# dependency tree is the one that needs it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# pyproject alone first, so a source-only change reuses the dependency layer.
# The install needs a package directory to exist, hence the placeholder --
# `pip install -e .` reads the metadata and does not walk the tree.
COPY pyproject.toml ./
RUN mkdir -p studium && touch studium/__init__.py \
 && pip install --no-cache-dir -e ".[serve,observability,retrieval,ingestion,evaluation]"

COPY . .
RUN pip install --no-cache-dir -e ".[serve,observability,retrieval,ingestion,evaluation]"


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    STUDIUM_SOURCE_STORAGE_ROOT=/data/sources

# Non-root. The process needs to read its own code and write to the volume,
# and nothing else -- a container that runs as root can rewrite its own
# application code, which is the difference between a code-execution bug and a
# persistent one.
RUN groupadd --system studium \
 && useradd --system --gid studium --home /app --shell /usr/sbin/nologin studium

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=studium:studium . .

# §4.5's Fly volume mount point. Created here so the directory exists with the
# right owner even on the first boot before anything has written to it.
RUN mkdir -p /data/sources && chown -R studium:studium /data

USER studium

EXPOSE 8000

# §4.2's health check. Fly's own check is configured in fly.toml and is the one
# that gates routing; this one makes `docker run` locally behave the same way,
# and makes the image self-describing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# One worker. §4.2 sizes the VM for "the FastAPI process, embedding worker,
# ingestion worker, and the scheduled retention/warming jobs" -- all of which
# are in-process background tasks, and every one of them would run once per
# worker. Two uvicorn workers means two cache warmers billing a reranker and
# two retention schedulers at 02:00. The advisory lock in
# studium.ops.scheduler covers the second; the first has no such guard, and one
# worker is the right answer at MVP concurrency anyway.
CMD ["uvicorn", "studium.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75"]
