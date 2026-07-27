#!/usr/bin/env bash
# AGE restore drill (prod-readiness P4) — RUN INSIDE the age-postgres pod (it has psql/pg_dump; the Dagger
# lineage-e2e AGE is not reachable from the runner). Proves the exact hazard docs/RUNBOOK-restore.md warns
# about: does a plain `pg_dump` of an Apache AGE database, restored into a fresh DB, KEEP the graph's labels
# and vertices — or does it come back with the ag_catalog metadata but no data (the known plain-pg_dump-of-AGE
# trap)? Self-contained on a throwaway `restore_drill` DB, so it never touches the real lineage graph.
# Exit 1 (labels lost) is a real, valuable finding: switch the backup to an AGE-aware dump, then re-drill.
set -euo pipefail

export PGUSER="${POSTGRES_USER:-lineage}" PGPASSWORD="${POSTGRES_PASSWORD:-lineage}"
DRILL=restore_drill

cleanup() { psql -d postgres -c "DROP DATABASE IF EXISTS $DRILL WITH (FORCE);" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 1. A fresh DB with a real AGE graph holding one labeled vertex (stand-in for the lineage graph's Datasets).
psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DRILL WITH (FORCE);" -c "CREATE DATABASE $DRILL;"
psql -d "$DRILL" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION age;"
psql -d "$DRILL" -v ON_ERROR_STOP=1 <<'SQL'
LOAD 'age'; SET search_path = ag_catalog, "$user", public;
SELECT create_graph('drilltest');
SELECT * FROM cypher('drilltest', $$ CREATE (:Dataset {name:'silver_features'}) $$) AS (v agtype);
SQL

# 2. Back it up exactly as backup-pg.yaml does — a plain pg_dump (+ gzip).
pg_dump -d "$DRILL" | gzip > /tmp/drill.sql.gz

# 3. Simulate disaster + restore per RUNBOOK-restore.md: drop, recreate, restore the dump (a full pg_dump
#    of an AGE DB carries the CREATE EXTENSION + graph, so restore into a clean DB — do NOT pre-create it).
psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE $DRILL WITH (FORCE);" -c "CREATE DATABASE $DRILL;"
gunzip -c /tmp/drill.sql.gz | psql -d "$DRILL" -v ON_ERROR_STOP=1

# 4. VERIFY the graph came back with its labels + vertex — not just the ag_catalog metadata rows. Use a
#    QUOTED heredoc (no shell $$-PID expansion of the Cypher dollar-quotes) + tail -1 so the count is the
#    LAST output line, not the "LOAD"/"SET" command output that a multi-statement psql -c prints first.
#    2>/dev/null + || true: if the graph is genuinely GONE (the hazard), the Cypher errors — we want an
#    empty count → a clean "not 1" verdict below, not a mid-script set -e abort.
n=$(psql -tA -d "$DRILL" 2>/dev/null <<'SQL' | tail -1
LOAD 'age';
SET search_path = ag_catalog, public;
SELECT count(*) FROM cypher('drilltest', $$ MATCH (d:Dataset) RETURN d $$) AS (d agtype);
SQL
) || true
echo "restored-Dataset-vertex-count=${n:-<none>}"
[ "$n" = "1" ] || {
  echo "!! AGE restore drill FAILED: the graph came back with $n Dataset vertices, expected 1 — a plain"
  echo "   pg_dump of AGE did NOT round-trip the labels. Switch backup-pg.yaml to an AGE-aware dump."
  exit 1
}
echo "OK: AGE pg_dump -> restore preserved the graph labels + data (RUNBOOK-restore.md proven)"
