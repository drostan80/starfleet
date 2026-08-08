# LCARS — SCOPE.md §11.3. Containerized, deployed as an additional
# service inside the existing Sonarr docker-compose stack (same host as
# Sonarr/Radarr, not a separate machine). This image also makes the
# eventual Python->Rust rewrite (§11.1) a clean drop-in image swap, with
# no change to how it's deployed or how it joins the Sonarr stack.
#
# Built by BUILD_PLAN.md 0.5, ahead of 0.4's CI workflow (which needs
# something to build) — no app code exists yet (Phase A), so the image
# builds and installs cleanly but has no real CMD to run yet; see the
# placeholder below.

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

# Placeholder — BUILD_PLAN.md 0.5 only requires the image to *build*,
# not run; there's no ASGI app yet (that's Phase A, §8). Replace with
# the real serve command once it exists, e.g.:
#   CMD ["uvicorn", "lcars.server:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["python", "-c", "import lcars; print(f'lcars {lcars.__version__} — no ASGI app yet, Phase A (BUILD_PLAN.md)')"]
