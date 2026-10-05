#!/usr/bin/env bash
# Tests of the release scripts the pipeline runs (no registry, no network):
# the tag guard's three answers, the Chart.yaml updater's line rules and the
# pin rules of check-pins.sh.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

# ── tag-guard.sh: a fake inspect that prints $FAKE_ERR and exits $FAKE_RC ──
cat > "$tmp/inspect" <<'EOF'
#!/usr/bin/env bash
[ -n "${FAKE_ERR:-}" ] && printf '%s\n' "$FAKE_ERR" >&2
exit "${FAKE_RC:-0}"
EOF
chmod +x "$tmp/inspect"
guard() { TAG_GUARD_INSPECT="$tmp/inspect" bash "$here/tag-guard.sh" "docker.io/x/y:t" 2>/dev/null; }

out="$(FAKE_RC=0 guard)"; rc=$?
[ $rc -eq 0 ] && [ "$out" = "build=false" ] && ok "guard: an existing tag is skipped" || bad "guard: existing tag -> rc=$rc out=$out"
out="$(FAKE_RC=1 FAKE_ERR='ERROR: docker.io/x/y:t: not found' guard)"; rc=$?
[ $rc -eq 0 ] && [ "$out" = "build=true" ] && ok "guard: 'not found' builds" || bad "guard: not found -> rc=$rc out=$out"
out="$(FAKE_RC=1 FAKE_ERR='ERROR: manifest unknown: manifest unknown' guard)"; rc=$?
[ $rc -eq 0 ] && [ "$out" = "build=true" ] && ok "guard: 'manifest unknown' builds" || bad "guard: manifest unknown -> rc=$rc out=$out"
for e in 'ERROR: toomanyrequests: You have reached your pull rate limit' \
         'ERROR: pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed' \
         'ERROR: failed to do request: Head "https://registry-1.docker.io/v2/x/y/manifests/t": dial tcp: i/o timeout' \
         'ERROR: unexpected status from HEAD request: 500 Internal Server Error' \
         ''; do
  out="$(FAKE_RC=1 FAKE_ERR="$e" guard)"; rc=$?
  [ $rc -ne 0 ] && [ -z "$out" ] && ok "guard: fails closed on '${e:0:40}…'" || bad "guard: '${e}' -> rc=$rc out=$out"
done

# ── update_chart.py ──
chart="$tmp/Chart.yaml"
cat > "$chart" <<'EOF'
apiVersion: v2
name: livellm-browser-operator
type: application

version: "0.10.0-dev"

# appVersion controls the browser image tag:
#   develop: "dev-2.0.1"  → kamasalyamov/livellm-browser:dev-2.0.1
appVersion: "dev-2.3.0"

annotations:
  # controllerVersion controls the controller image tag:
  #   develop: "dev-controller-2.0.1"
  controllerVersion: "dev-controller-2.5.0"
EOF
cp "$chart" "$tmp/orig"
up() { python3 "$here/update_chart.py" "$chart" --app "$1" --controller "$2" --camoufox "$3" 2>/dev/null; }

out="$(up dev-2.3.0 dev-controller-2.5.0 dev-camoufox-1.0.0)"
want="$(cat "$tmp/orig"; printf '  camoufoxVersion: "dev-camoufox-1.0.0"\n')"
[ "$out" = "changed=true" ] && [ "$(cat "$chart")" = "$want" ] && ok "chart: the camoufox annotation is inserted, every other line kept" || { bad "chart: insert"; diff <(echo "$want") "$chart"; }
grep -c '^appVersion: "dev-2.3.0"$' "$chart" | grep -qx 1 && grep -qx '  controllerVersion: "dev-controller-2.5.0"' "$chart" && ok "chart: appVersion and controllerVersion lines byte-identical" || bad "chart: chrome lines moved"
! grep -q camoufoxApiVersion "$chart" && ok "chart: no camoufoxApiVersion is written" || bad "chart: camoufoxApiVersion written"
before="$(cat "$chart")"
out="$(up dev-2.3.0 dev-controller-2.5.0 dev-camoufox-1.0.0)"
[ "$out" = "changed=false" ] && [ "$(cat "$chart")" = "$before" ] && ok "chart: unchanged versions write nothing" || bad "chart: rerun -> $out"
out="$(up dev-2.3.0 dev-controller-2.6.0 dev-camoufox-1.0.0)"
[ "$out" = "changed=true" ] && [ "$(diff <(echo "$before") "$chart" | grep -c '^[<>]')" = "2" ] && grep -qx '  controllerVersion: "dev-controller-2.6.0"' "$chart" && ok "chart: only the changed line is rewritten" || bad "chart: controller bump"
# the Chrome rename: dev-2.3.0 -> dev-chrome-2.4.0 rewrites that one line
before="$(cat "$chart")"
out="$(up dev-chrome-2.4.0 dev-controller-2.6.0 dev-camoufox-1.0.0)"
d="$(diff <(echo "$before") "$chart" | grep '^[<>]')"
[ "$out" = "changed=true" ] && [ "$d" = "$(printf '< appVersion: "dev-2.3.0"\n> appVersion: "dev-chrome-2.4.0"')" ] && ok "chart: appVersion dev-2.3.0 -> dev-chrome-2.4.0 rewrites only that line" || { bad "chart: chrome rename"; echo "$d"; }
printf 'apiVersion: v2\nname: x\n' > "$tmp/broken.yaml"
python3 "$here/update_chart.py" "$tmp/broken.yaml" --app a --controller b --camoufox c >/dev/null 2>&1 && bad "chart: a file without appVersion is refused" || ok "chart: a file without appVersion is refused"
python3 "$here/update_chart.py" "$chart" --app a --controller b --camoufox c --camoufox-api d >/dev/null 2>&1 && bad "chart: --camoufox-api is gone" || ok "chart: --camoufox-api is gone"

# ── check-pins.sh: a copy of this repo's pin files, then one pin moved at a time ──
repo="$(cd "$here/../.." && pwd)"
pins="$tmp/pins"
for f in browser/camoufox/uv.lock browser/camoufox/pyproject.toml browser/chrome/uv.lock browser/chrome/pyproject.toml controller/uv.lock controller/pyproject.toml; do
  mkdir -p "$pins/$(dirname "$f")"; cp "$repo/$f" "$pins/$f"
done
pinchk() { PINS_ROOT="$pins" bash "$here/check-pins.sh" >/dev/null 2>&1; }
pinchk && ok "pins: this repo's pins agree" || bad "pins: this repo's pins disagree"
# set_lock FILE PACKAGE VERSION / set_pyproj FILE PACKAGE VERSION (in the copy)
set_lock() { python3 - "$pins/$1" "$2" "$3" <<'PY'
import re, sys
p, name, ver = sys.argv[1:]
s = open(p).read()
s2 = re.sub(rf'(\[\[package\]\]\nname = "{re.escape(name)}"\nversion = ")[^"]+"', rf'\g<1>{ver}"', s, count=1)
assert s2 != s, (p, name)
open(p, "w").write(s2)
PY
}
set_pyproj() { sed -i -E "s/\"$2==[0-9][^\"]*\"/\"$2==$3\"/" "$pins/$1"; grep -q "\"$2==$3\"" "$pins/$1"; }
snapshot() { rm -rf "$tmp/pins.orig"; cp -r "$pins" "$tmp/pins.orig"; }
restore() { rm -rf "$pins"; cp -r "$tmp/pins.orig" "$pins"; }
snapshot
for case in "set_lock browser/camoufox/uv.lock playwright 1.63.0" \
            "set_pyproj browser/camoufox/pyproject.toml playwright 1.63.0" \
            "set_lock controller/uv.lock playwright 1.63.0" \
            "set_pyproj controller/pyproject.toml playwright 1.63.0" \
            "set_lock browser/chrome/uv.lock patchright 1.57.0" \
            "set_pyproj browser/chrome/pyproject.toml patchright 1.57.0" \
            "set_lock controller/uv.lock patchright 1.57.0" \
            "set_pyproj controller/pyproject.toml patchright 1.57.0"; do
  restore
  $case || { bad "pins: could not set up '$case'"; continue; }
  pinchk && bad "pins: '$case' passes" || ok "pins: '$case' fails"
done
restore
set_lock browser/camoufox/uv.lock playwright 1.63.0 && set_pyproj browser/camoufox/pyproject.toml playwright 1.63.0 && \
  set_lock controller/uv.lock playwright 1.63.0 && set_pyproj controller/pyproject.toml playwright 1.63.0
pinchk && ok "pins: a Playwright move in all four files passes" || bad "pins: a Playwright move in all four files fails"
restore
rm "$pins/controller/uv.lock"
pinchk && bad "pins: a missing lock passes" || ok "pins: a missing lock fails"


# ── image-tags.sh: the tags the build pushes and update-chart writes ──
tags() { bash "$here/image-tags.sh" "$@" 2>/dev/null; }
out="$(tags refs/heads/develop 2.4.0 2.6.0 1.0.0)"; rc=$?
want=$'CHROME_TAG=dev-chrome-2.4.0\nCONTROLLER_TAG=dev-controller-2.6.0\nCAMOUFOX_TAG=dev-camoufox-1.0.0'
[ $rc -eq 0 ] && [ "$out" = "$want" ] && ok "tags: develop -> dev-chrome-/dev-controller-/dev-camoufox-" || bad "tags: develop -> rc=$rc out=$out"
out="$(tags refs/heads/main 2.4.0 2.6.0 1.0.0)"; rc=$?
want=$'CHROME_TAG=chrome-2.4.0\nCONTROLLER_TAG=controller-2.6.0\nCAMOUFOX_TAG=camoufox-1.0.0'
[ $rc -eq 0 ] && [ "$out" = "$want" ] && ok "tags: main -> chrome-/controller-/camoufox-" || bad "tags: main -> rc=$rc out=$out"
for args in "refs/heads/camoufox-engine 2.4.0 2.6.0 1.0.0" "refs/tags/v1 2.4.0 2.6.0 1.0.0" \
            "refs/heads/develop  2.6.0 1.0.0" "refs/heads/develop 2.4.0 2.6 1.0.0" \
            "refs/heads/main 2.4.0 2.6.0 dev-1.0.0" "refs/heads/develop 2.4.0 2.6.0"; do
  # shellcheck disable=SC2086
  out="$(IFS=' '; tags $args)"; rc=$?
  [ $rc -ne 0 ] && [ -z "$out" ] && ok "tags: '$args' fails" || bad "tags: '$args' -> rc=$rc out=$out"
done
out="$(tags refs/heads/develop '' 2.6.0 1.0.0)"; rc=$?
[ $rc -ne 0 ] && [ -z "$out" ] && ok "tags: an empty version fails" || bad "tags: empty version -> rc=$rc out=$out"
# ci.yml takes every tag from it (the build and the chart update alike).
ci="$here/../workflows/ci.yml"
[ "$(grep -c 'bash .github/scripts/image-tags.sh' "$ci")" = 2 ] && ok "tags: ci.yml computes both sets with image-tags.sh" || bad "tags: ci.yml does not call image-tags.sh twice"
grep -nE '(dev-)?(chrome|controller|camoufox)-\$\{' "$ci" && bad "tags: ci.yml spells a tag inline" || ok "tags: ci.yml spells no tag inline"

exit $fail
