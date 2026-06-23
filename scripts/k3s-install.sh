#!/usr/bin/env bash
# One-time host setup for the local rask k3s stack. Idempotent; needs sudo.
# Installs: k3s (bundled containerd + Traefik + kubectl) -> helm ->
# NVIDIA k8s device-plugin -> KubeRay operator -> cluster infra foundation
# (NATS JetStream, Dapr control plane, Kueue, OpenFGA). The infra foundation is
# installed but NOT yet wired into rask — it's a base for upcoming work.
set -euo pipefail

KUBERAY_VERSION="${KUBERAY_VERSION:-1.4.2}"
DEVICE_PLUGIN_VERSION="${DEVICE_PLUGIN_VERSION:-v0.17.4}"
NATS_VERSION="${NATS_VERSION:-2.14.2}"
DAPR_VERSION="${DAPR_VERSION:-1.18.1}"
KUEUE_VERSION="${KUEUE_VERSION:-v0.18.1}"
OPENFGA_VERSION="${OPENFGA_VERSION:-0.3.9}"
KUBECONFIG_PATH="/etc/rancher/k3s/k3s.yaml"

echo ">> [1/4] k3s"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | sh -
fi
sudo k3s kubectl get nodes

# Make kubectl/helm work without sudo for this user.
export KUBECONFIG="$KUBECONFIG_PATH"
sudo chmod 644 "$KUBECONFIG_PATH" || true

echo ">> [2/4] helm"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo ">> [3/4] NVIDIA device-plugin + runtimeclass"
# k3s auto-detects the nvidia container runtime when nvidia-container-toolkit is
# present on the host (nvidia-ctk is already installed here). Ensure the runtimeclass.
sudo k3s kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF
sudo k3s kubectl apply -f "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/${DEVICE_PLUGIN_VERSION}/deployments/static/nvidia-device-plugin.yml"
# k3s registers the nvidia container runtime but does NOT make it the default, so
# the upstream device-plugin pod runs under runc and sees "No devices found".
# Pin it to the nvidia RuntimeClass so it can enumerate the GPU.
sudo k3s kubectl -n kube-system patch daemonset nvidia-device-plugin-daemonset --type merge \
  -p '{"spec":{"template":{"spec":{"runtimeClassName":"nvidia"}}}}'

echo ">> [4/8] KubeRay operator"
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ 2>/dev/null || true
helm repo update kuberay
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "${KUBERAY_VERSION}" \
  --namespace kuberay-operator --create-namespace --wait

echo ">> waiting for GPU to be advertised on the node..."
gpu_ok=0
for i in $(seq 1 30); do
  if sudo k3s kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' | grep -q '[1-9]'; then
    echo "GPU advertised."; gpu_ok=1; break
  fi
  echo "  ...not yet ($i)"; sleep 5
done
if [ "$gpu_ok" -eq 0 ]; then
  echo "WARN: nvidia.com/gpu not advertised after ~150s — check the device-plugin pod and nvidia-container-toolkit before 'make k3s-up'." >&2
fi

# ---- cluster infra foundation (not yet wired into rask) --------------------
echo ">> [5/8] NATS (JetStream)"
helm repo add nats https://nats-io.github.io/k8s/helm/charts/ 2>/dev/null || true
helm repo update nats
helm upgrade --install nats nats/nats \
  --version "${NATS_VERSION}" \
  --namespace nats --create-namespace \
  --set config.jetstream.enabled=true --wait

echo ">> [6/8] Dapr control plane"
helm repo add dapr https://dapr.github.io/helm-charts/ 2>/dev/null || true
helm repo update dapr
helm upgrade --install dapr dapr/dapr \
  --version "${DAPR_VERSION}" \
  --namespace dapr-system --create-namespace --wait

echo ">> [7/8] Kueue"
sudo k3s kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"
sudo k3s kubectl -n kueue-system rollout status deploy/kueue-controller-manager --timeout=300s

echo ">> [8/8] OpenFGA"
# datastore.engine=memory: ephemeral, fine for a local foundation. Point it at
# postgres (datastore.engine=postgres + uri) when OpenFGA is actually adopted.
helm repo add openfga https://openfga.github.io/helm-charts 2>/dev/null || true
helm repo update openfga
helm upgrade --install openfga openfga/openfga \
  --version "${OPENFGA_VERSION}" \
  --namespace openfga --create-namespace \
  --set datastore.engine=memory --wait

echo "k3s-install done. Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
