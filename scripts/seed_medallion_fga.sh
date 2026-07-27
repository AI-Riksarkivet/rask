#!/usr/bin/env bash
# Seed the medallion mover service-identity grants into OpenFGA, so the FGA-enforced movers
# (chart value medallion.fgaEnabled=true) are authorized to produce their target stage:
#   - the writer movers (raw→bronze, bronze→silver) get `writer` on the warehouse (→ can_create_table)
#   - the silver→gold mover gets `validator` on the gold namespace (→ can_promote)
# Revoke the last grant (`fga tuple delete ... validator namespace:gold`) to SEE the enforcement: the
# silver→gold mover is then denied and the cascade stops at silver — a plain writer cannot promote.
#
# Prereq: the catalog has provisioned the model, and OpenFGA is reachable. Port-forward first:
#   kubectl port-forward svc/lance-ns-openfga 8081:8080 &
#   OPENFGA_API_URL=http://localhost:8081 scripts/seed_medallion_fga.sh
set -euo pipefail

BIN="$(cd "$(dirname "$0")/.." && pwd)/.localbin"
API="${OPENFGA_API_URL:-http://localhost:8081}"
WAREHOUSE="warehouse:lance_catalog"

SID="$("$BIN/fga" store list --api-url "$API" \
  | python3 -c "import sys,json;print([s['id'] for s in json.load(sys.stdin)['stores'] if s['name']=='lance-catalog'][0])")"
echo "store: $SID"

# Idempotent write: a duplicate-tuple error is fine (re-run), ANY OTHER failure aborts the script
# (set -e) so callers see a non-zero exit — a blanket `|| true` here made the Makefile's seed-failure
# abort unreachable for grant-write failures (2026-07-10 review: model-not-provisioned / renamed
# relation failed every write while the script still printed "✓ seeded" and exited 0).
w() {
  local out
  if out=$("$BIN/fga" tuple write --api-url "$API" --store-id "$SID" "$@" 2>&1); then return 0; fi
  case "$out" in
    *already\ exists*|*duplicate*) return 0 ;;
    *) echo "!! seed write failed: $* — $out" >&2; return 1 ;;
  esac
}

# medallion stage namespaces under the warehouse (so the rung cascade reaches them) — the MEDIA lane's
# namespaces included: without them the media mover's can_create_table check on namespace:silver-media
# finds no parent chain and the governed cascade silently DROPs every media trigger (audit blocker).
w "$WAREHOUSE" parent namespace:raw
w "$WAREHOUSE" parent namespace:bronze
w "$WAREHOUSE" parent namespace:silver
w "$WAREHOUSE" parent namespace:gold
w "$WAREHOUSE" parent namespace:bronze-media
w "$WAREHOUSE" parent namespace:silver-media
# The cascade DATASETS' table→namespace parent links. The catalog seeds these for tables it creates, but
# the movers write Lance DIRECTLY — without a parent tuple on table:<dataset> nothing cascades to it, so
# under LINEAGE_FGA_ENABLED no human (not even a warehouse owner) can can_get_metadata a mover-produced
# dataset: the whole medallion estate is invisible in /runs, /datasets/*, /graph. Linking each dataset to
# its stage namespace restores the normal rung inheritance (warehouse reader → stage reader → table reader).
# INTENDED SIDE EFFECT (say it where the tuples are written): the parent links extend the FULL warehouse
# rung cascade, not just reads — a warehouse *writer* also gains can_write_data on every linked medallion
# table (that concentric inheritance is the model working as designed, not a leak). Grant warehouse rungs
# accordingly: humans who should only browse the estate get `reader`, never `writer`.
w namespace:raw parent 'table:raw_events'
w namespace:bronze parent 'table:bronze$events'
w namespace:silver parent 'table:silver$features'
w namespace:gold parent 'table:gold$catalog'
w namespace:bronze-media parent 'table:bronze-media$objects'
w namespace:silver-media parent 'table:silver-media$features'
# writer movers → can_create_table on their stage; the promoter mover → can_promote on gold
w user:service-raw-to-bronze writer "$WAREHOUSE"
w user:service-bronze-to-silver writer "$WAREHOUSE"
w user:service-media-to-silver writer "$WAREHOUSE"
w user:service-silver-to-gold validator namespace:gold

# --- Ray TRAIN (#115c, docs/RAY-TRAIN.md D5): the trainer's OWN identity + rung. Feature READER on the
# stages it consumes + WRITER on namespace:models ONLY — never the medallion writer rung (a trainer must
# not write stages; a mover must not write models). The models namespace parents under the warehouse so
# humans' warehouse-reader rung cascades to model registry datasets; per-model table→namespace parent
# links (namespace:models parent table:models$<name>) are written by the TRAINER CONSUMER at trigger
# time (#115b — idempotent, before the submit ack), exactly like the pre-seeded mover links above.
# Model PROMOTION (#17 candidate→blessed) stays behind the validator rung (not writer): the trainer WRITES
# candidate versions (writer namespace:models) but blessing one moves the `blessed` tag via the catalog
# POST /v1/model/<model>/promote endpoint, gated on can_promote = validator. A writer (incl. the trainer)
# is NOT a validator, so it is denied — exactly the silver→gold separation, reused for models. A `validator
# namespace:models` grant cascades to table:models$<model> via the per-model parent link the trainer seeds.
w "$WAREHOUSE" parent namespace:models
w user:service-trainer reader namespace:silver
w user:service-trainer reader namespace:gold
w user:service-trainer writer namespace:models
w user:service-blesser validator namespace:models

# The web BFF reads the WHOLE lineage graph as a service (no per-user login on the auth-on stack) — READER
# on the warehouse so its rung cascades to can_get_metadata on every dataset, exactly like a warehouse
# reader human. Read-only; never a writer. Pairs with LINEAGE_SERVICE_SUBJECTS + web.serviceIdentity.
w user:service-web reader "$WAREHOUSE"

echo "✓ seeded medallion grants (mover writers + media lane, silver→gold validator, trainer reader/models-writer, stage/table parent links) into store $SID"

# --- Per-TENANT enablement (#84, optional args: PROJECT [ZONE_WAREHOUSE]) -------------------------------
# A tenant cascade (`/produce?project=<p>`) targets the project-QUALIFIED namespaces (`<p>-bronze` …),
# which inherit NOTHING from the estate seed above — the movers are correctly denied and the trigger is
# dead-lettered (fail-closed, live-proven 2026-07-23). Enabling a tenant is exactly three tuple groups:
#   1. parent each `<p>-<stage>` namespace under the tenant's ZONE warehouse (its medallion bucket —
#      the registry resolves multiple actives to the LOWEST warehouse id; pass that one), so the
#      project's own admins/readers inherit visibility over their zone data;
#   2. the mover service rungs on the qualified target stages (same rungs as the estate seed);
#   3. the table→namespace parent links (movers write Lance directly; nothing else seeds tables).
# Media lanes stay estate-only (the media pipeline is not project-qualified — #84 scope).
PROJECT="${1:-}"
if [ -n "$PROJECT" ]; then
  ZONE_WH="${2:?usage: seed_medallion_fga.sh <project> <zone-warehouse-id> (the tenant medallion bucket warehouse)}"
  for ns in raw bronze silver gold; do
    w "warehouse:$ZONE_WH" parent "namespace:$PROJECT-$ns"
  done
  w user:service-raw-to-bronze writer "namespace:$PROJECT-bronze"
  w user:service-bronze-to-silver writer "namespace:$PROJECT-silver"
  w user:service-silver-to-gold validator "namespace:$PROJECT-gold"
  w "namespace:$PROJECT-raw" parent "table:$PROJECT-raw_events"
  w "namespace:$PROJECT-bronze" parent "table:$PROJECT-bronze\$events"
  w "namespace:$PROJECT-silver" parent "table:$PROJECT-silver\$features"
  w "namespace:$PROJECT-gold" parent "table:$PROJECT-gold\$catalog"
  echo "✓ enabled tenant '$PROJECT' medallion (zone warehouse:$ZONE_WH — stage parents, mover rungs, table links)"
fi
