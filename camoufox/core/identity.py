"""The browser's identity: one Camoufox fingerprint, drawn at its first
launch and kept in the profile (livellm-identity.json), so it travels with
snapshots, exports, imports and copies.

Every launch passes the saved fingerprint (a stable identity_salt: the same
fonts, voices, WebGL and audio seed) and the three navigator values
Camoufox resolved at the first launch, after its own fixes to them
(hardwareConcurrency, platform, oscpu). Passing any navigator.* key turns
those fixes off, which is why the values are captured after they ran; and
pinned, the identity does not change when the pod lands on a node with
another core count. A Firefox major bump changes the user agent and with it
the per-identity draws: announced with such an image.
"""
import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "livellm-identity.json"
FORMAT = 1
PINNED_KEYS = ("navigator.hardwareConcurrency", "navigator.platform", "navigator.oscpu")


def path_of(profile_dir: Path) -> Path:
    return Path(profile_dir) / FILE_NAME


def load(profile_dir: Path) -> Optional[dict]:
    p = path_of(profile_dir)
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 4 << 20:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("fingerprint"), dict):
        return None
    if not isinstance(data.get("pinned"), dict):
        data["pinned"] = {}
    return data


def save(profile_dir: Path, identity: dict) -> None:
    p = path_of(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(FILE_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(identity, f, ensure_ascii=False, sort_keys=True, default=str)
    os.replace(tmp, p)


def config_of(launch_opts: dict) -> dict:
    """The Camoufox config a launch_options() result carries (CAMOU_CONFIG_n)."""
    env = launch_opts.get("env") or {}
    chunks = sorted(
        (int(k.rsplit("_", 1)[1]), v) for k, v in env.items()
        if k.startswith("CAMOU_CONFIG_") and k.rsplit("_", 1)[1].isdigit()
    )
    if not chunks:
        return {}
    return json.loads("".join(v for _, v in chunks))


def create(generate: Callable[[], dict], resolve: Callable[[dict], dict], locale: str, timezone: str,
           created_with: str) -> dict:
    """A new identity: ``generate()`` draws the fingerprint, ``resolve(fp)``
    runs launch_options with it once and returns its result, whose config
    holds the navigator values Camoufox settled on."""
    # Through JSON first: the identity every later launch loads is this one,
    # value for value (its salt is a hash of it).
    fp = json.loads(json.dumps(generate(), default=str))
    config = config_of(resolve(copy.deepcopy(fp)))
    pinned = {k: config[k] for k in PINNED_KEYS if k in config}
    return {
        "format": FORMAT,
        "engine": "camoufox",
        "fingerprint": fp,
        "pinned": pinned,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "createdWith": created_with,
        "locale": locale,
        "timezone": timezone,
    }
