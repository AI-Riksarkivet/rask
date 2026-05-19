package main

import (
	"bufio"
	"context"
	"strings"

	"dagger/ra-hcp/internal/dagger"
)

// skipKeys are env vars managed by Dagger (service bindings) or intercepted
// by the Dagger telemetry pipeline. These must not be set from .env.
var skipKeys = map[string]bool{
	"REDIS_URL":        true,
	"BACKEND_URL":      true,
	"STORAGE_BACKEND":  true,
	"S3_ENDPOINT_URL":  true,
	"S3_ACCESS_KEY":    true,
	"S3_SECRET_KEY":    true,
}

// skipPrefixes are env var prefixes that Dagger intercepts and routes
// through its own telemetry collector, causing metric format errors.
var skipPrefixes = []string{"OTEL_", "DAGGER_"}

// applyEnvFile reads a .env file and sets each KEY=VALUE on the container.
// Lines starting with # and empty lines are skipped. Surrounding quotes
// on values are stripped. Keys in skipKeys and skipPrefixes are skipped.
func applyEnvFile(ctx context.Context, ctr *dagger.Container, envFile *dagger.File) (*dagger.Container, error) {
	contents, err := envFile.Contents(ctx)
	if err != nil {
		return nil, err
	}
	scanner := bufio.NewScanner(strings.NewReader(contents))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, val, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)
		// Strip surrounding quotes
		if len(val) >= 2 && ((val[0] == '"' && val[len(val)-1] == '"') || (val[0] == '\'' && val[len(val)-1] == '\'')) {
			val = val[1 : len(val)-1]
		}
		if key == "" || skipKeys[key] {
			continue
		}
		skip := false
		for _, prefix := range skipPrefixes {
			if strings.HasPrefix(key, prefix) {
				skip = true
				break
			}
		}
		if skip {
			continue
		}
		ctr = ctr.WithEnvVariable(key, val)
	}
	return ctr, nil
}

// Serve starts the backend as a Dagger service on port 8000 with Redis.
func (m *RaHcp) Serve(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// +optional
	// +defaultPath=".env"
	envFile *dagger.File,
) (*dagger.Service, error) {
	redisSvc := m.redis()

	ctr := m.BuildBackend(source)

	// Apply .env first (HCP connection settings, cache config, etc.)
	if envFile != nil {
		var err error
		ctr, err = applyEnvFile(ctx, ctr, envFile)
		if err != nil {
			return nil, err
		}
	}

	// Fully disable the OTEL SDK — Dagger v0.20.1 has a bug where it
	// crashes (nil pointer in otel-go LogValueFromPB) when re-exporting
	// OTEL log records from the backend. Disabling the SDK prevents any
	// OTEL activity that triggers the engine panic.
	ctr = ctr.WithEnvVariable("OTEL_SDK_DISABLED", "true")

	// Dagger-managed overrides (service bindings) — always last
	ctr = ctr.
		WithServiceBinding("redis", redisSvc).
		WithEnvVariable("REDIS_URL", "redis://redis:6379")

	return ctr.
		WithExposedPort(8000).
		AsService(), nil
}

// ServeAll starts the full stack: Redis + backend + frontend on port 8000.
// The frontend proxies /api requests to the backend internally.
func (m *RaHcp) ServeAll(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// +optional
	// +defaultPath=".env"
	envFile *dagger.File,
) (*dagger.Service, error) {
	backendSvc, err := m.Serve(ctx, source, envFile)
	if err != nil {
		return nil, err
	}

	return m.BuildFrontend(source).
		WithServiceBinding("backend", backendSvc).
		WithEnvVariable("BACKEND_URL", "http://backend:8000").
		WithExposedPort(8000).
		AsService(), nil
}

// ServeMinio starts the backend with MinIO as storage, plus Redis.
// No .env file needed — all config is wired by Dagger.
func (m *RaHcp) ServeMinio(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// +optional
	envFile *dagger.File,
) (*dagger.Service, error) {
	redisSvc := m.redis()
	minioSvc := m.minio()

	ctr := m.BuildBackend(source)

	// Apply .env if present (picks up API_SECRET_KEY, etc.)
	if envFile != nil {
		var err error
		ctr, err = applyEnvFile(ctx, ctr, envFile)
		if err != nil {
			return nil, err
		}
	}

	// Wire MinIO + Redis via service bindings
	ctr = ctr.
		WithServiceBinding("redis", redisSvc).
		WithEnvVariable("REDIS_URL", "redis://redis:6379").
		WithServiceBinding("minio", minioSvc).
		WithEnvVariable("STORAGE_BACKEND", "minio").
		WithEnvVariable("S3_ENDPOINT_URL", "http://minio:9000").
		WithEnvVariable("S3_ACCESS_KEY", minioRootUser).
		WithEnvVariable("S3_SECRET_KEY", minioRootPassword).
		WithEnvVariable("S3_VERIFY_SSL", "false").
		WithEnvVariable("S3_ADDRESSING_STYLE", "path").
		WithEnvVariable("API_SECRET_KEY", "dagger-dev-secret")

	return ctr.
		WithExposedPort(8000).
		AsService(), nil
}

// ServeMinioAll starts the full stack with MinIO: Redis + MinIO + backend + frontend.
// No .env file needed. Use `up --ports 8000:8000` to access the frontend.
func (m *RaHcp) ServeMinioAll(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// +optional
	envFile *dagger.File,
) (*dagger.Service, error) {
	backendSvc, err := m.ServeMinio(ctx, source, envFile)
	if err != nil {
		return nil, err
	}

	ctr := m.BuildFrontend(source).
		WithServiceBinding("backend", backendSvc).
		WithEnvVariable("BACKEND_URL", "http://backend:8000")

	return ctr.
		WithExposedPort(8000).
		AsService(), nil
}

// TestServer starts the backend and verifies it responds to a health check.
func (m *RaHcp) TestServer(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// +optional
	// +defaultPath=".env"
	envFile *dagger.File,
) (string, error) {
	svc, err := m.Serve(ctx, source, envFile)
	if err != nil {
		return "", err
	}

	return dag.Container().From("alpine:3.21").
		WithServiceBinding("backend", svc).
		WithExec([]string{"wget", "-qO-", "http://backend:8000/health"}).
		Stdout(ctx)
}
