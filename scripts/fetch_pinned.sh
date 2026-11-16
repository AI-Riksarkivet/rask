#!/usr/bin/env bash
# fetch_pinned.sh URL OUT: download URL to OUT once its sha256 is the one scripts/toolchain.sha256 pins for that exact URL.
# A URL with no pin, or bytes that do not match it, leave nothing at OUT and fail: a binary a script runs is the one
# reviewed in git, not whatever its host serves that day.
set -euo pipefail
url="$1"
out="$2"
pins="$(dirname "$0")/toolchain.sha256"
want="$(awk -v url="$url" '$2 == url { print $1 }' "$pins")"
[ -n "$want" ] || { echo "!! no pinned digest for $url in $pins" >&2; exit 1; }
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL -o "$tmp" "$url"
# sha256sum on Linux, shasum on macOS, which the bootstrap also serves.
got="$({ sha256sum "$tmp" 2>/dev/null || shasum -a 256 "$tmp"; } | awk '{ print $1 }')"
[ "$got" = "$want" ] || { echo "!! $url is $got, not its pinned $want" >&2; exit 1; }
mv "$tmp" "$out"
trap - EXIT
