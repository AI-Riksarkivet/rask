#!/usr/bin/env bash
# One-time host setup for the local image path: stand up a local image registry and
# point k3s's containerd at it, so Dagger can PUSH images (fast, cached) instead
# of the tarball side-load `make k3s-build` + `make k3s-import` do. Safe to re-run (idempotent). Requires sudo and restarts k3s once.
set -euo pipefail

# THE CLUSTER THIS SCRIPT MEANS ([[XC-057]]). This one targets the deployed estate on purpose, and
# saying so is the point: an absent declaration and a deliberate one used to look identical, so
# "I meant the live cluster" was indistinguishable from "I never thought about it". Overridable, so a
# second estate (another host, another context) is a variable rather than an edit.
: "${RASK_EXPECT_CONTEXT:=default}"
export RASK_EXPECT_CONTEXT


ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REG_PORT="${REG_PORT:-5000}"
REGFILE=/etc/rancher/k3s/registries.yaml

# 1. The registry, the fixture deploy/fixtures/registry ([[XC-202]]): a pod on the node's port $REG_PORT, its layers on a
#    volume, declared in git and installed like every other fixture rather than as a container started by hand.
#    REGISTRY_STORAGE_DELETE_ENABLED is set there: every rebuild pushes a UNIQUELY TAGGED image and nothing else removes
#    the old ones (measured 113 tags of web-home and 62 of lance-rest-catalog, 83.9 GB of dev registry), and `make
#    registry-gc` can reclaim them only through the DELETE the registry otherwise refuses.
echo ">> installing the registry fixture on :$REG_PORT"
bash "$ROOT/scripts/fixtures.sh" registry -- --set "port=$REG_PORT"

# 2. Tell k3s containerd to reach localhost:<port> over plaintext HTTP.
echo ">> writing $REGFILE"
sudo mkdir -p "$(dirname "$REGFILE")"
sudo tee "$REGFILE" >/dev/null <<EOF
mirrors:
  "localhost:${REG_PORT}":
    endpoint:
      - "http://localhost:${REG_PORT}"
configs:
  "localhost:${REG_PORT}":
    tls:
      insecure_skip_verify: true
EOF

# 3. Restart k3s to load the registry config (brief control-plane blip; workloads
#    keep running).
#
# The detection here is `systemctl cat`, NOT `list-unit-files | grep -q`. Under this
# script's `set -o pipefail`, `grep -q` exits the instant it matches, and if systemctl
# still has output to write it dies of SIGPIPE — so pipefail turns a SUCCESSFUL match
# into a false negative, non-deterministically, depending on how many units the host has
# and whether the remainder fits the 64 KiB pipe buffer. That misfire is what reported
# "k3s is not a systemd service here" on a host where `k3s.service` was loaded, active
# and running. `systemctl cat` needs no pipe and cannot exhibit it: exit 0 iff the unit
# exists.
echo ">> restarting k3s to pick up the registry config"
if systemctl cat k3s.service >/dev/null 2>&1; then
  sudo systemctl restart k3s
  # A restart is asynchronous — returning before the API server is back makes the very
  # next `kubectl` call fail for a reason that has nothing to do with the caller.
  echo ">> waiting for the k3s API server to come back"
  for _ in $(seq 1 60); do
    if sudo k3s kubectl get --raw='/readyz' >/dev/null 2>&1; then break; fi
    sleep 2
  done
  sudo k3s kubectl get --raw='/readyz' >/dev/null 2>&1 || {
    echo "!! k3s did not become ready within 120s after restart — check 'systemctl status k3s'"; exit 1; }
else
  # NEVER exit 0 here. The registry config is written but UNLOADED, so pushes will fail to
  # pull with an error that points nowhere near this script. A silent success is the same
  # defect class as a green helm install over a failed Job. A k3d cluster reads no host
  # registries.yaml at all: its registry config is per-cluster (`k3d registry create`).
  echo "!! k3s is not a systemd service here."
  echo "   $REGFILE has been written but is NOT loaded — restart k3s however you run it,"
  echo "   then re-run this script."
  exit 1
fi

# 4. Prove the registry is actually reachable before claiming success.
if curl -fsS "http://localhost:${REG_PORT}/v2/" >/dev/null 2>&1; then
  echo ">> registry responding on localhost:${REG_PORT}"
else
  echo "!! the registry fixture is installed but http://localhost:${REG_PORT}/v2/ did not answer"; exit 1
fi

echo ">> done. Push to localhost:${REG_PORT}/<image>:<tag>; k3s will pull it."
