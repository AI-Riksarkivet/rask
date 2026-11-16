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
	onlyDigest     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
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
		scanned, err := scannedReference(ref)
		if err != nil {
			return "", err
		}
		pushed = append(pushed, scanned)
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
	// The references PublishImage printed on each release runner, `<registry>/<image>@sha256:<hex>` separated by white
	// space: each index is built from exactly these, the images that passed the scan gate.
	pushed *dagger.File,
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
	handed, err := pushed.Contents(ctx)
	if err != nil {
		return "", fmt.Errorf("reading the scanned images handed over: %w", err)
	}
	children, err := signer.releaseChildren(ctx, registry, strings.Fields(handed))
	if err != nil {
		return "", err
	}
	sboms := make(map[string]*dagger.File)
	for _, image := range releaseImages {
		for platform, ref := range children[image.name] {
			sboms[ref] = m.sbomContainer(dag.Container(dagger.ContainerOpts{Platform: platform}).WithRegistryAuth(host, username, password).From(ref), image.name)
		}
	}
	published, err := signer.publishRelease(ctx, withRegistryLogin(withRenderableChart(m.base(src)), host, username, password), registry, version, children, sboms)
	if err != nil {
		return "", err
	}
	return published.report() + fmt.Sprintf("signed as %s (issuer %s)\n", releaseIdentity(version), releaseIssuer), nil
}

// scannedReference is `published`, the reference a push answered (`<registry>/<image>:<tag>@sha256:<hex>`), by its
// digest alone, as PublishImage prints it and releaseChildren takes it: the tag is what may move.
func scannedReference(published string) (string, error) {
	name, digest, found := strings.Cut(published, "@")
	if !found || !onlyDigest.MatchString(digest) {
		return "", fmt.Errorf("the registry answered %q, which names no digest", published)
	}
	if tag := strings.LastIndex(name, ":"); tag > strings.LastIndex(name, "/") {
		name = name[:tag]
	}
	return name + "@" + digest, nil
}

// pushedReference is a reference PublishImage returns: `<registry>/<image>@sha256:<hex>`.
var pushedReference = regexp.MustCompile(`^(.+)/([a-z0-9][a-z0-9-]*)@(sha256:[0-9a-f]{64})$`)

// releaseChildren binds `pushed`, the references PublishImage returned on each release runner, to the release image and
// platform each holds, reading the platform off the image's own config: every release image under `registry` needs
// exactly one image manifest per release platform, and a reference naming anything else refuses the release. A release's
// indexes are built from these alone, so what is signed is what passed the scan gate, whatever a tag names by then.
func (s releaseSigner) releaseChildren(ctx context.Context, registry string, pushed []string) (map[string]map[dagger.Platform]string, error) {
	children := make(map[string]map[dagger.Platform]string, len(releaseImages))
	for _, image := range releaseImages {
		children[image.name] = make(map[dagger.Platform]string, len(releasePlatforms))
	}
	for _, ref := range pushed {
		parts := pushedReference.FindStringSubmatch(strings.TrimSpace(ref))
		if parts == nil || parts[1] != registry || children[parts[2]] == nil {
			return nil, fmt.Errorf("%q is no release image under %s named by its digest", ref, registry)
		}
		platform, err := s.platformOf(ctx, parts[0])
		if err != nil {
			return nil, err
		}
		if !slices.Contains(releasePlatforms, platform) {
			return nil, fmt.Errorf("%s is built for %s, which a release does not publish", parts[0], platform)
		}
		if held, found := children[parts[2]][platform]; found {
			return nil, fmt.Errorf("%s and %s are both %s for %s", held, parts[0], parts[2], platform)
		}
		children[parts[2]][platform] = parts[0]
	}
	for _, image := range releaseImages {
		for _, platform := range releasePlatforms {
			if _, found := children[image.name][platform]; !found {
				return nil, fmt.Errorf("no scanned %s for %s was handed over", image.name, platform)
			}
		}
	}
	return children, nil
}

// platformOf is the platform the image manifest `ref` names was built for, read off its config; an index is refused, since
// a release's children are images.
func (s releaseSigner) platformOf(ctx context.Context, ref string) (dagger.Platform, error) {
	held, err := s.indexChildren(ctx, ref)
	if err != nil {
		return "", err
	}
	if held != nil {
		return "", fmt.Errorf("%s is an index, not one platform's image", ref)
	}
	config, err := s.crane.WithExec(slices.Concat([]string{"config"}, s.read, []string{ref}), dagger.ContainerWithExecOpts{UseEntrypoint: true}).Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("reading %s's config: %w", ref, err)
	}
	var built struct {
		OS           string `json:"os"`
		Architecture string `json:"architecture"`
	}
	if err := json.Unmarshal([]byte(config), &built); err != nil {
		return "", fmt.Errorf("reading %s's config: %w", ref, err)
	}
	if built.OS == "" || built.Architecture == "" {
		return "", fmt.Errorf("%s's config names no platform", ref)
	}
	return dagger.Platform(built.OS + "/" + built.Architecture), nil
}

// publishedRelease is what publishRelease pushed: each release image's index digest, under the image's name, and the
// chart's digest.
type publishedRelease struct {
	registry string
	version  string
	indexes  map[string]string
	chart    string
}

// pins are the indexes as the chart's `image.digests` takes them, `<image>=sha256:<hex>`, in releaseImages' order.
func (p publishedRelease) pins() []string {
	pins := make([]string, 0, len(releaseImages))
	for _, image := range releaseImages {
		pins = append(pins, image.name+"="+p.indexes[image.name])
	}
	return pins
}

func (p publishedRelease) report() string {
	var report strings.Builder
	for _, image := range releaseImages {
		fmt.Fprintf(&report, "image  %s/%s@%s\n", p.registry, image.name, p.indexes[image.name])
	}
	fmt.Fprintf(&report, "chart  oci://%s/charts/rask:%s@%s\n", p.registry, p.version, p.chart)
	return report.String()
}

// publishRelease joins each release image's `children` (releaseChildren) into one index, `<registry>/<image>:<version>`,
// refusing an index the registry then holds with any other child, signs it, and attests to each child its SBOM (`sboms`,
// by the child's reference); then pins a copy of the product chart in `chartPusher` to the indexes' digests, pushes it
// with `pushFlags` and signs it. Publish and ReleaseHandoff differ only in what they hand it.
func (s releaseSigner) publishRelease(
	ctx context.Context,
	chartPusher *dagger.Container,
	registry, version string,
	children map[string]map[dagger.Platform]string,
	sboms map[string]*dagger.File,
	pushFlags ...string,
) (publishedRelease, error) {
	published := publishedRelease{registry: registry, version: version, indexes: make(map[string]string, len(releaseImages))}
	for _, image := range releaseImages {
		digest, err := s.joinIndex(ctx, fmt.Sprintf("%s/%s:%s", registry, image.name, version), children[image.name])
		if err != nil {
			return publishedRelease{}, err
		}
		index := fmt.Sprintf("%s/%s@%s", registry, image.name, digest)
		attested := make(map[dagger.Platform]*dagger.File, len(children[image.name]))
		for platform, ref := range children[image.name] {
			attested[platform] = sboms[ref]
		}
		if err := s.signImage(ctx, index, attested); err != nil {
			return publishedRelease{}, err
		}
		published.indexes[image.name] = digest
	}
	chart, err := pushChart(ctx, chartPusher, version, registry, published.pins(), pushFlags...)
	if err != nil {
		return publishedRelease{}, err
	}
	if err := s.signArtifact(ctx, fmt.Sprintf("%s/charts/rask@%s", registry, chart)); err != nil {
		return publishedRelease{}, err
	}
	published.chart = chart
	return published, nil
}

// joinIndex pushes the index `tag` from `children` (one image manifest by digest per platform) and returns its digest,
// once the index the registry holds under that digest lists exactly those children. crane joins them by digest, so no
// tag is resolved on the way.
func (s releaseSigner) joinIndex(ctx context.Context, tag string, children map[dagger.Platform]string) (string, error) {
	join := slices.Concat([]string{"index", "append"}, s.read)
	for _, platform := range slices.Sorted(maps.Keys(children)) {
		join = append(join, "--manifest", children[platform])
	}
	out, err := s.crane.WithExec(append(join, "--tag", tag), dagger.ContainerWithExecOpts{UseEntrypoint: true}).Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("joining %s: %w", tag, err)
	}
	digest := imageDigest.FindString(out)
	if digest == "" {
		return "", fmt.Errorf("crane printed no digest for %s:\n%s", tag, out)
	}
	// The tag is the last `:`'s: a registry's own may carry a port.
	repository := tag[:strings.LastIndex(tag, ":")]
	held, err := s.indexChildren(ctx, repository+"@"+digest)
	if err != nil {
		return "", err
	}
	if len(held) != len(children) {
		return "", fmt.Errorf("%s@%s holds %d manifests, not the %d it was joined from", repository, digest, len(held), len(children))
	}
	for platform, ref := range children {
		if _, want, _ := strings.Cut(ref, "@"); held[platform] != want {
			return "", fmt.Errorf("%s@%s holds %s for %s, not the scanned %s", repository, digest, held[platform], platform, want)
		}
	}
	return digest, nil
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
	held, err := s.indexChildren(ctx, ref)
	if err != nil {
		return "", err
	}
	if held == nil {
		_, digest, _ := strings.Cut(ref, "@")
		return digest, nil
	}
	if digest, found := held[platform]; found {
		return digest, nil
	}
	return "", fmt.Errorf("%s lists no manifest for %s", ref, platform)
}

// indexChildren is the manifest digest the index `ref` lists for each `<os>/<architecture>`, or nil when `ref` names one
// image's manifest. An index listing one platform twice is refused, since which of the two a client pulls is not the
// release's choice.
func (s releaseSigner) indexChildren(ctx context.Context, ref string) (map[dagger.Platform]string, error) {
	out, err := s.crane.WithExec(slices.Concat([]string{"manifest"}, s.read, []string{ref}), dagger.ContainerWithExecOpts{UseEntrypoint: true}).Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading %s's manifest: %w", ref, err)
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
		return nil, fmt.Errorf("reading %s's manifest: %w", ref, err)
	}
	if len(manifest.Manifests) == 0 {
		return nil, nil
	}
	held := make(map[dagger.Platform]string, len(manifest.Manifests))
	for _, entry := range manifest.Manifests {
		platform := dagger.Platform(entry.Platform.OS + "/" + entry.Platform.Architecture)
		if _, found := held[platform]; found {
			return nil, fmt.Errorf("%s lists %s twice", ref, platform)
		}
		held[platform] = entry.Digest
	}
	return held, nil
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
	signer, err := sessionSigner(ctx, registry, run)
	if err != nil {
		return "", err
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

// sessionSigner is a releaseSigner on `registry`, one that lives only in this session, bound as `rask-registry`: it signs
// with a key pair generated in the session for `keys` and uploads nothing to a transparency log, and holds a registry
// credential mounted as Publish mounts one (the session registry asks for none).
func sessionSigner(ctx context.Context, registry *dagger.Service, keys string) (releaseSigner, error) {
	keyPassword, err := sessionSecret("release-session-key-password")
	if err != nil {
		return releaseSigner{}, err
	}
	registryPassword, err := sessionSecret("release-session-registry-password")
	if err != nil {
		return releaseSigner{}, err
	}
	credential, err := registryCredential(ctx, registryHost(dryRunRegistry), "session", registryPassword)
	if err != nil {
		return releaseSigner{}, err
	}
	return releaseSigner{
		cosign: cosign(credential).
			WithServiceBinding("rask-registry", registry).
			WithMountedDirectory("/keys", dryRunKeys(keys, keyPassword)).
			WithSecretVariable("COSIGN_PASSWORD", keyPassword),
		sign:   []string{"--key", "/keys/cosign.key", "--signing-config", "/keys/signing-config.json", "--allow-http-registry", "--allow-insecure-registry"},
		verify: []string{"--key", "/keys/cosign.pub", "--insecure-ignore-tlog=true", "--allow-http-registry", "--allow-insecure-registry"},
		crane:  crane(credential).WithServiceBinding("rask-registry", registry),
		read:   []string{"--insecure"},
	}, nil
}

// ReleaseHandoff proves a release signs only what passed its scan gate ([[XC-215]]). In a registry that lives only in this
// session it pushes, for each release image and platform, an image standing for the one PublishImage scanned (A), then
// moves that image's `<version>-<arch>` tag to another (B), as anything able to push could between the release's two
// jobs. Publishing from the references the scan returned must build each index from exactly the A images, and the chart
// fetched as Lakehouse fetches one (verifiedChart) must pin exactly those indexes. A handover that misses an image, names
// one twice, names another repository or names another image's digest is refused; and once the same version's chart tag
// is moved to a chart granting a workload cluster-admin, that chart is refused whether nobody signed it or another key did
// ([[XC-216]]). Nothing is built or scanned: each image is one file on an empty filesystem for its platform, so both architectures run
// on any runner.
//
// +cache="never"
func (m *Rask) ReleaseHandoff(
	ctx context.Context,
	// +ignore=["**/.venv", ".git", "**/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build",
	//          "**/.pytest_cache", "**/__pycache__", "**/.ruff_cache", "chart/charts", "deploy/fixtures/*/chart/charts"]
	// +defaultPath="/"
	src *dagger.Directory,
	// +optional
	// +default="0.0.0-handoff"
	version string,
) (string, error) {
	if err := requireReleaseVersion(version); err != nil {
		return "", err
	}
	run := time.Now().UTC().Format(time.RFC3339Nano)
	registry := sessionRegistry(run)
	signer, err := sessionSigner(ctx, registry, run)
	if err != nil {
		return "", err
	}
	fetcher := withCrane(withRenderableChart(m.base(src))).WithServiceBinding("rask-registry", registry)

	var report strings.Builder
	pushed := make([]string, 0, len(releaseImages)*len(releasePlatforms))
	moved := make(map[string]string, cap(pushed))
	sboms := make(map[string]*dagger.File, cap(pushed))
	for _, image := range releaseImages {
		for _, platform := range releasePlatforms {
			tag := fmt.Sprintf("%s/%s:%s-%s", dryRunRegistry, image.name, version, archOf(platform))
			scanned := handoffImage(platform, image.name+" scanned "+run)
			a, err := pushTarball(ctx, signer.crane, scanned, tag)
			if err != nil {
				return "", fmt.Errorf("pushing %s: %w", tag, err)
			}
			b, err := pushTarball(ctx, signer.crane, handoffImage(platform, image.name+" substituted "+run), tag)
			if err != nil {
				return "", fmt.Errorf("moving %s: %w", tag, err)
			}
			now, err := signer.crane.WithExec(slices.Concat([]string{"digest"}, signer.read, []string{tag}), dagger.ContainerWithExecOpts{UseEntrypoint: true}).Stdout(ctx)
			if err != nil {
				return "", fmt.Errorf("reading %s: %w", tag, err)
			}
			if a == b || strings.TrimSpace(now) != b {
				return "", fmt.Errorf("%s was not moved off the scanned %s (it names %s)", tag, a, strings.TrimSpace(now))
			}
			// What a push answers names the tag too; PublishImage prints it by its digest alone.
			ref, err := scannedReference(fmt.Sprintf("%s@%s", tag, a))
			if err != nil {
				return "", err
			}
			pushed = append(pushed, ref)
			moved[ref] = b
			sboms[ref] = m.sbomContainer(scanned, image.name)
		}
	}

	// Each handover is refused for its own reason: an image's digest is held only by the repository it was pushed to, so
	// naming it under another release image's repository is refused when that repository cannot read it.
	refused := map[string]struct {
		pushed []string
		reason string
	}{
		"an image missing":       {pushed[1:], "was handed over"},
		"an image named twice":   {append(slices.Clone(pushed), fmt.Sprintf("%s/%s@%s", dryRunRegistry, releaseImages[0].name, moved[pushed[0]])), "are both"},
		"another repository":     {append([]string{strings.Replace(pushed[0], dryRunRegistry, registryHost(dryRunRegistry)+"/elsewhere", 1)}, pushed[1:]...), "is no release image under"},
		"another image's digest": {append([]string{strings.Replace(pushed[len(releasePlatforms)], releaseImages[1].name, releaseImages[0].name, 1)}, pushed[1:]...), "reading"},
	}
	for _, handover := range slices.Sorted(maps.Keys(refused)) {
		_, err := signer.releaseChildren(ctx, dryRunRegistry, refused[handover].pushed)
		if err == nil || !strings.Contains(err.Error(), refused[handover].reason) {
			return "", fmt.Errorf("a handover with %s was not refused for its own reason (%q): %v", handover, refused[handover].reason, err)
		}
		fmt.Fprintf(&report, "refused  a handover with %s\n", handover)
	}

	children, err := signer.releaseChildren(ctx, dryRunRegistry, pushed)
	if err != nil {
		return "", err
	}
	published, err := signer.publishRelease(ctx, fetcher, dryRunRegistry, version, children, sboms, "--plain-http")
	if err != nil {
		return "", err
	}
	for _, image := range releaseImages {
		index := fmt.Sprintf("%s/%s@%s", dryRunRegistry, image.name, published.indexes[image.name])
		held, err := signer.indexChildren(ctx, index)
		if err != nil {
			return "", err
		}
		for _, platform := range releasePlatforms {
			scanned := children[image.name][platform]
			if _, want, _ := strings.Cut(scanned, "@"); held[platform] != want {
				return "", fmt.Errorf("%s holds %s for %s, not the scanned %s (the moved tag names %s)", index, held[platform], platform, want, moved[scanned])
			}
		}
		fmt.Fprintf(&report, "index    %s holds the scanned %s and none of the moved tags' images\n", index, strings.Join(slices.Sorted(maps.Values(held)), " "))
	}

	chart, err := signer.verifiedChart(ctx, fetcher, dryRunRegistry+"/charts/rask", version)
	if err != nil {
		return "", err
	}
	pins, err := chartPins(ctx, fetcher, chart)
	if err != nil {
		return "", err
	}
	if !maps.Equal(pins, published.indexes) {
		return "", fmt.Errorf("the verified chart pins %v, not the published indexes %v", pins, published.indexes)
	}
	fmt.Fprintf(&report, "chart    oci://%s/charts/rask:%s@%s verified, pins %s\n", dryRunRegistry, version, published.chart, strings.Join(published.pins(), " "))

	// The same version's tag moved to a chart that grants a workload cluster-admin, as anything able to push could: the
	// chart is refused whether nobody signed it or another key did.
	tampered, err := pushChart(ctx, fetcher.WithNewFile("/src/chart/templates/zz-handoff.yaml", handoffBinding), version, dryRunRegistry, published.pins(), "--plain-http")
	if err != nil {
		return "", err
	}
	impostor, err := sessionSigner(ctx, registry, run+"-impostor")
	if err != nil {
		return "", err
	}
	for _, how := range []string{"unsigned", "signed by another key"} {
		if how == "signed by another key" {
			if err := impostor.signArtifact(ctx, fmt.Sprintf("%s/charts/rask@%s", dryRunRegistry, tampered)); err != nil {
				return "", err
			}
		}
		_, err := signer.verifiedChart(ctx, fetcher, dryRunRegistry+"/charts/rask", version)
		if err == nil || !strings.Contains(err.Error(), "carries no signature") {
			return "", fmt.Errorf("the tampered chart %s, %s, was not refused as unsigned by the release: %v", tampered, how, err)
		}
		fmt.Fprintf(&report, "refused  the same version's tag moved to a chart granting cluster-admin, %s\n", how)
	}
	return report.String(), nil
}

// handoffBinding is what the tampered chart in ReleaseHandoff adds: a workload's ServiceAccount bound to cluster-admin.
const handoffBinding = `apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: handoff-tampered
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: rask-sa-catalog
    namespace: default
`

// handoffImage is an image for `platform` holding one file that says `what`: an image of that platform, built without
// running anything, so any runner makes one for every release platform.
func handoffImage(platform dagger.Platform, what string) *dagger.Container {
	return dag.Container(dagger.ContainerOpts{Platform: platform}).WithNewFile("/release", what+" "+string(platform)+"\n")
}

// helmChartContent is the media type of the layer an OCI helm chart's manifest carries the chart archive in.
const helmChartContent = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"

// withCrane puts the pinned crane on `c`'s PATH, for a container that also runs a shell.
func withCrane(c *dagger.Container) *dagger.Container {
	return c.WithFile("/usr/local/bin/crane", dag.Container().From(craneImage).File("/ko-app/crane"), dagger.ContainerWithFileOpts{Permissions: 0o755})
}

// verifiedChart is release `version` of the chart `chart` (`<registry>/charts/rask`, no scheme), fetched in `fetcher`
// (withCrane's, able to reach the registry) once `s` has verified the signature on the digest the version names. The
// archive is fetched by that digest, and its own digest is checked against the manifest's, so what is installed is what
// was verified, whatever the tag names by then.
func (s releaseSigner) verifiedChart(ctx context.Context, fetcher *dagger.Container, chart, version string) (*dagger.File, error) {
	read := func(args ...string) (string, error) {
		return fetcher.WithExec(slices.Concat([]string{"crane"}, args[:1], s.read, args[1:])).Stdout(ctx)
	}
	// The tag is resolved afresh on every call: Dagger reuses an exec whose inputs it has seen, and a tag is what moves.
	out, err := fetcher.WithEnvVariable("RESOLVED_AT", time.Now().UTC().Format(time.RFC3339Nano)).
		WithExec(slices.Concat([]string{"crane", "digest"}, s.read, []string{chart + ":" + version})).Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading release %s's chart: %w", version, err)
	}
	digest := strings.TrimSpace(out)
	if !onlyDigest.MatchString(digest) {
		return nil, fmt.Errorf("crane named no digest for release %s's chart: %q", version, out)
	}
	ref := chart + "@" + digest
	if err := verify(ctx, s.cosign, ref, s.verify...); err != nil {
		return nil, fmt.Errorf("release %s's chart carries no signature from the release: %w", version, err)
	}
	manifest, err := read("manifest", ref)
	if err != nil {
		return nil, fmt.Errorf("reading %s's manifest: %w", ref, err)
	}
	var shape struct {
		Layers []struct {
			MediaType string `json:"mediaType"`
			Digest    string `json:"digest"`
		} `json:"layers"`
	}
	if err := json.Unmarshal([]byte(manifest), &shape); err != nil {
		return nil, fmt.Errorf("reading %s's manifest: %w", ref, err)
	}
	var archives []string
	for _, layer := range shape.Layers {
		if layer.MediaType == helmChartContent && onlyDigest.MatchString(layer.Digest) {
			archives = append(archives, layer.Digest)
		}
	}
	if len(archives) != 1 {
		return nil, fmt.Errorf("%s holds %d chart archives, not one", ref, len(archives))
	}
	return fetcher.WithExec(slices.Concat([]string{"bash", "-c",
		`set -euo pipefail; crane blob "${@:3}" "$1@$2" > /tmp/chart.tgz; [ "sha256:$(sha256sum /tmp/chart.tgz | cut -d' ' -f1)" = "$2" ]`,
		"verified-chart", chart, archives[0]}, s.read)).
		File("/tmp/chart.tgz"), nil
}

// chartPins is the `image.digests` the chart archive `chart` pins, read in `reader` (a container with helm and uv).
func chartPins(ctx context.Context, reader *dagger.Container, chart *dagger.File) (map[string]string, error) {
	out, err := reader.WithMountedFile("/tmp/pinned.tgz", chart).
		WithExec([]string{"bash", "-c", "set -euo pipefail; helm show values /tmp/pinned.tgz | uv run --no-project --with pyyaml python -c " +
			"'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)[\"image\"][\"digests\"]))'"}).
		Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading the chart's pins: %w", err)
	}
	pins := map[string]string{}
	if err := json.Unmarshal([]byte(out), &pins); err != nil {
		return nil, fmt.Errorf("reading the chart's pins: %w", err)
	}
	return pins, nil
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
	repository, found := strings.CutPrefix(chart, "oci://")
	if !found {
		return nil, fmt.Errorf("chart %q is not an oci:// reference", chart)
	}
	run := time.Now().UTC().Format(time.RFC3339Nano)
	// The chart is installed only if the release workflow signed it at this version's tag, and as the archive whose digest
	// that signature covers, so a tag moved after the release installs nothing else.
	release := releaseSigner{cosign: cosign(nil), verify: []string{"--certificate-identity", releaseIdentity(version), "--certificate-oidc-issuer", releaseIssuer}}
	fetcher := withCrane(withRenderableChart(m.tools(src)))
	archive, err := release.verifiedChart(ctx, fetcher, repository, version)
	if err != nil {
		return nil, err
	}
	tool := fetcher.WithMountedFile(lakehouseChart, archive)

	pinned, err := tool.WithExec([]string{"bash", "-c", fmt.Sprintf("set -euo pipefail\nhelm show values %s | uv run --no-project --with pyyaml python -c "+
		"'import sys, yaml; i = yaml.safe_load(sys.stdin)[\"image\"]; print(i[\"repository\"] + \"/%s@\" + i[\"digests\"][\"%s\"])'",
		lakehouseChart, catalogImageName, catalogImageName)}).Stdout(ctx)
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
	install := cluster.WithEnvVariable("CHART", lakehouseChart).WithEnvVariable("CONSOLE_ENABLED", "false")
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

// lakehouseChart is where Lakehouse mounts the release's verified chart archive, which it installs.
const lakehouseChart = "/release/rask.tgz"

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
// account the release or a fixture renders is refused, so a client cannot act as a lakehouse service. The token lives `duration`: 10m at the least, the API server's floor, and an hour at the most, the longest a door admits.
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
	lifetime, err := time.ParseDuration(duration)
	if err != nil {
		return nil, fmt.Errorf("duration %q: %w", duration, err)
	}
	// A door refuses a token minted for longer than the chart's `auth.serviceAccountMaxTokenSeconds` (an hour), and
	// tells the caller only that the token is invalid, so a longer one is refused here, where the reason can be said.
	if lifetime > time.Hour {
		return nil, fmt.Errorf("duration %s is longer than the hour a lakehouse door admits a token minted for", duration)
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
