# contains with controller

# docker build -f Dockerfile -t qalby-tech/ubuntu-xfce-vnc-firefox .
# FROM kamasalyamov/ubuntu-xfce-vnc-firefox:latest 
FROM accetto/ubuntu-vnc-xfce-firefox-g3:latest

# Switch to root to install dependencies
USER root

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies for screenshot functionality
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gnome-screenshot \
    scrot \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python 3.9 using uv
RUN uv python install 3.9

# Create workspace and copy controller files
RUN mkdir -p /workspace /controller
COPY controller/ /controller/

# Create necessary directories for headless user and set permissions
RUN mkdir -p /home/headless/.cache /home/headless/.local/share/uv /controller/profile && \
    chown -R headless:headless /controller /workspace /home/headless/.cache /home/headless/.local

# Install dependencies and run the application
WORKDIR /controller
# Cache uv dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync
# Cache Chrome installation using BuildKit cache mount
RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    uv run patchright install chrome

# Switch back to headless user
USER headless
# CMD ["--tail-log"]
CMD ["uv", "run", "main.py"]







