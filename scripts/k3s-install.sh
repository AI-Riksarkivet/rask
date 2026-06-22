#!/usr/bin/env bash
# One-time host setup for the local rask k3s stack. Idempotent; needs sudo.
# Installs: k3s (bundled containerd + Traefik + kubectl) -> helm ->
# NVIDIA k8s device-plugin -> KubeRay operator.
set -euo pipefail

KUBERAY_VERSION="${KUBERAY_VERSION:-1.4.2}"
DEVICE_PLUGIN_VERSION="${DEVICE_PLUGIN_VERSION:-v0.17.4}"
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

echo ">> [4/4] KubeRay operator"
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ 2>/dev/null || true
helm repo update kuberay
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "${KUBERAY_VERSION}" \
  --namespace kuberay-operator --create-namespace --wait

echo ">> waiting for GPU to be advertised on the node..."
for i in $(seq 1 30); do
  if sudo k3s kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' | grep -q '[1-9]'; then
    echo "GPU advertised."; break
  fi
  echo "  ...not yet ($i)"; sleep 5
done

echo "k3s-install done. Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
