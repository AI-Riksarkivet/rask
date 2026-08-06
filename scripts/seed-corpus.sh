#!/usr/bin/env bash
# Seed the demo corpus into the volume the media plane ACTUALLY reads.
#
# WHY THIS EXISTS, and why it does not simply declare a volume of its own.
#
# The corpus was seeded on 2026-08-03 and the estate looked empty until 2026-08-06. Nothing was
# broken. A hand-applied Job mounted `corpus` as a hostPath (/home/blackwell/media-corpus) and wrote
# a real corpus into it — 10 chunk rows, 3 documents, an FTS index. The viewer mounted `corpus` as an
# emptyDir, read it honestly, and answered `{"datasets": []}`. Same volume NAME, different SOURCE.
# The seed exited 0. The viewer stayed 1/1 Ready and answered 200. Every probe was green.
#
# A failure that lives in the RELATION between two manifests cannot be caught by either one. So this
# script does not describe the corpus volume at all: it READS the volume source off the deployed
# viewer and mounts that. The writer therefore cannot target anything the readers do not read —
# not because someone kept two files in sync, but because there is only one source and the reader
# owns it.
#
# It also REFUSES to seed an emptyDir, which is the chart's default. Writing a corpus into a
# per-pod scratch directory succeeds, reports success, and is gone at the next restart — the exact
# shape of the bug this replaces, and worth a loud refusal rather than a green run.
#
#   make seed-corpus                 # seed the demo corpus
#   SEED_DATASET_ID=my_corpus …      # under a different dataset id
set -euo pipefail

RELEASE="${RELEASE:-rask}"
NAMESPACE="${NAMESPACE:-default}"
DATASET_ID="${SEED_DATASET_ID:-transcripts_v2}"
KUBECTL="${KUBECTL:-kubectl}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEED_SCRIPT="$REPO_ROOT/scripts/seed_demo_corpus.py"
JOB="$RELEASE-seed-corpus"
CM="$RELEASE-seed-corpus"

k() { "$KUBECTL" -n "$NAMESPACE" "$@"; }

[ -r "$SEED_SCRIPT" ] || { echo "!! no seed script at $SEED_SCRIPT" >&2; exit 1; }

VIEWER="deploy/$RELEASE-viewer"
if ! k get "$VIEWER" >/dev/null 2>&1; then
	echo "!! $VIEWER not found." >&2
	echo "   The media plane is off. Bring it up with: make k3s-up   (EXPLORER=true is the default)" >&2
	exit 1
fi

# ---- take the volume source, the mount path and the image FROM THE READER ------------------------
# Three things the writer must not invent. The image matters as much as the volume: the previous Job
# ran `localhost:5000/lance-rest-catalog:tilt-build-1785349317`, a tag from a build system that was
# removed on 2026-08-04 — so the only path that had ever put data in this estate could no longer be
# re-run at all.
read -r VOLUME_JSON MOUNT_PATH IMAGE VIEWER_PORT < <(
	k get "$VIEWER" -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)["spec"]["template"]["spec"]
vol = next((v for v in d["volumes"] if v["name"] == "corpus"), None)
if vol is None:
    sys.exit("!! the viewer has no volume named `corpus` — the chart changed shape; update this script")
c = d["containers"][0]
mount = next((m for m in c["volumeMounts"] if m["name"] == "corpus"), None)
if mount is None:
    sys.exit("!! the viewer mounts no `corpus` volume")
# The port is READ, never assumed: the viewer listens on 8101, and a hardcoded 8000 here made the
# final check fail with a connection error that reads exactly like "the corpus is still missing".
port = (c.get("ports") or [{}])[0].get("containerPort", 8101)
# compact separators: this line is consumed by a whitespace `read`, and json.dumps default
# separators put a space after every `:` and `,` — which split the volume across four fields.
print(json.dumps(vol, separators=(",", ":")), mount["mountPath"], c["image"], port)
'
)

KIND="$(printf '%s' "$VOLUME_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin); print(next(k for k in v if k != "name"))')"

echo ">> release=$RELEASE  dataset=$DATASET_ID"
echo ">> corpus volume (as the viewer mounts it): $KIND -> $MOUNT_PATH"
echo ">> image (the viewer's own): $IMAGE"

if [ "$KIND" = "emptyDir" ]; then
	cat >&2 <<-EOF

		!! REFUSING to seed an emptyDir.

		   An emptyDir is per-pod scratch. The seed would write a real corpus, exit 0, and the data
		   would be unreachable from the viewer's own pod and gone at the next restart. That is the
		   exact failure this script exists to end, so it is a refusal and not a warning.

		   Give the corpus a volume that outlives a pod, then re-run:

		       make k3s-up CORPUS=pvc     # chart-owned PVC (helm.sh/resource-policy: keep)
		       make seed-corpus

	EOF
	exit 2
fi

# ---- the script travels as a ConfigMap; scripts/seed_demo_corpus.py stays the ONE copy ------------
# Not baked into the catalog image on purpose: this is a dev FIXTURE, and the sanctioned ingestion
# path for real data is the medallion producer's POST /ingest-iiif. A production image should not
# carry a demo seeder.
k delete job "$JOB" --ignore-not-found >/dev/null
k create configmap "$CM" --from-file="seed_demo_corpus.py=$SEED_SCRIPT" \
	--dry-run=client -o yaml | k apply -f - >/dev/null

python3 - "$VOLUME_JSON" "$MOUNT_PATH" "$IMAGE" "$JOB" "$CM" "$DATASET_ID" <<'PY' | k apply -f - >/dev/null
import json, sys
volume, mount, image, job, cm, dataset = sys.argv[1:7]
manifest = {
    "apiVersion": "batch/v1", "kind": "Job",
    "metadata": {"name": job},
    "spec": {
        "backoffLimit": 0,
        "template": {"spec": {
            "restartPolicy": "Never",
            # The catalog image runs as uid 1000; the corpus must be writable by it.
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
            "containers": [{
                "name": "seed",
                "image": image,
                "command": ["python", "/seed/seed_demo_corpus.py", mount],
                "env": [{"name": "SEED_DATASET_ID", "value": dataset}],
                # Writable here — the readers mount the same source read-only, which is the point.
                "volumeMounts": [
                    {"name": "corpus", "mountPath": mount},
                    {"name": "seed", "mountPath": "/seed"},
                ],
            }],
            "volumes": [json.loads(volume), {"name": "seed", "configMap": {"name": cm}}],
        }},
    },
}
print(json.dumps(manifest))
PY

echo ">> seeding…"
k wait --for=condition=complete --timeout=300s "job/$JOB" 2>/dev/null &
WAIT_PID=$!
k wait --for=condition=failed --timeout=300s "job/$JOB" 2>/dev/null && FAILED=1 &
FAIL_PID=$!
wait -n "$WAIT_PID" "$FAIL_PID" 2>/dev/null || true
kill "$WAIT_PID" "$FAIL_PID" 2>/dev/null || true

k logs "job/$JOB" 2>&1 | sed 's/^/   /'

if [ "$(k get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null)" != "1" ]; then
	echo "!! seed job did not complete — see the log above" >&2
	exit 1
fi

# ---- prove the READER can see it -----------------------------------------------------------------
# The whole class of bug being fixed is "the writer succeeded and the reader saw nothing", so a
# successful write is explicitly NOT the success criterion. Ask the viewer.
echo ">> asking the viewer what it can see…"
k rollout restart "$VIEWER" >/dev/null
k rollout status "$VIEWER" --timeout=180s >/dev/null
k exec "$VIEWER" -c viewer -- python3 -c "
import json, urllib.request
with urllib.request.urlopen('http://localhost:$VIEWER_PORT/api/datasets', timeout=15) as r:
    ids = [d.get('id') or d.get('dataset_id') for d in json.load(r).get('datasets', [])]
print('   /api/datasets ->', ids or '[]  (STILL EMPTY)')
raise SystemExit(0 if ids else 1)
" || { echo "!! the corpus was written but the viewer still lists nothing" >&2; exit 1; }

echo ">> done."
