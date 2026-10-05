#!/usr/bin/env bash
# Two release rules of the Camoufox images (README, "Engines and images"):
#
# 1. Both Camoufox images pin ONE Playwright version: the browser image's
#    server (camoufox/uv.lock) and the Camoufox Browser API's client
#    (controller/requirements-camoufox.txt). A client of another minor is
#    refused by the server (428), so a mismatch fails the job.
# 2. The Camoufox Browser API is built from controller/, but its tag carries
#    camoufox/pyproject.toml's version. A controller change that bumps only
#    controller/pyproject.toml reaches Chrome pools and leaves Camoufox pools
#    on the older code: warned (not failed: a Chrome-only fix is legitimate).
#
#   check-camoufox-pins.sh [<base-ref>]   base-ref: the commit this one is
#                                         compared with for rule 2 (optional)
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

lock_pw="$(awk '/^\[\[package\]\]/{n=""} /^name = "playwright"$/{n=1; next} n && /^version = /{gsub(/"/,"",$3); print $3; exit}' camoufox/uv.lock)"
req_pw="$(sed -nE 's/^playwright==([0-9][^ ;\\]*).*/\1/p' controller/requirements-camoufox.txt | head -1)"
in_pw="$(sed -nE 's/^playwright==([0-9][^ ;]*).*/\1/p' controller/requirements-camoufox.in | head -1)"
pyproj_pw="$(sed -nE 's/^ *"playwright==([0-9][^"]*)".*/\1/p' camoufox/pyproject.toml | head -1)"
echo "playwright: camoufox/uv.lock=$lock_pw camoufox/pyproject.toml=$pyproj_pw controller/requirements-camoufox.in=$in_pw controller/requirements-camoufox.txt=$req_pw"
if [ -z "$lock_pw" ] || [ -z "$req_pw" ] || [ "$lock_pw" != "$req_pw" ] || [ "$lock_pw" != "$in_pw" ] || [ "$lock_pw" != "$pyproj_pw" ]; then
  echo "::error::The Camoufox images must pin one Playwright version (see above)." >&2
  exit 1
fi

base="${1:-}"
if [ -n "$base" ] && ! printf '%s' "$base" | grep -qE '^0+$' && git cat-file -e "$base^{commit}" 2>/dev/null; then
  ver() { { git show "$1:$2" 2>/dev/null || true; } | sed -nE 's/^version = "(.*)"/\1/p' | head -1; }
  c_old="$(ver "$base" controller/pyproject.toml)"; c_new="$(ver HEAD controller/pyproject.toml)"
  f_old="$(ver "$base" camoufox/pyproject.toml)"; f_new="$(ver HEAD camoufox/pyproject.toml)"
  echo "controller $c_old -> $c_new, camoufox $f_old -> $f_new"
  if [ "$c_old" != "$c_new" ] && [ "$f_old" = "$f_new" ]; then
    echo "::warning::controller/pyproject.toml moved ($c_old -> $c_new) but camoufox/pyproject.toml did not: Camoufox Browser APIs keep the older controller code. Bump camoufox/pyproject.toml too if the change is meant for them."
  fi
else
  echo "no base commit to compare versions with; version-move check skipped"
fi
