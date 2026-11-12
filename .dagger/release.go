package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
	"time"

	"dagger/rask/internal/dagger"
)

// A lakehouse release ([[XC-187]], decided by the owner 2026-11-03): the git tag `lakehouse-vX.Y.Z` publishes the
// catalog image and the console image, each as one amd64+arm64 index, and the product chart, pinned to both indexes'
// digests, as an OCI artifact on GHCR. A client names the whole lakehouse by that one version. Every image passes a scan
// gate before it is pushed; every index and the chart are signed as the release workflow, and each platform's manifest
// carries its CycloneDX SBOM as an attestation signed the same way ([[XC-202]]).
// catalogImageName is the image the product chart names `image.catalog.repository`.
const catalogImageName = "lance-rest-catalog"

// releaseImage is an image a release publishes: the dockerfile stem it is built from (images.go) and its name, which the
// chart's `rask.image` gives it and its `image.digests` pins it under.
type releaseImage struct {
	stem string
	name string
}

var releaseImages = []releaseImage{{stem: "rest-catalog", name: catalogImageName}, {stem: "frontend", name: "web-console"}}

const (
	// cosignImage signs and verifies a release's images and chart. Pinned by digest: on the release path it is a trust
	// root, and a tag can be moved.
	cosignImage = "ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d"
	// releaseIssuer is the OIDC issuer of the identity a release signs as: GitHub Actions'.
	releaseIssuer = "https://token.actions.githubusercontent.com"
	// releaseScanSeverity is the scan gate a release image passes: no HIGH or CRITICAL finding a fixed release exists for,
	// the gate the nightly image scan holds both images to.
	releaseScanSeverity = "HIGH,CRITICAL"
	// secretOwner owns each secret a release mounts for cosign and crane: both images run as distroless's nonroot user
	// (65532, read off the pinned digests 2026-11-12), to whom a secret mounted as root's, Dagger's default, is unreadable.
	secretOwner = "65532:65532"
	// identityTokenPath is where Publish mounts the release workflow's OIDC token, which cosign reads as a file.
	identityTokenPath = "/run/sigstore/token"
)

// releaseIdentity is the identity a release's signatures carry: the release workflow, run at the release's tag. A
// signature from anything else, another workflow or another tag included, does not verify as the release.
func releaseIdentity(version string) string {
	return "https://github.com/AI-Riksarkivet/rask/.github/workflows/release.yml@refs/tags/lakehouse-v" + version
}

// releasePlatforms are the architectures a release's image index holds: a client's CI on amd64 and a developer's
// arm64 host pull the same pinned digest. Each is built natively on a runner of its own (PublishImage): under the
// engine's emulation the amd64 image's import gate dies of SIGILL in a native wheel (measured on an arm64 host,
// 2026-11-03, exit 132), so an emulated build is no release.
var releasePlatforms = []dagger.Platform{"linux/amd64", "linux/arm64"}

var (
	releaseVersion = regexp.MustCompile(`^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$`)
	pushedDigest   = regexp.MustCompile(`(?m)^Digest: (sha256:[0-9a-f]{64})$`)
	imageDigest    = regexp.MustCompile(`sha256:[0-9a-f]{64}`)
)

// releaseBuild is what every image of one release is built with: the source, the labels each image is stamped with (when
// it was built, from which commit, at which version) and the platform it is built for.
type releaseBuild struct {
	src      *dagger.Directory
	built    string
	vcsRef   string
	version  string
	platform dagger.Platform
}

// PublishImage builds each release image for the engine's own platform, holds it to the scan gate (a HIGH or CRITICAL
// finding a fixed release exists for refuses the release before anything is pushed), and pushes it as
// `<registry>/<image>:<version>-<arch>`, returning those references by digest. The release workflow runs it once per
// release platform, each on a runner of that architecture; Publish then joins them. Never cached: a call is a push.
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
	build := releaseBuild{src: src, built: time.Now().UTC().Format(time.RFC3339), vcsRef: vcsRef, version: version, platform: platform}
	pushed := make([]string, 0, len(releaseImages))
	for _, image := range releaseImages {
		container, err := m.scannedImage(ctx, build, image)
		if err != nil {
			return "", err
		}
		ref, err := container.WithRegistryAuth(registryHost(registry), username, password).
			Publish(ctx, fmt.Sprintf("%s/%s:%s-%s", registry, image.name, version, archOf(platform)))
		if err != nil {
			return "", fmt.Errorf("pushing %s: %w", image.name, err)
		}
		pushed = append(pushed, ref)
	}
	return strings.Join(pushed, "\n"), nil
}

// scannedImage builds `image` as `build` says and holds it to the scan gate: trivy over the built image, refusing a HIGH
// or CRITICAL finding a fixed release exists for. An unfixed one does not gate, since no rebuild can act on it.
func (m *Rask) scannedImage(ctx context.Context, build releaseBuild, image releaseImage) (*dagger.Container, error) {
	container := m.Image(build.src, image.stem, build.built, build.vcsRef, build.version, nil, build.platform)
	if _, err := m.scanContainer(ctx, container, releaseScanSeverity, true); err != nil {
		return nil, fmt.Errorf("the scan gate refused %s: %w", image.name, err)
	}
	return container, nil
}

// Publish releases the lakehouse at `version`: it joins each image's per-architecture variants PublishImage pushed into
// one index, `<registry>/<image>:<version>`, signs each index and every manifest in it as the release workflow, attests
// each manifest's CycloneDX SBOM to it the same way, pushes the product chart pinned to both indexes' digests as
// `oci://<registry>/charts/rask:<version>`, and signs the chart. Signing is keyless: cosign exchanges `identityToken`,
// the release workflow's GitHub OIDC token for the audience `sigstore`, for a short-lived certificate naming that workflow
// at the release's tag, and records each signature in Sigstore's public transparency log, so no signing key exists to
// steal or rotate, and `cosign verify` with releaseIdentity proves an artifact is the release's. Each signature and
// attestation is verified that way as soon as it is made, so a release whose signing went wrong fails rather than
// reporting success, and an image's fails before the chart is pushed. Nothing is built or run here, so any runner joins
// any architecture. Returns what was published, as a client pins it. Never cached: a call is a push.
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
	// The release workflow's GitHub OIDC token for the audience `sigstore` (its job holds `id-token: write`).
	identityToken *dagger.Secret,
	// +optional
	// +default="ghcr.io/ai-riksarkivet"
	registry string,
) (string, error) {
	if err := requireReleaseVersion(version); err != nil {
		return "", err
	}
	host := registryHost(registry)
	credential, err := registryCredential(ctx, host, username, password)
	if err != nil {
		return "", err
	}
	signer := releaseSigner{
		cosign: cosign(credential).WithMountedSecret(identityTokenPath, identityToken, dagger.ContainerWithMountedSecretOpts{Owner: secretOwner}),
		sign:   []string{"--identity-token", identityTokenPath},
		verify: []string{"--certificate-identity", releaseIdentity(version), "--certificate-oidc-issuer", releaseIssuer},
		crane:  crane(credential),
	}

	var report strings.Builder
	digests := make([]string, 0, len(releaseImages))
	for _, image := range releaseImages {
		variants := make([]*dagger.Container, 0, len(releasePlatforms))
		sboms := make(map[dagger.Platform]*dagger.File, len(releasePlatforms))
		for _, platform := range releasePlatforms {
			variant := dag.Container(dagger.ContainerOpts{Platform: platform}).
				WithRegistryAuth(host, username, password).
				From(fmt.Sprintf("%s/%s:%s-%s", registry, image.name, version, archOf(platform)))
			variants = append(variants, variant)
			sboms[platform] = m.sbomContainer(variant, image.name)
		}
		ref, err := variants[0].
			Publish(ctx, fmt.Sprintf("%s/%s:%s", registry, image.name, version), dagger.ContainerPublishOpts{PlatformVariants: variants[1:]})
		if err != nil {
			return "", fmt.Errorf("publishing the %s index: %w", image.name, err)
		}
		_, digest, found := strings.Cut(ref, "@")
		if !found {
			return "", fmt.Errorf("the registry answered %q, which names no digest", ref)
		}
		index := fmt.Sprintf("%s/%s@%s", registry, image.name, digest)
		if err := signer.signImage(ctx, index, sboms); err != nil {
			return "", err
		}
		digests = append(digests, image.name+"="+digest)
		fmt.Fprintf(&report, "image  %s\n", index)
	}

	chartDigest, err := pushChart(ctx, withRegistryLogin(withRenderableChart(m.base(src)), host, username, password), version, registry, digests)
	if err != nil {
		return "", err
	}
	if err := signer.signArtifact(ctx, fmt.Sprintf("%s/charts/rask@%s", registry, chartDigest)); err != nil {
		return "", err
	}
	fmt.Fprintf(&report, "chart  oci://%s/charts/rask:%s@%s\nsigned as %s (issuer %s)\n", registry, version, chartDigest, releaseIdentity(version), releaseIssuer)
	return report.String(), nil
}

// pushChart pins a copy of the product chart in `pusher` (a container holding it, able to reach `registry`) to `digests`
// (`<image>=sha256:<hex>`, one per release image) at `version`, packages it and pushes it to `oci://<registry>/charts`
// with `pushFlags`, returning the chart's digest.
func pushChart(ctx context.Context, pusher *dagger.Container, version, registry string, digests []string, pushFlags ...string) (string, error) {
	pin := make([]string, 0, 2*len(digests))
	for _, digest := range digests {
		pin = append(pin, "--digest", digest)
	}
	pushed, err := pusher.
		WithExec(append([]string{"uv", "run", "python", "scripts/pin_chart_release.py", "chart", "--version", version, "--repository", registry}, pin...)).
		WithExec([]string{"helm", "package", "chart", "-d", "/release"}).
		WithExec([]string{"bash", "-c", fmt.Sprintf("set -euo pipefail\nexec 2>&1\nhelm push /release/rask-%s.tgz oci://%s/charts %s", version, registry, strings.Join(pushFlags, " "))}).
		Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("publishing the chart: %w", err)
	}
	chartDigest := pushedDigest.FindStringSubmatch(pushed)
	if chartDigest == nil {
		return "", fmt.Errorf("helm push printed no digest:\n%s", pushed)
	}
	return chartDigest[1], nil
}

// withRegistryLogin logs helm in `c` in to `host` as `username` with `password`, read on stdin from a mounted file, so it
// reaches neither an argv nor the environment.
func withRegistryLogin(c *dagger.Container, host, username string, password *dagger.Secret) *dagger.Container {
	return c.WithMountedSecret("/run/registry/password", password).
		WithExec([]string{"bash", "-c", `helm registry login "$0" -u "$1" --password-stdin < /run/registry/password`, host, username})
}

// cosign is the pinned cosign, holding a registry's credential (registryCredential) when one is given.
func cosign(credential *dagger.Secret) *dagger.Container {
	return withRegistryCredential(dag.Container().From(cosignImage), credential)
}

// crane is the pinned crane, holding a registry's credential (registryCredential) when one is given. Its image's
// entrypoint is the binary, so its commands run with UseEntrypoint.
func crane(credential *dagger.Secret) *dagger.Container {
	return withRegistryCredential(dag.Container().From(craneImage), credential)
}

// withRegistryCredential hands `c` a registry's credential, a docker config, as the file go-containerregistry's keychain
// reads, owned by the user cosign's and crane's images run as, so no token reaches an argv, the environment or the trace.
// A nil credential mounts nothing.
func withRegistryCredential(c *dagger.Container, credential *dagger.Secret) *dagger.Container {
	if credential == nil {
		return c
	}
	return c.WithEnvVariable("DOCKER_CONFIG", "/run/registry").
		WithMountedSecret("/run/registry/config.json", credential, dagger.ContainerWithMountedSecretOpts{Owner: secretOwner})
}

// registryCredential is a docker config logging in to `host` as `username` with `password`, as a secret: cosign and crane
// read their registry credential from such a file.
func registryCredential(ctx context.Context, host, username string, password *dagger.Secret) (*dagger.Secret, error) {
	token, err := password.Plaintext(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading the registry token: %w", err)
	}
	config, err := json.Marshal(map[string]any{"auths": map[string]any{host: map[string]string{"auth": base64.StdEncoding.EncodeToString([]byte(username + ":" + token))}}})
	if err != nil {
		return nil, err
	}
	return dag.SetSecret("release-registry-credential", string(config)), nil
}

// releaseSigner signs, attests and verifies a release's artifacts: cosign holding what it signs with and the registry's
// credential, the flags `cosign sign` and `cosign attest` take, the flags `cosign verify` and `cosign verify-attestation`
// hold an artifact to, and crane, which reads the manifests an index holds, with the flags it reads the registry with.
// Publish and ReleaseDryRun differ only in what they put here.
type releaseSigner struct {
	cosign *dagger.Container
	sign   []string
	verify []string
	crane  *dagger.Container
	read   []string
}

// signArtifact signs `ref` and, for an index, every manifest in it, then verifies the signature.
func (s releaseSigner) signArtifact(ctx context.Context, ref string) error {
	if _, err := s.cosign.WithExec(slices.Concat([]string{"cosign", "sign", "--yes", "--recursive"}, s.sign, []string{ref})).Sync(ctx); err != nil {
		return fmt.Errorf("signing %s: %w", ref, err)
	}
	return verify(ctx, s.cosign, ref, s.verify...)
}

// signImage signs the image pushed as `ref` (an index, or one image's manifest) with signArtifact, then attests to the
// manifest `ref` holds for each platform in `sboms` that platform's CycloneDX SBOM, verifying each attestation.
func (s releaseSigner) signImage(ctx context.Context, ref string, sboms map[dagger.Platform]*dagger.File) error {
	if err := s.signArtifact(ctx, ref); err != nil {
		return err
	}
	repository, _, _ := strings.Cut(ref, "@")
	for _, platform := range slices.Sorted(maps.Keys(sboms)) {
		digest, err := s.platformDigest(ctx, ref, platform)
		if err != nil {
			return err
		}
		manifest := repository + "@" + digest
		attest := slices.Concat([]string{"cosign", "attest", "--yes", "--type", "cyclonedx", "--predicate", "/sbom.cdx.json"}, s.sign, []string{manifest})
		if _, err := s.cosign.WithMountedFile("/sbom.cdx.json", sboms[platform]).WithExec(attest).Sync(ctx); err != nil {
			return fmt.Errorf("attesting %s's SBOM: %w", manifest, err)
		}
		if _, err := s.cosign.WithExec(slices.Concat([]string{"cosign", "verify-attestation", "--type", "cyclonedx"}, s.verify, []string{manifest})).Sync(ctx); err != nil {
			return fmt.Errorf("verifying %s's SBOM attestation: %w", manifest, err)
		}
	}
	return nil
}

// platformDigest is the digest of the manifest `ref` holds for `platform`: the entry an index lists for that operating
// system and architecture, or `ref`'s own digest when it names one image's manifest.
func (s releaseSigner) platformDigest(ctx context.Context, ref string, platform dagger.Platform) (string, error) {
	out, err := s.crane.WithExec(slices.Concat([]string{"manifest"}, s.read, []string{ref}), dagger.ContainerWithExecOpts{UseEntrypoint: true}).Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("reading %s's manifest: %w", ref, err)
	}
	var manifest struct {
		Manifests []struct {
			Digest   string `json:"digest"`
			Platform struct {
				OS           string `json:"os"`
				Architecture string `json:"architecture"`
			} `json:"platform"`
		} `json:"manifests"`
	}
	if err := json.Unmarshal([]byte(out), &manifest); err != nil {
		return "", fmt.Errorf("reading %s's manifest: %w", ref, err)
	}
	if len(manifest.Manifests) == 0 {
		_, digest, _ := strings.Cut(ref, "@")
		return digest, nil
	}
	want := strings.SplitN(string(platform), "/", 3)
	for _, entry := range manifest.Manifests {
		if len(want) >= 2 && entry.Platform.OS == want[0] && entry.Platform.Architecture == want[1] {
			return entry.Digest, nil
		}
	}
	return "", fmt.Errorf("%s lists no manifest for %s", ref, platform)
}

// dryRunRegistry is the registry path a dry run publishes under: the session's own registry, bound as `rask-registry`.
const dryRunRegistry = "rask-registry:5000/ai-riksarkivet"

// ReleaseDryRun runs a release of `version` end to end and publishes nothing ([[XC-202]]): each release image is built
// for the engine's own platform, held to the scan gate PublishImage holds it to, pushed into a registry that lives only
// in this session, signed, its SBOM attested, and both verified, and the product chart is pinned to both digests, pushed
// there, signed and verified too. Signing, attesting, verifying and reading the registry run the code Publish runs, with
// a registry credential mounted as Publish mounts one (the session registry asks for none). A dry run has no release
// workflow to sign as, so it signs with a key pair generated in the session and uploads nothing to a transparency log:
// only where the signing identity comes from differs from Publish. Returns each artifact by digest with its verification.
// No git tag is involved and nothing reaches a registry outside the session.
//
// +cache="never"
func (m *Rask) ReleaseDryRun(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
	// The release's version, semver without the tag's `lakehouse-v` prefix.
	version string,
	// The commit the dry run builds, stamped into the images' labels.
	// +optional
	vcsRef string,
) (string, error) {
	if err := requireReleaseVersion(version); err != nil {
		return "", err
	}
	platform, err := dag.DefaultPlatform(ctx)
	if err != nil {
		return "", err
	}
	run := time.Now().UTC().Format(time.RFC3339Nano)
	registry := sessionRegistry(run)
	keyPassword, err := sessionSecret("release-dry-run-key-password")
	if err != nil {
		return "", err
	}
	registryPassword, err := sessionSecret("release-dry-run-registry-password")
	if err != nil {
		return "", err
	}
	credential, err := registryCredential(ctx, registryHost(dryRunRegistry), "dry-run", registryPassword)
	if err != nil {
		return "", err
	}
	signer := releaseSigner{
		cosign: cosign(credential).
			WithServiceBinding("rask-registry", registry).
			WithMountedDirectory("/keys", dryRunKeys(run, keyPassword)).
			WithSecretVariable("COSIGN_PASSWORD", keyPassword),
		sign:   []string{"--key", "/keys/cosign.key", "--signing-config", "/keys/signing-config.json", "--allow-http-registry", "--allow-insecure-registry"},
		verify: []string{"--key", "/keys/cosign.pub", "--insecure-ignore-tlog=true", "--allow-http-registry", "--allow-insecure-registry"},
		crane:  crane(credential).WithServiceBinding("rask-registry", registry),
		read:   []string{"--insecure"},
	}

	var report strings.Builder
	fmt.Fprintf(&report, "dry run of lakehouse-v%s on %s, published nowhere\n", version, platform)
	build := releaseBuild{src: src, built: time.Now().UTC().Format(time.RFC3339), vcsRef: vcsRef, version: version, platform: platform}
	digests := make([]string, 0, len(releaseImages))
	for _, image := range releaseImages {
		container, err := m.scannedImage(ctx, build, image)
		if err != nil {
			return "", err
		}
		digest, err := pushTarball(ctx, signer.crane, container, fmt.Sprintf("%s/%s:%s", dryRunRegistry, image.name, version))
		if err != nil {
			return "", fmt.Errorf("pushing %s: %w", image.name, err)
		}
		ref := fmt.Sprintf("%s/%s@%s", dryRunRegistry, image.name, digest)
		if err := signer.signImage(ctx, ref, map[dagger.Platform]*dagger.File{platform: m.sbomContainer(container, image.name)}); err != nil {
			return "", err
		}
		digests = append(digests, image.name+"="+digest)
		fmt.Fprintf(&report, "image  %s  passed the scan gate (no fixable %s), signed and its SBOM attested, both verified\n", ref, releaseScanSeverity)
	}

	chartDigest, err := pushChart(ctx, withRenderableChart(m.base(src)).WithServiceBinding("rask-registry", registry), version, dryRunRegistry, digests, "--plain-http")
	if err != nil {
		return "", err
	}
	chart := fmt.Sprintf("%s/charts/rask@%s", dryRunRegistry, chartDigest)
	if err := signer.signArtifact(ctx, chart); err != nil {
		return "", err
	}
	fmt.Fprintf(&report, "chart  oci://%s/charts/rask:%s@%s  pins %s, signed, signature verified\n", dryRunRegistry, version, chartDigest, strings.Join(digests, " "))
	return report.String(), nil
}

// sessionSecret is a random secret that exists only in this session, named `name` and a random suffix that shares no
// byte with it.
func sessionSecret(name string) (*dagger.Secret, error) {
	raw := make([]byte, 40)
	if _, err := rand.Read(raw); err != nil {
		return nil, fmt.Errorf("generating %s: %w", name, err)
	}
	return dag.SetSecret(name+"-"+hex.EncodeToString(raw[32:]), base64.RawURLEncoding.EncodeToString(raw[:32])), nil
}

// dryRunKeys generates a dry run's signing key pair in the session, the private key encrypted under `password`, beside a
// signing config that names no certificate authority, transparency log or timestamp authority, so a signature made with
// it is recorded nowhere outside the session (cosign v3 takes that in place of the deprecated `--tlog-upload=false`).
// Nothing leaves the session: the pair and the config are a directory in the engine and the password a secret.
func dryRunKeys(run string, password *dagger.Secret) *dagger.Directory {
	return dag.Container().From(cosignImage).
		WithSecretVariable("COSIGN_PASSWORD", password).
		// The run's own key pair: `run` keys the exec, so the engine never answers it from another run's cache.
		WithEnvVariable("RUN", run).
		WithWorkdir("/keys").
		WithExec([]string{"cosign", "generate-key-pair"}).
		WithExec([]string{"cosign", "signing-config", "create", "--out", "/keys/signing-config.json"}).
		Directory("/keys")
}

// pushTarball pushes `container`'s image, as the OCI tarball it exports, with `pusher` (crane, bound to the session's
// plain-HTTP registry) to `ref`, returning its digest. crane pushes it, because the engine pushes only over HTTPS to a
// registry its own configuration does not mark insecure.
func pushTarball(ctx context.Context, pusher *dagger.Container, container *dagger.Container, ref string) (string, error) {
	out, err := pusher.
		WithMountedFile("/image.tar", container.AsTarball(), dagger.ContainerWithMountedFileOpts{Owner: secretOwner}).
		WithExec([]string{"push", "--insecure", "/image.tar", ref}, dagger.ContainerWithExecOpts{UseEntrypoint: true}).
		Stdout(ctx)
	if err != nil {
		return "", err
	}
	digest := imageDigest.FindString(out)
	if digest == "" {
		return "", fmt.Errorf("crane push printed no digest:\n%s", out)
	}
	return digest, nil
}

// verify checks `ref`'s signature with `verifier` and the flags it needs (a public key, or a certificate identity).
func verify(ctx context.Context, verifier *dagger.Container, ref string, flags ...string) error {
	args := append([]string{"cosign", "verify"}, flags...)
	if _, err := verifier.WithExec(append(args, ref)).Sync(ctx); err != nil {
		return fmt.Errorf("verifying %s: %w", ref, err)
	}
	return nil
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
// at `version`, until every workload is Ready (scripts/install_from_empty.sh). The release's catalog image is verified as
// signed by the release workflow at that version's tag (releaseIdentity, [[XC-202]]), then copied by the digest its chart
// pins into a registry inside the session, which the cluster mirrors, so the cluster runs the published bytes as the
// local estate it is (image.localImages); nothing is built.
//
// A client integrates as its production release does ([[XC-189]]): `values` is the chart values that release sets
// (`networkPolicy.objectStoreExtraFrom`), `dexClient` registers the client's own sign-in client with Dex, trusted by
// the lakehouse's audience, and the client's roles are granted to its own identity at the catalog's access doors
// ([[LH-563]]), as the estate admin. A key the stack sets with `--set`
// (scripts/stack_values.sh) is the stack's, because Helm applies `--set` over every values file: `route.enabled` among
// them, since the session's cluster runs no Gateway. A client outside the cluster asks the result for its
// ServiceAccount's token (ServiceAccountToken).
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
	// The chart values the client's own release sets; a key the stack sets with --set stays the stack's.
	// +optional
	values *dagger.File,
	// The client's own Dex sign-in client, which the lakehouse's audience trusts; empty registers none.
	// +optional
	dexClient string,
	// The sign-in client's secret; required with dexClient.
	// +optional
	dexClientSecret *dagger.Secret,
	// The sign-in client's OIDC redirect URIs.
	// +optional
	dexRedirectUris []string,
) (*Lakehouse, error) {
	if err := requireReleaseVersion(version); err != nil {
		return nil, err
	}
	if (dexClient == "") != (dexClientSecret == nil) {
		return nil, fmt.Errorf("dexClient and dexClientSecret are given together")
	}
	if dexClient == "" && len(dexRedirectUris) > 0 {
		return nil, fmt.Errorf("dexRedirectUris names a sign-in client's redirects, and no dexClient is given")
	}
	if dexClient != "" && !dexClientID.MatchString(dexClient) {
		return nil, fmt.Errorf("dexClient %q is not a client id (%s)", dexClient, dexClientID)
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
	// The image runs only if the release workflow signed it at this version's tag: a chart naming another image, or an
	// image another identity pushed, stands nothing up.
	if err := verify(ctx, cosign(nil), image, "--certificate-identity", releaseIdentity(version), "--certificate-oidc-issuer", releaseIssuer); err != nil {
		return nil, fmt.Errorf("release %s's catalog image carries no signature from %s: %w", version, releaseIdentity(version), err)
	}

	registry, err := sessionRegistry(run).Start(ctx)
	if err != nil {
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
	// A client's e2e drives the doors, not the console, so the session's registry holds the catalog image alone.
	install := cluster.WithEnvVariable("CHART", chart).WithEnvVariable("CHART_VERSION", version).WithEnvVariable("CONSOLE_ENABLED", "false")
	if values != nil {
		install = install.WithMountedFile("/client/values.yaml", values).WithEnvVariable("VALUES", "/client/values.yaml")
	}
	if dexClient != "" {
		uris, err := json.Marshal(append([]string{}, dexRedirectUris...))
		if err != nil {
			return nil, err
		}
		install = install.
			WithEnvVariable("DEX_CLIENT", dexClient).
			WithEnvVariable("DEX_REDIRECT_URIS", string(uris)).
			WithSecretVariable("DEX_CLIENT_SECRET", dexClientSecret)
	}
	if _, err := runInstall(ctx, install, "lakehouse "+version); err != nil {
		return nil, err
	}

	service, err := cluster.
		WithExposedPort(2333).
		WithExposedPort(8000).
		WithExposedPort(5556).
		WithExposedPort(9000).
		AsService(dagger.ContainerAsServiceOpts{Args: []string{"bash", "-c", clusterReady +
			"kubectl port-forward --address 0.0.0.0 svc/rask-catalog 2333:2333 & " +
			"kubectl port-forward --address 0.0.0.0 svc/rask-lineage 8000:8000 & " +
			"kubectl port-forward --address 0.0.0.0 svc/rask-dex 5556:5556 & " +
			"kubectl port-forward --address 0.0.0.0 svc/rask-rustfs 9000:9000 & wait -n; exit 1"}}).
		Start(ctx)
	if err != nil {
		return nil, err
	}
	return &Lakehouse{Service: service, Cluster: cluster}, nil
}

var (
	dexClientID  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)
	k8sAccount   = regexp.MustCompile(`^([a-z0-9]([a-z0-9-]*[a-z0-9])?):([a-z0-9]([a-z0-9.-]*[a-z0-9])?)$`)
	doorAudience = map[string]bool{"rask-catalog": true, "rask-lineage": true}
)

// clientAccountLabel marks a ServiceAccount ServiceAccountToken created for a client, the only kind it mints for.
const clientAccountLabel = "rask.dev/client-account"

// Lakehouse is a published release the Lakehouse function stood up, for the life of the session.
type Lakehouse struct {
	// The lakehouse as one service: the catalog on 2333, lineage on 8000 (its OpenLineage intake is
	// `POST /api/v1/lineage`), Dex on 5556 and the object store on 9000. Bind it under the in-cluster names,
	// `rask-catalog`, `rask-lineage`, `rask-dex` and `rask-rustfs`, so a token's issuer (http://rask-dex:5556/dex) and a
	// vended endpoint resolve as the catalog names them. The estate admin is alice@example.com (password `password`),
	// whom the Dex fixture serves.
	Service *dagger.Service
	// The installer bound to the session's cluster, which ServiceAccountToken asks.
	// +private
	Cluster *dagger.Container
}

// ServiceAccountToken is a token for the ServiceAccount `account` (`<namespace>:<name>`) with the audience of one
// lakehouse door, `rask-catalog` (the decision door included) or `rask-lineage` (the OpenLineage intake): what the
// account's pod would project, for a client running outside the cluster. The account is created as the client's own
// release would render it, labelled as a client's, and holds only what the catalog's doors grant it ([[LH-563]]); an
// account the release or a fixture renders is refused, so a client cannot act as a lakehouse service. The token lives `duration` (10m at the least, the API server's floor).
//
// +cache="session"
func (l *Lakehouse) ServiceAccountToken(
	ctx context.Context,
	account string,
	audience string,
	// +optional
	// +default="1h"
	duration string,
) (*dagger.Secret, error) {
	parts := k8sAccount.FindStringSubmatch(account)
	if parts == nil {
		return nil, fmt.Errorf("account %q is not <namespace>:<serviceaccount>", account)
	}
	if !doorAudience[audience] {
		return nil, fmt.Errorf("audience %q is not a lakehouse door's (rask-catalog, rask-lineage)", audience)
	}
	if _, err := time.ParseDuration(duration); err != nil {
		return nil, fmt.Errorf("duration %q: %w", duration, err)
	}
	namespace, name := parts[1], parts[3]
	token, err := l.Cluster.
		WithEnvVariable("TOKEN_REQUEST", time.Now().UTC().Format(time.RFC3339Nano)).
		WithExec([]string{"bash", "-c", clusterReady +
			fmt.Sprintf("kubectl create namespace %s --dry-run=client -o yaml | kubectl apply -f - >/dev/null\n", namespace) +
			fmt.Sprintf("if kubectl -n %[1]s get serviceaccount %[2]s >/dev/null 2>&1; then\n"+
				"  [ \"$(kubectl -n %[1]s get serviceaccount %[2]s -o jsonpath='{.metadata.labels.%[3]s}')\" = true ] ||"+
				" { echo \"%[1]s:%[2]s is the release's or a fixture's account, not a client's\" >&2; exit 3; }\n"+
				"else\n  kubectl -n %[1]s create serviceaccount %[2]s >/dev/null && kubectl -n %[1]s label serviceaccount %[2]s %[4]s=true >/dev/null\nfi\n",
				namespace, name, strings.ReplaceAll(clientAccountLabel, ".", "\\."), clientAccountLabel) +
			fmt.Sprintf("kubectl -n %s create token %s --audience %s --duration %s > /tmp/token\n", namespace, name, audience, duration)}).
		File("/tmp/token").
		Contents(ctx)
	if err != nil {
		return nil, fmt.Errorf("minting %s's token for %s: %w", account, audience, err)
	}
	return dag.SetSecret(fmt.Sprintf("lakehouse-token-%s-%s-%s-%s", namespace, name, audience, duration), strings.TrimSpace(token)), nil
}
