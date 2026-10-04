"""One place that prepares every Chrome launch.

Boot (create_browser), restart, the watchdog relaunch, a driver recovery and
a resume after a pause all call ``prepare_launch``. No launch setting may live
on one path only: a restart that forgot the locale or the WebRTC policy would
quietly undo what the person chose.

Settings come from the pod's environment (the platform renders them on the
Browser):

- BROWSER_LOCALE     BCP-47 tag from the offered table (locales.json)
- BROWSER_LANGUAGES  comma list for Accept-Language / navigator.languages
- BROWSER_GEOLOCATION  "off" | "fixed:<lat>,<lon>,<accuracy>"
- BROWSER_PROXY_SERVER the platform proxy; turns on the WebRTC policy
- TZ                 inherited by Chrome from the container as is

With none of them set, the launch is exactly what it was before.
"""
import json
import logging
import os
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

LOCALES_PATHS = (
    Path("/etc/livellm/locales.json"),
    Path(__file__).resolve().parent.parent / "locales.json",
)

# Written into the profile when the platform manages a setting, so clearing
# the setting later removes exactly what we wrote and nothing the person set.
MARKER_NAME = ".livellm-locale"

WEBRTC_POLICY = "disable_non_proxied_udp"

BASE_ARGS = (
    "--start-maximized",
    "--ignore-gpu-blocklist",
    "--enable-webgl",
    "--enable-gpu",
)

_locales_cache: Optional[dict] = None


def load_locales() -> dict:
    global _locales_cache
    if _locales_cache is None:
        for p in LOCALES_PATHS:
            try:
                with open(p, encoding="utf-8") as f:
                    _locales_cache = json.load(f)
                break
            except (OSError, ValueError):
                continue
        else:
            _locales_cache = {}
    return _locales_cache


class LaunchSettings:
    """The env-driven launch settings, parsed once per launch."""

    def __init__(self, locale=None, languages=None, geolocation=None, platform_proxy=None):
        self.locale: Optional[str] = locale
        self.languages: list = list(languages or [])
        # None | "off" | {"latitude", "longitude", "accuracy"}
        self.geolocation = geolocation
        self.platform_proxy: Optional[str] = platform_proxy

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LaunchSettings":
        env = os.environ if env is None else env
        locale = (env.get("BROWSER_LOCALE") or "").strip() or None
        languages = [x.strip() for x in (env.get("BROWSER_LANGUAGES") or "").split(",") if x.strip()]
        if locale and not languages:
            languages = [locale]
        if not locale:
            languages = []
        geo = None
        raw = (env.get("BROWSER_GEOLOCATION") or "").strip()
        if raw == "off":
            geo = "off"
        elif raw.startswith("fixed:"):
            try:
                lat, lon, acc = (float(x) for x in raw[len("fixed:"):].split(","))
                if -90 <= lat <= 90 and -180 <= lon <= 180 and acc > 0:
                    geo = {"latitude": lat, "longitude": lon, "accuracy": acc}
            except ValueError:
                pass
            if geo is None:
                logger.warning("BROWSER_GEOLOCATION is not fixed:<lat>,<lon>,<accuracy>; ignoring")
        proxy = (env.get("BROWSER_PROXY_SERVER") or "").strip() or None
        return cls(locale, languages, geo, proxy)

    def chrome_env(self) -> Optional[dict]:
        """Chrome's environment, or None to inherit the container's untouched.

        Playwright's ``env=`` REPLACES the environment, so the container's
        (DISPLAY, HOME, TZ…) is always the base.
        """
        if not self.locale:
            return None
        row = load_locales().get(self.locale) or {}
        glibc = row.get("glibc") or self.locale.replace("-", "_")
        ui = (row.get("chromeUi") or self.locale).replace("-", "_")
        base = self.locale.split("-")[0]
        language = ui if ui == base else f"{ui}:{base}"
        return {**os.environ, "LANG": f"{glibc}.UTF-8", "LANGUAGE": language}


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _delete(d: dict, dotted: str) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


def apply_profile_prefs(profile_path: Path, s: LaunchSettings) -> bool:
    """Write the managed prefs into Default/Preferences while Chrome is stopped.

    The marker records which prefs the platform wrote; a setting that is no
    longer set removes exactly its own keys. A profile the platform never
    managed keeps its own settings. Returns True when anything changed.
    """
    default_dir = profile_path / "Default"
    prefs_path = default_dir / "Preferences"
    marker_path = default_dir / MARKER_NAME
    marker = _read_json(marker_path)

    want: dict = {}
    if s.locale:
        langs = ",".join(s.languages or [s.locale])
        want["intl.accept_languages"] = langs
        want["intl.selected_languages"] = langs
    if s.geolocation == "off":
        want["profile.default_content_setting_values.geolocation"] = 2
    if s.platform_proxy:
        want["webrtc.ip_handling_policy"] = WEBRTC_POLICY

    owned = set(marker.get("keys") or [])
    drop = owned - set(want)
    if not want and not drop and not marker_path.exists():
        return False

    prefs = _read_json(prefs_path)
    changed = False
    for key in sorted(drop):
        if _get(prefs, key) is not None:
            _delete(prefs, key)
            changed = True
    for key, value in want.items():
        if _get(prefs, key) != value:
            _set(prefs, key, value)
            changed = True

    if changed:
        default_dir.mkdir(parents=True, exist_ok=True)
        tmp = prefs_path.with_name("Preferences.livellm-tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False)
        os.replace(tmp, prefs_path)

    if want:
        new_marker = {"keys": sorted(want), "locale": s.locale}
        if new_marker != marker:
            default_dir.mkdir(parents=True, exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(new_marker, f)
    elif marker_path.exists():
        marker_path.unlink()
        changed = True
    return changed


def prepare_launch(
    chrome_port: int,
    profile_path: Optional[Path],
    proxy_config: Optional[dict],
    settings: Optional[LaunchSettings] = None,
) -> dict:
    """Build the launch kwargs for one Chrome start (and write its prefs).

    ``profile_path`` set = a persistent context (prefs are written into it);
    None = an ephemeral browser (no prefs to write).
    """
    s = settings if settings is not None else LaunchSettings.from_env()
    args = list(BASE_ARGS) + [
        f"--remote-debugging-port={chrome_port}",
        "--remote-allow-origins=*",
    ]
    if s.platform_proxy:
        # Not protection on its own (the pref below is): kept as a second say.
        args.append(f"--force-webrtc-ip-handling-policy={WEBRTC_POLICY}")
    kwargs: dict = {"headless": False, "channel": "chrome", "args": args}
    if proxy_config:
        kwargs["proxy"] = proxy_config
    env = s.chrome_env()
    if env is not None:
        kwargs["env"] = env
    if isinstance(s.geolocation, dict):
        kwargs["geolocation"] = dict(s.geolocation)
        kwargs["permissions"] = ["geolocation"]
    if profile_path is not None:
        try:
            apply_profile_prefs(profile_path, s)
        except OSError as e:
            logger.warning(f"Could not write the profile's managed prefs: {e}")
        kwargs["user_data_dir"] = str(profile_path)
        kwargs["no_viewport"] = True
    return kwargs


def platform_proxy_config() -> Optional[dict]:
    """The proxy the platform set on the pod, if any.

    With a platform proxy, bypass entries are ignored: every bypassed host
    would go out directly, around the proxy the person chose.
    """
    server = (os.environ.get("BROWSER_PROXY_SERVER") or "").strip()
    if not server:
        return None
    cfg = {"server": server}
    if os.environ.get("BROWSER_PROXY_USERNAME"):
        cfg["username"] = os.environ["BROWSER_PROXY_USERNAME"]
    if os.environ.get("BROWSER_PROXY_PASSWORD"):
        cfg["password"] = os.environ["BROWSER_PROXY_PASSWORD"]
    return cfg
