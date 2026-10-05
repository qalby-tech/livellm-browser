"""The single launch-preparation function: env, prefs, flags, geolocation."""
import json
import shutil
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.launch_prep as lp
from core.launch_prep import LaunchSettings, apply_profile_prefs, prepare_launch
from core.local_browser import LocalBrowserManager, LocalBrowserInfo


def prefs_of(profile: Path) -> dict:
    return json.loads((profile / "Default" / "Preferences").read_text())


def test_no_settings_is_todays_launch(tmp_path, monkeypatch):
    for k in ("BROWSER_LOCALE", "BROWSER_LANGUAGES", "BROWSER_GEOLOCATION", "BROWSER_PROXY_SERVER"):
        monkeypatch.delenv(k, raising=False)
    kw = prepare_launch(4444, tmp_path, None)
    assert kw == {
        "headless": False,
        "channel": "chrome",
        "args": [
            "--start-maximized", "--ignore-gpu-blocklist", "--enable-webgl", "--enable-gpu",
            "--remote-debugging-port=4444", "--remote-allow-origins=*",
        ],
        "user_data_dir": str(tmp_path),
        "no_viewport": True,
    }
    # Nothing written into a profile the platform doesn't manage.
    assert not (tmp_path / "Default").exists()


def test_locale_env_merges_the_container_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setenv("TZ", "Europe/Moscow")
    s = LaunchSettings.from_env({"BROWSER_LOCALE": "ru-RU", "BROWSER_LANGUAGES": "ru-RU,ru,en-US,en"})
    kw = prepare_launch(1, tmp_path, None, s)
    env = kw["env"]
    assert env["DISPLAY"] == ":1"          # never env={...} alone
    assert env["TZ"] == "Europe/Moscow"
    assert env["LANG"] == "ru_RU.UTF-8"
    assert env["LANGUAGE"] == "ru"
    assert "LC_ALL" not in env or env.get("LC_ALL") == __import__("os").environ.get("LC_ALL")
    assert not any(a.startswith("--lang") for a in kw["args"])


def test_language_keeps_the_region_ui(tmp_path):
    s = LaunchSettings.from_env({"BROWSER_LOCALE": "pt-BR"})
    assert s.chrome_env()["LANGUAGE"] == "pt_BR:pt"
    assert s.chrome_env()["LANG"] == "pt_BR.UTF-8"
    s = LaunchSettings.from_env({"BROWSER_LOCALE": "es-MX"})
    assert s.chrome_env()["LANGUAGE"] == "es_419:es"


def test_prefs_written_and_cleared_through_the_marker(tmp_path):
    profile = tmp_path
    (profile / "Default").mkdir()
    (profile / "Default" / "Preferences").write_text(json.dumps({"keep": 1, "intl": {"other": "x"}}))

    s = LaunchSettings.from_env({"BROWSER_LOCALE": "ru-RU", "BROWSER_LANGUAGES": "ru-RU,ru,en-US,en",
                                 "BROWSER_GEOLOCATION": "off"})
    assert apply_profile_prefs(profile, s) is True
    p = prefs_of(profile)
    assert p["intl"]["accept_languages"] == "ru-RU,ru,en-US,en"
    assert p["intl"]["selected_languages"] == "ru-RU,ru,en-US,en"
    assert p["profile"]["default_content_setting_values"]["geolocation"] == 2
    assert p["keep"] == 1 and p["intl"]["other"] == "x"
    assert (profile / "Default" / lp.MARKER_NAME).exists()

    # Same settings again: nothing to rewrite.
    assert apply_profile_prefs(profile, s) is False

    # Cleared: exactly our keys go, the person's stay.
    assert apply_profile_prefs(profile, LaunchSettings.from_env({})) is True
    p = prefs_of(profile)
    assert "accept_languages" not in p["intl"] and "selected_languages" not in p["intl"]
    assert "geolocation" not in p["profile"]["default_content_setting_values"]
    assert p["keep"] == 1 and p["intl"]["other"] == "x"
    assert not (profile / "Default" / lp.MARKER_NAME).exists()


def test_unmanaged_profile_keeps_its_own_language(tmp_path):
    (tmp_path / "Default").mkdir()
    own = {"intl": {"accept_languages": "fr-FR,fr"}}
    (tmp_path / "Default" / "Preferences").write_text(json.dumps(own))
    assert apply_profile_prefs(tmp_path, LaunchSettings.from_env({})) is False
    assert prefs_of(tmp_path) == own


def test_geolocation_fixed_goes_on_the_context(tmp_path):
    s = LaunchSettings.from_env({"BROWSER_GEOLOCATION": "fixed:55.75,37.62,50"})
    kw = prepare_launch(1, tmp_path, None, s)
    assert kw["geolocation"] == {"latitude": 55.75, "longitude": 37.62, "accuracy": 50.0}
    assert kw["permissions"] == ["geolocation"]
    bad = LaunchSettings.from_env({"BROWSER_GEOLOCATION": "fixed:200,0,1"})
    assert bad.geolocation is None


def test_webrtc_policy_with_a_platform_proxy(tmp_path):
    s = LaunchSettings.from_env({"BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"})
    kw = prepare_launch(1, tmp_path, {"server": "http://127.0.0.1:3128"}, s)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in kw["args"]
    assert prefs_of(tmp_path)["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert kw["proxy"] == {"server": "http://127.0.0.1:3128"}


def test_platform_proxy_ignores_bypass_through_the_relay(monkeypatch):
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "http://127.0.0.1:3128")
    monkeypatch.setenv("BROWSER_PROXY_BYPASS", "*.example.com")
    assert lp.platform_proxy_config() == {"server": "http://127.0.0.1:3128"}
    monkeypatch.delenv("BROWSER_PROXY_SERVER")
    assert lp.platform_proxy_config() is None


@pytest.mark.parametrize("server", ["http://proxy.internal:3128", "http://127.0.0.1:8080", "socks5://10.0.0.5:1080"])
def test_any_other_proxy_keeps_its_bypass(monkeypatch, server):
    monkeypatch.setenv("BROWSER_PROXY_SERVER", server)
    monkeypatch.setenv("BROWSER_PROXY_BYPASS", "internal.svc")
    assert lp.platform_proxy_config() == {"server": server, "bypass": "internal.svc"}


def test_locales_table_shape():
    table = json.loads((Path(__file__).resolve().parents[2] / "desktop" / "locales.json").read_text())
    assert len(table) == 36
    for tag, row in table.items():
        assert re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2}|-[0-9]{3})?", tag), tag
        assert set(row) == {"chromeUi", "glibc", "font"}, tag
        assert row["glibc"].replace("_", "-") == tag, tag


def test_image_version_matches_pyproject():
    from core.const import IMAGE_VERSION
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == IMAGE_VERSION


# ── every launch path goes through prepare_launch ──

def _fake_context():
    browser = SimpleNamespace(close=AsyncMock(), is_connected=lambda: True, version="154.0.1.2")
    return SimpleNamespace(browser=browser, close=AsyncMock(), add_cookies=AsyncMock())


@pytest.fixture
def launch_env(monkeypatch):
    calls = []

    async def launch_persistent_context(**kw):
        calls.append(kw)
        return _fake_context()

    m = LocalBrowserManager()
    m.playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch_persistent_context))
    monkeypatch.setattr("core.local_browser.wait_for_chrome_cdp", AsyncMock(return_value="/devtools/browser/x"))

    class FakeProxy:
        def __init__(self, **kw):
            self.retarget = MagicMock()

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr("core.local_browser.CDPProxy", FakeProxy)
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", Path("/nonexistent"))
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "http://127.0.0.1:3128")
    return m, calls


async def test_webrtc_pref_on_every_launch_path(tmp_path, launch_env, monkeypatch):
    m, calls = launch_env
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", tmp_path)
    await m.create_browser(profile_uid="default")
    profile = tmp_path / "default"

    def reset():
        p = prefs_of(profile)
        p["webrtc"] = {}
        (profile / "Default" / "Preferences").write_text(json.dumps(p))

    assert prefs_of(profile)["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    # restart
    reset()
    await m.restart_browser("default")
    assert prefs_of(profile)["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    # pause + resume (e.g. after a restored profile)
    await m.pause_browser("default", 30)
    reset()
    await m.resume_browser("default")
    assert prefs_of(profile)["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert all("--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in c["args"] for c in calls)
    assert len(calls) == 3
    # the request proxy never replaces the platform one
    assert all(c["proxy"] == {"server": "http://127.0.0.1:3128"} for c in calls)


async def test_create_ignores_a_requested_proxy_under_a_platform_proxy(tmp_path, launch_env, monkeypatch):
    m, calls = launch_env
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", tmp_path)
    req = SimpleNamespace(server="http://evil:1", username=None, password=None, bypass="*")
    _, info = await m.create_browser(profile_uid="x", proxy=req)
    assert calls[0]["proxy"] == {"server": "http://127.0.0.1:3128"}
    assert info.proxy_config == {"server": "http://127.0.0.1:3128"}


async def test_pause_is_idempotent_and_blocks_restart(tmp_path, launch_env, monkeypatch):
    m, calls = launch_env
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", tmp_path)
    await m.create_browser(profile_uid="default")
    await m.pause_browser("default", 30)
    await m.pause_browser("default", 60)  # only moves the deadline
    assert m.paused("default")
    with pytest.raises(ValueError):
        await m.restart_browser("default")
    await m.resume_browser("default")
    await m.resume_browser("default")  # no-op
    assert not m.paused("default")
    assert len(calls) == 2


async def test_auto_resume_at_max_seconds(tmp_path, launch_env, monkeypatch):
    import asyncio
    m, calls = launch_env
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", tmp_path)
    await m.create_browser(profile_uid="default")
    await m.pause_browser("default", 0.05)
    await asyncio.sleep(0.2)
    assert not m.paused("default")
    assert len(calls) == 2


# ── extensions on every launch path ──

def _cached_extension(root: Path, ext_id: str) -> Path:
    cache = root / "cache" / ext_id
    cache.mkdir(parents=True)
    (cache / "manifest.json").write_text(json.dumps({"name": ext_id, "version": "1.2"}))
    return cache


async def test_extensions_put_back_after_a_profile_swap(tmp_path, launch_env, monkeypatch):
    from core import local_browser as lb
    m, calls = launch_env
    monkeypatch.setattr("core.local_browser.PROFILES_DIR", tmp_path)
    cache = _cached_extension(tmp_path, "extx")
    monkeypatch.setattr("core.local_browser.download_extension", AsyncMock(return_value=cache))
    await m.create_browser(profile_uid="default", extensions=["extx"])
    profile = tmp_path / "default"
    assert lb.extension_installed("extx", profile)

    # A restore/import swaps in a profile without it, then resumes.
    await m.pause_browser("default", 30)
    shutil.rmtree(profile)
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({"from": "elsewhere"}))
    await m.resume_browser("default")
    assert lb.extension_installed("extx", profile)
    assert prefs_of(profile)["from"] == "elsewhere"

    # Present (here recorded only in Secure Preferences, where Chrome keeps
    # it): left alone, Secure Preferences not reset.
    p = prefs_of(profile)
    p["extensions"]["settings"].pop("extx")
    (profile / "Default" / "Preferences").write_text(json.dumps(p))
    (profile / "Default" / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {"extx": {"state": 0}}}}))
    await m.restart_browser("default")
    assert (profile / "Default" / "Secure Preferences").exists()
    assert "extx" not in prefs_of(profile)["extensions"]["settings"]

    # Removed by the person: never put back.
    await m.restart_browser("default", remove_extensions=["extx"])
    await m.restart_browser("default")
    assert not lb.extension_installed("extx", profile)
