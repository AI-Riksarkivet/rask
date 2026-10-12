package main

import (
	"context"
	"fmt"
	"strings"
	"time"

	"dagger/rask/internal/dagger"
)

// The empty cluster and its image source, pinned. k3s v1.31 matches the kind node the e2e lanes ran on.
const (
	k3sImage       = "rancher/k3s:v1.31.2-k3s1"
	registryImage  = "registry:2.8.3"
	craneImage     = "gcr.io/go-containerregistry/crane:v0.20.3"
	kubectlVersion = "v1.31.2"
)

// k3sEntrypoint moves the container's processes out of the root cgroup before k3s starts, so the kubelet can enable
// the controllers it needs under cgroup v2; k3s inside a container cannot start its pods without it. Taken from moby's
// hack/dind (Apache 2.0), as k3d and the daggerverse k3s module do.
const k3sEntrypoint = `#!/bin/sh
set -eu
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  mkdir -p /sys/fs/cgroup/init
  xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
  sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
fi
exec "$@"
`

// k3sRegistries sends every Docker Hub pull through the session's registry first. rask's images render as a bare
// `<component>:<tag>` (image.localImages), which containerd reads as docker.io/library/<component>, so pushing them
// under library/ makes them pullable with the render unchanged; anything the registry lacks falls through to Docker Hub.
const k3sRegistries = `mirrors:
  docker.io:
    endpoint:
      - "http://rask-registry:5000"
`

// InstallFromEmpty installs rask onto an empty cluster the way a platform team does ([[XC-133]], ADR 0153): an empty
// k3s, the development prerequisites and stand-ins from deploy/fixtures, then the product chart, until every workload
// is Ready (scripts/install_from_empty.sh). The images the render names are built from this source and served to the
// cluster by a registry inside the session, so the same call runs on a laptop and in CI with nothing but Dagger.
//
// Returns the install's output; a failed install returns its diagnostics in the error.
func (m *Rask) InstallFromEmpty(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	run := time.Now().UTC().Format(time.RFC3339Nano)
	registry := dag.Container().From(registryImage).WithExposedPort(5000).AsService(dagger.ContainerAsServiceOpts{UseEntrypoint: true})

	tool := withRenderableChart(m.base(src))
	images, err := tool.WithExec([]string{"bash", "-c", "RELEASE=rask . scripts/stack_values.sh && helm template rask ./chart \"${HELM_SET[@]}\"" +
		" --api-versions external-secrets.io/v1/ExternalSecret --api-versions generators.external-secrets.io/v1alpha1/Password --api-versions dapr.io/v1alpha1" +
		" | uv run python scripts/side_loaded_images.py --tag dev"}).Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("listing the images the render names: %w", err)
	}
	// crane's image runs as a non-root user, and a mounted archive is root's.
	pusher := dag.Container().From(craneImage).WithUser("0").WithServiceBinding("rask-registry", registry)
	for _, line := range strings.Split(strings.TrimSpace(images), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			return "", fmt.Errorf("side_loaded_images.py printed %q, not `<image> <stem>`", line)
		}
		ref, stem := fields[0], fields[1]
		archive := "/images/" + stem + ".tar"
		pusher = pusher.
			WithMountedFile(archive, m.Image(src, stem, "", "", "", nil).AsTarball()).
			WithExec([]string{"push", "--insecure", archive, "rask-registry:5000/library/" + ref}, dagger.ContainerWithExecOpts{UseEntrypoint: true})
	}
	if _, err := pusher.Sync(ctx); err != nil {
		return "", fmt.Errorf("pushing the images to the session registry: %w", err)
	}

	kubeconfig := dag.CacheVolume("rask-install-from-empty-kubeconfig")
	k3s := dag.Container().From(k3sImage).
		WithNewFile("/usr/bin/entrypoint.sh", k3sEntrypoint, dagger.ContainerWithNewFileOpts{Permissions: 0o755}).
		WithNewFile("/etc/rancher/k3s/registries.yaml", k3sRegistries).
		WithServiceBinding("rask-registry", registry).
		WithMountedCache("/kc", kubeconfig).
		WithMountedTemp("/var/lib/kubelet").
		WithMountedTemp("/var/lib/rancher").
		WithMountedTemp("/var/lib/cni").
		WithMountedTemp("/var/log").
		WithEnvVariable("RUN", run).
		WithExec([]string{"rm", "-f", "/kc/k3s.yaml"}).
		WithExposedPort(6443).
		AsService(dagger.ContainerAsServiceOpts{
			Args: []string{"entrypoint.sh", "sh", "-c", "exec k3s server --bind-address $(ip route | grep src | awk '{print $NF}')" +
				" --tls-san rask-k3s --disable traefik --disable metrics-server --egress-selector-mode=disabled" +
				" --write-kubeconfig /kc/k3s.yaml --write-kubeconfig-mode 644"},
			InsecureRootCapabilities: true,
		})

	install := tool.
		WithFile("/usr/local/bin/kubectl", kubectlBinary(), dagger.ContainerWithFileOpts{Permissions: 0o755}).
		WithServiceBinding("rask-k3s", k3s).
		WithMountedCache("/kc", kubeconfig).
		WithEnvVariable("RUN", run).
		WithExec([]string{"bash", "-c", "set -euo pipefail\nexec 2>&1\nset -x\n" +
			"until [ -s /kc/k3s.yaml ]; do sleep 2; done\n" +
			"sed -E 's#https://[^:/]+:6443#https://rask-k3s:6443#' /kc/k3s.yaml > /tmp/kubeconfig\n" +
			"export KUBECONFIG=/tmp/kubeconfig\n" +
			"until [ -n \"$(kubectl get nodes -o name 2>/dev/null)\" ]; do sleep 2; done\n" +
			"kubectl wait --for=condition=Ready node --all --timeout=180s\n" +
			"set +x\nscripts/install_from_empty.sh"}, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})
	out, err := install.Stdout(ctx)
	if err != nil {
		return "", err
	}
	code, err := install.ExitCode(ctx)
	if err != nil {
		return "", err
	}
	if code != 0 {
		return "", fmt.Errorf("rask did not install from empty (exit %d):\n%s", code, out)
	}
	return out, nil
}

// kubectlBinary is the pinned kubectl for the client side of InstallFromEmpty, read off the release archive for the
// container's own architecture, as helmBinary does.
func kubectlBinary() *dagger.File {
	return dag.Container().
		From(chartsBaseImage).
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "curl", "ca-certificates"}).
		WithExec([]string{"sh", "-c", "set -eu; a=\"$(dpkg --print-architecture)\"; " +
			"curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/" + kubectlVersion + "/bin/linux/${a}/kubectl && chmod 0755 /usr/local/bin/kubectl"}).
		File("/usr/local/bin/kubectl")
}
