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

The window is drawn so that it fits, with the desktop's window frame, inside
the screen the identity claims (pages read the outer size with the frame).

Camoufox counts the cores the process may run on (its CPU affinity: every
core of the node), not the container's CPU limit, so a 2-CPU browser on a
32-core node would report 32 while a page timing parallel workers measures
about 2. At creation the core count is therefore fitted to the CPU limit,
snapped into Camoufox's own table of real desktop counts (its floor is 4).
"""
import copy
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

FILE_NAME = "livellm-identity.json"
FORMAT = 1
PINNED_KEYS = ("navigator.hardwareConcurrency", "navigator.platform", "navigator.oscpu")


CORES_KEY = "navigator.hardwareConcurrency"
# camoufox.fingerprints.PLAUSIBLE_CORE_COUNTS (0.5.7), for a library without it.
CORE_COUNTS = (4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 28, 32)


def cpu_limit(cgroup_root: Path = Path("/sys/fs/cgroup")) -> Optional[float]:
    """The CPUs the container's CFS quota allows (the pod's CPU limit), or
    None without a limit. cgroup v2 cpu.max, else v1 cfs_quota/cfs_period."""
    root = Path(cgroup_root)
    try:
        quota, _, period = (root / "cpu.max").read_text().strip().partition(" ")
        if quota == "max":
            return None
        return int(quota) / int(period or 100000)
    except (OSError, ValueError, ZeroDivisionError):
        pass
    for d in (root / "cpu", root / "cpu,cpuacct", root):
        try:
            quota = int((d / "cpu.cfs_quota_us").read_text().strip())
            period = int((d / "cpu.cfs_period_us").read_text().strip())
        except (OSError, ValueError):
            continue
        return None if quota <= 0 or period <= 0 else quota / period
    return None


def fit_cores(resolved, limit: Optional[float], table=CORE_COUNTS):
    """The core count an identity pins: what Camoufox resolved (the node's
    count, snapped into ``table``), lowered to the CPU limit and snapped
    DOWN into the table again, never under its floor."""
    if not limit or not isinstance(resolved, int) or isinstance(resolved, bool) or not table:
        return resolved
    target = min(resolved, max(1, math.ceil(limit - 1e-9)))
    allowed = [c for c in table if c <= target]
    return allowed[-1] if allowed else min(table)


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


def fit_window(fp: dict, config: dict, frame: Tuple[int, int]) -> bool:
    """Shrink the fingerprint's window so that, with the window manager's
    ``frame`` around it, it still fits the screen's available area the
    resolved ``config`` reports (a page reads outerWidth/outerHeight with the
    frame: a window wider or taller than its own screen is a tell). The
    chrome between outer and inner is kept. True when anything changed."""
    win = fp.get("window")
    if not isinstance(win, dict):
        return False
    changed = False
    for axis, extra, pos_key, origin_key in (("Width", frame[0], "screenX", "screen.availLeft"),
                                             ("Height", frame[1], "screenY", "screen.availTop")):
        avail = config.get(f"screen.avail{axis}") or config.get(f"screen.{axis.lower()}")
        outer = config.get(f"window.outer{axis}")
        if not (isinstance(avail, int) and isinstance(outer, int)) or extra <= 0 or avail <= extra:
            continue
        want = avail - extra
        if outer > want:
            win[f"outer{axis}"] = want
            inner = config.get(f"window.inner{axis}")
            if isinstance(inner, int):
                win[f"inner{axis}"] = max(1, min(want, inner - (outer - want)))
            outer = want
            changed = True
        # ...and where it sits: the framed window inside the available area
        # (from the fingerprint's own value: the next resolution starts there)
        origin = config.get(origin_key) if isinstance(config.get(origin_key), int) else 0
        pos = win.get(pos_key) if isinstance(win.get(pos_key), int) else config.get(f"window.{pos_key}")
        last = origin + avail - (outer + extra)
        if isinstance(pos, int) and not (origin <= pos <= last):
            win[pos_key] = max(origin, min(pos, last))
            changed = True
    return changed


def create(generate: Callable[[], dict], resolve: Callable[[dict], dict], locale: str, timezone: str,
           created_with: str, frame: Tuple[int, int] = (0, 0)) -> dict:
    """A new identity: ``generate()`` draws the fingerprint, ``resolve(fp)``
    runs launch_options with it once and returns its result, whose config
    holds the navigator values Camoufox settled on. ``frame``: the window
    manager's frame around the window (see fit_window)."""
    # Through JSON first: the identity every later launch loads is this one,
    # value for value (its salt is a hash of it).
    fp = json.loads(json.dumps(generate(), default=str))
    config = config_of(resolve(copy.deepcopy(fp)))
    if fit_window(fp, config, frame):
        # The window changed (and with it the identity's salt): what every
        # later launch resolves is this fingerprint.
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
