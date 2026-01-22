# contains with controller

# docker build -f Dockerfile -t qalby-tech/ubuntu-xfce-vnc-firefox .
# FROM kamasalyamov/ubuntu-xfce-vnc-firefox:latest 
# FROM accetto/ubuntu-vnc-xfce-firefox-g3:latest
FROM accetto/ubuntu-vnc-xfce-g3:24.04

# Switch to root to install dependencies
# USER root
USER 0

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies for screenshot functionality and display detection
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gnome-screenshot \
    scrot \
    x11-utils \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python 3.9 using uv
RUN uv python install 3.9

ENV PYTHONUNBUFFERED=1

WORKDIR "${HOME}"/Desktop/app
# Copy only dependency files first (for better caching)
COPY pyproject.toml uv.lock ./

# Install Python dependencies and Chrome (cached layer)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev
RUN uv run patchright install chrome

# Now copy the rest of the application code
COPY . .

# # Create necessary directories for headless user and set permissions
# # profiles/default is the default browser profile directory
# RUN mkdir -p /home/headless/.cache /home/headless/.local/share/uv /controller/profiles/default && \
#     chown -R headless:headless /controller /workspace /home/headless/.cache /home/headless/.local

# Fix permissions for startup script modification
RUN chmod 666 /etc/passwd /etc/group

# Ensure the app directory and cache are owned by the headless user
RUN chown -R "${HEADLESS_USER_ID}":"${HEADLESS_USER_GROUP_ID}" "${HOME}"/Desktop/app "${HOME}"/.cache "${HOME}"/.local

# Create a custom startup script that waits for VNC, then runs main.py
RUN printf '%s\n' \
    '#!/bin/bash' \
    'set -e' \
    '' \
    '# Track child PIDs for graceful shutdown' \
    'STARTUP_PID=""' \
    'APP_PID=""' \
    '' \
    '# Graceful shutdown handler' \
    'shutdown() {' \
    '  echo "Received shutdown signal, stopping services..."' \
    '  if [ -n "$APP_PID" ] && kill -0 $APP_PID 2>/dev/null; then' \
    '    echo "Stopping main.py (PID $APP_PID)..."' \
    '    kill -TERM $APP_PID 2>/dev/null || true' \
    '    wait $APP_PID 2>/dev/null || true' \
    '  fi' \
    '  if [ -n "$STARTUP_PID" ] && kill -0 $STARTUP_PID 2>/dev/null; then' \
    '    echo "Stopping VNC services (PID $STARTUP_PID)..."' \
    '    kill -TERM $STARTUP_PID 2>/dev/null || true' \
    '    wait $STARTUP_PID 2>/dev/null || true' \
    '  fi' \
    '  echo "Shutdown complete"' \
    '  exit 0' \
    '}' \
    '' \
    '# Trap SIGTERM and SIGINT' \
    'trap shutdown SIGTERM SIGINT' \
    '' \
    '# Start VNC/desktop environment in background' \
    '/dockerstartup/startup.sh "$@" &' \
    'STARTUP_PID=$!' \
    '' \
    '# Wait for X11 display to be ready (up to 80 seconds)' \
    'echo "Waiting for display :1 to be ready..."' \
    'for i in $(seq 1 80); do' \
    '  if xdpyinfo -display :1 >/dev/null 2>&1; then' \
    '    echo "Display :1 is ready!"' \
    '    break' \
    '  fi' \
    '  sleep 1' \
    'done' \
    '' \
    '# Start main.py with logs to stdout (visible in docker logs)' \
    'echo "Starting main.py..."' \
    'cd /home/headless/Desktop/app && /bin/uv run main.py 2>&1 &' \
    'APP_PID=$!' \
    'echo "main.py started (PID $APP_PID)"' \
    '' \
    '# Wait for any child to exit (keeps container running)' \
    'wait -n 2>/dev/null || wait' \
    > /usr/local/bin/custom-startup.sh \
    && chmod +x /usr/local/bin/custom-startup.sh

# Switch back to headless user
USER "${HEADLESS_USER_ID}"

# Use custom entrypoint that starts main.py then hands off to VNC startup
ENTRYPOINT ["/usr/local/bin/custom-startup.sh"]
CMD ["--wait"]
