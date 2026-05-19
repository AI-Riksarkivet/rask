// HCP App CI/CD pipeline powered by Dagger
//
// Provides build, lint, type-check, and test functions for the
// ra-hcp backend (Python/FastAPI) and frontend (SvelteKit/Deno).

package main

import (
	"dagger/ra-hcp/internal/dagger"
)

const (
	uvPythonImage = "ghcr.io/astral-sh/uv:0.10.9-python3.13-trixie-slim"
	denoImage     = "denoland/deno:2.7.1"
	redisImage    = "redis:8.0.1-alpine"
	minioImage    = "minio/minio:latest"
	backendDir    = "backend"
	frontendDir   = "frontend"

	minioRootUser     = "minioadmin"
	minioRootPassword = "minioadmin123"
)

type RaHcp struct{}

// buildBackendDev returns a container with all backend deps (including dev) installed.
func (m *RaHcp) buildBackendDev(source *dagger.Directory) *dagger.Container {
	backend := source.Directory(backendDir)

	return dag.Container().From(uvPythonImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("uv-cache")).
		WithMountedDirectory("/app", backend).
		WithWorkdir("/app").
		WithExec([]string{"uv", "sync", "--frozen", "--extra", "lance"})
}

// buildFrontendDev returns a Deno container with frontend deps installed.
func (m *RaHcp) buildFrontendDev(source *dagger.Directory) *dagger.Container {
	frontend := source.Directory(frontendDir)

	return dag.Container().From(denoImage).
		WithEnvVariable("DENO_DIR", "/deno-dir").
		WithMountedCache("/deno-dir", dag.CacheVolume("deno-cache")).
		WithMountedDirectory("/app", frontend).
		WithWorkdir("/app").
		WithExec([]string{"deno", "install"})
}

// redis returns a Redis service for integration tests.
func (m *RaHcp) redis() *dagger.Service {
	return dag.Container().From(redisImage).
		WithExposedPort(6379).
		AsService()
}

// minio returns a MinIO service with a single default bucket.
func (m *RaHcp) minio() *dagger.Service {
	return dag.Container().From(minioImage).
		WithEnvVariable("MINIO_ROOT_USER", minioRootUser).
		WithEnvVariable("MINIO_ROOT_PASSWORD", minioRootPassword).
		WithExposedPort(9000).
		WithExposedPort(9001).
		WithExec([]string{"minio", "server", "/data", "--console-address", ":9001"}).
		AsService()
}
