# syntax=docker/dockerfile:1
# Lakana: one image for the server, workers and first-run setup.
# Build context: run these two commands first, from the repo root:
#   uv build --all-packages -o dist
#   uv export --frozen --no-dev --all-packages --no-emit-workspace --no-hashes -o dist/constraints.txt

FROM python:3.14-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*
COPY dist/ /wheels/
RUN python -m venv /opt/lakana \
 && /opt/lakana/bin/pip install -c /wheels/constraints.txt /wheels/*.whl \
 && /opt/lakana/bin/pip uninstall -y pip

# uv, for the Python assets' venvs. MIT OR Apache-2.0. Pinned by tag and digest, in a stage
# of its own: Dependabot's docker ecosystem reads `FROM` lines but not the image of a
# `COPY --from=<image>` (dependabot-core #5103), so a bare COPY would never get an update PR.
# Dependabot bumps the tag and the digest together; by hand, use `docker buildx imagetools inspect`.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.14-slim
ENV TMPDIR=/var/tmp/lakana \
    HOME=/home/lakana \
    PATH=/opt/lakana/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LAKANA_HOST=0.0.0.0 \
    LAKANA_PORT=8000
# Directories that named volumes inherit owner and mode from on first use: the worker's attempt
# directories (shared with task containers through the launcher), the launcher's socket directory
# (its group is the worker's; set-group-id keeps the socket and token in that group, sticky stops
# the worker from renaming or deleting them), the launcher's own state and the egress proxy's key.
RUN groupadd --system --gid 10001 lakana \
 && useradd --system --uid 10001 --gid lakana --no-create-home --home-dir /home/lakana --shell /usr/sbin/nologin lakana \
 && mkdir -p /var/tmp/lakana /home/lakana/.cache/lakana-worker \
 && chown -R lakana:lakana /home/lakana /var/tmp/lakana \
 && chmod 0700 /home/lakana/.cache/lakana-worker \
 && install -d -o 10001 -g 10001 -m 0700 /home/lakana/.cache/lakana-worker/attempts \
 && install -d -o 0 -g 10001 -m 0770 /run/lakana-launcher \
 && chmod 3770 /run/lakana-launcher \
 && install -d -o 0 -g 0 -m 0700 /var/lib/lakana-launcher \
 && install -d -o 0 -g 10001 -m 2750 /run/lakana-egress
# git and ssh fetch repositories for deploys; runtime packages only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*
# uv builds the venvs of Python assets: copied from the `uv` stage above. The worker's venv
# cache is private to uid 10001 (the worker refuses one that is not); a named volume mounted over
# it inherits that owner and mode on first use.
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=builder /opt/lakana /opt/lakana
COPY --from=builder /usr/bin/tini /usr/bin/tini
# The launcher holds the Docker socket, so it must not start on the venv above: every `.pth` file
# of a dependency would run in it before `main`. It runs from this copy of its own package (it
# imports only the standard library) with the base interpreter, `-I` (no environment, no user
# site) and `-S` (no site at all). The bundle's `launcher` service sets this as its command:
# launcher command: /usr/local/bin/python -I -S -c "import sys; sys.path.insert(0, '/opt/lakana-launcher'); from lakana_worker.launcher.__main__ import main; main()"
COPY --from=builder /opt/lakana/lib/python3.14/site-packages/lakana_worker/launcher/ /opt/lakana-launcher/lakana_worker/launcher/
RUN touch /opt/lakana-launcher/lakana_worker/__init__.py \
 && chmod -R go-w /opt/lakana-launcher
USER 10001:10001
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('LAKANA_PORT', '8000')}/healthz\", timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["lakana-server"]
