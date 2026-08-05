// Supply-chain scanning: the DEPENDENCIES we pull in, as opposed to the code we write.
//
// The gates in checks.go judge rask's own source (ruff, ty). Nothing judged the ~145 Python packages
// and the JS graph underneath it, and nothing judged the base layers the .docker/* images sit on. These
// functions close that, split by what each tool is actually best at rather than by running two scanners
// over the same ground:
//
//	dagger call audit                       osv-scanner over EVERY lockfile in the workspace
//	dagger call scan-config                 trivy misconfig + secrets over .docker/ and chart/
//	dagger call scan-image --name=gateway   trivy over the image dagger call image just built
//	dagger call scan-zone-image --zone=home  … the same, for a micro-frontend
//
// ── Why two tools and not one ───────────────────────────────────────────────────────────────────
// OSV-Scanner reads `uv.lock` and `bun.lock` natively, which is the whole ballgame here. The recursion
// is deliberately NOT narrowed to the two workspace roots, because the roots are not where the risk is —
// the first run of this gate found SIX lockfiles plus the Dagger module's own go.mod:
//
//	uv.lock                    232 pkgs   the fleet
//	frontend/bun.lock          652 pkgs   7 zones + 6 packages
//	runners/htr/uv.lock        156 pkgs   SEALED — torch, htrflow, ultralytics, transformers
//	runners/assist/uv.lock      46 pkgs   SEALED
//	runners/dummy/uv.lock       32 pkgs   SEALED
//	tests/e2e/bun.lock           4 pkgs   the standalone Playwright project
//	.dagger/go.mod              37 pkgs   this module's own dependencies
//
// The sealed runners are the point. Each carries its own lock and its own venv, matched by no workspace
// glob, so they are invisible to every root-level tool — `uv sync`, ruff, ty, the root pytest. On the
// first run they held 25 of the 38 findings while the root `uv.lock` held 1. A scan narrowed to the
// roots would have reported near-clean and been worse than no scan at all, because it would have read
// as coverage.
//
// (Only 3 of the 9 directories under runners/ have a lockfile at all. The other six — asr, diarize,
// insid3, kg, topics, voiceprint — are unlocked and therefore unscannable; that is a real gap in the
// coverage this function claims, and it belongs to those runners, not to this gate.)
//
// Trivy is NOT here for a second opinion on those packages. It covers the two layers OSV does not reach
// at all: the OS packages baked into the trixie/bun BASE images (Debian CVEs never appear in a lockfile),
// and misconfiguration + secret detection across the 8 dockerfiles and the Helm chart.
//
// ── Why the image scan is not a per-PR gate ─────────────────────────────────────────────────────
// It cannot be cheap: scanning an image means BUILDING it (a zone measures ~90 s, ray-cluster 238 s).
// `audit` and `scan-config` need no build at all and run in seconds, so those two are the PR gates and
// the image scan belongs on a schedule. Wiring the expensive one into the PR path is how a security gate
// becomes the thing everybody skips.
//
// No credentials anywhere in this file. Both scanners are ordinary containers reading public advisory
// data over HTTPS — there is no API key, no LLM, and nothing to put in GitHub Actions secrets.
package main

import (
	"context"
	"fmt"
	"strings"

	"dagger/rask/internal/dagger"
)

// Pinned, not floating. A scanner that silently changes version between a developer's run and CI's run
// produces findings nobody can reproduce, which is the same class of bug as an unpinned toolchain — see
// ci.yml pinning the Dagger CLI to the module's engineVersion.
//
// NOTE the tag conventions differ and neither is a typo: osv-scanner publishes `v2.4.0`, trivy `0.73.0`.
const (
	OsvScannerImage = "ghcr.io/google/osv-scanner:v2.4.0"
	TrivyImage      = "ghcr.io/aquasecurity/trivy:0.73.0"
)

// The `+ignore` set repeated on the source-scanning functions below is images.go's, and it is
// load-bearing for the same two reasons documented there: it keeps the host snapshot small, and it
// shrinks the window in which a second writer (another agent session, a watcher, a Playwright run)
// mutates a file mid-sync and aborts the call with `size changed … during sync`.
//
// It also fixes a scanner-specific failure: `node_modules` and `.venv` hold thousands of nested
// manifests, and a recursive scan that descends into them reports the same advisory dozens of times
// while taking minutes. The lockfiles are the source of truth; the installed trees are derived. The set
// is spelled out per-function because `+ignore` is a Dagger pragma read from the comment text — a Go
// variable cannot be substituted into it.
//
// THE GLOBS ARE `**/.venv`, NOT `.venv`, and that is not tidiness. A bare `.venv` matches at the ROOT
// ONLY, so the first run of scan-config walked straight into `runners/htr/.venv` and reported three
// CRITICAL/HIGH "leaked secrets" — moto's bundled test-fixture CA key, its cert key, and an example AWS
// access key baked into an ec2 model module. Vendored test fixtures in a sealed runner's virtualenv,
// every one of them, and exactly the sort of false positive that gets a security gate switched off in
// its first week. images.go carries the same bare `.venv` in its build-context excludes; whether that
// matters for a build context is a separate question from whether it matters for a scanner, and it has
// not been changed here.

// validSeverities is the trivy severity vocabulary, and checking against it is not ceremony.
// ScanConfig has to run its three scans as ONE `sh -c` line (see there for why), which means the
// caller's `severity` string reaches a shell. Unvalidated, `--severity 'HIGH; some-other-command'`
// executes inside the scanner container. The blast radius is one throwaway container with no
// credentials in it, so this is not a vulnerability — but a security gate that interpolates unchecked
// input into a shell is not a security gate anybody should trust, and the fix costs four lines.
var validSeverities = map[string]bool{
	"UNKNOWN": true, "LOW": true, "MEDIUM": true, "HIGH": true, "CRITICAL": true,
}

// checkSeverity rejects anything outside trivy's own vocabulary. Unexported = shared helper.
func checkSeverity(severity string) error {
	for _, s := range strings.Split(severity, ",") {
		if !validSeverities[strings.TrimSpace(s)] {
			return fmt.Errorf(
				"invalid severity %q: expected a comma-separated subset of UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
				s,
			)
		}
	}
	return nil
}

// trivyBase is trivy with its vulnerability DB on a cache volume. Unexported = shared helper.
//
// The DB mount is not an optimization detail. Without it every invocation re-pulls the full advisory
// database from ghcr, which is both slow and rate-limited — and a rate-limited pull FAILS the scan
// rather than degrading it, so an uncached trivy gate goes red for reasons that have nothing to do with
// the code under test.
func (m *Rask) trivyBase() *dagger.Container {
	return dag.Container().
		From(TrivyImage).
		WithMountedCache("/root/.cache/trivy", dag.CacheVolume("rask-trivy-cache"))
}

// Audit scans every lockfile in the workspace against the OSV database (the CI supply-chain gate).
//
// Exit code 1 means "vulnerabilities found" and fails the call — that IS the gate. Note there is NO
// severity threshold to soften it with: osv-scanner exposes `--config` and `--licenses`, and nothing
// resembling --fail-on-severity, so ANY finding at ANY severity is fatal. That is why ci.yml runs this
// leg report-only today (38 findings on the first run, 0 of them Critical) — a gate that goes red the
// hour it lands teaches everyone to ignore it.
//
// The way OUT of report-only is not a threshold, it is `osv-scanner.toml` at the repo root: it is
// auto-discovered for the scanned directory and each ignore entry carries a reason and a review date.
// Baseline the known 38 there and the gate can block on the 39th — a suppression becomes a recorded
// decision with an expiry instead of a deleted CI job.
func (m *Rask) Audit(
	ctx context.Context,
	// +ignore=["**/.venv", "**/node_modules", ".git", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", "**/test-results", "**/playwright-report", "**/coverage",
	//          "**/storybook-static", "**/build"]
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return dag.Container().
		From(OsvScannerImage).
		WithMountedDirectory("/src", src).
		WithWorkdir("/src").
		WithExec([]string{"/osv-scanner", "scan", "source", "--recursive", "/src"}).
		Stdout(ctx)
}

// ScanConfig runs trivy's misconfiguration and secret scanners over the deploy surface.
//
// Scoped to `.docker/` and `chart/` deliberately: `trivy config` on the whole repo also parses every
// docker-compose file and test fixture, and the resulting noise buries the findings that matter.
// Needs no build, so it belongs in the fast PR gate next to Audit.
func (m *Rask) ScanConfig(
	ctx context.Context,
	// +ignore=["**/.venv", "**/node_modules", ".git", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", "**/test-results", "**/playwright-report", "**/coverage",
	//          "**/storybook-static", "**/build"]
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Severities that FAIL the gate. Lower ones are still printed, just not fatal.
	// +optional
	// +default="HIGH,CRITICAL"
	severity string,
) (string, error) {
	if err := checkSeverity(severity); err != nil {
		return "", err
	}
	return m.trivyBase().
		WithMountedDirectory("/src", src).
		WithWorkdir("/src").
		// THREE scans, ONE exit code, and the `|| rc=1` is the whole reason this is a shell line
		// instead of three chained WithExecs. A failing WithExec aborts the chain, so the obvious
		// version reports `.docker` and then never runs `chart` or the secret scan at all — the first
		// finding hides every later one, and a triage pass needs two more round-trips to see the rest.
		//
		// ONE directory per `trivy config` invocation: it takes a single DIR argument and answers a
		// second one by printing its help and exiting 1, which looks exactly like a scan failure.
		WithExec([]string{"sh", "-c", fmt.Sprintf(
			"rc=0; "+
				"trivy config --exit-code 1 --severity %[1]s .docker || rc=1; "+
				"trivy config --exit-code 1 --severity %[1]s chart || rc=1; "+
				"trivy fs --scanners secret --exit-code 1 --severity %[1]s /src || rc=1; "+
				"exit $rc",
			severity,
		)}).
		Stdout(ctx)
}

// scanContainer is the shared image-scan body: hand trivy the built container as a tarball.
//
// `--input` on a tarball is what makes this work with NO docker daemon — Dagger materializes the image
// it just built straight into the scanner's filesystem, so the artefact under test is byte-identical to
// the one `dagger call image … publish` would push. Nothing is exported to the host and no registry is
// involved. Unexported = shared helper.
func (m *Rask) scanContainer(
	ctx context.Context,
	img *dagger.Container,
	severity string,
	ignoreUnfixed bool,
) (string, error) {
	args := []string{
		"trivy", "image", "--input", "/img.tar", "--exit-code", "1", "--severity", severity,
	}
	// Default ON, and this is a considered default rather than a way to make the number smaller. A
	// Debian base carries a long tail of CVEs with no fixed version available in ANY release — they
	// cannot be actioned by bumping a pin or rebuilding, so a gate that fails on them is permanently
	// red and teaches everyone to ignore it. Pass --ignore-unfixed=false for the full audit picture.
	if ignoreUnfixed {
		args = append(args, "--ignore-unfixed")
	}
	return m.trivyBase().
		WithMountedFile("/img.tar", img.AsTarball()).
		WithExec(args).
		Stdout(ctx)
}

// ScanImage builds a deployable from .docker/<name>.dockerfile and scans that exact image.
//
// `name` is the dockerfile stem, identical to `dagger call image --name=…`: gateway, compute, runner,
// ray-cluster, …
func (m *Rask) ScanImage(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Dockerfile stem under .docker/.
	name string,
	// Severities that FAIL the gate.
	// +optional
	// +default="HIGH,CRITICAL"
	severity string,
	// Skip CVEs with no fix available upstream.
	// +optional
	// +default=true
	ignoreUnfixed bool,
) (string, error) {
	if name == "" {
		return "", fmt.Errorf("scan-image: --name is required (the .docker/<stem>.dockerfile stem)")
	}
	return m.scanContainer(ctx, m.Image(src, name, "", "", "", nil), severity, ignoreUnfixed)
}

// ScanZoneImage builds one micro-frontend zone and scans it, mirroring ScanImage for the frontend plane.
func (m *Rask) ScanZoneImage(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Zone directory under frontend/microfrontends.
	zone string,
	// Severities that FAIL the gate.
	// +optional
	// +default="HIGH,CRITICAL"
	severity string,
	// Skip CVEs with no fix available upstream.
	// +optional
	// +default=true
	ignoreUnfixed bool,
) (string, error) {
	if zone == "" {
		return "", fmt.Errorf("scan-zone-image: --zone is required")
	}
	return m.scanContainer(ctx, m.ZoneImage(src, zone, "", "", ""), severity, ignoreUnfixed)
}
