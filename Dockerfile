# Start from scratch with Ubuntu 24.04
FROM ubuntu:24.04

# Prevent interactive prompts during apt
ENV DEBIAN_FRONTEND=noninteractive

# Set environment variables for the headless user and display
ENV HEADLESS_USER_ID=1000 \
    HEADLESS_USER_GROUP_ID=1000 \
    USER=headless \
    HOME=/home/headless \
    DISPLAY=:1 \
    RESOLUTION=1920x1080 \
    VNC_PORT=5901 \
    NOVNC_PORT=6901 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    sudo \
    bash \
    net-tools \
    novnc \
    websockify \
    tigervnc-standalone-server \
    tigervnc-common \
    dbus-x11 \
    x11-utils \
    x11-xserver-utils \
    xfce4 \
    xfce4-terminal \
    gnome-screenshot \
    scrot \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    curl \
    wget \
    ca-certificates \
    tzdata \
    && fc-cache -fv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Handle the user creation/modification (Ubuntu 24.04 uses ubuntu user with UID 1000 by default)
RUN if getent passwd 1000 > /dev/null; then \
        usermod -l ${USER} -d ${HOME} -m $(getent passwd 1000 | cut -d: -f1) && \
        groupmod -n ${USER} $(getent group 1000 | cut -d: -f1); \
    else \
        groupadd -g ${HEADLESS_USER_GROUP_ID} ${USER} && \
        useradd -u ${HEADLESS_USER_ID} -g ${HEADLESS_USER_GROUP_ID} -G sudo -d ${HOME} -m -s /bin/bash ${USER}; \
    fi && \
    mkdir -p ${HOME} && \
    chown -R ${USER}:${USER} ${HOME} && \
    echo "${USER} ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Install uv for Python dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Switch to root to install dependencies and configure app (already root, but being explicit)
USER 0

# Install Python 3.9 using uv
RUN uv python install 3.9

WORKDIR ${HOME}/Desktop/app

# Copy only dependency files first (for better caching)
COPY pyproject.toml uv.lock ./

# Install Python dependencies and Chrome (cached layer)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev
# Install patchright system dependencies and Chrome
RUN uv run patchright install-deps chrome || true
RUN uv run patchright install chrome

# Now copy the rest of the application code
COPY . .

# Fix permissions for startup script modification and cache
RUN chmod 666 /etc/passwd /etc/group && \
    mkdir -p "${HOME}/Desktop/app/profiles/default" && \
    chown -R "${HEADLESS_USER_ID}":"${HEADLESS_USER_GROUP_ID}" "${HOME}" "${HOME}/Desktop/app/profiles"

# Create a custom startup script
RUN printf '%s\n' \
    '#!/bin/bash' \
    'set -e' \
    '' \
    '# Track child PIDs for graceful shutdown' \
    'VNC_PID=""' \
    'NOVNC_PID=""' \
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
    '  if [ -n "$NOVNC_PID" ] && kill -0 $NOVNC_PID 2>/dev/null; then' \
    '    echo "Stopping NoVNC (PID $NOVNC_PID)..."' \
    '    kill -TERM $NOVNC_PID 2>/dev/null || true' \
    '  fi' \
    '  echo "Stopping VNC server..."' \
    '  vncserver -kill $DISPLAY 2>/dev/null || true' \
    '  echo "Shutdown complete"' \
    '  exit 0' \
    '}' \
    '' \
    '# Trap SIGTERM and SIGINT' \
    'trap shutdown SIGTERM SIGINT' \
    '' \
    '# Set up VNC password and xstartup' \
    'mkdir -p ~/.vnc' \
    'echo "#!/bin/bash" > ~/.vnc/xstartup' \
    'echo "xrdb \$HOME/.Xresources 2>/dev/null || true" >> ~/.vnc/xstartup' \
    'echo "startxfce4 &" >> ~/.vnc/xstartup' \
    'echo "tail -f /dev/null" >> ~/.vnc/xstartup' \
    'chmod +x ~/.vnc/xstartup' \
    '' \
    '# Remove old VNC locks' \
    'rm -rf /tmp/.X1-lock /tmp/.X11-unix/X1 ~/.vnc/*.log ~/.vnc/*.pid' \
    '' \
    '# Start VNC server' \
    'echo "Starting VNC server on $DISPLAY..."' \
    'vncserver $DISPLAY -geometry $RESOLUTION -depth 24 -SecurityTypes None > /dev/null 2>&1' \
    '' \
    '# Start NoVNC' \
    'echo "Starting NoVNC..."' \
    'websockify --web /usr/share/novnc/ $NOVNC_PORT localhost:$VNC_PORT > /dev/null 2>&1 &' \
    'NOVNC_PID=$!' \
    '' \
    '# Wait for X11 display to be ready (up to 80 seconds)' \
    'echo "Waiting for display $DISPLAY to be ready..."' \
    'for i in $(seq 1 80); do' \
    '  if xdpyinfo -display $DISPLAY >/dev/null 2>&1; then' \
    '    echo "Display $DISPLAY is ready!"' \
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

# Use custom entrypoint
ENTRYPOINT ["/usr/local/bin/custom-startup.sh"]
CMD ["--wait"]
