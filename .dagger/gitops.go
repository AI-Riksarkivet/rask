package main

import (
	"context"
	"fmt"
	"strings"
	"time"

	"dagger/rask/internal/dagger"
)

// argoCDVersion is the Argo CD release the lane installs, pinned by exact release; scripts/gitops_from_empty.sh reads it
// as ARGOCD_VERSION, so the release and the images copied for it (mirrorArgoCDImages) are one pin.
const argoCDVersion = "v3.5.7"

// ecrLibrary is ECR Public's copy of Docker Hub's official library, which Argo CD's manifest names its redis from.
const ecrLibrary = "public.ecr.aws/docker/library/"

// GitopsFromEmpty proves the GitOps fact ([[XC-164]]): on an empty k3s with the development fixtures, a pinned Argo CD
// installs rask from an in-cluster Helm repository serving this tree's packaged chart, with the governed stack's values,
// until every workload is Ready; then one changed input (scripts/gitops_from_empty.sh's CHANGE) converges on the next
// automated sync, each changed item checked on the cluster. The images the render names are built from this source, as
// InstallFromEmpty builds them.
//
// Returns the lane's output; a failed lane returns its diagnostics in the error.
func (m *Rask) GitopsFromEmpty(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
) (string, error) {
	run := time.Now().UTC().Format(time.RFC3339Nano)
	// Started here, so it runs until the session ends: a service no exec binds is stopped, and its storage with it, so a
	// registry left to start on demand is a new, empty one by the time the cluster pulls (measured 2026-11-07: every
	// rask image fell through to Docker Hub and sat in ImagePullBackOff).
	registry, err := sessionRegistry(run).Start(ctx)
	if err != nil {
		return "", fmt.Errorf("starting the session registry: %w", err)
	}

	tool := withRenderableChart(m.base(src))
	if err := m.pushSourceImages(ctx, src, tool, registry); err != nil {
		return "", err
	}
	if err := mirrorArgoCDImages(ctx, registry); err != nil {
		return "", err
	}
	// jq builds the Application and reads its status; curl fetches Argo CD's pinned release manifest.
	tool = tool.WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "jq", "curl"}).
		WithEnvVariable("ARGOCD_VERSION", argoCDVersion)

	kubeconfig := dag.CacheVolume("rask-gitops-from-empty-kubeconfig-" + run)
	cluster := installer(tool, emptyCluster(registry, kubeconfig, run), kubeconfig, run)
	return runScript(ctx, cluster, "scripts/gitops_from_empty.sh", "rask from git under Argo CD")
}

// mirrorArgoCDImages copies each image the pinned Argo CD manifest names from ECR Public's copy of Docker Hub's official
// library into the session registry, pulled from Docker Hub itself, at the path the cluster's mirror of public.ecr.aws
// asks for (k3sRegistries). ECR Public throttles anonymous pulls from a CI runner's shared address (measured
// 2026-11-25: argocd-redis held in ImagePullBackOff past the lane's 300 s wait, in two runs of one day), and the
// lane's Docker Hub pulls have not failed. An image this does not copy falls through to ECR Public, as before.
func mirrorArgoCDImages(ctx context.Context, registry *dagger.Service) error {
	manifest, err := dag.HTTP("https://raw.githubusercontent.com/argoproj/argo-cd/" + argoCDVersion + "/manifests/core-install.yaml").Contents(ctx)
	if err != nil {
		return fmt.Errorf("reading Argo CD %s's manifest: %w", argoCDVersion, err)
	}
	names := ecrLibraryImages(manifest)
	if len(names) == 0 {
		return nil
	}
	// crane's image runs as a non-root user, and a mounted archive is root's.
	pusher := dag.Container().From(craneImage).WithUser("0").WithServiceBinding("rask-registry", registry)
	for _, name := range names {
		archive := "/images/" + strings.NewReplacer(":", "_", "/", "_").Replace(name) + ".tar"
		pusher = pusher.
			WithMountedFile(archive, dag.Container().From("docker.io/library/"+name).AsTarball()).
			WithExec([]string{"push", "--insecure", archive, "rask-registry:5000/docker/library/" + name}, dagger.ContainerWithExecOpts{UseEntrypoint: true})
	}
	if _, err := pusher.Sync(ctx); err != nil {
		return fmt.Errorf("copying Argo CD's images into the session registry: %w", err)
	}
	return nil
}

// ecrLibraryImages lists, once each, the `<name>:<tag>` of every image `manifest` names under ecrLibrary.
func ecrLibraryImages(manifest string) []string {
	seen := map[string]bool{}
	var names []string
	for _, line := range strings.Split(manifest, "\n") {
		field := strings.TrimSpace(line)
		if !strings.HasPrefix(field, "image:") {
			continue
		}
		ref := strings.Trim(strings.TrimSpace(strings.TrimPrefix(field, "image:")), `"'`)
		name, ok := strings.CutPrefix(ref, ecrLibrary)
		if ok && !seen[name] {
			seen[name] = true
			names = append(names, name)
		}
	}
	return names
}
