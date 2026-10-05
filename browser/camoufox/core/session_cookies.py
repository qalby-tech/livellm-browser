"""Session cookies survive a browser restart (as in the Chrome image 2.3.1).

Firefox drops every cookie without an expiry when it starts again, and a
platform browser restarts often (the host's nightly boot, a settings change,
the watchdog, a pause for a profile copy). Sites keep sign-ins and anti-bot
clearances in session cookies, so the default context's session cookies are
written to the profile (.livellm-session-cookies.json) once a minute and
before every deliberate close, and added back right after the next launch,
before any client can connect. A cookie the browser already has is left as
it is. The file lives in the profile, so snapshots, exports and imports
carry it.
"""
import json
import os
from pathlib import Path
from typing import Iterable

FILE_NAME = ".livellm-session-cookies.json"
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
MAX_COOKIES = 3000
MAX_FILE_BYTES = 8 << 20


def file_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / FILE_NAME


def is_session(cookie: dict) -> bool:
    exp = cookie.get("expires")
    return exp is None or (isinstance(exp, (int, float)) and exp < 0)


def session_only(cookies: Iterable[dict]) -> list:
    out = []
    for c in cookies:
        if not isinstance(c, dict) or not is_session(c) or not c.get("name") or not c.get("domain"):
            continue
        row = {k: c[k] for k in COOKIE_KEYS if k in c and c[k] is not None}
        row["expires"] = -1
        out.append(row)
        if len(out) >= MAX_COOKIES:
            break
    return out


def write(profile_dir: Path, cookies: list) -> bool:
    path = file_path(profile_dir)
    if not cookies:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
    data = json.dumps({"version": 1, "cookies": cookies}, ensure_ascii=False, sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == data:
            return False
    except (OSError, ValueError):
        pass
    tmp = path.with_name(FILE_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def read(profile_dir: Path) -> list:
    path = file_path(profile_dir)
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return session_only((data.get("cookies") if isinstance(data, dict) else None) or [])
