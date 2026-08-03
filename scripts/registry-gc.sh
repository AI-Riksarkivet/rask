#!/usr/bin/env bash
# Reclaim the local dev registry: drop every tag but the newest KEEP per repository, then sweep blobs.
#
# WHY THIS IS NEEDED. Tilt pushes a uniquely-tagged image on EVERY rebuild (`tilt-build-<epoch>`), and
# nothing ever deletes the previous one. Left alone that is unbounded: measured 113 tags of web-home and
# 62 of lance-rest-catalog, 83.9 GB, on a machine that had also let the Dagger cache reach 1.1 TB.
#
# Safe by construction: this is the DEV registry. Every tag it holds is reproducible with
# `dagger call image|zone-image ... publish`, and k3s has running images in its own containerd cache, so
# deleting a tag here cannot stop a running pod.
set -euo pipefail

REG="${RASK_REGISTRY:-localhost:5000}"
KEEP="${RASK_REGISTRY_KEEP:-2}"
NAME="${RASK_REGISTRY_NAME:-rask-registry}"

curl -sf "http://$REG/v2/" >/dev/null || { echo "!! no registry at $REG (make tilt-registry)" >&2; exit 1; }

repos=$(curl -s "http://$REG/v2/_catalog?n=1000" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("repositories") or []))')
deleted=0
for repo in $repos; do
  # Tags sort lexically, and Tilt's are `tilt-build-<epoch>` — same width, so lexical == chronological.
  tags=$(curl -s "http://$REG/v2/$repo/tags/list" | python3 -c 'import json,sys; print("\n".join(sorted(json.load(sys.stdin).get("tags") or [])))')
  total=$(printf '%s\n' "$tags" | grep -c . || true)
  [[ "$total" -le "$KEEP" ]] && continue
  drop=$(printf '%s\n' "$tags" | head -n "$((total - KEEP))")
  echo ">> $repo: $total tags, dropping $((total - KEEP))"
  for t in $drop; do
    # A tag is deleted by deleting the manifest DIGEST it points at, which needs this Accept header —
    # without it the registry answers with the v1 manifest and its digest does not match, so the DELETE
    # 404s while looking like the tag simply is not there.
    d=$(curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
          "http://$REG/v2/$repo/manifests/$t" | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')
    [[ -n "$d" ]] || continue
    curl -s -X DELETE "http://$REG/v2/$repo/manifests/$d" >/dev/null && deleted=$((deleted + 1))
  done
done

if [[ -z "${repos// }" ]]; then
  # A freshly created registry has no `repositories/` directory at all, and garbage-collect treats that
  # as a hard error ("Path not found: /docker/registry/v2/repositories") rather than as "nothing to do".
  # Exiting non-zero there would make `make dev-gc` fail on a clean machine.
  echo ">> registry is empty — nothing to collect"
  exit 0
fi

echo ">> deleted $deleted manifests; sweeping blobs"
docker exec "$NAME" registry garbage-collect --delete-untagged /etc/docker/registry/config.yml 2>&1 | tail -3
echo ">> registry now $(docker exec "$NAME" du -sh /var/lib/registry 2>/dev/null | cut -f1)"
