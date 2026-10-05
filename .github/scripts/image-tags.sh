#!/usr/bin/env bash
# The three image tags of one push, the ones the build pushes and update-chart
# writes into the operator chart (both ci.yml steps call this, so they cannot
# drift apart):
#   image-tags.sh <ref> <chrome version> <controller version> <camoufox version>
# develop -> dev-chrome-<v>, dev-controller-<v>, dev-camoufox-<v>;
# main    -> chrome-<v>, controller-<v>, camoufox-<v>.
# Prints CHROME_TAG=, CONTROLLER_TAG= and CAMOUFOX_TAG= lines. Any other ref,
# or a version that is not X.Y.Z, fails (nothing is printed).
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: $0 <ref> <chrome> <controller> <camoufox>" >&2; exit 2; }
ref=$1
case "$ref" in
  refs/heads/develop) prefix=dev- ;;
  refs/heads/main) prefix= ;;
  *) echo "image-tags: no image tags for $ref (only develop and main build)" >&2; exit 1 ;;
esac
for v in "$2" "$3" "$4"; do
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "image-tags: '$v' is not a version X.Y.Z" >&2; exit 1; }
done
echo "CHROME_TAG=${prefix}chrome-$2"
echo "CONTROLLER_TAG=${prefix}controller-$3"
echo "CAMOUFOX_TAG=${prefix}camoufox-$4"
