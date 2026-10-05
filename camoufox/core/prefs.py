"""The Firefox prefs the platform manages, and their clean removal.

A pref Firefox was once given stays in the profile's prefs.js after the
launch that set it (Playwright also rewrites user.js at every launch, but
prefs.js keeps what Firefox saved). So before every launch, with Firefox not
running, every pref the PREVIOUS launch passed (recorded in
.livellm-managed-prefs.json; a fixed list before the first) is removed from
prefs.js, and only what the current settings need is passed again. A
setting cleared on the Browser really goes back to the browser's default.
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from core.settings import Settings

logger = logging.getLogger(__name__)

RECORD_NAME = ".livellm-managed-prefs.json"
QUOTA_PREF = "dom.quotaManager.temporaryStorage.fixedLimit"
INT32_MAX = 2**31 - 1

GEO_PREF = "permissions.default.geo"
GEO_DENY, GEO_ALLOW = 2, 1


def proxied_prefs(proxy: dict) -> dict:
    """While the browser is proxied: the proxy itself from the first moment
    of startup (Playwright's own proxy setting reaches Firefox only once it
    is connected), and nothing that would go around it: no DNS of its own,
    no prefetch or speculative connections, no HTTP/3 (UDP), no WebRTC
    candidate that is not the proxy's."""
    u = urlsplit(proxy["server"] if "://" in proxy["server"] else "http://" + proxy["server"])
    host = u.hostname or "127.0.0.1"
    scheme = (u.scheme or "http").lower()
    prefs = {
        "network.proxy.type": 1,
        "network.proxy.no_proxies_on": "",
        # Defence in depth: pod loopback goes to the proxy too (which refuses it).
        "network.proxy.allow_hijacking_localhost": True,
        "network.proxy.failover_direct": False,
        "network.trr.mode": 5,
        "network.dns.disablePrefetch": True,
        "network.predictor.enabled": False,
        "network.prefetch-next": False,
        "network.http.speculative-parallel-limit": 0,
        "network.http.http3.enable": False,
        "media.peerconnection.ice.proxy_only_if_behind_proxy": True,
        "media.peerconnection.ice.default_address_only": True,
        "media.peerconnection.ice.no_host": True,
    }
    if scheme.startswith("socks"):
        prefs.update({
            "network.proxy.socks": host,
            "network.proxy.socks_port": u.port or 1080,
            "network.proxy.socks_version": 5,
            "network.proxy.socks_remote_dns": True,
        })
    else:
        port = u.port or (443 if scheme == "https" else 80)
        prefs.update({
            "network.proxy.http": host,
            "network.proxy.http_port": port,
            "network.proxy.ssl": host,
            "network.proxy.ssl_port": port,
        })
    return prefs


# Before any record exists: every name this module can manage.
_EVERY_PROXY_NAME = sorted(set(proxied_prefs({"server": "http://127.0.0.1:3128"})) | set(proxied_prefs({"server": "socks5://127.0.0.1:1080"})))
FIRST_LAUNCH_NAMES = tuple(sorted({GEO_PREF, QUOTA_PREF, *_EVERY_PROXY_NAME}))


def quota_limit_kb(profile_dir: Path, statvfs=os.statvfs) -> Optional[int]:
    """Half the profile disk's capacity in KB, as Gecko derives it for a disk
    it measures itself (pages then read min(that / 5, 10 GiB) as their quota).
    Camoufox would measure $HOME's filesystem, the node's disk; the profile's
    own volume is the one its data lands on."""
    probe = Path(profile_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = statvfs(str(probe))
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return None
    return min(total // 2 // 1024, INT32_MAX)


def managed_prefs(settings: Settings, profile_dir: Path, statvfs=os.statvfs) -> dict:
    """The prefs this launch passes for the browser's settings."""
    prefs: dict = {}
    if settings.geolocation == "off":
        prefs[GEO_PREF] = GEO_DENY
    elif isinstance(settings.geolocation, dict):
        # Written explicitly, so the Permissions API says granted
        # (daijro/camoufox#769), not just the position arriving.
        prefs[GEO_PREF] = GEO_ALLOW
    kb = quota_limit_kb(profile_dir, statvfs)
    if kb:
        prefs[QUOTA_PREF] = kb
    if settings.proxy:
        prefs.update(proxied_prefs(settings.proxy))
    return prefs


_PREF_LINE = re.compile(r'^\s*user_pref\(\s*("(?:[^"\\]|\\.)*")\s*,')


def strip_prefs_js(profile_dir: Path, names: Iterable[str]) -> int:
    """Remove the user_pref lines of ``names`` from prefs.js (Firefox must
    not be running). Returns how many lines went."""
    names = set(names)
    path = Path(profile_dir) / "prefs.js"
    if not names or not path.is_file() or path.is_symlink():
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    except OSError:
        return 0
    kept, removed = [], 0
    for line in lines:
        m = _PREF_LINE.match(line)
        if m:
            try:
                name = json.loads(m.group(1))
            except ValueError:
                name = None
            if name in names:
                removed += 1
                continue
        kept.append(line)
    if removed:
        tmp = path.with_name("prefs.js.livellm-tmp")
        tmp.write_text("".join(kept), encoding="utf-8", errors="surrogateescape")
        os.replace(tmp, path)
    return removed


def previous_names(profile_dir: Path) -> list:
    try:
        data = json.loads((Path(profile_dir) / RECORD_NAME).read_text(encoding="utf-8"))
        names = data.get("names")
        if isinstance(names, list):
            return [n for n in names if isinstance(n, str)]
    except (OSError, ValueError, AttributeError):
        pass
    return list(FIRST_LAUNCH_NAMES)


def clear_previous(profile_dir: Path) -> int:
    """Before a launch: drop from prefs.js what the previous launch passed."""
    removed = strip_prefs_js(profile_dir, previous_names(profile_dir))
    if removed:
        logger.info(f"Cleared {removed} prefs the last launch had set")
    return removed


def record(profile_dir: Path, names: Iterable[str]) -> None:
    """After building a launch: the names it passes, for the next clear."""
    path = Path(profile_dir) / RECORD_NAME
    data = json.dumps({"names": sorted(set(names))})
    tmp = path.with_name(RECORD_NAME + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)
