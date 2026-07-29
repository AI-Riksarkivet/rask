// Building the deployable IMAGES, as opposed to running the gates.
//
// Until 2026-07-29 this module ran only CI gates (test, lint, typecheck, openapi, charts, frontend,
// e2e) and every image in the repo was built by `docker buildx` — from the Makefile (`k3s-build`,
// `frontend-images`), from ci.yml, and from Tilt's own `docker_build`. Dagger being "the build system"
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
package main

import (
	"fmt"

	"dagger/rask/internal/dagger"
)

// devVenvOwner is the uid:gid the Python images' /opt/venv must carry for Tilt's live_update to land a
// file. The images run as uid 10001; a root-owned venv makes the sync fail with exit code 2 ("the
// container filesystem denied access") and Tilt falls back to a full rebuild. See the VENV_OWNER ARG in
// .docker/rest-catalog.dockerfile.
const devVenvOwner = "10001:10001"

// Image builds one of the repo's Python deployables from its own dockerfile.
//
// `name` is the dockerfile stem under `.docker/` — gateway, compute, controlplane, rest-catalog.
// `dev` (default true here because the caller is the dev loop) passes VENV_OWNER so the image can accept
// a live_update sync; leave it false for anything shipped.
func (m *Rask) Image(
	// +ignore=[".venv", ".git", "node_modules", "frontend/node_modules", "**/.svelte-kit", "**/.turbo", ".localbin"]
	// +defaultPath="/"
	src *dagger.Directory,
	// Dockerfile stem under .docker/ (gateway | compute | controlplane | rest-catalog).
	name string,
	// Make /opt/venv writable by the app user so Tilt can hot-reload into it. Dev only.
	// +optional
	// +default=true
	dev bool,
) *dagger.Container {
	args := []dagger.BuildArg{}
	if dev {
		args = append(args, dagger.BuildArg{Name: "VENV_OWNER", Value: devVenvOwner})
	}
	return src.
		DockerBuild(dagger.DirectoryDockerBuildOpts{
			Dockerfile: fmt.Sprintf(".docker/%s.dockerfile", name),
			BuildArgs:  args,
		})
}

// ZoneImage builds one micro-frontend zone from the single parametrized frontend dockerfile.
//
// `zone` is a directory under frontend/microfrontends — home, lakehouse, media, annotator, compute,
// studio, train — and becomes the APP build arg, exactly as `make frontend-images` passes it.
func (m *Rask) ZoneImage(
	// +ignore=[".venv", ".git", "node_modules", "frontend/node_modules", "**/.svelte-kit", "**/.turbo", ".localbin"]
	// +defaultPath="/"
	src *dagger.Directory,
	// Zone directory under frontend/microfrontends.
	zone string,
) *dagger.Container {
	return src.
		DockerBuild(dagger.DirectoryDockerBuildOpts{
			Dockerfile: ".docker/frontend.dockerfile",
			BuildArgs:  []dagger.BuildArg{{Name: "APP", Value: zone}},
		})
}
