# kamasalyamov/livellm-browser:camoufox-<v> (<v> = camoufox/pyproject.toml).
#
# The Camoufox engine: Camoufox (a Firefox build) on the same desktop as the
# Chrome image (TigerVNC :1 1920x1080, xfce, noVNC on 6901, browser/startup.sh),
# served to Playwright clients by its own Playwright server and the launcher
# API on :9000. Built from the repo root (it shares browser/keeper,
# browser/startup.sh and browser/locales.json); Dockerfile.dockerignore keeps
# the context to exactly those and camoufox/.
#
# Nothing is fetched at runtime: the browser, its fingerprint model and
# uBlock Origin are pinned by sha256 and baked in here.

# The control sidecar (keeper): the same Go source as the Chrome image's.
FROM golang:1.25.1-bookworm AS keeper
WORKDIR /src/keeper
COPY browser/keeper/go.mod browser/keeper/go.sum ./
RUN go mod download
COPY browser/keeper/ ./
RUN CGO_ENABLED=0 GOFLAGS=-trimpath go build -ldflags="-s -w" -o /out/livellm-keeper .

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ENV DEBIAN_FRONTEND=noninteractive

ENV HEADLESS_USER_ID=1000 \
    HEADLESS_USER_GROUP_ID=1000 \
    USER=headless \
    HOME=/home/headless \
    DISPLAY=:1 \
    RESOLUTION=1920x1080 \
    VNC_PORT=5901 \
    NOVNC_PORT=6901 \
    PYTHONUNBUFFERED=1

# The desktop, as in the Chrome image. Firefox itself never sees these
# fonts: Camoufox renders only with its own bundle (FONTCONFIG_FILE).
# apt package versions are not pinned (as in the Chrome image).
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
    curl \
    wget \
    ca-certificates \
    tzdata \
    unzip \
    libgl1 \
    libgl1-mesa-dri \
    libegl1 \
    libglx-mesa0 \
    mesa-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uid 1000 = headless (Ubuntu 24.04 ships "ubuntu" at 1000).
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

COPY --from=ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a /uv /uvx /bin/

USER 0

# Python 3.12 (uv-managed, readable by every user) and the locked
# dependencies (hashed uv.lock). At runtime uv only runs what is here.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_NO_SYNC=1 \
    UV_OFFLINE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1
WORKDIR ${HOME}/Desktop/app
COPY camoufox/pyproject.toml camoufox/uv.lock camoufox/.python-version ./
RUN UV_OFFLINE=0 UV_PYTHON_DOWNLOADS=automatic uv python install 3.12 && \
    UV_OFFLINE=0 UV_NO_SYNC=0 uv sync --frozen --no-dev && \
    rm -rf /tmp/uv-cache

# Firefox's system libraries (the versions apt has today; not pinned).
RUN apt-get update && \
    .venv/bin/python -m playwright install-deps firefox && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# The browser: one exact release, checked against our own pinned sha256, in
# ONE layer, so the font groups removed below never sit in a lower one.
# Camoufox's bundle stores each face once, in a directory named for the OSes
# that read it (fonts/groups.json); a Linux identity reads only L, LM, LW and
# LMW, so M, W and MW (1.24 GB of macOS/Windows faces) are never unpacked
# (unzip also exits 1 on their non-UTF-8 local names, which set -e refuses).
ARG CAMOUFOX_TAG=v156.0.1-beta.34
ARG CAMOUFOX_ZIP=camoufox-156.0.1-beta.34-lin.x86_64.zip
ARG CAMOUFOX_SHA256=09effb44efec6f0617b6940b787054b4b8773f28d6573bcbabefe3163e75d0cc
ARG FIREFOX_MAJOR=156
RUN set -eu; \
    curl -fsSL --retry 5 -o /tmp/camoufox.zip "https://github.com/daijro/camoufox/releases/download/${CAMOUFOX_TAG}/${CAMOUFOX_ZIP}"; \
    echo "${CAMOUFOX_SHA256}  /tmp/camoufox.zip" | sha256sum -c -; \
    mkdir -p /opt/camoufox; \
    unzip -q /tmp/camoufox.zip -x 'fonts/M/*' 'fonts/W/*' 'fonts/MW/*' -d /opt/camoufox; \
    rm -f /tmp/camoufox.zip; \
    .venv/bin/python -c "import json,sys; lin=set(json.load(open('/opt/camoufox/fonts/groups.json'))['readBy']['lin']); print('Linux reads font groups', sorted(lin)); sys.exit(0 if lin <= {'L','LM','LW','LMW'} else 1)"; \
    test ! -e /opt/camoufox/fonts/M && test ! -e /opt/camoufox/fonts/W && test ! -e /opt/camoufox/fonts/MW; \
    v="$(sed -n 's/^Version=//p' /opt/camoufox/application.ini)"; \
    echo "Camoufox $v (Firefox major expected ${FIREFOX_MAJOR})"; \
    test "${v%%.*}" = "${FIREFOX_MAJOR}"; \
    test "v$v" = "${CAMOUFOX_TAG}"; \
    chmod -R a+rX /opt/camoufox; \
    du -sh /opt/camoufox
ENV CAMOUFOX_EXECUTABLE_PATH=/opt/camoufox/camoufox-bin

# uBlock Origin, pinned (Camoufox's default add-on, which the library would
# otherwise fetch from addons.mozilla.org at every launch). Passed by path.
ARG UBO_VERSION=1.75.0
ARG UBO_SHA256=5b74415860456370644bd80f16125e865b0e6c356bb5dfcfb84069967eaa5287
RUN set -eu; \
    curl -fsSL --retry 5 -o /tmp/ubo.xpi "https://github.com/gorhill/uBlock/releases/download/${UBO_VERSION}/uBlock0_${UBO_VERSION}.firefox.signed.xpi"; \
    echo "${UBO_SHA256}  /tmp/ubo.xpi" | sha256sum -c -; \
    mkdir -p /opt/camoufox-addons/ubo; \
    unzip -q /tmp/ubo.xpi -d /opt/camoufox-addons/ubo; \
    rm -f /tmp/ubo.xpi; \
    test -f /opt/camoufox-addons/ubo/manifest.json; \
    chmod -R a+rX /opt/camoufox-addons

# The fingerprint model, installed from the library's own pinned release and
# checked against its sha256; at runtime it is only read.
RUN UV_OFFLINE=0 .venv/bin/python -c "from camoufox.fpgen_model import ensure_fpgen_model, is_pinned; ensure_fpgen_model(); assert is_pinned()"

# The offered locales (the same table as the Chrome image).
COPY browser/locales.json /etc/livellm/locales.json

COPY --from=keeper /out/livellm-keeper /usr/local/bin/livellm-keeper

COPY camoufox/ ./

# The profile volume's mount point, ~/.camoufox (Firefox probes it) and the
# library's runtime cache (it writes a fontconfig file there at every launch)
# belong to the browser's user.
RUN chmod 666 /etc/passwd /etc/group && \
    mkdir -p "${HOME}/Desktop/app/profiles/default" "${HOME}/.cache/camoufox" "${HOME}/.camoufox" && \
    chown -R "${HEADLESS_USER_ID}":"${HEADLESS_USER_GROUP_ID}" "${HOME}/Desktop/app/profiles" "${HOME}/.cache" "${HOME}/.camoufox" && \
    chown "${HEADLESS_USER_ID}":"${HEADLESS_USER_GROUP_ID}" "${HOME}" "${HOME}/Desktop" "${HOME}/Desktop/app"

COPY browser/startup.sh /usr/local/bin/custom-startup.sh
RUN sed -i 's/\r$//' /usr/local/bin/custom-startup.sh && \
    chmod +x /usr/local/bin/custom-startup.sh

RUN printf '%s' '<!doctype html><meta charset="utf-8"><title>noVNC</title><meta http-equiv="refresh" content="0; url=vnc.html?autoconnect=true&resize=scale">' > /usr/share/novnc/index.html

# The image's own checks: the launcher imports, serve.js parses, and the
# options it builds reach the browser it names.
RUN .venv/bin/python -c "import launch" && \
    node_bin="$(.venv/bin/python -c 'from playwright._impl._driver import compute_driver_executable as c; n=c()[0]; print(n[0] if isinstance(n, tuple) else n)')" && \
    "$node_bin" --check serve.js

USER "${HEADLESS_USER_ID}"

ENTRYPOINT ["/usr/local/bin/custom-startup.sh"]
CMD ["--wait"]
