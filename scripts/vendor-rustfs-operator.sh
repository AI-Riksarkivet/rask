#!/usr/bin/env bash
# Vendors the RustFS operator Helm chart from github.com/rustfs/operator into
# third_party/rustfs-operator/ (chart/charts/ is gitignored, so the committed
# source lives here and helm dependency build packages it from a file:// repo).
set -euo pipefail
REF="${1:-main}"
DEST="third_party/rustfs-operator"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> cloning rustfs/operator @ ${REF}"
git clone --depth 1 --branch "$REF" https://github.com/rustfs/operator "$TMP/op" 2>/dev/null \
  || git clone "https://github.com/rustfs/operator" "$TMP/op"
( cd "$TMP/op" && git checkout "$REF" >/dev/null 2>&1 || true )
SHA="$(cd "$TMP/op" && git rev-parse HEAD)"

test -d "$TMP/op/deploy/rustfs-operator" || { echo "ERROR: deploy/rustfs-operator not found at $REF"; exit 1; }
rm -rf "$DEST"; mkdir -p "$DEST"
cp -R "$TMP/op/deploy/rustfs-operator/." "$DEST/"
printf 'ref: %s\ncommit: %s\n' "$REF" "$SHA" > "$DEST/.vendored-ref"
echo ">> vendored to ${DEST} (commit ${SHA}). Review 'git diff' before committing."
