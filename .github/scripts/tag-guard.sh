#!/usr/bin/env bash
# Image tags are immutable: a tag is built only when the registry says, for
# certain, that it does not exist yet.
#
#   tag-guard.sh <image:tag>      prints "build=true" or "build=false"
#
# - the tag answers (exit 0)                  -> build=false ("tag exists, skipped")
# - a definite "not found" / "manifest unknown" -> build=true
# - anything else (rate limit, auth, network)  -> exit 1: the job fails, it
#   never builds (and re-pushes over a running tag) on an unclear answer.
#
# Pods pull their tags Always, so re-pushing an existing tag would roll new
# bits into every running browser at its next restart.
set -u
ref="${1:?usage: tag-guard.sh <image:tag>}"
inspect="${TAG_GUARD_INSPECT:-docker buildx imagetools inspect}"
err="$(mktemp)"
trap 'rm -f "$err"' EXIT
if $inspect "$ref" >/dev/null 2>"$err"; then
  echo "tag exists, skipped: $ref" >&2
  echo "build=false"
  exit 0
fi
# The registry's own words for a tag that is not there. "pull access
# denied, repository does not exist or may require authorization" is NOT one:
# it is what a missing login says too.
if grep -qE '(: not found$)|manifest unknown' "$err"; then
  echo "tag absent, building: $ref" >&2
  echo "build=true"
  exit 0
fi
echo "tag guard: the registry gave no clear answer for $ref; refusing to build:" >&2
sed 's/^/  /' "$err" >&2
exit 1
