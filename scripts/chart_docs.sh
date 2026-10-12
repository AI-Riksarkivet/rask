#!/usr/bin/env bash
# Generate chart/README.md from chart/values.yaml's `# --` comments and chart/README.md.gotmpl (helm-docs), and
# chart/values.schema.json from values.yaml's types and the same comments (helm-values-schema-json), or, with --check,
# fail when either committed file differs from what would be generated or when a row of the values table carries no
# description. Helm validates every install's and upgrade's merged values against the schema, so a mistyped value fails
# the render, not a pod.
set -euo pipefail

CHART="${CHART:-chart}"
# The pinned .localbin copy first: another helm-docs version renders another README, and the check would
# then answer differently per host. The Dagger gate excludes .localbin and pins the same version on PATH.
if [ -z "${HELM_DOCS:-}" ]; then
  if [ -x .localbin/helm-docs ]; then HELM_DOCS=.localbin/helm-docs; else HELM_DOCS="$(command -v helm-docs 2>/dev/null || true)"; fi
fi
[ -n "$HELM_DOCS" ] && [ -x "$HELM_DOCS" ] || { echo "!! helm-docs not found in .localbin or on PATH — run 'make bootstrap'" >&2; exit 1; }
if [ -z "${HELM_SCHEMA:-}" ]; then
  if [ -x .localbin/helm-schema ]; then HELM_SCHEMA=.localbin/helm-schema; else HELM_SCHEMA="$(command -v helm-schema 2>/dev/null || true)"; fi
fi
[ -n "$HELM_SCHEMA" ] && [ -x "$HELM_SCHEMA" ] || { echo "!! helm-schema not found in .localbin or on PATH — run 'make bootstrap'" >&2; exit 1; }

generated="$(mktemp)"
schema="$(mktemp)"
trap 'rm -f "$generated" "$schema"' EXIT
"$HELM_SCHEMA" --use-helm-docs --values "$CHART/values.yaml" --output "$schema" >/dev/null
# --dry-run prints the README on stdout and logs on stderr. -c/-g name the one chart, so the vendored
# subcharts under chart/charts are not documented in its place.
"$HELM_DOCS" --chart-search-root "$CHART" --chart-to-generate "$CHART" --dry-run --log-level error > "$generated"
[ -s "$generated" ] || { echo "!! helm-docs generated nothing for $CHART" >&2; exit 1; }

if [ "${1:-}" != "--check" ]; then
  cp "$generated" "$CHART/README.md"
  cp "$schema" "$CHART/values.schema.json"
  echo "wrote $CHART/README.md and $CHART/values.schema.json"
  exit 0
fi

if ! diff -q "$CHART/values.schema.json" "$schema" >/dev/null 2>&1; then
  echo "!! $CHART/values.schema.json is stale against $CHART/values.yaml — run 'make chart-docs'" >&2
  exit 1
fi
if ! diff -u "$CHART/README.md" "$generated"; then
  echo "!! $CHART/README.md is stale against $CHART/values.yaml or README.md.gotmpl — run 'make chart-docs'" >&2
  exit 1
fi
# A values row ends `| <description> |`; an empty description is a setting nobody described.
undocumented="$(sed -n '/^## Values/,$p' "$generated" | grep -E '^\| .* \|  \|$' || true)"
if [ -n "$undocumented" ]; then
  echo "!! values with no '# --' description in $CHART/values.yaml:" >&2
  echo "$undocumented" | cut -d'|' -f2 >&2
  exit 1
fi
echo "chart-docs-check: $CHART/README.md and values.schema.json are current and every value is described"
