#!/usr/bin/env bash
# One-time host setup for the Tilt dev loop: stand up a local image registry and
# point k3s's containerd at it, so Tilt can PUSH :dev images (fast, cached) instead
# of the slow `docker save | k3s ctr images import -` side-load that `make k3s-build`
# uses. Safe to re-run (idempotent). Requires sudo and restarts k3s once.
set -euo pipefail

REG_NAME="${REG_NAME:-rask-registry}"
REG_PORT="${REG_PORT:-5000}"
REGFILE=/etc/rancher/k3s/registries.yaml

# 1. The registry container (host-local, restart-always).
if docker ps --format '{{.Names}}' | grep -qx "$REG_NAME"; then
  echo ">> registry '$REG_NAME' already running"
else
  echo ">> starting registry '$REG_NAME' on :$REG_PORT"
  # REGISTRY_STORAGE_DELETE_ENABLED: registry:2 refuses DELETE without it, and without DELETE there is
  # no way to reclaim anything — `registry garbage-collect` only sweeps blobs no manifest references.
  # This matters because Tilt pushes a UNIQUELY TAGGED image on every single rebuild and nothing ever
  # removes the old ones: measured 113 tags of web-home and 62 of lance-rest-catalog, 83.9 GB of dev
  # registry. `make registry-gc` is the reclaim; this flag is what makes it possible at all.
  docker run -d --restart=always -p "${REG_PORT}:5000" --name "$REG_NAME" \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true \
    registry:2 >/dev/null
fi

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
  # next `kubectl`/`tilt` call fail for a reason that has nothing to do with the caller.
  echo ">> waiting for the k3s API server to come back"
  for _ in $(seq 1 60); do
    if sudo k3s kubectl get --raw='/readyz' >/dev/null 2>&1; then break; fi
    sleep 2
  done
  sudo k3s kubectl get --raw='/readyz' >/dev/null 2>&1 || {
    echo "!! k3s did not become ready within 120s after restart — check 'systemctl status k3s'"; exit 1; }
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^k3d-'; then
  # k3d runs k3s inside containers and does NOT read /etc/rancher/k3s/registries.yaml on
  # the host; its registry config is per-cluster. Say so rather than implying success.
  echo "!! k3s here is k3d (containerised). The host $REGFILE this script just wrote does"
  echo "   NOT apply to it — configure the registry with 'k3d registry create' / recreate"
  echo "   the cluster with '--registry-use'. See https://k3d.io/stable/usage/registries/"
  exit 1
else
  # NEVER exit 0 here. The registry config is written but UNLOADED, so tilt will fail to
  # pull with an error that points nowhere near this script. A silent success is the same
  # defect class as a green helm install over a failed Job.
  echo "!! k3s is not a systemd service here and no k3d cluster was found."
  echo "   $REGFILE has been written but is NOT loaded — restart k3s however you run it,"
  echo "   then re-run this script."
  exit 1
fi

# 4. Prove the registry is actually reachable before claiming success.
if curl -fsS "http://localhost:${REG_PORT}/v2/" >/dev/null 2>&1; then
  echo ">> registry responding on localhost:${REG_PORT}"
else
  echo "!! registry container is up but http://localhost:${REG_PORT}/v2/ did not answer"; exit 1
fi

echo ">> done. Push to localhost:${REG_PORT}/<image>:dev; k3s will pull it. Run 'make tilt-up'."
