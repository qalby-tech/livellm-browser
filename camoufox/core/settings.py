"""The browser's settings, from the pod's environment (the platform renders
them on the Browser; the names are the Chrome image's):

- BROWSER_LOCALE       BCP-47 tag from the offered table (locales.json); unset = en-US
- BROWSER_LANGUAGES    comma list for Accept-Language / navigator.languages; unset = en-US,en
- BROWSER_GEOLOCATION  "off" | "fixed:<lat>,<lon>,<accuracy>" | unset (the page asks)
- BROWSER_PROXY_SERVER the platform proxy (the control sidecar's relay), with the
                       optional BROWSER_PROXY_USERNAME / _PASSWORD / _BYPASS
- BROWSER_COOKIES_FILE cookies added at boot
- TZ                   the browser's time zone; unset = the container's (UTC)
"""
import json
import logging
import math
import os
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en-US"
DEFAULT_LANGUAGES = ("en-US", "en")

LOCALES_PATHS = (
    Path("/etc/livellm/locales.json"),
    Path(__file__).resolve().parent.parent.parent / "browser" / "locales.json",
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


# The control sidecar's relay.
RELAY_HOSTS = ("127.0.0.1", "localhost", "::1")
RELAY_PORT = 3128


def is_keeper_relay(server: str) -> bool:
    try:
        u = urlsplit(server if "://" in server else f"http://{server}")
        return (u.hostname or "") in RELAY_HOSTS and u.port == RELAY_PORT
    except ValueError:
        return False


def parse_geolocation(raw: str):
    """None | "off" | {"latitude", "longitude", "accuracy"}. Accepts Go's
    float formatting (1e+06, -0, …) as the operator renders it."""
    raw = (raw or "").strip()
    if raw == "off":
        return "off"
    if not raw.startswith("fixed:"):
        if raw:
            logger.warning("BROWSER_GEOLOCATION is not off or fixed:<lat>,<lon>,<accuracy>; ignoring")
        return None
    try:
        lat, lon, acc = (float(x) for x in raw[len("fixed:"):].split(","))
    except ValueError:
        lat = lon = acc = math.nan
    if all(math.isfinite(v) for v in (lat, lon, acc)) and -90 <= lat <= 90 and -180 <= lon <= 180 and acc > 0:
        return {"latitude": lat, "longitude": lon, "accuracy": acc}
    logger.warning("BROWSER_GEOLOCATION is not fixed:<lat>,<lon>,<accuracy>; ignoring")
    return None


class Settings:
    def __init__(self, locale=DEFAULT_LOCALE, languages=DEFAULT_LANGUAGES, timezone="", geolocation=None,
                 proxy=None, cookies_file=""):
        self.locale: str = locale
        self.languages: list = list(languages)
        self.timezone: str = timezone
        self.geolocation = geolocation
        # {"server", "username"?, "password"?, "bypass"?} or None
        self.proxy: Optional[dict] = proxy
        self.cookies_file: str = cookies_file

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        locale = (env.get("BROWSER_LOCALE") or "").strip()
        languages = [x.strip() for x in (env.get("BROWSER_LANGUAGES") or "").split(",") if x.strip()]
        if not locale:
            locale = DEFAULT_LOCALE
            if not languages:
                languages = list(DEFAULT_LANGUAGES)
        if not languages:
            languages = [locale]
        proxy = None
        server = (env.get("BROWSER_PROXY_SERVER") or "").strip()
        if server:
            proxy = {"server": server}
            if env.get("BROWSER_PROXY_USERNAME"):
                proxy["username"] = env["BROWSER_PROXY_USERNAME"]
            if env.get("BROWSER_PROXY_PASSWORD"):
                proxy["password"] = env["BROWSER_PROXY_PASSWORD"]
            bypass = (env.get("BROWSER_PROXY_BYPASS") or "").strip()
            # Through the sidecar's relay a bypassed host would go out
            # directly, around the proxies the person chose.
            if bypass and not is_keeper_relay(server):
                proxy["bypass"] = bypass
        return cls(
            locale=locale,
            languages=languages,
            timezone=(env.get("TZ") or "").strip(),
            geolocation=parse_geolocation(env.get("BROWSER_GEOLOCATION") or ""),
            proxy=proxy,
            cookies_file=(env.get("BROWSER_COOKIES_FILE") or "").strip(),
        )

    @property
    def proxied(self) -> bool:
        return self.proxy is not None

    def locale_env(self) -> dict:
        """LANG / LANGUAGE for the browser process, from the locale table."""
        row = load_locales().get(self.locale) or {}
        glibc = row.get("glibc") or self.locale.replace("-", "_")
        base = self.locale.split("-")[0]
        language = glibc if glibc == base else f"{glibc}:{base}"
        return {"LANG": f"{glibc}.UTF-8", "LANGUAGE": language}

    def accept_languages(self) -> str:
        """Camoufox's own form: "ru-RU, ru, en-US, en"."""
        return ", ".join(self.languages)
