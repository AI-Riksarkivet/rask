#!/usr/bin/env bash
# tla_check.sh [--full] [Module...]: check every model under specs/ with TLC, the TLA+ model checker (ADR 0221), or only
# the named modules (`scripts/tla_check.sh CommitPath`).
#
# A model is specs/<area>/<Module>.tla, checked once per <Module>*.cfg beside it. A config's first line says how:
#   "EXPECT VIOLATION <Invariant>"  the model must FAIL that invariant: the config removes an assumption the design
#                                   rests on, and the failure is the evidence that it rests on it;
#   "EXPECT VIOLATION temporal"     the same for a property over a whole behaviour, which TLC reports without a name;
#   "ON DEMAND"                     too large for every run: checked only with --full;
#   anything else                   the model must hold.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
jar="$root/.localbin/tla2tools.jar"
full=false
[ "${1:-}" = "--full" ] && { full=true; shift; }
only=" $* "
command -v java >/dev/null || { echo "!! java is not on PATH: TLC needs a Java runtime" >&2; exit 1; }
if [ ! -f "$jar" ]; then
  mkdir -p "$root/.localbin"
  bash "$root/scripts/fetch_pinned.sh" "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar" "$jar"
fi

failed=0
for model in "$root"/specs/*/*.tla; do
  module="$(basename "$model" .tla)"
  [ "$only" = "  " ] || [[ "$only" == *" $module "* ]] || continue
  for config in "${model%.tla}"*.cfg; do
    name="${config#"$root"/}"
    first="$(head -1 "$config")"
    if [[ "$first" == *"ON DEMAND"* ]] && ! $full; then
      echo "skipped   $name (on demand: make tla-check-full)"
      continue
    fi
    work="$(mktemp -d)"
    # TLC exits non-zero on a violation, which an expected one is: its output decides, not its status.
    out="$(cd "$(dirname "$model")" && java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers "${TLC_WORKERS:-auto}" -deadlock -metadir "$work" -config "$(basename "$config")" "$(basename "$model")" 2>&1 || true)"
    rm -rf "$work"
    states="$(grep -oE '[0-9,]+ distinct states found' <<<"$out" | tail -1 || true)"
    if [[ "$first" =~ EXPECT\ VIOLATION\ ([A-Za-z]+) ]]; then
      expected="${BASH_REMATCH[1]}"
      reported="Invariant $expected is violated"
      [ "$expected" = "temporal" ] && reported="Temporal properties were violated"
      if grep -q "$reported" <<<"$out"; then
        echo "fails as expected  $name ($expected)"
      else
        echo "!! $name was expected to violate $expected and did not" >&2
        failed=1
      fi
    elif grep -q "Model checking completed. No error has been found." <<<"$out"; then
      echo "holds     $name ($states)"
    else
      echo "!! $name does not hold:" >&2
      grep -E '^Error|is violated|line [0-9]+, col' <<<"$out" | head -5 >&2
      failed=1
    fi
  done
done
exit $failed
