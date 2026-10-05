"""Launch options, the identity, and what is checked before Camoufox opens
the profile."""
import json
import os
import signal
from pathlib import Path

import pytest

from core import identity, options, profile_guard
from core.settings import Settings


class Screen:
    def __init__(self, **kw):
        self.kw = kw


UBO = object()


def test_camoufox_kwargs_carry_every_setting():
    s = Settings.from_env({"BROWSER_LOCALE": "ru-RU", "BROWSER_LANGUAGES": "ru-RU,ru,en-US,en", "TZ": "Europe/Moscow",
                           "BROWSER_GEOLOCATION": "fixed:55.75,37.61,50", "BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"})
    ident = {"fingerprint": {"navigator": {"userAgent": "x"}},
             "pinned": {"navigator.hardwareConcurrency": 16, "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64"}}
    kw = options.camoufox_kwargs(s, ident, {"permissions.default.geo": 1}, Screen(max_width=1920), UBO)
    assert kw["config"] == {
        "locale:all": "ru-RU, ru, en-US, en", "timezone": "Europe/Moscow",
        "geolocation:latitude": 55.75, "geolocation:longitude": 37.61, "geolocation:accuracy": 50.0,
        "navigator.hardwareConcurrency": 16, "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64",
    }
    assert kw["locale"] == "ru-RU" and kw["os"] == "linux" and kw["headless"] is False
    assert kw["window"] == (1920, 1080)
    assert kw["addons"] == ["/opt/camoufox-addons/ubo"] and kw["exclude_addons"] == [UBO]
    assert kw["main_world_eval"] is True and kw["humanize"] is False and kw["enable_cache"] is False
    assert kw["i_know_what_im_doing"] is True
    assert kw["firefox_user_prefs"] == {"permissions.default.geo": 1}
    assert "proxy" not in kw  # Playwright's proxy option is added to the server options instead
    assert kw["fingerprint"] == ident["fingerprint"] and kw["fingerprint"] is not ident["fingerprint"]
    # first resolution: nothing pinned, no fingerprint argument
    first = options.camoufox_kwargs(Settings.from_env({}), None, {}, Screen(), UBO)
    assert "fingerprint" not in first and first["config"] == {"locale:all": "en-US, en"}


def test_launch_server_options():
    from camoufox.server import to_camel_case_dict

    lib_opts = {
        "executable_path": "/opt/camoufox/camoufox-bin", "args": [], "headless": False,
        "env": {"CAMOU_CONFIG_1": "{}", "DISPLAY": ":1", "N": 5},
        "firefox_user_prefs": {"a": 1}, "proxy": {"server": "x"},
    }
    s = Settings.from_env({"BROWSER_LOCALE": "de-DE", "BROWSER_PROXY_SERVER": "http://127.0.0.1:3128"})
    out = options.launch_server_options(lib_opts, s, Path("/p/default"), ["-allow-downgrade"], to_camel=to_camel_case_dict)
    assert out["executablePath"] == "/opt/camoufox/camoufox-bin"
    assert out["firefoxUserPrefs"] == {"a": 1}
    assert out["args"] == ["-allow-downgrade"]
    assert out["env"]["LANG"] == "de_DE.UTF-8" and out["env"]["N"] == "5" and out["env"]["DISPLAY"] == ":1"
    assert out["proxy"] == {"server": "http://127.0.0.1:3128"}
    # literally (a test that walked the constant passed with a key deleted
    # from it): without noDefaultViewport contexts[0] is 1280x720 under the
    # window and a second new_page() can hang (daijro/camoufox#666)
    assert out["noDefaultViewport"] is True
    assert out["colorScheme"] == out["reducedMotion"] == out["forcedColors"] == out["contrast"] == "no-override"
    assert out["_userDataDir"] == "/p/default" and out["_sharedBrowser"] is True
    assert out["host"] == "127.0.0.1" and out["port"] == 0 and out["timeout"] == 120000
    assert len(out["wsPath"]) == 33 and out["wsPath"][0] == "/" and int(out["wsPath"][1:], 16) >= 0
    assert options.new_ws_path() != options.new_ws_path()
    assert "executable_path" not in out and "firefox_user_prefs" not in out
    # unproxied: no proxy at all (the library's never passes through)
    out = options.launch_server_options(lib_opts, Settings.from_env({}), Path("/p"), to_camel=to_camel_case_dict)
    assert "proxy" not in out


def test_identity_pins_what_camoufox_resolved(tmp_path):
    fp = {"navigator": {"userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0"}, "t": (1, 2)}
    config = {"navigator.hardwareConcurrency": 8, "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64", "other": 1}
    blob = json.dumps(config)
    resolved = {"env": {"CAMOU_CONFIG_1": blob[:10], "CAMOU_CONFIG_2": blob[10:], "PATH": "/bin"}}
    seen = []
    ident = identity.create(lambda: fp, lambda f: seen.append(f) or resolved, "ru-RU", "Europe/Moscow", "camoufox-0.5.7/v156")
    assert ident["pinned"] == {k: config[k] for k in identity.PINNED_KEYS}
    assert ident["fingerprint"]["t"] == [1, 2]  # through JSON: what a later launch loads
    assert seen[0] == ident["fingerprint"] and seen[0] is not ident["fingerprint"]
    identity.save(tmp_path, ident)
    assert identity.load(tmp_path) == ident
    assert oct(os.stat(identity.path_of(tmp_path)).st_mode & 0o777) == "0o600"
    identity.path_of(tmp_path).write_text("{not json")
    assert identity.load(tmp_path) is None
    identity.path_of(tmp_path).write_text(json.dumps({"format": 9, "fingerprint": {}}))
    assert identity.load(tmp_path) is None


def test_the_window_fits_its_screen_with_the_frame():
    # Camoufox sizes the window's inside to window.outer*; the window
    # manager's frame (10 x 34) comes on top, and pages read it
    fp = {"window": {"outerWidth": 1920, "outerHeight": 1080, "innerWidth": 1920, "innerHeight": 994, "screenX": 0, "screenY": 13}}
    config = {"screen.width": 1920, "screen.height": 1080, "screen.availWidth": 1920, "screen.availHeight": 1053,
              "window.outerWidth": 1920, "window.outerHeight": 1053, "window.innerWidth": 1920, "window.innerHeight": 967,
              "window.screenX": 0, "window.screenY": 0}
    assert identity.fit_window(fp, config, (10, 34))
    w = fp["window"]
    assert (w["outerWidth"], w["outerHeight"]) == (1910, 1019)
    assert (w["innerWidth"], w["innerHeight"]) == (1910, 933)  # the chrome between outer and inner kept
    assert (w["screenX"], w["screenY"]) == (0, 0)
    # a smaller screen, a window placed so its frame would cross the edge
    fp = {"window": {"outerWidth": 1200, "outerHeight": 700, "screenX": 160, "screenY": 30}}
    config = {"screen.availWidth": 1366, "screen.availHeight": 741, "window.outerWidth": 1200, "window.outerHeight": 700,
              "window.screenX": 160, "window.screenY": 30}
    assert identity.fit_window(fp, config, (10, 34))
    assert fp["window"] == {"outerWidth": 1200, "outerHeight": 700, "screenX": 156, "screenY": 7}
    # already fits: untouched
    fp = {"window": {"outerWidth": 1000, "outerHeight": 600}}
    config = {"screen.availWidth": 1366, "screen.availHeight": 741, "window.outerWidth": 1000, "window.outerHeight": 600,
              "window.screenX": 10, "window.screenY": 10}
    assert not identity.fit_window(fp, config, (10, 34)) and fp == {"window": {"outerWidth": 1000, "outerHeight": 600}}
    assert not identity.fit_window({}, config, (10, 34))


def test_a_new_identity_resolves_the_fitted_window(tmp_path):
    fp = {"window": {"outerWidth": 1920, "outerHeight": 1080, "innerWidth": 1920, "innerHeight": 994, "screenX": 0, "screenY": 0}}
    seen = []

    def resolve(f):
        seen.append(f)
        w = f["window"]
        config = {"screen.availWidth": 1920, "screen.availHeight": 1053, "window.outerWidth": min(w["outerWidth"], 1920),
                  "window.outerHeight": min(w["outerHeight"], 1053), "window.innerHeight": min(w["innerHeight"], 967),
                  "window.screenX": 0, "window.screenY": 0, "navigator.hardwareConcurrency": 32}
        return {"env": {"CAMOU_CONFIG_1": json.dumps(config)}}

    ident = identity.create(lambda: fp, resolve, "en-US", "", "x", frame=(10, 34))
    assert len(seen) == 2 and seen[1]["window"]["outerWidth"] == 1910
    assert ident["fingerprint"]["window"]["outerHeight"] == 1019
    assert ident["pinned"] == {"navigator.hardwareConcurrency": 32}


@pytest.mark.parametrize("resolved,limit,want", [
    (32, 2, 4),      # a 2-CPU browser on a 32-core node: the table's floor
    (32, 0.5, 4),
    (32, 4, 4),
    (32, 5, 4),      # snapped DOWN into real desktop counts
    (32, 6, 6),
    (16, 12.5, 12),  # 13 is no desktop's count
    (16, 64, 16),    # never more than the node has
    (32, None, 32),  # no CPU limit: what Camoufox resolved
    ("x", 2, "x"),
])
def test_the_core_count_follows_the_cpu_limit(resolved, limit, want):
    assert identity.fit_cores(resolved, limit) == want


def test_cpu_limit_from_the_cgroup(tmp_path):
    assert identity.cpu_limit(tmp_path) is None
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    assert identity.cpu_limit(tmp_path) == 2.0
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert identity.cpu_limit(tmp_path) is None
    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("150000\n")
    (v1 / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert identity.cpu_limit(v1) == 1.5
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
    assert identity.cpu_limit(v1) is None


def test_chrome_profile_is_recognised(tmp_path):
    assert not profile_guard.chrome_profile(tmp_path)
    (tmp_path / "Local State").write_text("{}")
    assert profile_guard.chrome_profile(tmp_path)
    other = tmp_path / "o"
    (other / "Default").mkdir(parents=True)
    (other / "Default" / "Preferences").write_text("{}")
    assert profile_guard.chrome_profile(other)


OWN = ("156.0.1-beta.34", "20261003194815")


def compat(dir, version, build):
    (dir / "compatibility.ini").write_text(f"[Compatibility]\nLastVersion={version}_{build}/{build}\nLastOSABI=Linux_x86_64-gcc3\n")


@pytest.mark.parametrize("version,build,args", [
    ("156.0.1-beta.34", "20261003194815", []),                 # the same build
    ("156.0.1-beta.33", "20260930000000", []),                 # older
    ("156.0.1-beta.35", "20261005000000", ["-allow-downgrade"]),  # a rollback within the major
    ("156.0.1-beta.34", "20261009000000", ["-allow-downgrade"]),  # same version, newer build
])
def test_downgrade_within_a_major(tmp_path, version, build, args):
    compat(tmp_path, version, build)
    assert profile_guard.downgrade_args(tmp_path, OWN) == args


def test_a_newer_major_is_refused_unless_imported_on_purpose(tmp_path):
    compat(tmp_path, "157.0-beta.1", "20261101000000")
    with pytest.raises(profile_guard.ProfileNewer) as e:
        profile_guard.downgrade_args(tmp_path, OWN)
    assert "newer Camoufox" in str(e.value)
    marker = tmp_path / profile_guard.DOWNGRADE_MARKER
    marker.write_text("157.0-beta.1\n")
    assert profile_guard.downgrade_marker(tmp_path)
    assert profile_guard.downgrade_args(tmp_path, OWN) == ["-allow-downgrade"]
    # still there: a launch that fails is retried with it; the launcher
    # removes it once the browser is up
    assert marker.exists()
    assert profile_guard.downgrade_args(tmp_path, OWN) == ["-allow-downgrade"]
    profile_guard.consume_downgrade_marker(tmp_path)
    assert not marker.exists() and not profile_guard.downgrade_marker(tmp_path)
    profile_guard.consume_downgrade_marker(tmp_path)  # gone already: fine
    with pytest.raises(profile_guard.ProfileNewer):
        profile_guard.downgrade_args(tmp_path, OWN)
    # what the caller read decides, not the disk
    assert profile_guard.downgrade_args(tmp_path, OWN, forced=True) == ["-allow-downgrade"]


def test_no_compatibility_file_and_a_stray_marker(tmp_path):
    assert profile_guard.downgrade_args(tmp_path, OWN) == []
    (tmp_path / profile_guard.DOWNGRADE_MARKER).write_text("x")
    compat(tmp_path, "155.0-beta.9", "1")
    assert profile_guard.downgrade_args(tmp_path, OWN) == []


def fake_proc(root: Path, pid: int, argv, state="S"):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x y) {state} 1 1 1")


def test_camoufox_processes_are_found_by_their_binary(tmp_path):
    proc = tmp_path / "proc"
    fake_proc(proc, 10, ["/opt/camoufox/camoufox-bin", "--foreground", "--profile", "/p"])
    fake_proc(proc, 11, ["/opt/camoufox/camoufox-bin", "-contentproc", "1"])
    fake_proc(proc, 12, ["/opt/camoufox/camoufox-bin", "-contentproc", "2"], state="Z")
    fake_proc(proc, 13, ["/usr/bin/node", "serve.js"])
    fake_proc(proc, 14, ["/opt/camoufoxx/camoufox-bin"])
    assert sorted(profile_guard.camoufox_pids(Path("/opt/camoufox"), proc)) == [10, 11]
    assert profile_guard.camoufox_pids(Path("/opt/camoufox"), proc, parents_only=True) == [10]
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        (proc / str(pid) / "stat").write_text(f"{pid} (x) Z 1")

    assert profile_guard.kill_all(Path("/opt/camoufox"), proc, timeout=1, kill=kill) == 0
    assert sorted(killed) == [(10, signal.SIGKILL), (11, signal.SIGKILL)]


def test_stale_locks_go(tmp_path):
    os.symlink("127.0.0.1:+4242", tmp_path / "lock")
    (tmp_path / ".parentlock").write_text("")
    profile_guard.remove_locks(tmp_path)
    assert not os.path.lexists(tmp_path / "lock") and not (tmp_path / ".parentlock").exists()
