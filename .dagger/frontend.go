package main

import (
	"context"
	"strings"

	"dagger/rask/internal/dagger"
)

// bunImage is the frontend toolchain's base image — the exact digest-pinned tag from
// .docker/frontend.dockerfile (oven/bun:1.3.14-slim), so the Dagger run matches the shipped zone images
// rather than trusting the CI setup-bun action's unpinned 'latest'. Bun is the whole toolchain here: turbo,
// oxlint and rsvelte-fmt both arrive as frontend/ root devDependencies via
// `bun install --frozen-lockfile` — no globally installed tool, no second package manager.
const bunImage = "oven/bun:1.3.14-slim@sha256:d56a2534ffd262e92c12fd3249d3924d296d97086da773f821d7d0477435ea04"

// frontendBase returns the installed turborepo workspace container: the bun image + source +
// `bun install --frozen-lockfile` (the packages/* + microfrontends/* workspace from frontend/bun.lock).
// Unexported → a shared private helper, not a Dagger Function. The bun install cache mirrors the
// dockerfile's `--mount=type=cache,target=/root/.bun/install/cache`.
func (m *Rask) frontendBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(bunImage).
		WithMountedCache("/root/.bun/install/cache", dag.CacheVolume("lance-ns-bun")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			// Exclude checked-in build/install artefacts so the container installs clean from the lockfile.
			// The zone build outputs (.svelte-kit/build) are stripped generically via the globs below.
			Exclude: []string{
				// A developer's `.env` is NOT a build input. It is untracked, so CI checking out fresh
				// never has one while a LOCAL run shipped it into the container — the same command taking
				// a different, secret-bearing input depending on whose machine ran it.
				"**/.env",
				"**/.env.*",
				".git",
				"node_modules",
				"frontend/node_modules",
				"frontend/.turbo",
				"frontend/microfrontends/*/.svelte-kit",
				"frontend/microfrontends/*/build",
				"frontend/microfrontends/*/test-results",
			},
		}).
		WithWorkdir("/src/frontend").
		WithExec([]string{"bun", "install", "--frozen-lockfile"})
}

// Frontend runs the frontend job's bun gates as ONE turbo invocation fanned across the workspace graph:
// svelte-check (check), TypeScript 7 via tsgo (check:tsgo), the unit tests, and lint + fmt:check — which
// are package tasks now, so they parallelise and cache instead of running once repo-wide. The Playwright
// e2e leg (`test:e2e`) is browser-bound and lives in its own function; it is not part of this hermetic
// subset.
//
// ── `--continue` IS LOAD-BEARING, and its absence cost the estate a whole gate ────────────────────
// Turbo fail-fasts by default: the first non-zero task cancels every task not already started. For a
// BUILD that is right — there is no point compiling against a broken dependency. For a GATE it is
// exactly wrong, because a gate's job is to report the state of the estate, not to stop at the first
// bad news.
//
// Measured on this workspace, same command, warm cache:
//
//	without --continue:  45 of 61 tasks ran, 1 failure reported
//	with    --continue:  70 of 74 tasks ran, 4 failures reported
//
// So three of the four real failures were invisible — including `@rask/api#test`, which has collected
// ZERO tests since 2026-08-05 (its `live.svelte.ts` imports `svelte`, which `@rask/api` never declared)
// and was masked the whole time by whichever task happened to fail first. `annotator:test` (422) and
// `explorer:test` (116) did not run at all. A gate reporting one failure of sixty-one is not a gate; it
// is a sample.
//
// The exit code is unchanged — turbo still exits non-zero if ANY task failed, so Dagger still surfaces
// the container error and CI still goes red. `--continue` buys completeness of the report, not leniency.
func (m *Rask) Frontend(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.frontendBase(src).
		WithExec([]string{"bunx", "turbo", "run", "check", "check:tsgo", "test", "lint", "fmt:check", "--continue"}).
		Stdout(ctx)
}

// ZoneSmoke builds ONE zone's image, serves it, and proves the SSR shell actually renders at that
// zone's own base path.
//
// It replaces the `docker run -d --name smoke-$z` loop in `ci.yml`, which was the last docker
// invocation in the workflow. Three assertions carry over unchanged, because each was earned:
//
//   - HTTP 200 at `${base}/` — the zone's `paths.base` from its own svelte.config.js, not a guess.
//     A bare `/` answers 404 under a base, which is exactly the failure this catches.
//   - `data-sveltekit` present in the body — a 200 alone can be an error page or a redirect target.
//   - the marker is read from the FINAL response after redirects, since `/zone` answers 308 and
//     `/zone/` answers 200.
//
// What does NOT carry over is the `docker image inspect web-$z:dev` pre-check, which existed to catch
// a stale Makefile ZONES list. That is not lost: `@rask/zone-contract`'s manifest test already pins
// the roster across all five places that declare it, and it fails at unit speed rather than after a
// full image build. Dagger builds the zone here from its directory, so there is no separately-tagged
// artefact to be missing in the first place.
func (m *Rask) ZoneSmoke(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Zone directory under frontend/microfrontends.
	zone string,
) (string, error) {
	base, err := dag.Container().
		From(bunImage).
		WithDirectory("/src", src.Directory("frontend/microfrontends/"+zone)).
		WithWorkdir("/src").
		// The base path is READ FROM THE ZONE, never passed in: `svelte.config.js` is the one place
		// that declares it, and a smoke test that took it as an argument could agree with itself while
		// disagreeing with what the server serves.
		WithExec([]string{"sh", "-c", `sed -n "s/.*paths:[[:space:]]*{[^}]*base:[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" svelte.config.js | head -1`}).
		Stdout(ctx)
	if err != nil {
		return "", err
	}
	base = strings.TrimSpace(base)

	server := m.ZoneImage(src, zone, "", "", "").
		WithExposedPort(3000).
		AsService()

	return dag.Container().
		From(bunImage).
		WithServiceBinding("zone", server).
		WithEnvVariable("ZONE", zone).
		WithEnvVariable("BASE", base).
		WithExec([]string{"sh", "-c", `
set -u
url="http://zone:3000${BASE}/"
code=000
for _ in $(seq 1 30); do
  code=$(bunx --bun node -e "
    fetch('${url}', {redirect:'follow'})
      .then(async r => { require('fs').writeFileSync('/tmp/body.html', await r.text()); console.log(r.status) })
      .catch(() => console.log('000'))
  " 2>/dev/null | tail -1)
  [ "$code" = 200 ] && break
  sleep 2
done
if [ "$code" != 200 ]; then echo "FAIL  $ZONE  base='$BASE'  http=$code"; exit 1; fi
if ! grep -q 'data-sveltekit' /tmp/body.html; then
  echo "FAIL  $ZONE  base='$BASE'  200 but no data-sveltekit marker — an error page or a redirect target"
  exit 1
fi
title=$(grep -o '<title>[^<]*</title>' /tmp/body.html | head -1)
echo "ok    $ZONE  base='$BASE'  200  $title"`}).
		Stdout(ctx)
}
