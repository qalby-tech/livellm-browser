"""Settings from the environment, the managed prefs and their clean removal."""
import json
import os
from types import SimpleNamespace

import pytest

from core import prefs
from core.settings import Settings, load_locales, parse_geolocation


def test_defaults_are_en_us():
    s = Settings.from_env({})
    assert s.locale == "en-US" and s.languages == ["en-US", "en"]
    assert s.accept_languages() == "en-US, en"
    assert s.timezone == "" and s.geolocation is None and s.proxy is None


def test_languages_take_camoufox_form():
    s = Settings.from_env({"BROWSER_LOCALE": "ru-RU", "BROWSER_LANGUAGES": "ru-RU,ru,en-US,en", "TZ": "Europe/Moscow"})
    assert s.accept_languages() == "ru-RU, ru, en-US, en"
    assert s.timezone == "Europe/Moscow"
    assert s.locale_env() == {"LANG": "ru_RU.UTF-8", "LANGUAGE": "ru_RU:ru"}
    # a locale alone: its own tag
    assert Settings.from_env({"BROWSER_LOCALE": "de-DE"}).languages == ["de-DE"]


def test_every_offered_locale_has_a_region_and_an_environment():
    table = load_locales()
    assert len(table) == 36
    for tag in table:
        assert "-" in tag, tag  # Camoufox's Locale.as_config() asserts a region
        env = Settings.from_env({"BROWSER_LOCALE": tag}).locale_env()
        assert env["LANG"].endswith(".UTF-8")


@pytest.mark.parametrize("raw,want", [
    ("off", "off"),
    ("fixed:55.7558,37.6173,100", {"latitude": 55.7558, "longitude": 37.6173, "accuracy": 100.0}),
    ("fixed:5.57558e+01,3.76173e+01,1e+02", {"latitude": 55.7558, "longitude": 37.6173, "accuracy": 100.0}),  # Go %g
    ("fixed:-0,0,1", {"latitude": 0.0, "longitude": 0.0, "accuracy": 1.0}),
    ("fixed:91,0,1", None),
    ("fixed:1,2", None),
    ("fixed:NaN,0,1", None),
    ("fixed:1,2,0", None),
    ("on", None),
    ("", None),
])
def test_geolocation(raw, want):
    assert parse_geolocation(raw) == want


def test_proxy_through_the_relay_ignores_bypass():
    s = Settings.from_env({"BROWSER_PROXY_SERVER": "http://127.0.0.1:3128", "BROWSER_PROXY_BYPASS": "*.lan"})
    assert s.proxy == {"server": "http://127.0.0.1:3128"}
    s = Settings.from_env({"BROWSER_PROXY_SERVER": "http://p.example:8080", "BROWSER_PROXY_BYPASS": "*.lan",
                           "BROWSER_PROXY_USERNAME": "u", "BROWSER_PROXY_PASSWORD": "p"})
    assert s.proxy == {"server": "http://p.example:8080", "bypass": "*.lan", "username": "u", "password": "p"}


def fake_statvfs(total_bytes):
    return lambda p: SimpleNamespace(f_blocks=total_bytes // 4096, f_frsize=4096)


def test_managed_prefs(tmp_path):
    four_gi = fake_statvfs(4 << 30)
    assert prefs.managed_prefs(Settings.from_env({}), tmp_path, four_gi) == {
        prefs.QUOTA_PREF: (4 << 30) // 2 // 1024,
        # camoufox.cfg ships false: a file:// page could read the profile
        "security.fileuri.strict_origin_policy": True,
    }
    off = prefs.managed_prefs(Settings.from_env({"BROWSER_GEOLOCATION": "off"}), tmp_path, four_gi)
    assert off[prefs.GEO_PREF] == 2
    fixed = prefs.managed_prefs(Settings.from_env({"BROWSER_GEOLOCATION": "fixed:1,2,3"}), tmp_path, four_gi)
    assert fixed[prefs.GEO_PREF] == 1
    huge = prefs.managed_prefs(Settings.from_env({}), tmp_path, fake_statvfs(20 << 40))
    assert huge[prefs.QUOTA_PREF] == prefs.INT32_MAX


def test_proxied_prefs_cover_startup_dns_and_webrtc(tmp_path):
    p = prefs.managed_prefs(Settings.from_env({"BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"}), tmp_path, fake_statvfs(4 << 30))
    want = {
        "network.proxy.type": 1, "network.proxy.http": "127.0.0.1", "network.proxy.http_port": 3128,
        "network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": 3128, "network.proxy.no_proxies_on": "",
        "network.proxy.allow_hijacking_localhost": True, "network.proxy.failover_direct": False,
        "network.trr.mode": 5, "network.dns.disablePrefetch": True, "network.predictor.enabled": False,
        "network.prefetch-next": False, "network.http.speculative-parallel-limit": 0,
        "network.http.http3.enable": False, "media.peerconnection.ice.proxy_only": True,
        "media.peerconnection.ice.proxy_only_if_behind_proxy": True,
        "media.peerconnection.ice.default_address_only": True, "media.peerconnection.ice.no_host": True,
    }
    for k, v in want.items():
        assert p[k] == v, k
    socks = prefs.proxied_prefs({"server": "socks5://10.0.0.1:1080"})
    assert socks["network.proxy.socks"] == "10.0.0.1" and socks["network.proxy.socks_remote_dns"] is True


def write_prefs_js(dir, lines):
    (dir / "prefs.js").write_text("// Mozilla User Preferences\n" + "".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in lines))


def test_a_cleared_setting_leaves_prefs_js(tmp_path):
    # launch 1: geolocation off and proxied; Firefox saved them into prefs.js
    s1 = Settings.from_env({"BROWSER_GEOLOCATION": "off", "BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"})
    p1 = prefs.managed_prefs(s1, tmp_path, fake_statvfs(4 << 30))
    prefs.record(tmp_path, list(p1) + ["intl.locale.requested"])
    write_prefs_js(tmp_path, list(p1.items()) + [("intl.locale.requested", "en-US"), ("browser.startup.page", 3), ("network.cookie.cookieBehavior", 5)])
    # launch 2: both cleared
    removed = prefs.clear_previous(tmp_path)
    assert removed == len(p1) + 1
    text = (tmp_path / "prefs.js").read_text()
    assert "permissions.default.geo" not in text and "network.proxy" not in text
    # what the person set themselves stays
    assert 'user_pref("browser.startup.page", 3);' in text and "cookieBehavior" in text


def test_the_first_launch_clears_every_name_it_could_manage(tmp_path):
    write_prefs_js(tmp_path, [("permissions.default.geo", 2), ("network.proxy.type", 1), ("ui.x", 1)])
    assert prefs.previous_names(tmp_path) == list(prefs.FIRST_LAUNCH_NAMES)
    assert prefs.clear_previous(tmp_path) == 2
    assert "ui.x" in (tmp_path / "prefs.js").read_text()


def test_strip_tolerates_odd_files(tmp_path):
    assert prefs.strip_prefs_js(tmp_path, ["a"]) == 0  # no prefs.js
    (tmp_path / "prefs.js").write_text('user_pref("a\\"b", 1);\nuser_pref("a", 2);\ngarbage\n')
    assert prefs.strip_prefs_js(tmp_path, ['a"b']) == 1
    assert (tmp_path / "prefs.js").read_text() == 'user_pref("a", 2);\ngarbage\n'


def test_every_launch_strips_every_managed_name_whatever_the_record_says(tmp_path):
    """The record travels in archives: one shorter than its prefs.js (a
    hand-edited export, an older image's) must not keep a managed pref live."""
    write_prefs_js(tmp_path, [("network.proxy.type", 1), ("network.proxy.http", "203.0.113.9"), ("network.proxy.http_port", 8080),
                              (prefs.QUOTA_PREF, 5), ("security.fileuri.strict_origin_policy", False), ("ui.x", 1)])
    prefs.record(tmp_path, [prefs.QUOTA_PREF])
    assert prefs.clear_previous(tmp_path) == 5
    text = (tmp_path / "prefs.js").read_text()
    assert "network.proxy" not in text and "strict_origin_policy" not in text
    assert 'user_pref("ui.x", 1);' in text
    # and a recorded name outside the managed set (a library pref) goes too
    write_prefs_js(tmp_path, [("webgl.force-enabled", True), ("ui.x", 1)])
    prefs.record(tmp_path, ["webgl.force-enabled"])
    assert prefs.clear_previous(tmp_path) == 1


def test_the_managed_set_covers_every_pref_this_module_passes(tmp_path):
    every = {}
    for env in ({}, {"BROWSER_GEOLOCATION": "off"}, {"BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"},
                {"BROWSER_PROXY_SERVER": "socks5://10.0.0.1:1080"}):
        every.update(prefs.managed_prefs(Settings.from_env(env), tmp_path, fake_statvfs(4 << 30)))
    assert set(every) <= set(prefs.FIRST_LAUNCH_NAMES)
