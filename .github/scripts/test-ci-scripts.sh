#!/usr/bin/env bash
# Tests of the release scripts the pipeline runs (no registry, no network):
# the tag guard's three answers and the Chart.yaml updater's line rules.
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
up() { python3 "$here/update_chart.py" "$chart" --app "$1" --controller "$2" --camoufox "$3" --camoufox-api "$4" 2>/dev/null; }

out="$(up dev-2.3.0 dev-controller-2.5.0 dev-camoufox-1.0.0 dev-camoufox-api-1.0.0)"
want="$(cat "$tmp/orig"; printf '  camoufoxVersion: "dev-camoufox-1.0.0"\n  camoufoxApiVersion: "dev-camoufox-api-1.0.0"\n')"
[ "$out" = "changed=true" ] && [ "$(cat "$chart")" = "$want" ] && ok "chart: the camoufox annotations are inserted, every other line kept" || { bad "chart: insert"; diff <(echo "$want") "$chart"; }
grep -c '^appVersion: "dev-2.3.0"$' "$chart" | grep -qx 1 && grep -qx '  controllerVersion: "dev-controller-2.5.0"' "$chart" && ok "chart: appVersion and controllerVersion lines byte-identical" || bad "chart: chrome lines moved"
before="$(cat "$chart")"
out="$(up dev-2.3.0 dev-controller-2.5.0 dev-camoufox-1.0.0 dev-camoufox-api-1.0.0)"
[ "$out" = "changed=false" ] && [ "$(cat "$chart")" = "$before" ] && ok "chart: unchanged versions write nothing" || bad "chart: rerun -> $out"
out="$(up dev-2.3.0 dev-controller-2.6.0 dev-camoufox-1.0.0 dev-camoufox-api-1.0.0)"
[ "$out" = "changed=true" ] && [ "$(diff "$tmp/orig" "$chart" | grep -c '^[<>]')" = "4" ] && grep -qx '  controllerVersion: "dev-controller-2.6.0"' "$chart" && ok "chart: only the changed line is rewritten" || bad "chart: controller bump"
printf 'apiVersion: v2\nname: x\n' > "$tmp/broken.yaml"
python3 "$here/update_chart.py" "$tmp/broken.yaml" --app a --controller b --camoufox c --camoufox-api d >/dev/null 2>&1 && bad "chart: a file without appVersion is refused" || ok "chart: a file without appVersion is refused"

exit $fail
