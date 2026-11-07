package main

import (
	"context"
	"fmt"
	"time"

	"dagger/rask/internal/dagger"
)

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
	// jq builds the Application and reads its status; curl fetches Argo CD's pinned release manifest.
	tool = tool.WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "jq", "curl"})

	kubeconfig := dag.CacheVolume("rask-gitops-from-empty-kubeconfig-" + run)
	cluster := installer(tool, emptyCluster(registry, kubeconfig, run), kubeconfig, run)
	return runScript(ctx, cluster, "scripts/gitops_from_empty.sh", "rask from git under Argo CD")
}
