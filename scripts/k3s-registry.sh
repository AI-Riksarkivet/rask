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
  docker run -d --restart=always -p "${REG_PORT}:5000" --name "$REG_NAME" registry:2 >/dev/null
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
#    keep running). Falls back to a hint if k3s isn't a systemd service.
echo ">> restarting k3s to pick up the registry config"
if systemctl list-unit-files 2>/dev/null | grep -q '^k3s\.service'; then
  sudo systemctl restart k3s
else
  echo "!! k3s is not a systemd service here — restart it however you run it, then re-run."
  exit 0
fi

echo ">> done. Push to localhost:${REG_PORT}/<image>:dev; k3s will pull it. Run 'make tilt-up'."
