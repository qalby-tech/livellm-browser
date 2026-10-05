#!/usr/bin/env bash
# The release rules of the client pins (README, "Engines and images"). One
# Browser API image (controller/) drives both browser images, so each of its
# two clients pins exactly what the browser image it drives pins:
#
# 1. Playwright: the Camoufox image's server (browser/camoufox/uv.lock and
#    pyproject.toml) and the Browser API's client (controller/uv.lock and
#    pyproject.toml) pin ONE version. A client of another minor is refused by
#    the server (428), so a mismatch fails the job.
# 2. patchright: the Browser API's (controller/) equals the Chrome image's
#    (browser/chrome/), the version the Chrome image's driver is tested with.
#
#   check-pins.sh            PINS_ROOT (default: this repo) is the tree read
set -euo pipefail
root="${PINS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$root"

# lock_ver FILE PACKAGE: the version a uv.lock pins
lock_ver() { awk -v want="name = \"$2\"" '/^\[\[package\]\]/{n=0} $0==want{n=1; next} n && /^version = /{gsub(/"/,"",$3); print $3; exit}' "$1" 2>/dev/null || true; }
# pyproj_ver FILE PACKAGE: the exact pin in a pyproject.toml's dependencies
pyproj_ver() { sed -nE "s/^ *\"$2==([0-9][^\"]*)\".*/\\1/p" "$1" 2>/dev/null | head -1 || true; }

fail=0
cf_lock="$(lock_ver browser/camoufox/uv.lock playwright)"
cf_pyproj="$(pyproj_ver browser/camoufox/pyproject.toml playwright)"
api_lock="$(lock_ver controller/uv.lock playwright)"
api_pyproj="$(pyproj_ver controller/pyproject.toml playwright)"
echo "playwright: browser/camoufox/uv.lock=$cf_lock browser/camoufox/pyproject.toml=$cf_pyproj controller/uv.lock=$api_lock controller/pyproject.toml=$api_pyproj"
if [ -z "$cf_lock" ] || [ "$cf_lock" != "$cf_pyproj" ] || [ "$cf_lock" != "$api_lock" ] || [ "$cf_lock" != "$api_pyproj" ]; then
  echo "::error::The Camoufox image and the Browser API must pin one Playwright version (see above)." >&2
  fail=1
fi

ch_lock="$(lock_ver browser/chrome/uv.lock patchright)"
ch_pyproj="$(pyproj_ver browser/chrome/pyproject.toml patchright)"
apip_lock="$(lock_ver controller/uv.lock patchright)"
apip_pyproj="$(pyproj_ver controller/pyproject.toml patchright)"
echo "patchright: browser/chrome/uv.lock=$ch_lock browser/chrome/pyproject.toml=$ch_pyproj controller/uv.lock=$apip_lock controller/pyproject.toml=$apip_pyproj"
if [ -z "$ch_lock" ] || [ "$ch_lock" != "$ch_pyproj" ] || [ "$ch_lock" != "$apip_lock" ] || [ "$ch_lock" != "$apip_pyproj" ]; then
  echo "::error::The Chrome image and the Browser API must pin one patchright version (see above)." >&2
  fail=1
fi
exit $fail
