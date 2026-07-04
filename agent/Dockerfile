FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# No apt ffmpeg needed for run recordings: browser-use's VideoRecorderService
# encodes via imageio, whose imageio-ffmpeg dep ships its own ffmpeg binary.

# Copy the project into the image
COPY . /app

# Disable development dependencies
ENV UV_NO_DEV=1

WORKDIR /app
# Sync the project into a new environment, asserting the lockfile is up to date.
RUN uv sync
# The agent connects OUT over CDP to a Browser pod; it does not launch its own
# Chromium, so no browser binary is installed here (patchright is the CDP client).

CMD ["uv", "run", "main.py"]
