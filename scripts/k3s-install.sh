#!/usr/bin/env bash
# Minimal bootstrap for the local rask k3s stack: install k3s + helm and make
# kubectl/helm usable without sudo. Idempotent; needs sudo.
#
# EVERYTHING ELSE is in the Helm chart (chart/Chart.yaml dependencies +
# templates) and comes up with `make k3s-up`: the NVIDIA device-plugin, KubeRay,
# NATS, Dapr, Kueue, OpenFGA, Postgres, MinIO, and the rask fleet. k3s and helm
# are the only things that can't live in a chart, so they're the only bootstrap.
#
# Host prerequisite for the GPU: nvidia-container-toolkit (nvidia-ctk) installed,
# so k3s auto-detects the nvidia containerd runtime.
set -euo pipefail

KUBECONFIG_PATH="/etc/rancher/k3s/k3s.yaml"

echo ">> [1/2] k3s"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | sh -
fi
sudo k3s kubectl get nodes

# Make kubectl/helm work without sudo for this user.
export KUBECONFIG="$KUBECONFIG_PATH"
sudo chmod 644 "$KUBECONFIG_PATH" || true

echo ">> [2/2] helm"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo "bootstrap done. Next: make k3s-build && make k3s-import && make k3s-up"
echo "Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
