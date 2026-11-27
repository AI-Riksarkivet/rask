#!/usr/bin/env bash
# Reclaim the local dev registry: drop old tags per repository, then sweep blobs.
#
# WHY THIS IS NEEDED. Every rebuild pushes a uniquely-tagged image and nothing ever deletes the previous
# one. Left alone that is unbounded: measured 2026-09-09, 289 GB — 165 tags of `web-lakehouse`, 231 of
# `lance-rest-catalog` — on a machine whose Dagger cache had separately reached 1.1 TB.
#
# TWO RULES DECIDE WHAT SURVIVES, AND THE FIRST IS THE LOAD-BEARING ONE.
#
# 1. A TAG ANY RUNNING WORKLOAD REFERENCES IS NEVER DELETED. Read off the live cluster, plus every tag
#    named in `deploy/values-live-pins.yaml`. This is not belt-and-braces: it is the whole safety
#    argument, and it replaces one that was FALSE. This script used to keep the lexically-last N tags
#    on the reasoning that "epoch-suffixed tags are same-width, so lexical == chronological" — true of
#    a tag scheme the estate no longer uses. Its tags are `main-<git-sha>` / `zone-<sha>` / `fix-<sha>`,
#    and lexical order over hex is arbitrary. Measured 2026-09-09 against the live release: the old
#    rule would have deleted `web-lakehouse:main-90b8d0ed`, `lance-rest-catalog:main-bf141737` and
#    `ray-lance:h14-ce91f68b` — EVERY pin the estate was running — keeping `zone-75b5a141`,
#    `verlist-215945` and `wire-32ff50cb` instead. Nothing would have gone red: k3s serves running pods
#    from containerd's own cache, so the estate would have failed on the next restart or reschedule,
#    with no event connecting it to the reclaim.
#
# 2. Of what is left, keep the newest KEEP by the image's own creation time, read from its config blob.
#    Registries expose no push time, and the tag string carries none either.
#
# Safe by construction OTHERWISE: this is the DEV registry, every tag it holds is reproducible with
# `dagger call image|zone-image ... publish`, and deleting a tag cannot stop a pod already running.
set -euo pipefail

REG="${RASK_REGISTRY:-localhost:5000}"
KEEP="${RASK_REGISTRY_KEEP:-2}"
# The registry is the fixture deploy/fixtures/registry, so its own commands run in its pod.
REGISTRY_NAMESPACE="${RASK_REGISTRY_NAMESPACE:-dev-registry}"
PINS="${RASK_REGISTRY_PINS:-deploy/values-live-pins.yaml}"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
# The cluster whose running tags are protected, declared as every k3s caller declares it (scripts/helm.sh): a
# kubeconfig that resolves another cluster would protect that cluster's tags and delete this one's.
EXPECT_CONTEXT="${RASK_EXPECT_CONTEXT:-default}"

curl -sf "http://$REG/v2/" >/dev/null || { echo "!! no registry at $REG (make dev-registry)" >&2; exit 1; }

actual="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl config current-context 2>/dev/null || echo '<none>')"
if [[ "$actual" != "$EXPECT_CONTEXT" ]]; then
  echo "!! refusing to reclaim: kubectl's context is '$actual', not '$EXPECT_CONTEXT' (KUBECONFIG=$KUBECONFIG_PATH)" >&2
  exit 1
fi

# Every image reference the cluster currently names, across every pod spec AND every controller
# template — a controller whose pods are momentarily gone still needs its image on the next schedule.
# A read that fails stops the reclaim: rule 1 is the whole safety argument, and an empty list protects nothing.
in_use=$(KUBECONFIG="$KUBECONFIG_PATH" kubectl get pods,deployments,statefulsets,daemonsets,cronjobs,jobs -A \
  -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{end}') \
  || { echo "!! could not read the images the cluster runs; nothing is reclaimed" >&2; exit 1; }

plan_file="$(mktemp)"
trap 'rm -f "$plan_file"' EXIT

export REG KEEP PINS
uv run --no-sync python - "$in_use" <<'PY' > "$plan_file"
import json, os, sys, urllib.request
from pathlib import Path

sys.path.insert(0, "scripts")
from registry_gc_plan import parse_cluster_images, plan, protected_refs

REG, KEEP, PINS = os.environ["REG"], int(os.environ["KEEP"]), os.environ["PINS"]


def get(path, headers=None):
    req = urllib.request.Request(f"http://{REG}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


pins = Path(PINS).read_text() if Path(PINS).exists() else ""
protected = protected_refs(parse_cluster_images(sys.argv[1:2]), pins)

repos = {}
for repo in json.loads(get("/v2/_catalog?n=1000")).get("repositories") or []:
    created_by_tag = {}
    for tag in json.loads(get(f"/v2/{repo}/tags/list")).get("tags") or []:
        if (repo, tag) in protected:
            continue  # never ranked, never dropped — and one fewer manifest fetch
        try:
            accept = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
            manifest = json.loads(get(f"/v2/{repo}/manifests/{tag}", accept))
            created_by_tag[tag] = json.loads(get(f"/v2/{repo}/blobs/{manifest['config']['digest']}")).get("created") or ""
        except Exception:  # noqa: BLE001 — an unreadable tag is one we decline to rank, never one we protect
            created_by_tag[tag] = ""
    repos[repo] = created_by_tag
    live = sorted(t for r, t in protected if r == repo)
    if live:
        print(f"# {repo}: protecting {len(live)} in-use tag(s): {' '.join(live)}", file=sys.stderr)

print("\n".join(f"{repo} {tag}" for repo, tag in plan(repos, protected, KEEP)))
PY

if [[ -n "${RASK_REGISTRY_DRY_RUN:-}" ]]; then
  # A reclaim whose safety rests on a protection list deserves a way to READ that list before trusting
  # it — the rule this script replaced was wrong for months and nothing could have shown it.
  echo ">> DRY RUN — would delete $(grep -c . "$plan_file" || echo 0) manifest(s); no blobs swept"
  cat "$plan_file"
  exit 0
fi

deleted=0
while read -r repo t; do
  [[ -n "${repo:-}" ]] || continue
  # A tag is deleted by deleting the manifest DIGEST it points at, which needs this Accept header —
  # without it the registry answers with the v1 manifest and its digest does not match, so the DELETE
  # 404s while looking like the tag simply is not there.
  d=$(curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
        "http://$REG/v2/$repo/manifests/$t" | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')
  [[ -n "$d" ]] || continue
  if curl -sf -X DELETE "http://$REG/v2/$repo/manifests/$d" >/dev/null; then
    deleted=$((deleted + 1))
  else
    echo "!! the registry refused to delete $repo:$t ($d)" >&2
  fi
done < "$plan_file"

if [[ "$deleted" -eq 0 ]]; then
  # A freshly created registry has no `repositories/` directory at all, and garbage-collect treats that
  # as a hard error ("Path not found: /docker/registry/v2/repositories") rather than as "nothing to do".
  echo ">> nothing to drop"
fi

echo ">> deleted $deleted manifests; sweeping blobs"
KUBECONFIG="$KUBECONFIG_PATH" kubectl -n "$REGISTRY_NAMESPACE" exec deploy/rask-registry -- registry garbage-collect --delete-untagged /etc/docker/registry/config.yml 2>&1 | tail -3
echo ">> registry now $(KUBECONFIG="$KUBECONFIG_PATH" kubectl -n "$REGISTRY_NAMESPACE" exec deploy/rask-registry -- du -sh /var/lib/registry 2>/dev/null | cut -f1)"
