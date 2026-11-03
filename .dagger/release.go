package main

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"time"

	"dagger/rask/internal/dagger"
)

// A lakehouse release ([[XC-187]], decided by the owner 2026-11-03): the git tag `lakehouse-vX.Y.Z` publishes the
// catalog image as one amd64+arm64 index and the product chart, pinned to that index's digest, as an OCI artifact on
// GHCR; the platform package is pinned by the same tag through uv's git source. A client names the whole lakehouse by
// that one version.
// catalogImageName is the one first-party image the product chart names (`image.catalog.repository`).
const catalogImageName = "lance-rest-catalog"

// releasePlatforms are the architectures a release's image index holds: a client's CI on amd64 and a developer's
// arm64 host pull the same pinned digest. Each is built natively on a runner of its own (PublishImage): under the
// engine's emulation the amd64 image's import gate dies of SIGILL in a native wheel (measured on an arm64 host,
// 2026-11-03, exit 132), so an emulated build is no release.
var releasePlatforms = []dagger.Platform{"linux/amd64", "linux/arm64"}

var (
	releaseVersion = regexp.MustCompile(`^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$`)
	pushedDigest   = regexp.MustCompile(`(?m)^Digest: (sha256:[0-9a-f]{64})$`)
)

// PublishImage builds the catalog image for the engine's own platform and pushes it as
// `<registry>/lance-rest-catalog:<version>-<arch>`, returning that reference by digest. The release workflow runs it
// once per release platform, each on a runner of that architecture; Publish then joins them. Never cached: a call is a push.
//
// +cache="never"
func (m *Rask) PublishImage(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache"]
	// +defaultPath="/"
	src *dagger.Directory,
	// The release's version, semver without the tag's `lakehouse-v` prefix.
	version string,
	// The registry user (the CI job's `github.actor`).
	username string,
	// The registry token (the CI job's GITHUB_TOKEN, with packages: write).
	password *dagger.Secret,
	// The commit the release is cut from, stamped into the image's labels.
	// +optional
	vcsRef string,
	// +optional
	// +default="ghcr.io/ai-riksarkivet"
	registry string,
) (string, error) {
	if err := requireReleaseVersion(version); err != nil {
		return "", err
	}
	platform, err := dag.DefaultPlatform(ctx)
	if err != nil {
		return "", err
	}
	ref := fmt.Sprintf("%s/%s:%s-%s", registry, catalogImageName, version, archOf(platform))
	return m.Image(src, "rest-catalog", time.Now().UTC().Format(time.RFC3339), vcsRef, version, nil, platform).
		WithRegistryAuth(registryHost(registry), username, password).
		Publish(ctx, ref)
}

// Publish releases the lakehouse at `version`: it joins the per-architecture images PublishImage pushed into one
// index, `<registry>/lance-rest-catalog:<version>`, and pushes the product chart pinned to that index's digest as
// `oci://<registry>/charts/rask:<version>`. Nothing is built or run here, so any runner joins any architecture.
// Returns what was published, as a client pins it. Never cached: a call is a push.
//
// +cache="never"
func (m *Rask) Publish(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
	// The release's version, semver without the tag's `lakehouse-v` prefix.
	version string,
	// The registry user (the CI job's `github.actor`).
	username string,
	// The registry token (the CI job's GITHUB_TOKEN, with packages: write).
	password *dagger.Secret,
	// +optional
	// +default="ghcr.io/ai-riksarkivet"
	registry string,
) (string, error) {
	if err := requireReleaseVersion(version); err != nil {
		return "", err
	}
	host := registryHost(registry)

	variants := make([]*dagger.Container, 0, len(releasePlatforms))
	for _, platform := range releasePlatforms {
		variants = append(variants, dag.Container(dagger.ContainerOpts{Platform: platform}).
			WithRegistryAuth(host, username, password).
			From(fmt.Sprintf("%s/%s:%s-%s", registry, catalogImageName, version, archOf(platform))))
	}
	ref, err := variants[0].
		Publish(ctx, fmt.Sprintf("%s/%s:%s", registry, catalogImageName, version), dagger.ContainerPublishOpts{PlatformVariants: variants[1:]})
	if err != nil {
		return "", fmt.Errorf("publishing the catalog image index: %w", err)
	}
	_, digest, found := strings.Cut(ref, "@")
	if !found {
		return "", fmt.Errorf("the registry answered %q, which names no digest", ref)
	}

	pushed, err := withRenderableChart(m.base(src)).
		WithEnvVariable("REGISTRY_USER", username).
		WithSecretVariable("REGISTRY_PASSWORD", password).
		WithExec([]string{"bash", "-c", "set -euo pipefail\nexec 2>&1\n" +
			fmt.Sprintf("uv run python scripts/pin_chart_release.py chart --version %s --repository %s --digest %s=%s\n", version, registry, catalogImageName, digest) +
			"helm package chart -d /release\n" +
			fmt.Sprintf("printf '%%s' \"$REGISTRY_PASSWORD\" | helm registry login %s -u \"$REGISTRY_USER\" --password-stdin\n", host) +
			fmt.Sprintf("helm push /release/rask-%s.tgz oci://%s/charts", version, registry)}).
		Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("publishing the chart: %w", err)
	}
	chartDigest := pushedDigest.FindStringSubmatch(pushed)
	if chartDigest == nil {
		return "", fmt.Errorf("helm push printed no digest:\n%s", pushed)
	}
	return fmt.Sprintf("image  %s/%s@%s\nchart  oci://%s/charts/rask:%s@%s\nplatform package  service-kit @ git+https://github.com/AI-Riksarkivet/rask@lakehouse-v%s#subdirectory=packages/service-kit\n",
		registry, catalogImageName, digest, registry, version, chartDigest[1], version), nil
}

// requireReleaseVersion refuses a version that is not semver, before anything is built or pushed.
func requireReleaseVersion(version string) error {
	if !releaseVersion.MatchString(version) {
		return fmt.Errorf("version %q is not semver (the tag is lakehouse-v<version>)", version)
	}
	return nil
}

// archOf is a platform's architecture, the per-architecture tag's suffix: `linux/arm64` is `arm64`.
func archOf(platform dagger.Platform) string {
	parts := strings.Split(string(platform), "/")
	return parts[len(parts)-1]
}

// registryHost is the host a registry path logs in to: `ghcr.io/ai-riksarkivet` logs in to `ghcr.io`.
func registryHost(registry string) string {
	return strings.SplitN(registry, "/", 2)[0]
}

// Lakehouse stands a published lakehouse release up for another repository's e2e: an empty k3s, the development
// prerequisites and stand-ins from this module's deploy/fixtures, and the chart `oci://ghcr.io/ai-riksarkivet/charts/rask`
// at `version`, until every workload is Ready (scripts/install_from_empty.sh). The release's catalog image is copied by
// the digest its chart pins into a registry inside the session, which the cluster mirrors, so the cluster runs the
// published bytes as the local estate it is (image.localImages); nothing is built.
//
// Returns the lakehouse as one service serving the catalog on 2333, Dex on 5556 and the object store on 9000. Bind it
// under the in-cluster names, `rask-catalog`, `rask-dex` and `rask-rustfs`, so a token's issuer
// (http://rask-dex:5556/dex) and a vended endpoint resolve as the catalog names them. The estate admin is
// alice@example.com (password `password`), whom the Dex fixture serves.
//
// Cached per session: the services it starts live as long as the session, so a result cached across sessions would
// name a cluster nobody installed (measured on engine v0.21.8: by default a second `dagger call` returns the first
// call's result).
//
// +cache="session"
func (m *Rask) Lakehouse(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
	// The release's version, semver without the tag's `lakehouse-v` prefix.
	version string,
	// +optional
	// +default="oci://ghcr.io/ai-riksarkivet/charts/rask"
	chart string,
) (*dagger.Service, error) {
	if err := requireReleaseVersion(version); err != nil {
		return nil, err
	}
	run := time.Now().UTC().Format(time.RFC3339Nano)
	tool := withRenderableChart(m.tools(src))

	pinned, err := tool.WithExec([]string{"bash", "-c", fmt.Sprintf("set -euo pipefail\nhelm show values %s --version %s | uv run --no-project --with pyyaml python -c "+
		"'import sys, yaml; i = yaml.safe_load(sys.stdin)[\"image\"]; print(i[\"repository\"] + \"/%s@\" + i[\"digests\"][\"%s\"])'",
		chart, version, catalogImageName, catalogImageName)}).Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading the image release %s pins: %w", version, err)
	}
	image := strings.TrimSpace(pinned)

	registry := dag.Container().From(registryImage).WithExposedPort(5000).AsService(dagger.ContainerAsServiceOpts{UseEntrypoint: true})
	if registry, err = registry.Start(ctx); err != nil {
		return nil, fmt.Errorf("starting the session registry: %w", err)
	}
	if _, err := dag.Container().From(craneImage).WithServiceBinding("rask-registry", registry).
		WithExec([]string{"copy", "--insecure", image, "rask-registry:5000/library/" + catalogImageName + ":dev"}, dagger.ContainerWithExecOpts{UseEntrypoint: true}).
		Sync(ctx); err != nil {
		return nil, fmt.Errorf("copying %s into the session registry: %w", image, err)
	}

	kubeconfig := dag.CacheVolume("rask-lakehouse-kubeconfig-" + run)
	k3s, err := emptyCluster(registry, kubeconfig, run).Start(ctx)
	if err != nil {
		return nil, fmt.Errorf("starting the cluster: %w", err)
	}
	cluster := installer(tool, k3s, kubeconfig, run)
	if _, err := runInstall(ctx, cluster.WithEnvVariable("CHART", chart).WithEnvVariable("CHART_VERSION", version), "lakehouse "+version); err != nil {
		return nil, err
	}

	return cluster.
		WithExposedPort(2333).
		WithExposedPort(5556).
		WithExposedPort(9000).
		AsService(dagger.ContainerAsServiceOpts{Args: []string{"bash", "-c", clusterReady +
			"kubectl port-forward --address 0.0.0.0 svc/rask-catalog 2333:2333 & " +
			"kubectl port-forward --address 0.0.0.0 svc/rask-dex 5556:5556 & " +
			"kubectl port-forward --address 0.0.0.0 svc/rask-rustfs 9000:9000 & wait -n; exit 1"}}).
		Start(ctx)
}
