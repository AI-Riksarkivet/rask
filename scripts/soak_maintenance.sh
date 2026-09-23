#!/usr/bin/env bash
# Sample the maintenance plane's memory and restart counts for [[LH-183]]'s remaining clause:
# "survives a full day of sweep AND reconcile ticks inside its limit".
#
# VmRSS from /proc, NOT `kubectl top`: the metrics API reports a container's WORKING SET, which read
# 217Mi on the same pod whose VmRSS was 256.7 MiB (measured 2026-09-22). Within a series each is
# consistent; mixing them measures the instrument.
#
# Appends CSV so a restarted sampler extends the record rather than replacing it. An OOMKill shows up
# twice over — as a restart count that moves, and as a gap in the series.
set -u
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
OUT=${1:-.soak/maintenance.csv}
INTERVAL=${SOAK_INTERVAL:-60}
[ -s "$OUT" ] || echo "ts,pod,container,rss_kib,restarts,last_terminated" >>"$OUT"
while true; do
  now=$(date -u +%FT%TZ)
  for p in $(kubectl get pods --no-headers 2>/dev/null | awk '$1 ~ /^rask-maintenance/ && $3=="Running" {print $1}'); do
    rss=$(kubectl exec "$p" -c maintenance -- sh -c 'grep VmRSS /proc/1/status' 2>/dev/null | awk '{print $2}')
    read -r restarts last < <(kubectl get pod "$p" -o json 2>/dev/null | python3 -c '
import sys,json
try: p=json.load(sys.stdin)
except Exception: print("", ""); raise SystemExit
for cs in p.get("status",{}).get("containerStatuses",[]):
    if cs["name"]=="maintenance":
        t=(cs.get("lastState") or {}).get("terminated") or {}
        print(cs["restartCount"], t.get("reason","none")); break
else: print("", "")
')
    echo "$now,$p,maintenance,${rss:-},${restarts:-},${last:-}" >>"$OUT"
  done
  sleep "$INTERVAL"
done
