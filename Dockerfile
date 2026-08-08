# LCARS — SCOPE.md §11.3. Containerized, deployed as an additional
# service inside the existing Sonarr docker-compose stack (same host as
# Sonarr/Radarr, not a separate machine). This image also makes the
# eventual Python->Rust rewrite (§11.1) a clean drop-in image swap, with
# no change to how it's deployed or how it joins the Sonarr stack.
#
# Originally scaffolded by BUILD_PLAN.md 0.5, ahead of 0.4's CI workflow
# (which needed something to build) — at that point no app code existed
# yet, so the image only needed to build, not run (see git history for
# the placeholder CMD that stood in until A.3). CMD below is real now.

FROM python:3.12-slim AS base

# Matches pyproject.toml's requires-python floor (>=3.12). Pinned to a
# specific minor rather than the 3.14 the local dev venv happens to run,
# for a stable, widely-wheel-covered base — bump deliberately, not by
# accident, once dependencies are proven fine on a newer minor.

WORKDIR /app

# Dependency layer first so it's cached across app-code-only rebuilds.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# lcars.db/lcars.ini are runtime state, not baked into the image —
# SCOPE.md §11.2, same reasoning as the .gitignore entries for them.
# Alembic migrations ship in the image so the container can run them
# on startup against a mounted volume.
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Non-root — no reason for this process to run as root inside the
# Sonarr stack.
RUN useradd --create-home --uid 1000 lcars
USER lcars

EXPOSE 8000

# Real serve command, landed in A.3 (was a placeholder through 0.5/0.4).
# Runs pending migrations first, against the volume-mounted lcars.db —
# fulfills this file's own comment above about why migrations ship in
# the image at all. `lcars` is the pyproject.toml console script
# (src/lcars/cli.py), binding 0.0.0.0 by default (needed for Docker to
# publish the port on any interface — not a network-topology decision,
# see the standing side-question answer in this project's history).
CMD ["sh", "-c", "alembic upgrade head && lcars"]
