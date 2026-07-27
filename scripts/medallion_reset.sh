#!/usr/bin/env bash
# Reset the live demo to a CLEAN SLATE so you can drive it yourself from empty:
#   - wipes the Lance tables on S3 (RustFS)
#   - empties the lineage graph (Apache AGE) AND the durable events feed (lineage_events table)
#   - restarts lineage-api (recreates the empty events table + reconnects)
#
# Just run it — it reads .medallion-demo.env (written when the stack started) for the endpoints:
#   ./scripts/medallion_reset.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer the endpoints recorded when the stack started; else DEMO_*_PORT; else defaults.
if [ -f .medallion-demo.env ]; then
	set -a
	# shellcheck disable=SC1091
	. ./.medallion-demo.env
	set +a
fi
S3_ENDPOINT="${S3_ENDPOINT:-http://localhost:${DEMO_S3_PORT:-9000}}"
LINEAGE_URL="${LINEAGE_URL:-http://localhost:${DEMO_LINEAGE_PORT:-8000}}"

echo "== 1) wipe the Lance tables on S3 (${S3_ENDPOINT}) =="
S3_ENDPOINT="$S3_ENDPOINT" S3_ACCESS_KEY="${S3_ACCESS_KEY:-rustfsadmin}" \
	S3_SECRET_KEY="${S3_SECRET_KEY:-rustfsadmin}" S3_BUCKET="${S3_BUCKET:-lakehouse}" \
	uv run --no-sync scripts/medallion_demo.py --reset

echo "== 2) empty the lineage graph (Apache AGE) + the durable events feed =="
docker exec lance-lineage-postgres psql -U lineage -d lineage -tA \
	-c "LOAD 'age'; SET search_path = ag_catalog, \"\$user\", public; SELECT drop_graph('lineage', true); SELECT create_graph('lineage'); DROP TABLE IF EXISTS public.lineage_events;" >/dev/null

echo "== 3) recreate the events table + reconnect (restart lineage-api) =="
docker restart lance-lineage-api >/dev/null
until curl -fsS "${LINEAGE_URL}/livez" >/dev/null 2>&1; do sleep 2; done

echo
echo "== clean slate. Refresh the UI, then you are the producer: =="
echo "   uv run scripts/medallion_demo.py --step 1   # then --step 2, 3, 4, 5"
