"""Paths and names of the Camoufox browser image (one browser per pod)."""
import os
import re
from importlib import metadata
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = Path(os.environ.get("LIVELLM_PROFILES_DIR") or "/home/headless/Desktop/app/profiles")
DEFAULT_BROWSER_ID = "default"
# The Firefox profile itself (the keeper snapshots, exports and imports it).
PROFILE_DIR = PROFILES_DIR / DEFAULT_BROWSER_ID

# The browser's public automation address: ws://<pod>:<AUTOMATION_PORT>/playwright/default
STABLE_WS_PATH = "/playwright/" + DEFAULT_BROWSER_ID
DEFAULT_AUTOMATION_PORT = 9222

EXECUTABLE = Path(os.environ.get("CAMOUFOX_EXECUTABLE_PATH") or "/opt/camoufox/camoufox-bin")
CAMOUFOX_DIR = EXECUTABLE.parent
UBO_DIR = Path(os.environ.get("LIVELLM_UBO_DIR") or "/opt/camoufox-addons/ubo")

# The window: the desktop's whole display (startup.sh runs Xvnc at 1920x1080).
DISPLAY_SIZE = (1920, 1080)
# The frame xfwm4 (the desktop's window manager, its Default theme) draws
# around the browser window: 5 px left and right, a 29 px title bar and 5 px
# below. Camoufox sizes the window's inside to the identity's window size and
# pages read outerWidth/outerHeight WITH the frame, so the identity's window
# is drawn this much smaller than its screen's available area.
WINDOW_FRAME = (10, 34)


def automation_port() -> int:
    try:
        return int(os.environ.get("AUTOMATION_PORT") or DEFAULT_AUTOMATION_PORT)
    except ValueError:
        return DEFAULT_AUTOMATION_PORT


def _project_version() -> str:
    try:
        text = (APP_DIR / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r'^version = "([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


# The image's own version, as its tag names it (camoufox-<v>).
IMAGE_VERSION = "camoufox-" + _project_version()


def _dist_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


# The Playwright this browser's server speaks: clients must run the same minor.
PLAYWRIGHT_VERSION = _dist_version("playwright")
CAMOUFOX_LIB_VERSION = _dist_version("camoufox")


def browser_version(camoufox_dir: Path = None) -> tuple:
    """(Version, BuildID) of the baked Camoufox, from its application.ini:
    ("156.0.1-beta.34", "20261003194815"); ("", "") when it can't be read."""
    ini = (camoufox_dir or CAMOUFOX_DIR) / "application.ini"
    version = build = ""
    try:
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version=") and not version:
                version = line.split("=", 1)[1].strip()
            elif line.startswith("BuildID=") and not build:
                build = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return version, build


def major_of(version: str):
    head = (version or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None
