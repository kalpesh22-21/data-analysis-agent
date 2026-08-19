# One image, seven workloads (runtime API, UI BFF, inbox service, learning
# consumer/sweeper/scheduler, hydrator). The Helm chart overrides `command:`
# per Deployment; the default CMD below is the runtime API.
#
# Base is pinned to 3.12: the couchbase C-extension wheels are reliable there,
# and the sqlglot pin in pyproject.toml is load-bearing (blueprint dedup keys).
# Both stages AND the cross-stage site-packages path read this one ARG — they
# must never drift apart, or the runtime interpreter looks in a lib directory
# the packages were not installed into.
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 — build: resolve deps from the committed lockfile, install into the
# system interpreter so the final stage can copy site-packages wholesale.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv lives at /uv (not /usr/local/bin) so the final stage can copy the whole of
# the builder's /usr/local/bin without dragging the uv binary into the image.
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uv

ENV UV_LINK_MODE=copy
WORKDIR /app

# Third-party deps first, straight from uv.lock, so editing source does not
# invalidate this layer. Hashes come from the lock and are enforced on install.
COPY pyproject.toml uv.lock ./
RUN /uv export --frozen --no-dev --no-emit-project --no-editable -o /tmp/requirements.txt \
    && /uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt

# The project itself is installed EDITABLE on purpose. RuntimeSettings resolves
# its default fixture paths as `Path(__file__).resolve().parents[3]`
# (src/data_agent/runtime/config.py -> repo root); a normal site-packages
# install makes that the python lib dir and the lookups break. Editable keeps
# /app/src/data_agent as the live source tree, so parents[3] == /app.
COPY src ./src
RUN /uv pip install --system --no-cache --no-deps -e .

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

# Re-declared: a global ARG is not in scope inside a stage until restated.
ARG PYTHON_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /usr/local/lib/python${PYTHON_VERSION}/site-packages /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# WORKDIR /app is load-bearing, not cosmetic: every workload is launched by a
# relative script path (`python scripts/run_*.py`) and the editable install above
# points at /app/src. `ui/` ships as source rather than in the wheel;
# scripts/run_ui_bff.py puts the repo root on sys.path itself, because running a
# script by path prepends the SCRIPT's directory, not the working directory.
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY ui ./ui
COPY scripts ./scripts

# tests/fixtures/ is a production runtime asset despite the path (~224 KB):
# scripts/run_learning_consumer.py reads tests/fixtures/catalog_export.json at
# startup unconditionally, and corpus/{blueprints,knowledge}.yaml are read when
# CORPUS_SOURCE=fixture. Both are resolved relative to /app by config.py.
COPY tests/fixtures ./tests/fixtures

# runtime API / UI BFF / inbox service (inbox is opt-in).
EXPOSE 8000 3000 8100

# Everything above is root-owned and world-readable; the chart also runs with
# readOnlyRootFilesystem=true, so the image needs no writable paths.
USER 10001

# The launcher, not `uvicorn` — a bare CLI serve dies by signal at 143 on a clean
# SIGTERM shutdown (ISSUES.md C3). The chart overrides this per Deployment; the
# default is what `docker run <image>` (and any compose file without a command) gets.
CMD ["python", "scripts/run_runtime_api.py", "--host", "0.0.0.0", "--port", "8000"]
