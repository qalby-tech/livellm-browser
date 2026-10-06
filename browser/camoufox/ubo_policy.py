"""uBlock Origin's managed settings, written into Firefox's policies.json at
image build (Dockerfile), so the ad blocker never fetches at runtime.

uBO (``uBlock0@raymondhill.net``) reads Firefox enterprise policy
``3rdparty.Extensions["uBlock0@raymondhill.net"]`` as ``storage.managed``
(its managed_storage.json):

- ``toOverwrite.filterLists`` replaces the list selection at every launch
  (restoreAdminSettings runs before loadSelectedFilterLists), so uBO never
  auto-selects the regional list of the browser's language. Those lists are
  not in the extension; a ru-RU browser fetched RU AdList at every launch
  and, with no internet egress, held page loads ~80 s waiting for it.
- ``userSettings`` ``autoUpdate false`` (no list updates, no emergency
  update of the bundled copies at launch) and ``suspendUntilListsAreLoaded
  false`` (a page load never waits for the lists).

The selection (ubo.json) is uBO's own default set, every list of it with a
copy bundled in the pinned xpi; each bundled file is pinned again by its
sha256, and the build fails if a selected list has no bundled copy, so no
selected list can ever need the network. Refresh: see the README
("Camoufox's ad blocker").

    python ubo_policy.py <ubo.json> <extracted xpi dir> <policies.json>
"""
import hashlib
import json
import sys
from pathlib import Path

UBO_ID = "uBlock0@raymondhill.net"
USER_FILTERS = "user-filters"


def local_copies(entry: dict) -> list:
    """The bundled (non-http) contentURLs of one assets.json entry, in uBO's order."""
    urls = entry.get("contentURL") or []
    if isinstance(urls, str):
        urls = [urls]
    return [u for u in urls if not u.startswith(("http://", "https://"))]


def check_selection(spec: dict, xpi: Path) -> list:
    """Problems with spec against the extracted xpi; [] = every selected list
    is a filter list uBO knows, bundled, and its files match their pins."""
    problems = []
    assets = json.loads((xpi / "assets" / "assets.json").read_text())
    pinned = dict(spec.get("files") or {})
    seen = set()
    for key in spec["filterLists"]:
        if key == USER_FILTERS:
            continue
        entry = assets.get(key)
        if entry is None or entry.get("content") != "filters":
            problems.append(f"{key}: not a filter list in assets.json")
            continue
        present = [u for u in local_copies(entry) if (xpi / u).is_file()]
        if not present:
            problems.append(f"{key}: no copy bundled in the extension (it would be fetched)")
            continue
        for u in present:
            seen.add(u)
            want = pinned.get(u)
            got = hashlib.sha256((xpi / u).read_bytes()).hexdigest()
            if want is None:
                problems.append(f"{key}: {u} has no pinned sha256 (sha256 {got})")
            elif want != got:
                problems.append(f"{key}: {u} sha256 {got}, pinned {want}")
    for u in sorted(set(pinned) - seen):
        problems.append(f"{u}: pinned but no selected list reads it")
    return problems


def managed_settings(spec: dict) -> dict:
    """The 3rdparty entry uBO reads as storage.managed."""
    return {
        "toOverwrite": {"filterLists": list(spec["filterLists"])},
        "userSettings": [[k, v] for k, v in spec["userSettings"].items()],
    }


def main(argv) -> int:
    spec = json.loads(Path(argv[1]).read_text())
    xpi = Path(argv[2])
    manifest = json.loads((xpi / "manifest.json").read_text())
    if manifest["browser_specific_settings"]["gecko"]["id"] != UBO_ID:
        print("ubo_policy: the extension is not uBlock Origin", file=sys.stderr)
        return 1
    problems = check_selection(spec, xpi)
    if problems:
        for p in problems:
            print("ubo_policy:", p, file=sys.stderr)
        return 1
    path = Path(argv[3])
    policies = json.loads(path.read_text())
    policies["policies"].setdefault("3rdparty", {}).setdefault("Extensions", {})[UBO_ID] = managed_settings(spec)
    path.write_text(json.dumps(policies, indent=2))
    print("uBO", manifest["version"], "lists:", " ".join(spec["filterLists"]), "| settings:", spec["userSettings"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
