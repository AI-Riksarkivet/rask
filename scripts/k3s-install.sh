#!/usr/bin/env bash
# Minimal bootstrap for the local rask k3s stack: install k3s + helm and make
# kubectl/helm usable without sudo. Idempotent; needs sudo.
#
# It also switches on the development Gateway (k3s's Traefik, Gateway API provider). EVERYTHING ELSE comes up with
# `make k3s-up`: the development fixtures (deploy/fixtures,
# each its own release; the NVIDIA device plugin is one, for a GPU node), then the Helm
# chart (chart/Chart.yaml dependencies + templates) with NATS,
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
# k3s's own installer at a pinned version, held to its digest before it runs; it then holds the k3s binary it fetches to
# that release's published sha256sum.
K3S_VERSION="v1.36.4+k3s1"
K3S_INSTALLER_SHA256="46177d4c99440b4c0311b67233823a8e8a2fc09693f6c89af1a7161e152fbfad"
if ! command -v k3s >/dev/null 2>&1; then
  installer="$(mktemp)"
  curl -fsSL -o "$installer" "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION/+/%2B}/install.sh"
  echo "$K3S_INSTALLER_SHA256  $installer" | sha256sum --check --strict -
  INSTALL_K3S_VERSION="$K3S_VERSION" sh "$installer"
  rm -f "$installer"
fi
sudo k3s kubectl get nodes

# Make kubectl/helm work without sudo for this user.
export KUBECONFIG="$KUBECONFIG_PATH"
sudo chmod 644 "$KUBECONFIG_PATH" || true

# The development Gateway: k3s's Traefik with its Gateway API provider on (deploy/k3s/traefik-gateway.yaml). A file in
# the server's manifests directory is applied by k3s at every start, so the setting outlives a k3s upgrade.
sudo install -m 0644 "$(dirname "$0")/../deploy/k3s/traefik-gateway.yaml" /var/lib/rancher/k3s/server/manifests/traefik-gateway.yaml

echo ">> [2/2] helm"
# The helm .dagger/charts.go pins (helmVersion, helmSHA256), read from there so the two cannot drift, its release
# archive held to its digest before it is unpacked.
if ! command -v helm >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) echo "!! no pinned helm digest for $(uname -m)" >&2; exit 1 ;;
  esac
  pins="$(dirname "$0")/../.dagger/charts.go"
  HELM_VERSION="$(sed -n 's/^const helmVersion = "\(.*\)"$/\1/p' "$pins")"
  sum="$(sed -n "s/^[[:space:]]*\"$arch\": \"\([0-9a-f]\{64\}\)\",\$/\1/p" "$pins")"
  [ -n "$HELM_VERSION" ] && [ -n "$sum" ] || { echo "!! no helm pin for $arch in $pins" >&2; exit 1; }
  archive="$(mktemp)"
  curl -fsSL -o "$archive" "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz"
  echo "$sum  $archive" | sha256sum --check --strict -
  tar -xzf "$archive" -C /tmp "linux-${arch}/helm"
  sudo install -m 0755 "/tmp/linux-${arch}/helm" /usr/local/bin/helm
  rm -rf "$archive" "/tmp/linux-${arch}"
fi

echo "bootstrap done. Next: make k3s-build && make k3s-import && make k3s-up"
echo "Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
