#!/usr/bin/env bash
# Seed the medallion service-identity grants into OpenFGA, so the FGA-enforced stage runners
# (chart value medallion.fgaEnabled=true) are authorized to produce their target stage (R23: the
# governed tiers are bronze/silver/gold — raw is the external world and owns no namespace):
#   - the bronze ingest head (medallion-producer, the producer) + the bronze→silver / media stage runners get `writer`
#     on the warehouse (→ can_create_table)
#   - the silver→gold stage runner gets `validator` on the gold namespace (→ can_promote)
# Revoke the last grant (`fga tuple delete ... validator namespace:gold`) to SEE the enforcement: the
# silver→gold stage runner is then denied and the cascade stops at silver — a plain writer cannot promote.
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

# BOTH directions of one parent -> child link. `w <parent> parent <child>` alone is what this script
# used to do, and a forward-only edge is a tuple OpenFGA accepts and no rule ever reads back: C1's
# `can_get_metadata: reader or can_get_metadata from child` needs the INVERSE stored, because OpenFGA
# cannot walk a tuple backwards. Without it a grantee on one table can read it and cannot see the
# namespace or warehouse containing it — their own breadcrumb 403s and every list above comes back
# empty, which is the exact symptom C1 was written to cure.
#
# The inverse goes only on parents whose type declares `child` (warehouse, namespace) — the shell
# twin of `_CHILD_EDGE_PARENT_TYPES` in service_kit.governed.fga, whose `hierarchy_edge_tuples` owns
# the same pairing for every Python writer. Adding `child` to a type in model.fga means adding it in
# both places.
#
# Idempotent: `w` already swallows "already exists", so re-running this over an estate that has some
# of these is the normal case — which is also how a pre-C1 estate is BACKFILLED. There is no other
# backfill path; `revoke_object_tuples` reconstructs and deletes the inverse on drop, so nothing is
# orphaned by adding them.
link() {
  local parent="$1" child="$2"
  w "$parent" parent "$child"
  case "${parent%%:*}" in
    warehouse|namespace) w "$child" child "$parent" ;;
  esac
}

# medallion stage namespaces under the warehouse (so the rung cascade reaches them) — the MEDIA lane's
# namespaces included: without them the media stage runner's can_create_table check on namespace:silver-media
# finds no parent chain and the governed cascade silently DROPs every media trigger (audit blocker).
link "$WAREHOUSE" namespace:bronze
link "$WAREHOUSE" namespace:silver
link "$WAREHOUSE" namespace:gold
link "$WAREHOUSE" namespace:bronze-media
link "$WAREHOUSE" namespace:silver-media
# The cascade DATASETS' table→namespace parent links. The catalog seeds these for tables it creates, but
# the stage runners write Lance DIRECTLY — without a parent tuple on table:<dataset> nothing cascades to it, so
# under RASK_FGA_ENABLED no human (not even a warehouse owner) can can_get_metadata a stage runner-produced
# dataset: the whole medallion estate is invisible in /runs, /datasets/*, /graph. Linking each dataset to
# its stage namespace restores the normal rung inheritance (warehouse reader → stage reader → table reader).
# INTENDED SIDE EFFECT (say it where the tuples are written): the parent links extend the FULL warehouse
# rung cascade, not just reads — a warehouse *writer* also gains can_write_data on every linked medallion
# table (that concentric inheritance is the model working as designed, not a leak). Grant warehouse rungs
# accordingly: humans who should only browse the estate get `reader`, never `writer`.
link namespace:bronze 'table:bronze$events'
link namespace:bronze 'table:bronze$pages'
link namespace:silver 'table:silver$features'
link namespace:gold 'table:gold$catalog'
link namespace:bronze-media 'table:bronze-media$objects'
link namespace:silver-media 'table:silver-media$features'
# writers → can_create_table on their stage; the promoter stage runner → can_promote on gold. The bronze
# ingest head writes as the PRODUCER's identity (medallion-producer) — the retired raw→bronze stage runner's writer
# rung moved here with the collapse (R23).
w user:service-medallion-producer writer "$WAREHOUSE"
# The INGEST plane writes bronze too, and it was never seeded. `services/ingest` is the P7a
# acquisition head — it creates the bronze table and commits the fragments — so it needs the same rung
# the producer has. Without it every run reached the catalog fully AUTHENTICATED and was refused on
# authorization alone (`403 can_get_metadata required on table:…`), which reads like a broken door
# rather than a missing grant.
w user:service-ingest writer "$WAREHOUSE"
w user:service-bronze-to-silver writer "$WAREHOUSE"
w user:service-media-to-silver writer "$WAREHOUSE"
# THREE RUNGS, AND EACH IS A MEASURED FINDING RATHER THAN A PREFERENCE. Granting any ONE of them
# fails, and each failure looks like a different bug:
#   `writer`    — `can_create_table` and `can_write_data`. Without it the silver->gold stage runner is
#                 refused `describe` AND `create` on its own tier (measured 2026-08-26 on the live
#                 estate, when this file granted `validator` alone).
#   `publisher` — `can_update_tag` + `can_create_tag`, which is what `publish` is guarded by. This rung
#                 did not exist until 2026-09-10; the grant was `owner`, and buying one capability at
#                 the owner bar is what put `can_drop`/`can_deregister`/`can_restore`/`manage_grants`
#                 on every tenant's data (LH-052).
#   `validator` — `can_promote`, the SECOND door on `/publish`, taken only when the body carries
#                 `accept_assertions`. That is the producer resuming a promotion a person approved.
#
# KEEP THIS IN STEP WITH `catalog/api/fga_deps.py::_CASCADE_RUNGS`. Two seeders disagreeing about one
# identity's rung is how a denial read as a permissions mystery for an hour, and it is why the catalog's
# create path and its backfill were collapsed onto one builder. This script seeds the DEMO namespaces
# directly and cannot share that builder, so the rungs are restated here with the reason attached —
# never `owner`.
w user:service-silver-to-gold writer namespace:gold
w user:service-silver-to-gold publisher namespace:gold
w user:service-silver-to-gold validator namespace:gold
# The PRODUCER publishes the promotion a person approved — `publish_promotion` runs in ITS process,
# because the workflow instance and the approve door must share an app-id for `raise_workflow_event`
# to resolve. Without these the review path ends in `403 can_update_tag` AFTER someone said yes.
w user:service-medallion-producer writer namespace:silver
w user:service-medallion-producer publisher namespace:silver
w user:service-medallion-producer validator namespace:silver
w user:service-medallion-producer writer namespace:gold
w user:service-medallion-producer publisher namespace:gold
w user:service-medallion-producer validator namespace:gold

# --- Ray TRAIN (#115c, docs/RAY-TRAIN.md D5): the trainer's OWN identity + rung. Feature READER on the
# stages it consumes + WRITER on namespace:models ONLY — never the medallion writer rung (a trainer must
# not write stages; a stage runner must not write models). The models namespace parents under the warehouse so
# humans' warehouse-reader rung cascades to model registry datasets; per-model table→namespace parent
# links (namespace:models parent table:models$<name>) are written by the TRAINER CONSUMER at trigger
# time (#115b — idempotent, before the submit ack), exactly like the pre-seeded stage runner links above.
# Model PROMOTION (#17 candidate→blessed) stays behind the validator rung (not writer): the trainer WRITES
# candidate versions (writer namespace:models) but blessing one moves the `blessed` tag via the catalog
# POST /v1/model/<model>/promote endpoint, gated on can_promote = validator. A writer (incl. the trainer)
# is NOT a validator, so it is denied — exactly the silver→gold separation, reused for models. A `validator
# namespace:models` grant cascades to table:models$<model> via the per-model parent link the trainer seeds.
link "$WAREHOUSE" namespace:models
w user:service-trainer reader namespace:silver
w user:service-trainer reader namespace:gold
w user:service-trainer writer namespace:models
w user:service-blesser validator namespace:models

# The web BFF reads the WHOLE lineage graph as a service (no per-user login on the auth-on stack) — READER
# on the warehouse so its rung cascades to can_get_metadata on every dataset, exactly like a warehouse
# reader human. Read-only; never a writer. Pairs with LINEAGE_SERVICE_SUBJECTS + web.serviceIdentity.

echo "✓ seeded medallion grants (stage runner writers + media lane, silver→gold validator, trainer reader/models-writer, stage/table parent links) into store $SID"

# --- Per-TENANT enablement (#84, optional args: PROJECT [ZONE_WAREHOUSE]) -------------------------------
# A tenant cascade (`/produce?project=<p>`) targets the project-QUALIFIED namespaces (`<p>-bronze` …),
# which inherit NOTHING from the estate seed above — the stage runners are correctly denied and the trigger is
# dead-lettered (fail-closed, live-proven 2026-07-23). Enabling a tenant is exactly three tuple groups:
#   1. parent each `<p>-<stage>` namespace under the tenant's ZONE warehouse (its medallion bucket —
#      the registry resolves multiple actives to the LOWEST warehouse id; pass that one), so the
#      project's own admins/readers inherit visibility over their zone data;
#   2. the stage runner service rungs on the qualified target stages (same rungs as the estate seed);
#   3. the table→namespace parent links (stage runners write Lance directly; nothing else seeds tables).
# Media lanes stay estate-only (the media pipeline is not project-qualified — #84 scope).
PROJECT="${1:-}"
if [ -n "$PROJECT" ]; then
  ZONE_WH="${2:?usage: seed_medallion_fga.sh <project> <zone-warehouse-id> (the tenant medallion bucket warehouse)}"
  for ns in bronze silver gold; do
    link "warehouse:$ZONE_WH" "namespace:$PROJECT-$ns"
  done
  w user:service-medallion-producer writer "namespace:$PROJECT-bronze"
  w user:service-ingest writer "namespace:$PROJECT-bronze"
  # THE SAME THREE RUNGS the estate-level block grants, never `owner` — see the reasoning there.
  # This block is per-TENANT and was missed on the first pass of that narrowing (2026-09-10); the
  # estate block alone would have left every project-qualified tenant over-granted while the change
  # looked applied. `test_seeders_agree_on_stage_runner_rungs.py` is what caught it.
  for rung in writer publisher validator; do
    w "user:service-bronze-to-silver" "$rung" "namespace:$PROJECT-silver"
    w "user:service-silver-to-gold" "$rung" "namespace:$PROJECT-gold"
    # The producer completes an approved hold in its own process — see the note above the
    # single-tenant rungs. Omitted here until 2026-08-26, so a TENANT's approved promotion 403'd.
    w "user:service-medallion-producer" "$rung" "namespace:$PROJECT-silver"
    w "user:service-medallion-producer" "$rung" "namespace:$PROJECT-gold"
  done
  # NO READ GRANT FOR `service-web` — its absence is the control (Q17-8). That identity is what
  # `bff.ts` sends when there is NO session, so granting it reader on a tenant's tiers grants the
  # PUBLIC those tiers. Four such grants were seeded here, and this script is a documented
  # production prerequisite (`values-prod.yaml`). A human reads with their own bearer.
  link "namespace:$PROJECT-bronze" "table:$PROJECT-bronze\$events"
  # The ingest lane's table. `INGEST_TABLE` because the ETL form lets a user name it, unlike the
  # producer's fixed `events` lane — pass it when seeding a tenant whose first ingest is not `pages`.
  link "namespace:$PROJECT-bronze" "table:$PROJECT-bronze\$${INGEST_TABLE:-pages}"
  # SILVER'S TABLES ARE A LIST, not the single `features` this seeded. A silver namespace holds one
  # table per LANE, and the link is what makes the stage runner's warehouse-level writer rung reach the
  # table it writes — without it the lane fails with a message naming exactly the missing link.
  # Measured: the dummy lane's e2e could not reach its terminal-event assertion because
  # `namespace:<p>-silver -> table:<p>-silver$dummy` was never written.
  #
  # A VARIABLE rather than another hard-coded row, for the reason `INGEST_TABLE` above is one, and a
  # sharper one here: `dummy` is a WORKLOAD name, and the platform is not supposed to know any. A new
  # lane is a value, not an edit to this script.
  for silver_table in ${SILVER_TABLES:-features dummy}; do
    link "namespace:$PROJECT-silver" "table:$PROJECT-silver\$$silver_table"
  done
  link "namespace:$PROJECT-gold" "table:$PROJECT-gold\$catalog"
  echo "✓ enabled tenant '$PROJECT' medallion (zone warehouse:$ZONE_WH — stage parents, stage runner rungs, table links)"
fi
