#!/usr/bin/env bash
# Generate chart/README.md from chart/values.yaml's `# --` comments and chart/README.md.gotmpl (helm-docs),
# or, with --check, fail when the committed README differs from what would be generated or when a row of
# its values table carries no description.
set -euo pipefail

CHART="${CHART:-chart}"
# The pinned .localbin copy first: another helm-docs version renders another README, and the check would
# then answer differently per host. The Dagger gate excludes .localbin and pins the same version on PATH.
if [ -z "${HELM_DOCS:-}" ]; then
  if [ -x .localbin/helm-docs ]; then HELM_DOCS=.localbin/helm-docs; else HELM_DOCS="$(command -v helm-docs 2>/dev/null || true)"; fi
fi
[ -n "$HELM_DOCS" ] && [ -x "$HELM_DOCS" ] || { echo "!! helm-docs not found in .localbin or on PATH — run 'make bootstrap'" >&2; exit 1; }

generated="$(mktemp)"
trap 'rm -f "$generated"' EXIT
# --dry-run prints the README on stdout and logs on stderr. -c/-g name the one chart, so the vendored
# subcharts under chart/charts are not documented in its place.
"$HELM_DOCS" --chart-search-root "$CHART" --chart-to-generate "$CHART" --dry-run --log-level error > "$generated"
[ -s "$generated" ] || { echo "!! helm-docs generated nothing for $CHART" >&2; exit 1; }

if [ "${1:-}" != "--check" ]; then
  cp "$generated" "$CHART/README.md"
  echo "wrote $CHART/README.md"
  exit 0
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
echo "chart-docs-check: $CHART/README.md is current and every value is described"
