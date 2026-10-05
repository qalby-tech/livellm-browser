"""One place that builds every Camoufox launch (boot, restart, the
watchdog's relaunch, a resume after a pause): no setting may live on one
path only.

camoufox.launch_options() resolves the identity, fonts, voices, WebGL and
prefs into an executable, env (CAMOU_CONFIG_* / CAMOU_PREFS_*), args and
prefs; ``launch_server_options`` turns that into Playwright's
firefox.launchServer() options (camelCase; launchServer silently drops any
key it does not know) and adds what a served, persistent, headful browser
needs.
"""
import copy
import secrets
from pathlib import Path
from typing import Callable, Optional

from core import const
from core.settings import Settings

# Playwright's own context defaults, switched off: a persistent context gets
# 1280x720 under the 1920x1080 window without noDefaultViewport (and the
# second new_page() can hang, daijro/camoufox#666), and forced light colors
# without the no-override values.
CONTEXT_DEFAULTS_OFF = {
    "noDefaultViewport": True,
    "colorScheme": "no-override",
    "reducedMotion": "no-override",
    "forcedColors": "no-override",
    "contrast": "no-override",
}


def camoufox_kwargs(settings: Settings, identity: Optional[dict], prefs: dict, screen, ubo_default) -> dict:
    """The keyword arguments of camoufox.launch_options() for one launch.

    ``identity`` None = the identity's first resolution (its navigator values
    not pinned yet). ``screen`` is a camoufox Screen bounding the display;
    ``ubo_default`` is camoufox.DefaultAddons.UBO (left out: the pinned copy
    baked into the image is passed by path instead).
    """
    config = {
        # Camoufox's own form ("ru-RU, ru, en-US, en"). locale= below sets
        # only locale:language/region/script, so it leaves this alone.
        "locale:all": settings.accept_languages(),
    }
    if settings.timezone:
        config["timezone"] = settings.timezone
    if isinstance(settings.geolocation, dict):
        config["geolocation:latitude"] = settings.geolocation["latitude"]
        config["geolocation:longitude"] = settings.geolocation["longitude"]
        config["geolocation:accuracy"] = settings.geolocation["accuracy"]
    kwargs = {
        "os": "linux",
        "executable_path": str(const.EXECUTABLE),
        "headless": False,
        "locale": settings.locale,
        "screen": screen,
        "window": const.DISPLAY_SIZE,
        "firefox_user_prefs": dict(prefs),
        "addons": [str(const.UBO_DIR)],
        "exclude_addons": [ubo_default],
        "main_world_eval": True,
        "humanize": False,
        "enable_cache": False,
        "i_know_what_im_doing": True,
        # No proxy here: Playwright's own proxy option is added after (the
        # library would warn and reach for geoip).
    }
    if identity is not None:
        config.update(identity.get("pinned") or {})
        kwargs["fingerprint"] = copy.deepcopy(identity["fingerprint"])
    kwargs["config"] = config
    return kwargs


def new_ws_path() -> str:
    """The internal server's path: 128 random bits. With the ephemeral port
    it keeps a page inside the pod (which Playwright's loopback Origin check
    would admit from localhost) from finding the server."""
    return "/" + secrets.token_hex(16)


def launch_server_options(lib_opts: dict, settings: Settings, user_data_dir: Path, extra_args=(),
                          ws_path: Optional[str] = None, to_camel: Callable[[dict], dict] = None) -> dict:
    """firefox.launchServer() options from a launch_options() result."""
    if to_camel is None:
        from camoufox.server import to_camel_case_dict as to_camel
    out = to_camel(copy.deepcopy(lib_opts))
    env = dict(out.get("env") or {})
    env.update(settings.locale_env())
    # Every value a string (Playwright's env option).
    out["env"] = {str(k): str(v) for k, v in env.items() if v is not None}
    out["args"] = list(out.get("args") or []) + list(extra_args)
    out.pop("proxy", None)
    out.update(CONTEXT_DEFAULTS_OFF)
    if settings.proxy:
        out["proxy"] = dict(settings.proxy)
    out.update({
        "_userDataDir": str(user_data_dir),
        "_sharedBrowser": True,
        "host": "127.0.0.1",
        "port": 0,
        "wsPath": ws_path or new_ws_path(),
        "timeout": 120000,
    })
    return out
