// Building the deployable IMAGES, as opposed to running the gates.
//
// Until 2026-07-29 this module ran only CI gates (test, lint, typecheck, openapi, charts, frontend,
// e2e) and every image in the repo was built by `docker buildx` — from the Makefile (`k3s-build`,
// `frontend-images`) and from ci.yml. Dagger being "the build system"
// was therefore true of the checks and not of the artefacts.
//
// These functions close that gap WITHOUT forking the build definition: they hand the same
// `.docker/*.dockerfile` to BuildKit through Dagger, so the dockerfile stays the single source of truth
// and `dagger call image` produces the same layers `docker buildx build` does. Nothing here reimplements
// a build; it changes who drives it.
//
//   dagger call image --name=gateway
//   dagger call image --name=gateway publish --address=172.17.0.1:5000/gateway:dev
//   dagger call zone-image --zone=lakehouse publish --address=…
//
// ── The one host requirement ────────────────────────────────────────────────────────────────────
// Dagger's engine runs in a container, so `localhost:5000` inside it is the ENGINE, not the host
// registry — and Dagger always speaks HTTPS to a registry, while the dev registry is plain HTTP:
//
//	failed to do request: Head "https://172.17.0.1:5000/v2/…":
//	http: server gave HTTP response to HTTPS client
//
// There is no `--insecure` flag on publish. The fix is an engine that has been TOLD the registry is
// http, which is what `make dagger-engine` provisions (scripts/dagger-engine.sh). Address the registry
// by the bridge gateway (172.17.0.1), never `localhost`.
// NOTE ON THE IGNORE LISTS BELOW. They are not only about context size. Dagger snapshots the host
// directory, and a file that CHANGES DURING that snapshot aborts the whole build:
//
//	failed to sync: conflict at "frontend/microfrontends/lakehouse/e2e/admin/access.spec.ts":
//	size changed from "24995" to "21182" during sync
//
// On a repo with a second editor active — another agent session, a watch task, a Playwright run writing
// artefacts — that turns into intermittent `exit status 1` builds with no other explanation. Excluding
// the trees an image does not need (tests, e2e specs, build output) shrinks both the context and the
// window in which someone else's write can kill the build.
package main

import (
	"fmt"
	"strings"

	"dagger/rask/internal/dagger"
)

// provenance turns the OCI label args into BuildArgs, dropping empties.
//
// These are NOT computed here on purpose. A Dagger Function is sandboxed and has no host access, so it
// cannot read git — but more importantly, stamping `time.Now()` inside the module would change the build
// args on every single invocation and defeat the layer cache the whole exercise exists to keep. The
// caller (the Makefile, CI) supplies them; an unset value is simply omitted so the dockerfile's own
// default applies rather than an empty label being baked in.
func provenance(buildDate, vcsRef, version string) []dagger.BuildArg {
	out := []dagger.BuildArg{}
	for _, a := range []dagger.BuildArg{
		{Name: "BUILD_DATE", Value: buildDate},
		{Name: "VCS_REF", Value: vcsRef},
		{Name: "VERSION", Value: version},
	} {
		if a.Value != "" {
			out = append(out, a)
		}
	}
	return out
}

// extraArgs parses KEY=VALUE strings so ANY dockerfile is buildable through this one function —
// cnpg-age-ext needs AGE_REF/CNPG_BASE, and a future image will need something else. Without this the
// module would need a new Go function per dockerfile, which is how a build system drifts back into
// special cases. A token with no "=" is ignored rather than silently becoming an empty-valued arg.
func extraArgs(kv []string) []dagger.BuildArg {
	out := []dagger.BuildArg{}
	for _, s := range kv {
		if i := strings.Index(s, "="); i > 0 {
			out = append(out, dagger.BuildArg{Name: s[:i], Value: s[i+1:]})
		}
	}
	return out
}

// Image builds ANY deployable from its own dockerfile under .docker/.
//
// `name` is the dockerfile stem: gateway, compute, controlplane, rest-catalog, ray-cluster, ray-lance,
// runner, assist-runner, cnpg-age-ext.
func (m *Rask) Image(
	// +ignore=[".venv", ".git", "node_modules", "frontend/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build"]
	// +defaultPath="/"
	src *dagger.Directory,
	// Dockerfile stem under .docker/.
	name string,
	// +optional
	buildDate string,
	// +optional
	vcsRef string,
	// +optional
	version string,
	// Extra build args as KEY=VALUE (e.g. AGE_REF=..., CNPG_BASE=...).
	// +optional
	buildArg []string,
) *dagger.Container {
	args := provenance(buildDate, vcsRef, version)
	args = append(args, extraArgs(buildArg)...)
	return src.DockerBuild(dagger.DirectoryDockerBuildOpts{
		Dockerfile: fmt.Sprintf(".docker/%s.dockerfile", name),
		BuildArgs:  args,
	})
}

// ZoneImage builds one micro-frontend zone from the single parametrized frontend dockerfile.
//
// `zone` is a directory under frontend/microfrontends and becomes the APP build arg, exactly as
// `make frontend-images` passed it to docker buildx before this module took the build over.
func (m *Rask) ZoneImage(
	// +ignore=[".venv", ".git", "node_modules", "frontend/node_modules", "**/.svelte-kit", "**/.turbo",
	//          ".localbin", ".playwright-cli", "**/e2e", "**/test-results", "**/playwright-report", "**/*.spec.ts",
	//          "**/coverage", "**/storybook-static", "**/build"]
	// +defaultPath="/"
	src *dagger.Directory,
	// Zone directory under frontend/microfrontends.
	zone string,
	// +optional
	buildDate string,
	// +optional
	vcsRef string,
	// +optional
	version string,
) *dagger.Container {
	args := append(provenance(buildDate, vcsRef, version), dagger.BuildArg{Name: "APP", Value: zone})
	return src.DockerBuild(dagger.DirectoryDockerBuildOpts{
		Dockerfile: ".docker/frontend.dockerfile",
		BuildArgs:  args,
	})
}
