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
    libgl1 \
    libgl1-mesa-dri \
    libegl1 \
    libglx-mesa0 \
    mesa-utils \
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

# Copy the custom startup script
COPY startup.sh /usr/local/bin/custom-startup.sh
RUN chmod +x /usr/local/bin/custom-startup.sh

# Switch back to headless user
USER "${HEADLESS_USER_ID}"

# Use custom entrypoint
ENTRYPOINT ["/usr/local/bin/custom-startup.sh"]
CMD ["--wait"]
