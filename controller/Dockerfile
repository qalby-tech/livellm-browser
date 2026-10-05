# kamasalyamov/livellm-browser:controller-<v> (<v> = controller/pyproject.toml):
# the Browser API, one image for both engines. Both clients are installed
# (core/pw.py): patchright drives Chrome browsers over CDP, stock Playwright a
# Camoufox browser's own Playwright server; each starts its own Node driver.
# Every dependency comes from uv.lock (hashed), nothing is fetched at runtime.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

COPY --from=ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=0 \
    HOME=/tmp

WORKDIR /app
# The locked dependencies first (a layer of their own), then the code.
COPY pyproject.toml uv.lock .python-version ./
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen --no-dev --no-install-project && \
    rm -rf /tmp/uv-cache

COPY . /app
# The image's own checks: both clients import, each from its package, and so
# does the app.
RUN .venv/bin/python -c "import core.pw as pw; assert pw._chrome_playwright.__module__.startswith('patchright.'); assert pw._camoufox_playwright.__module__.startswith('playwright.')" && \
    .venv/bin/python -c "import main"

USER 1000:1000
EXPOSE 8000
CMD ["/app/.venv/bin/python", "main.py"]
