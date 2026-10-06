"""uBlock Origin's managed settings (ubo.json, ubo_policy.py)."""
import hashlib
import json
from pathlib import Path

import ubo_policy

HERE = Path(__file__).resolve().parent.parent
SPEC = json.loads((HERE / "ubo.json").read_text())


def fake_xpi(tmp_path: Path, assets: dict, files: dict) -> Path:
    x = tmp_path / "ubo"
    (x / "assets").mkdir(parents=True)
    (x / "assets" / "assets.json").write_text(json.dumps(assets))
    (x / "manifest.json").write_text(json.dumps(
        {"version": "1.0", "browser_specific_settings": {"gecko": {"id": ubo_policy.UBO_ID}}}))
    for rel, body in files.items():
        (x / rel).parent.mkdir(parents=True, exist_ok=True)
        (x / rel).write_bytes(body)
    return x


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_spec_is_fixed_and_never_updates():
    # The same lists for every locale: no regional list, and uBO never updates or waits.
    assert SPEC["filterLists"][0] == "user-filters"
    assert not any(k.startswith(("RUS", "DEU", "FRA")) for k in SPEC["filterLists"])
    assert SPEC["userSettings"] == {"autoUpdate": "false", "suspendUntilListsAreLoaded": "false"}
    m = ubo_policy.managed_settings(SPEC)
    assert m["toOverwrite"]["filterLists"] == SPEC["filterLists"]
    assert ["autoUpdate", "false"] in m["userSettings"] and ["suspendUntilListsAreLoaded", "false"] in m["userSettings"]


def test_bundled_pinned_list_passes(tmp_path):
    body = b"||ads.example^\n"
    assets = {"a": {"content": "filters", "contentURL": ["https://x/a.txt", "assets/a.txt"]}}
    x = fake_xpi(tmp_path, assets, {"assets/a.txt": body})
    spec = {"filterLists": ["user-filters", "a"], "files": {"assets/a.txt": sha(body)}}
    assert ubo_policy.check_selection(spec, x) == []


def test_unbundled_moved_or_unknown_lists_fail(tmp_path):
    assets = {
        "a": {"content": "filters", "contentURL": "assets/a.txt"},
        "RUS-0": {"content": "filters", "off": True, "contentURL": ["https://x/ru.txt"]},
        "psl": {"content": "internal", "contentURL": "assets/psl.dat"},
    }
    x = fake_xpi(tmp_path, assets, {"assets/a.txt": b"new", "assets/psl.dat": b"p"})
    spec = {"filterLists": ["a", "RUS-0", "psl", "nope"],
            "files": {"assets/a.txt": sha(b"old"), "assets/gone.txt": sha(b"x")}}
    problems = "\n".join(ubo_policy.check_selection(spec, x))
    assert "a: assets/a.txt sha256" in problems
    assert "RUS-0: no copy bundled" in problems
    assert "psl: not a filter list" in problems and "nope: not a filter list" in problems
    assert "assets/gone.txt: pinned but no selected list reads it" in problems


def test_main_writes_3rdparty_and_keeps_other_policies(tmp_path):
    body = b"x\n"
    x = fake_xpi(tmp_path, {"a": {"content": "filters", "contentURL": "assets/a.txt"}}, {"assets/a.txt": body})
    spec = tmp_path / "ubo.json"
    spec.write_text(json.dumps({"filterLists": ["user-filters", "a"], "files": {"assets/a.txt": sha(body)},
                                "userSettings": {"autoUpdate": "false"}}))
    pol = tmp_path / "policies.json"
    pol.write_text(json.dumps({"policies": {"ExtensionUpdate": False}}))
    assert ubo_policy.main(["ubo_policy.py", str(spec), str(x), str(pol)]) == 0
    d = json.loads(pol.read_text())["policies"]
    assert d["ExtensionUpdate"] is False
    assert d["3rdparty"]["Extensions"][ubo_policy.UBO_ID] == {
        "toOverwrite": {"filterLists": ["user-filters", "a"]}, "userSettings": [["autoUpdate", "false"]]}
    # A broken selection writes nothing.
    spec.write_text(json.dumps({"filterLists": ["b"], "files": {}, "userSettings": {}}))
    pol.write_text("{\"policies\": {}}")
    assert ubo_policy.main(["ubo_policy.py", str(spec), str(x), str(pol)]) == 1
    assert json.loads(pol.read_text()) == {"policies": {}}
