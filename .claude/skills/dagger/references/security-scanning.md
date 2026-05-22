# Security Scanning & Supply Chain

## Contents
- Trivy vulnerability scanning
- SBOM generation (SPDX, CycloneDX)
- SARIF output for GitHub Security
- Provenance attestation extraction
- OpenTelemetry verification with Jaeger
- GitHub Actions with signing and attestations

---

## Trivy vulnerability scanning

Export container as tarball and scan with Trivy:

```go
func (m *Myproject) Scan(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="CRITICAL,HIGH"
    severity string,
    // +default="table"
    format string,
    // +default=1
    exitCode int,
) (string, error) {
    container, err := m.Build(ctx, source, "")
    if err != nil {
        return "", fmt.Errorf("build failed before scanning: %w", err)
    }

    tarFile := container.AsTarball()

    output, err := dag.Container().
        From("aquasec/trivy:latest").
        WithMountedFile("/image.tar", tarFile).
        WithExec([]string{
            "trivy", "image",
            "--input", "/image.tar",
            "--severity", severity,
            "--format", format,
            "--exit-code", fmt.Sprintf("%d", exitCode),
        }).
        Stdout(ctx)

    if err != nil {
        if output == "" {
            return "", fmt.Errorf("trivy scan failed: %w", err)
        }
        return output, fmt.Errorf("vulnerabilities found: %w", err)
    }
    return output, nil
}
```

Convenience wrappers for common use cases:

```go
// ScanJson returns JSON for programmatic consumption
func (m *Myproject) ScanJson(ctx context.Context, source *dagger.Directory, severity string) (string, error) {
    return m.Scan(ctx, source, severity, "json", 0)
}

// ScanCi fails the build on CRITICAL/HIGH vulnerabilities
func (m *Myproject) ScanCi(ctx context.Context, source *dagger.Directory) (string, error) {
    output, err := m.Scan(ctx, source, "CRITICAL,HIGH", "table", 1)
    if err != nil {
        return output, fmt.Errorf("CI scan failed - vulnerabilities found: %w", err)
    }
    return output, nil
}
```

## SBOM generation

Generate Software Bill of Materials in SPDX or CycloneDX format:

```go
func (m *Myproject) GenerateSbom(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="python:3.13-alpine"
    // +optional
    baseImage string,
    // +default="spdx-json"
    format string,
) (*dagger.File, error) {
    container, err := m.Build(ctx, source, baseImage)
    if err != nil {
        return nil, err
    }

    tarFile := container.AsTarball()

    trivyContainer := dag.Container().
        From("aquasec/trivy:latest").
        WithMountedFile("/image.tar", tarFile).
        WithExec([]string{"mkdir", "-p", "/output"}).
        WithExec([]string{
            "trivy", "image",
            "--input", "/image.tar",
            "--format", format,
            "--output", "/output/sbom.json",
        })

    return trivyContainer.File("/output/sbom.json"), nil
}

// ExportSbom generates and writes SBOM to a local file
func (m *Myproject) ExportSbom(
    ctx context.Context,
    source *dagger.Directory,
    baseImage string,
    format string,
    // +default="./sbom.json"
    outputPath string,
) (string, error) {
    sbomFile, err := m.GenerateSbom(ctx, source, baseImage, format)
    if err != nil {
        return "", err
    }
    _, err = sbomFile.Export(ctx, outputPath)
    if err != nil {
        return "", fmt.Errorf("failed to export SBOM: %w", err)
    }
    return fmt.Sprintf("SBOM exported to %s", outputPath), nil
}
```

Call: `dagger call generate-sbom --format spdx-json export --path ./sbom.spdx.json`

## SARIF output for GitHub Security

```go
func (m *Myproject) ScanSarif(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="trivy-results.sarif"
    outputPath string,
) (*dagger.File, error) {
    container, err := m.Build(ctx, source, "")
    if err != nil {
        return nil, err
    }

    tarFile := container.AsTarball()

    return dag.Container().
        From("aquasec/trivy:latest").
        WithMountedFile("/image.tar", tarFile).
        WithExec([]string{
            "trivy", "image",
            "--input", "/image.tar",
            "--format", "sarif",
            "--output", "/output/" + outputPath,
        }).
        File("/output/" + outputPath), nil
}
```

## Provenance attestation extraction

Extract SLSA provenance from BuildKit attestations using crane:

```go
func (m *Myproject) ExtractProvenanceAttestation(
    ctx context.Context,
    // e.g., "myorg/myapp:v1.2.3"
    imageRef string,
    // +default="./provenance.intoto.jsonl"
    outputPath string,
) (*dagger.File, error) {
    // Add docker.io prefix if needed
    fullRef := imageRef
    if !containsRegistry(imageRef) {
        fullRef = "docker.io/" + imageRef
    }

    craneBinary := dag.Container().
        From("gcr.io/go-containerregistry/crane:latest").
        File("/ko-app/crane")

    extractContainer := dag.Container().
        From("alpine:latest").
        WithExec([]string{"apk", "add", "--no-cache", "jq"}).
        WithFile("/usr/local/bin/crane", craneBinary).
        WithExec([]string{
            "sh", "-c",
            fmt.Sprintf(`
MANIFEST=$(crane manifest %s)
ATTESTATION_DIGEST=$(echo "$MANIFEST" | jq -r '.manifests[] |
    select(.annotations."vnd.docker.reference.type" == "attestation-manifest") |
    .digest' | head -1)
[ -z "$ATTESTATION_DIGEST" ] && echo "No attestation manifest" && exit 1

ATT_MANIFEST=$(crane manifest %s@$ATTESTATION_DIGEST)
PROV_DIGEST=$(echo "$ATT_MANIFEST" | jq -r '.layers[] |
    select(.annotations."in-toto.io/predicate-type" == "https://slsa.dev/provenance/v0.2") |
    .digest')
[ -z "$PROV_DIGEST" ] && echo "No provenance layer" && exit 1

crane blob %s@$PROV_DIGEST > /provenance.intoto.jsonl
`, fullRef, fullRef, fullRef),
        })

    provenanceFile := extractContainer.File("/provenance.intoto.jsonl")
    _, err := provenanceFile.Export(ctx, outputPath)
    if err != nil {
        return nil, fmt.Errorf("failed to export provenance: %w", err)
    }
    return provenanceFile, nil
}
```

## OpenTelemetry verification with Jaeger

Spin up Jaeger as a service, run instrumented code, query for traces:

```go
func (m *Myproject) VerifyTelemetry(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
) (string, error) {
    jaeger := dag.Container().
        From("jaegertracing/jaeger:latest").
        WithExposedPort(4317).   // OTLP gRPC
        WithExposedPort(4318).   // OTLP HTTP
        WithExposedPort(16686).  // Jaeger UI / Query API
        AsService()

    appContainer := m.buildDev(source).
        WithServiceBinding("jaeger", jaeger).
        WithEnvVariable("CACHE_BUST", time.Now().String()) // bust cache

    // Run instrumented code, then query Jaeger API
    appContainer = appContainer.WithExec([]string{"run-instrumented-tests"})
    appContainer = appContainer.WithExec([]string{"sleep", "5"}) // ingest delay

    servicesOutput, err := appContainer.
        WithExec([]string{"sh", "-c", "wget -qO- 'http://jaeger:16686/api/services'"}).
        Stdout(ctx)
    // ... validate traces exist
}
```

Key gotcha: Don't set `OTEL_EXPORTER_OTLP_ENDPOINT` as a container env var
when using `WithServiceBinding` — Dagger rewrites env vars containing service
hostnames to tunnel addresses. Pass the endpoint directly in code instead.

## GitHub Actions with signing and attestations

Full publish workflow with SBOM, provenance, and cosign signing:

```yaml
name: Publish
on:
  push:
    tags: ["v*"]

permissions:
  contents: write
  id-token: write
  packages: write
  attestations: write

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Determine version
        id: version
        run: |
          if [[ "$GITHUB_REF" == refs/tags/v* ]]; then
            echo "tag=${GITHUB_REF#refs/tags/}" >> "$GITHUB_OUTPUT"
          else
            VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
            echo "tag=v${VERSION}" >> "$GITHUB_OUTPUT"
          fi

      # Run Dagger checks
      - name: Run checks
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: checks
          cloud-token: ${{ secrets.DAGGER_CLOUD_TOKEN }}
          version: "latest"

      # Build with BuildKit attestations
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_SECRET }}

      - name: Build and push with attestations
        id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          file: .docker/myapp.dockerfile
          push: true
          tags: |
            myorg/myapp:${{ steps.version.outputs.tag }}
            myorg/myapp:latest
          platforms: linux/amd64,linux/arm64
          sbom: true
          provenance: mode=max
          cache-from: type=gha
          cache-to: type=gha,mode=max

      # Generate standalone SBOM via Dagger
      - name: Generate SBOM
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: "generate-sbom --format spdx-json export --path ./sbom.spdx.json"
          version: "latest"

      # Extract provenance via Dagger
      - name: Extract provenance
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: "extract-provenance-attestation --image-ref myorg/myapp:${{ steps.version.outputs.tag }} export --path ./provenance.intoto.jsonl"
          version: "latest"

      # Upload as release assets
      - uses: softprops/action-gh-release@v2
        with:
          files: |
            ./sbom.spdx.json
            ./provenance.intoto.jsonl

      # Cosign keyless signing
      - uses: sigstore/cosign-installer@v4
      - name: Sign images
        run: cosign sign --yes myorg/myapp@${{ steps.build.outputs.digest }}
```

### Minimal CI workflow

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Checks
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: checks
          cloud-token: ${{ secrets.DAGGER_CLOUD_TOKEN }}
          version: "latest"
      - name: Tests
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: test
          cloud-token: ${{ secrets.DAGGER_CLOUD_TOKEN }}
          version: "latest"
```

Pin action SHAs in production for supply-chain security:
```yaml
uses: dagger/dagger-for-github@456fc3af63a2ba6f9789af9c55045b459115541b # v8.3.0
uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
```
