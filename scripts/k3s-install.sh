#!/usr/bin/env bash
# Minimal bootstrap for the local rask k3s stack: install k3s + helm and make
# kubectl/helm usable without sudo. Idempotent; needs sudo.
#
# It also switches on the development Gateway (k3s's Traefik, Gateway API provider). EVERYTHING ELSE comes up with
# `make k3s-up`: the development fixtures (deploy/fixtures,
# each its own release; the NVIDIA device plugin is one, for a GPU node), then the Helm
# chart (chart/Chart.yaml dependencies + templates) with NATS, Dapr,
# OpenFGA, Postgres, RustFS and the rask fleet. k3s and helm are the only bootstrap.
#
# Host prerequisite for the GPU: nvidia-container-toolkit (nvidia-ctk) installed,
# so k3s auto-detects the nvidia containerd runtime.
set -euo pipefail

KUBECONFIG_PATH="/etc/rancher/k3s/k3s.yaml"

# INOTIFY INSTANCES ARE PER UID, and every container on the node is root's. Under cgroup v2 the
# containerd shim holds one inotify instance per container (its OOM watch on memory.events), k3s-server
# about twenty and systemd ten more. Measured 2026-10-14 on the single-node estate (56 pods): root held
# 127 of the distro default 128, shims 78 of them, each shim at most its container count, so it is
# sizing and not a leak. At the ceiling the next process that asks fails, and silently: the Dagger
# engine's dnsmasq exits 5 ("failed to create inotify: Too many open files"), and every build then dies
# resolving github.com on 10.87.0.1:53. The values are the ones kind documents for a Kubernetes node.
echo ">> [0/2] inotify limits"
printf 'fs.inotify.max_user_instances = 8192\nfs.inotify.max_user_watches = 524288\n' | sudo tee /etc/sysctl.d/90-rask-inotify.conf >/dev/null
sudo sysctl -q --load=/etc/sysctl.d/90-rask-inotify.conf

echo ">> [1/2] k3s"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | sh -
fi
sudo k3s kubectl get nodes

# Make kubectl/helm work without sudo for this user.
export KUBECONFIG="$KUBECONFIG_PATH"
sudo chmod 644 "$KUBECONFIG_PATH" || true

# The development Gateway: k3s's Traefik with its Gateway API provider on (deploy/k3s/traefik-gateway.yaml). A file in
# the server's manifests directory is applied by k3s at every start, so the setting outlives a k3s upgrade.
sudo install -m 0644 "$(dirname "$0")/../deploy/k3s/traefik-gateway.yaml" /var/lib/rancher/k3s/server/manifests/traefik-gateway.yaml

echo ">> [2/2] helm"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo "bootstrap done. Next: make k3s-build && make k3s-import && make k3s-up"
echo "Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
