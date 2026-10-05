"""What must hold before Camoufox opens the profile, and the processes it
leaves behind.

- A disk that holds a Chrome profile is never opened (and never touched):
  the launcher refuses to start the browser and says so on /health.
- Firefox's locks (lock, .parentlock) are removed when no Camoufox runs.
- compatibility.ini records the Camoufox that last opened the profile.
  Opened by a newer build of the same major (a rollback), Camoufox starts
  with -allow-downgrade; by a newer major, it does not start, unless the
  keeper imported that profile on purpose (it leaves .livellm-allow-downgrade,
  used once).
- Playwright starts Firefox in a process group of its own, so killing the
  server does not kill Firefox: every Camoufox process is found in /proc and
  killed before a relaunch.
"""
import logging
import os
import signal
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DOWNGRADE_MARKER = ".livellm-allow-downgrade"
ALLOW_DOWNGRADE_ARG = "-allow-downgrade"


def chrome_profile(profile_dir: Path) -> bool:
    p = Path(profile_dir)
    return (p / "Default" / "Preferences").exists() or (p / "Local State").exists()


# ── versions ──

def parse_version(v: str) -> Tuple[List[int], int]:
    """"156.0.1-beta.34" -> ([156, 0, 1], 34)."""
    main, _, build = (v or "").strip().partition("-")
    nums = []
    for part in main.split("."):
        nums.append(int(part) if part.isdigit() else 0)
    tail = build.replace("-", ".").rsplit(".", 1)[-1] if build else ""
    return nums, (int(tail) if tail.isdigit() else 0)


def compare(a: Tuple[str, str], b: Tuple[str, str]) -> int:
    """Compare (version, buildID) pairs: -1, 0 or 1."""
    an, ab = parse_version(a[0])
    bn, bb = parse_version(b[0])
    width = max(len(an), len(bn))
    an += [0] * (width - len(an))
    bn += [0] * (width - len(bn))
    for x, y in ((an, bn), (ab, bb)):
        if x != y:
            return 1 if x > y else -1
    ai = int(a[1]) if (a[1] or "").isdigit() else 0
    bi = int(b[1]) if (b[1] or "").isdigit() else 0
    return (ai > bi) - (ai < bi)


def last_opened_by(profile_dir: Path) -> Optional[Tuple[str, str]]:
    """(version, buildID) from compatibility.ini's LastVersion
    ("156.0.1-beta.34_20261003194815/20261003194815"), or None."""
    try:
        text = (Path(profile_dir) / "compatibility.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("LastVersion="):
            value = line.split("=", 1)[1].strip()
            head, _, build = value.partition("/")
            version, _, build2 = head.rpartition("_")
            if not version:
                return None
            return version, (build or build2)
    return None


class ProfileNewer(Exception):
    """The profile was last opened by a newer Camoufox major."""

    def __init__(self, profile_version: str, own_version: str):
        super().__init__(f"This profile was last opened by a newer Camoufox ({profile_version}; this browser runs {own_version}).")
        self.profile_version = profile_version
        self.own_version = own_version


def downgrade_args(profile_dir: Path, own: Tuple[str, str]) -> List[str]:
    """The extra arguments this profile needs, or ProfileNewer.

    The keeper's marker is used once: removed here whatever it decided."""
    marker = Path(profile_dir) / DOWNGRADE_MARKER
    forced = marker.exists() or marker.is_symlink()
    if forced:
        try:
            marker.unlink()
        except OSError:
            pass
    last = last_opened_by(profile_dir)
    if not last or not own[0] or compare(last, own) <= 0:
        return []
    last_major = parse_version(last[0])[0][:1]
    own_major = parse_version(own[0])[0][:1]
    if last_major == own_major:
        logger.warning(f"The profile was last opened by Camoufox {last[0]} ({last[1]}); starting {own[0]} with {ALLOW_DOWNGRADE_ARG}")
        return [ALLOW_DOWNGRADE_ARG]
    if forced:
        logger.warning(f"The profile is from Camoufox {last[0]}; imported on purpose, starting {own[0]} with {ALLOW_DOWNGRADE_ARG}")
        return [ALLOW_DOWNGRADE_ARG]
    raise ProfileNewer(last[0], own[0])


# ── processes ──

def camoufox_pids(camoufox_dir: Path, proc_root: Path = Path("/proc"), parents_only: bool = False) -> List[int]:
    """Every running process of the Camoufox at camoufox_dir (its argv[0]),
    or only the browser processes (not -contentproc children)."""
    prefix = str(camoufox_dir).rstrip("/") + "/"
    out = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
            with open(os.path.join(proc_root, name, "stat"), "rb") as f:
                state = f.read().rsplit(b")", 1)[-1].split()[0:1]
        except OSError:
            continue
        if state == [b"Z"]:
            continue
        if not argv or not argv[0].decode(errors="replace").startswith(prefix):
            continue
        if parents_only and any(a == b"-contentproc" for a in argv):
            continue
        out.append(int(name))
    return out


def pid_alive(pid: Optional[int], proc_root: Path = Path("/proc")) -> bool:
    if not pid:
        return False
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as f:
            state = f.read().rsplit(b")", 1)[-1].split()[0:1]
    except OSError:
        return False
    return state != [b"Z"]


def kill_all(camoufox_dir: Path, proc_root: Path = Path("/proc"), timeout: float = 5.0, kill=os.kill) -> int:
    """SIGKILL every Camoufox process (a stopped one too) and wait for them."""
    pids = camoufox_pids(camoufox_dir, proc_root)
    for pid in pids:
        try:
            kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + timeout
    while pids and time.monotonic() < deadline:
        pids = [p for p in pids if pid_alive(p, proc_root)]
        if pids:
            time.sleep(0.1)
    if pids:
        logger.warning(f"Camoufox processes still there after SIGKILL: {pids}")
    return len(pids)


def remove_locks(profile_dir: Path) -> None:
    """Firefox's profile locks, left by a killed browser (call with none running)."""
    for name in ("lock", ".parentlock"):
        p = Path(profile_dir) / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
                logger.info(f"Removed a stale {name}")
        except OSError as e:
            logger.warning(f"Could not remove {name}: {e}")
