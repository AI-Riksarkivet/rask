#!/usr/bin/env bash
# Clean-slate the lineage graph for a fresh demo. DESTRUCTIVE: removes EVERY node from the AGE
# 'lineage' graph (all accumulated runs/datasets/columns from every past test on this cluster).
#
# Safe from rebuild: the reconcile cron only back-fills datasets ALREADY in the graph, so an empty graph
# gives it nothing to rebuild; the delivery consumers are caught up at their ack floor, so old events do
# not replay. New `make produce` cascades repopulate a CLEAN graph — which also fixes the frontend lag,
# since the explorer then renders + polls ~6 nodes instead of ~500.
#
# Run on the box that can reach the cluster:  bash scripts/reset_lineage_graph.sh   (or: make reset-lineage)
set -euo pipefail

BIN="$(cd "$(dirname "$0")/.." && pwd)/.localbin"
KC="${KUBECTL:-kubectl}"
command -v "$KC" >/dev/null 2>&1 || KC="$BIN/kubectl"

before=$("$KC" exec lance-ns-age-0 -- psql -U lance -d lineage -tAc \
  "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('lineage', \$\$ MATCH (n) RETURN count(n) \$\$) AS (n agtype);" 2>/dev/null | tail -1)
echo "lineage graph nodes before: ${before:-?}"

"$KC" exec lance-ns-age-0 -- psql -U lance -d lineage -c \
  "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('lineage', \$\$ MATCH (n) DETACH DELETE n \$\$) AS (r agtype);"

after=$("$KC" exec lance-ns-age-0 -- psql -U lance -d lineage -tAc \
  "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('lineage', \$\$ MATCH (n) RETURN count(n) \$\$) AS (n agtype);" 2>/dev/null | tail -1)
echo "lineage graph nodes after:  ${after:-?}"
echo "✓ clean slate — now run 'make produce' and refresh the frontend for a crisp raw→bronze→silver→gold graph."
